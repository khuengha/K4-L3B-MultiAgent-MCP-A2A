from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def _ids(values: Any) -> list[str]:
    return list(dict.fromkeys(str(v) for v in values if v is not None and str(v)))[:20]


def _money(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and amount >= 0 else None
    except InvalidOperation:
        return None


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        return None


def _rows(value: Any, key: str) -> list[dict[str, Any]]:
    rows = value.get(key, []) if isinstance(value, dict) else value
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


class Investigation:
    """Case-local evidence cache and observable specialist handoffs."""

    def __init__(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter):
        self.case = case
        self.gateway = gateway
        self.trace = trace
        self.tools: set[str] = set()
        self.cache: dict[tuple[Any, ...], Any] = {}
        self.refs: dict[str, list[str]] = {}
        self.failures: set[str] = set()
        self.failure_types: dict[str, str] = {}
        self.calls = 0
        self._traced_refs: set[str] = set()
        self._assigned_agents: set[tuple[str, str | None]] = set()

    def emit(self, event: str, actor: str, **kwargs: Any) -> None:
        # The submission caps the *uncompressed* trace at 1 MiB. Record each
        # evidence ref once per case; later reuse is represented by handoffs and
        # verification_completed, rather than repeating every ledger read.
        new_refs: set[str] = set()
        assignment = (actor, kwargs.get("target"))
        if event == "tool_result_consumed":
            refs = list(dict.fromkeys(kwargs.get("evidence_refs", [])))
            refs = [ref for ref in refs if ref not in self._traced_refs]
            if not refs:
                return
            kwargs["evidence_refs"] = refs
            new_refs = set(refs)
        elif event == "task_assigned" and assignment in self._assigned_agents:
            return
        self.trace.emit(case_id=self.case["case_id"], event_type=event, actor=actor, **kwargs)
        self._traced_refs.update(new_refs)
        if event == "task_assigned":
            self._assigned_agents.add(assignment)

    async def fetch(self, actor: str, tool: str, **arguments: str) -> Any:
        key = (tool, *sorted(arguments.items()))
        if key in self.cache:
            return self.cache[key]
        self.emit("task_assigned", "coordinator", target=actor, tool_name=tool)
        if tool not in self.tools or self.calls >= 20:
            self.failures.add(tool)
            self.cache[key] = None
            self.emit("handoff", actor, target="coordinator", decision_code="TOOL_UNAVAILABLE")
            return None
        self.calls += 1
        try:
            evidence = await asyncio.wait_for(
                self.gateway.call(tool, case_id=self.case["case_id"], **arguments), timeout=35
            )
        except (RuntimeError, ValueError, TimeoutError, OSError) as exc:
            self.failure_types[tool] = type(exc).__name__
            self.failures.add(tool)
            self.cache[key] = None
            self.emit("handoff", actor, target="coordinator", decision_code="EVIDENCE_UNAVAILABLE")
            return None
        data = evidence["data"]
        # Never consume rows belonging to another order or customer.
        for field in ("order_id", "customer_unique_id"):
            expected = arguments.get(field)
            rows = list(_objects(data))
            if expected and any(row.get(field) not in (None, expected) for row in rows):
                self.failures.add(tool)
                self.cache[key] = None
                self.emit("handoff", actor, target="coordinator", decision_code="SCOPE_MISMATCH")
                return None
        ref = evidence["evidence_ref"]
        self.refs.setdefault(tool, []).append(ref)
        self.emit("tool_result_consumed", actor, tool_name=tool, evidence_refs=[ref])
        self.emit("handoff", actor, target="coordinator", evidence_refs=[ref])
        self.cache[key] = data
        return data


def _objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _shipment(data: Any) -> dict[str, Any]:
    result = {"verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False}
    if not isinstance(data, dict):
        return result
    events = _rows(data, "events")
    confirmed = [e for e in events if e.get("status") in ("confirmed", "completed", "delivered")]
    types = {e.get("event_type") for e in confirmed}
    delivered = _date(data.get("delivered_customer_at"))
    estimated = _date(data.get("estimated_delivery_at"))
    carrier = _date(data.get("delivered_carrier_at"))
    limits = _rows(data, "shipping_limits")
    late = _ids(
        r.get("seller_id")
        for r in limits
        if carrier and _date(r.get("shipping_limit_at")) and carrier > _date(r["shipping_limit_at"])
    )
    result["timeline_complete"] = bool(delivered and estimated and carrier and limits)
    if "lost" in types:
        result["verdict"] = "lost"
    elif "returned" in types:
        result["verdict"] = "returned"
    elif "delivered_late" in types:
        actors = {e.get("actor") for e in confirmed if e.get("event_type") == "delivered_late"}
        if actors == {"seller"}:
            result.update(verdict="seller_delay", late_seller_ids=late)
        elif actors == {"logistics_provider"}:
            result["verdict"] = "logistics_delay"
        else:
            result["verdict"] = "conflicting"
    elif delivered and estimated:
        result["verdict"] = (
            "on_time"
            if delivered <= estimated
            else (
                "seller_delay"
                if late
                else "logistics_delay"
                if carrier and limits
                else "insufficient_evidence"
            )
        )
        if result["verdict"] == "seller_delay":
            result["late_seller_ids"] = late
    return result


def _payment(payment: Any, refund: Any, items: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "verdict": "insufficient_evidence",
        "captured_total_brl": None,
        "refunded_total_brl": None,
        "refundable_total_brl": None,
    }
    if not isinstance(payment, dict):
        return result
    events = _rows(payment, "events")
    captures = [
        e
        for e in events
        if e.get("event_type") in ("captured", "capture")
        and e.get("status") in ("confirmed", "completed", "succeeded")
    ]
    amounts = [_money(e.get("amount_brl")) for e in captures]
    if not captures or any(a is None for a in amounts):
        return result
    captured = sum(amounts, Decimal(0))
    result["captured_total_brl"] = float(captured)
    if not isinstance(refund, dict) or not isinstance(refund.get("events"), list):
        return result
    refunds = _rows(refund, "events")
    completed = [
        e
        for e in refunds
        if e.get("event_type") in ("refunded", "refund_completed", "refund_succeeded")
        and e.get("status") in ("confirmed", "completed", "succeeded")
    ]
    refunded_amounts = [_money(e.get("amount_brl")) for e in completed]
    if any(a is None for a in refunded_amounts):
        return result
    refunded = sum(refunded_amounts, Decimal(0))
    if refunded > captured:
        return result
    result.update(
        refunded_total_brl=float(refunded),
        refundable_total_brl=float(max(Decimal(0), captured - refunded)),
    )
    # Last lifecycle state per refund prevents an old pending event overriding completion.
    latest: dict[str, dict[str, Any]] = {}
    for event in sorted(refunds, key=lambda e: str(e.get("event_at", ""))):
        latest[str(event.get("refund_id", event.get("refund_reference", "order")))] = event
    states = {
        str(e.get("event_type", "")) + " " + str(e.get("status", "")) for e in latest.values()
    }
    if any("failed" in s for s in states):
        result["verdict"] = "refund_failed"
    elif any(e.get("status") in ("pending", "requested", "processing") for e in latest.values()):
        result["verdict"] = "refund_pending"
    elif refunded > 0:
        result["verdict"] = "refunded"
    elif any(
        e.get("event_type") in ("duplicate_capture", "duplicate_charge")
        and e.get("status") in ("confirmed", "completed", "succeeded")
        for e in events
    ):
        result["verdict"] = "duplicate_capture"
    else:
        totals = [(_money(i.get("price")), _money(i.get("freight_value"))) for i in items]
        if totals and all(p is not None and f is not None for p, f in totals):
            expected = sum((p + f for p, f in totals), Decimal(0))
            result["verdict"] = (
                "reconciled" if abs(captured - expected) <= Decimal(".01") else "capture_mismatch"
            )
    return result


def _conflicts(
    order: dict[str, Any], history: Any, items: list[dict[str, Any]], shipment: Any
) -> list[dict[str, Any]]:
    conflicts = []
    for field in ("order_status", "order_delivered_customer_date", "order_estimated_delivery_date"):
        values = {
            r.get(field)
            for r in _rows(history, "orders")
            if r.get("order_id") == order.get("order_id") and r.get(field) is not None
        }
        if order.get(field) is not None:
            values.add(order[field])
        if len(values) > 1:
            conflicts.append(
                {
                    "field": field,
                    "sources": ["get_order", "get_customer_history"],
                    "selected_source": None,
                    "resolution_code": "UNRESOLVED_CONFLICT",
                }
            )
    seen: dict[str, dict[str, Any]] = {}
    for item in items:
        key = str(item.get("order_item_id"))
        if key in seen and item != seen[key]:
            conflicts.append(
                {
                    "field": "order_items",
                    "sources": ["get_order_items:first_row", "get_order_items:conflicting_row"],
                    "selected_source": None,
                    "resolution_code": "DUPLICATE_ITEM_CONFLICT",
                }
            )
            break
        seen[key] = item
    if isinstance(shipment, dict):
        delivered = _date(shipment.get("delivered_customer_at"))
        estimated = _date(shipment.get("estimated_delivery_at"))
        if (
            delivered
            and estimated
            and delivered <= estimated
            and any(e.get("event_type") == "delivered_late" for e in _rows(shipment, "events"))
        ):
            conflicts.append(
                {
                    "field": "shipment.delivery_status",
                    "sources": ["shipment_summary", "shipment_events"],
                    "selected_source": None,
                    "resolution_code": "UNRESOLVED_CONFLICT",
                }
            )
    return conflicts[:5]


def _split_payment(payment: Any) -> bool | None:
    """Count distinct payment identities, not duplicated source rows."""
    rows = _rows(payment, "payments")
    if not rows:
        return None
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = row.get(
            "payment_reference", row.get("payment_id", row.get("payment_sequential"))
        )
        if identity is None:
            return None
        key = str(identity)
        if key in seen and row != seen[key]:
            return None
        seen[key] = row
    return len(seen) > 1


def _claim_finding(
    topic: str,
    order: dict[str, Any],
    shipping: dict[str, Any],
    payments: dict[str, Any],
    conflicts: list[dict[str, Any]],
    issues: list[str],
    split_payment: bool | None = None,
) -> tuple[str | None, tuple[str, ...]]:
    """Evaluate only this claim's dependencies, independently of payout readiness.

    None means insufficient evidence, never an automatic rejection of the claim.
    Neither complaint text nor policy refund amounts establish a business fact.
    """
    fields = {c["field"] for c in conflicts if c.get("selected_source") is None}
    identity = ("get_customer_history", "get_order")
    payment_tools = (*identity, "get_payment_timeline", "get_order_items")
    shipment_tools = (*identity, "get_shipment_summary", "get_order_items")
    refund_tools = (*identity, "get_payment_timeline", "get_refund_timeline")
    if topic in {"refund_pending", "refund_failed"}:
        # Shipment/item disagreements do not invalidate an observed refund lifecycle.
        if payments["verdict"] == topic:
            return "supported", refund_tools
        if payments["verdict"] in {"refunded", "reconciled"} and (
            payments["refunded_total_brl"] is not None
        ):
            return "unsupported", refund_tools
        return None, refund_tools
    if topic in {"canceled_order_paid", "unavailable_order_paid"}:
        captured = _money(payments["captured_total_brl"])
        status = order.get("order_status")
        if "order_status" in fields or captured is None or not status:
            return None, payment_tools
        expected_status = "canceled" if topic == "canceled_order_paid" else "unavailable"
        return (
            "supported" if status == expected_status and captured > 0 else "unsupported",
            payment_tools,
        )
    if topic in {"late_delivery_seller", "late_delivery_logistics"}:
        if fields.intersection(
            {
                "order_delivered_customer_date",
                "order_estimated_delivery_date",
                "shipment.delivery_status",
                "order_items",
            }
        ):
            return None, shipment_tools
        expected = "seller_delay" if topic == "late_delivery_seller" else "logistics_delay"
        actual = shipping["verdict"]
        if actual == expected:
            return "supported", shipment_tools
        if actual in {"on_time", "seller_delay", "logistics_delay"}:
            return "unsupported", shipment_tools
        return None, shipment_tools
    if topic in {"duplicate_charge", "payment_mismatch", "valid_split_payment"}:
        actual = payments["verdict"]
        # A confirmed duplicate event is independent of item price disputes.
        if topic == "duplicate_charge" and actual == "duplicate_capture":
            return "supported", payment_tools
        if "order_items" in fields:
            return None, payment_tools
        if actual == "reconciled":
            if topic == "valid_split_payment":
                if split_payment is None:
                    return None, payment_tools
                return ("supported" if split_payment else "unsupported"), payment_tools
            return "unsupported", payment_tools
        if topic == "payment_mismatch" and actual == "capture_mismatch":
            return "supported", payment_tools
        # An unexplained mismatch alone does not prove or disprove a duplicate.
        return None, payment_tools
    if topic == "unsupported_claim":
        tools = tuple(dict.fromkeys((*shipment_tools, *payment_tools)))
        if not fields and shipping["verdict"] == "on_time" and payments["verdict"] == "reconciled":
            return ("supported" if topic in issues else "unsupported"), tools
        return None, tools
    return None, (*identity, "get_policy", "get_payment_timeline", "get_refund_timeline")


def _claim_refs(ctx: Investigation, tools: tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(ref for tool in tools for ref in ctx.refs.get(tool, [])))


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Investigate only discovered MCP evidence; never treat complaint topics as facts."""
    ctx = Investigation(case, gateway, trace)
    ctx.tools = set(await gateway.list_tools())
    request = case.get("customer_request", {})
    scope = case.get("investigation_scope", {})
    candidates = _ids([request.get("claimed_order_id"), *case.get("candidate_order_ids", [])])
    hint = case.get("customer_unique_id_hint")
    history = None
    if hint:
        history = await ctx.fetch("entity-agent", "get_customer_history", customer_unique_id=hint)
    history_ids = _ids(row.get("order_id") for row in _rows(history, "orders"))
    matches = [oid for oid in candidates if oid in history_ids]
    # A claimed ID is a hint, never proof; multiple matching candidates stay ambiguous.
    eligible = matches if history is not None else candidates
    if not candidates:
        eligible = history_ids
    resolved: list[str] = []
    rejected = [oid for oid in candidates if history is not None and oid not in history_ids]
    order: dict[str, Any] = {}
    if len(eligible) == 1:
        value = await ctx.fetch("entity-agent", "get_order", order_id=eligible[0])
        if isinstance(value, dict) and value.get("order_id") == eligible[0]:
            order = value
            resolved = eligible
    elif history is None and len(eligible) > 1:
        found = []
        for oid in eligible[:5]:
            value = await ctx.fetch("entity-agent", "get_order", order_id=oid)
            if isinstance(value, dict) and value.get("order_id") == oid:
                found.append(value)
            elif value is not None:
                rejected.append(oid)
        if len(found) == 1 and len(eligible) <= 5 and "get_order" not in ctx.failures:
            order = found[0]
            resolved = [order["order_id"]]
    entity_status = "resolved" if resolved else "ambiguous" if eligible else "not_found"
    customer_id = history.get("customer_unique_id") if isinstance(history, dict) else None
    if resolved and not customer_id and scope.get("include_customer_history"):
        customer_id = order.get("customer_unique_id")
        if customer_id:
            history = await ctx.fetch(
                "entity-agent", "get_customer_history", customer_unique_id=customer_id
            )
            history_ids = _ids(r.get("order_id") for r in _rows(history, "orders"))
    policy = await ctx.fetch(
        "policy-agent", "get_policy", policy_version=case.get("policy_version", "")
    )
    if not ctx.refs:
        failures = ", ".join(
            f"{tool} ({ctx.failure_types.get(tool, 'unavailable')})"
            for tool in sorted(ctx.failures)
        )
        raise RuntimeError(
            f"{case['case_id']}: no usable MCP evidence; stopping instead of writing an "
            f"unscorable submission. Failed tools: {failures or 'none returned evidence'}. "
            "Check MCP service/access/quota before rerunning."
        )
    items: list[dict[str, Any]] = []
    shipment = payment = refund = None
    if resolved:
        oid = resolved[0]
        items = _rows(await ctx.fetch("order-agent", "get_order_items", order_id=oid), "items")
        shipment = await ctx.fetch("shipment-agent", "get_shipment_summary", order_id=oid)
        payment = await ctx.fetch("payment-agent", "get_payment_timeline", order_id=oid)
        refund = await ctx.fetch("payment-agent", "get_refund_timeline", order_id=oid)
        if scope.get("include_product_context"):
            await ctx.fetch("order-agent", "get_product_context", order_id=oid)
    shipping = _shipment(shipment)
    payments = _payment(payment, refund, items)
    conflicts = _conflicts(order, history, items, shipment)
    ctx.emit("task_assigned", "coordinator", target="conflict-agent")
    ctx.emit(
        "handoff",
        "conflict-agent",
        target="coordinator",
        decision_code="UNRESOLVED_CONFLICT" if conflicts else "NO_CONFLICT_DETECTED",
        attributes={"conflict_count": len(conflicts)},
    )
    issues: list[str] = []
    payment_issue = {
        "capture_mismatch": "payment_mismatch",
        "duplicate_capture": "duplicate_charge",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }
    if payments["verdict"] in payment_issue:
        issues.append(payment_issue[payments["verdict"]])
    if payments["captured_total_brl"] and order.get("order_status") in ("canceled", "unavailable"):
        issues.append(f"{order['order_status']}_order_paid")
    if shipping["verdict"] in ("seller_delay", "logistics_delay"):
        issues.append("late_delivery_" + shipping["verdict"].split("_")[0])
    if not issues and payments["verdict"] == "reconciled" and shipping["verdict"] == "on_time":
        rows = _rows(payment, "payments")
        issues.append("valid_split_payment" if len(rows) > 1 else "unsupported_claim")
    primary = issues[0] if issues else "insufficient_evidence"
    # No money recommendation while sources conflict or refund balance is unknown.
    uncertain = bool(
        conflicts or ctx.failures or not resolved or primary == "insufficient_evidence"
    )
    rules = policy.get("rules", {}) if isinstance(policy, dict) else {}
    rule = rules.get(primary, {})
    status = rule.get("case_status", "needs_investigation")
    actions = (
        [rule["recommended_action"]] if rule.get("recommended_action") else ["investigate_case"]
    )
    amount = Decimal(0)
    available = _money(payments["refundable_total_brl"])
    suggested = _money(rule.get("refund_brl"))
    uncertain = uncertain or not rule or available is None or suggested is None
    if uncertain:
        status, actions = "needs_investigation", ["investigate_case"]
    elif status == "action_required":
        amount = min(suggested, available).quantize(Decimal(".01"))
    split_payment = _split_payment(payment)
    diagnosis, _ = _claim_finding(
        primary, order, shipping, payments, conflicts, issues, split_payment
    )
    diagnosis_proven = bool(resolved and diagnosis == "supported")
    # A pending-refund policy asks for monitoring, not a new money transfer.
    # Keep that precise action even when unrelated shipment sources disagree.
    if (
        primary == "refund_pending"
        and diagnosis_proven
        and rule.get("case_status") == "needs_investigation"
        and suggested == Decimal(0)
        and rule.get("recommended_action")
    ):
        actions = [rule["recommended_action"]]
    # Confidence concerns the diagnosis, not whether a payout can safely proceed.
    # These are conservative defaults, not calibration fitted to leaderboard labels.
    confidence = 0.8 if uncertain and diagnosis_proven else 0.45 if uncertain else 0.9
    refs = list(dict.fromkeys(ref for group in ctx.refs.values() for ref in group))
    entities = {
        "order_ids": resolved,
        "item_ids": _ids(i.get("order_item_id") for i in items),
        "seller_ids": _ids(i.get("seller_id") for i in items),
        "payment_references": _ids(
            e.get("payment_reference", e.get("payment_id")) for e in _rows(payment, "events")
        ),
        "shipment_ids": _ids(e.get("shipment_id") for e in _rows(shipment, "events")),
    }
    parties = []
    if not uncertain or diagnosis_proven:
        for party in rule.get("responsible_parties", []):
            if party.get("party_type") == "seller":
                ids = shipping["late_seller_ids"] or entities["seller_ids"]
                parties.extend({"party_type": "seller", "party_id": sid} for sid in ids[:5])
            else:
                parties.append(party)
    claims = []
    for claim in request.get("claims", [])[:5]:
        topic = claim.get("topic")
        finding, tools = _claim_finding(
            topic, order, shipping, payments, conflicts, issues, split_payment
        )
        verdict = finding if resolved and finding is not None else "insufficient_evidence"
        claim_confidence = 0.8 if verdict != "insufficient_evidence" else 0.45
        if topic == "requested_full_refund":
            # An entitlement or a known diagnosis is not proof of an available balance.
            tools = tuple(ctx.refs)
            if not uncertain:
                captured = _money(payments["captured_total_brl"])
                verdict = (
                    "supported"
                    if captured and amount >= captured
                    else "partially_supported"
                    if amount > 0
                    else "unsupported"
                )
                claim_confidence = confidence
        elif not uncertain and finding is None:
            # Unrecognized topics are left open, rather than rejected by string mismatch.
            verdict, claim_confidence = "insufficient_evidence", 0.45
        claims.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": claim_confidence,
                "evidence_refs": _claim_refs(ctx, tools),
            }
        )
    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": primary,
            "secondary_issues": issues[1:],
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": entities,
        "entity_resolution": {
            "status": entity_status,
            "resolved_order_ids": resolved,
            "rejected_candidates": _ids(rejected),
            "confidence": 0.95 if resolved and history is not None else 0.7 if resolved else 0.2,
        },
        "customer_context": {"customer_unique_id": customer_id, "related_order_ids": history_ids},
        "shipment_analysis": shipping,
        "payment_analysis": payments,
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": issue.upper(), "rank": rank}
                for rank, issue in enumerate(issues[:5], 1)
            ],
            "responsible_parties": parties[:5],
        },
        "claim_assessments": claims,
        "evidence_refs": refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(amount),
            "refund_lines": [
                {
                    "reason_code": primary.upper(),
                    "amount_brl": float(amount),
                    "entity_id": resolved[0],
                }
            ]
            if amount
            else [],
        },
        "resolution_actions": actions,
    }
    ctx.emit(
        "policy_decided",
        "policy-agent",
        decision_code=primary.upper(),
        evidence_refs=ctx.refs.get("get_policy", []),
    )
    ctx.emit("task_assigned", "coordinator", target="verifier")
    _verify(output, ctx)
    trace.contracts.validate_output(output, f"workflow/{case['case_id']}")
    ctx.emit(
        "verification_completed",
        "verifier",
        decision_code="OUTPUT_VALIDATED",
        attributes={
            "mcp_calls": ctx.calls,
            "missing_tools": len(ctx.failures),
            "unresolved_conflicts": len(conflicts),
        },
    )
    return output


def _verify(output: dict[str, Any], ctx: Investigation) -> None:
    """Independent structural/provenance checks; does not issue more MCP calls."""
    entity = output["entity_resolution"]
    resolved = set(entity["resolved_order_ids"])
    if resolved.intersection(entity["rejected_candidates"]):
        raise ValueError("Resolved order also appears among rejected candidates")
    if resolved != set(output["affected_entities"]["order_ids"]):
        raise ValueError("Affected order scope differs from resolved scope")
    known = {ref for group in ctx.refs.values() for ref in group}
    if not set(output["evidence_refs"]).issubset(known):
        raise ValueError("Output uses evidence outside this case")
    for claim in output["claim_assessments"]:
        if not set(claim["evidence_refs"]).issubset(set(output["evidence_refs"])):
            raise ValueError("Claim uses evidence outside the submitted case evidence")
        if claim["verdict"] != "insufficient_evidence" and not claim["evidence_refs"]:
            raise ValueError("Definitive claim has no evidence")
    financial = output["financial_resolution"]
    amount = Decimal(str(financial["recommended_refund_brl"]))
    lines = sum(
        (Decimal(str(line["amount_brl"])) for line in financial["refund_lines"]), Decimal(0)
    )
    if amount != lines:
        raise ValueError("Refund lines do not match total")
    balance = _money(output["payment_analysis"]["refundable_total_brl"])
    if amount > 0 and (balance is None or amount > balance or not resolved):
        raise ValueError("Refund exceeds verified available balance")
    if output["assessment"]["case_status"] != "action_required" and amount:
        raise ValueError("Refund recommendation requires action_required status")
