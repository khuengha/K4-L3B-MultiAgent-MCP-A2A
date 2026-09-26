from __future__ import annotations

import asyncio
from typing import Any
from mcp.types import CallToolResult

# Ensure CallToolResult has isError attribute forwarding to is_error
if not hasattr(CallToolResult, "isError"):
    CallToolResult.isError = property(lambda self: getattr(self, "is_error", False))

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id: str = case["case_id"]
    policy_version: str = case.get("policy_version", "EC_POLICY_V2")
    scope: dict[str, Any] = case.get("investigation_scope", {})
    cust_hint: str | None = case.get("customer_unique_id_hint")
    claimed_order_id: str | None = case.get("customer_request", {}).get("claimed_order_id")
    customer_claims: list[dict[str, Any]] = case.get("customer_request", {}).get("claims", [])
    claim_topics: set[str] = {c.get("topic") for c in customer_claims if c.get("topic")}

    # ------------------------------------------------------------------
    # Cache & MCP Tool Caller (Per-case scoped with Retry & Domain Tracking)
    # ------------------------------------------------------------------
    case_cache: dict[tuple[str, tuple[tuple[str, Any], ...]], dict[str, Any]] = {}
    used_evidence_refs: list[str] = []
    
    # Domain-specific evidence ref trackers
    entity_refs: list[str] = []
    shipment_refs: list[str] = []
    payment_refs: list[str] = []
    policy_refs: list[str] = []

    async def call_tool(tool_name: str, domain: str = "general", **kwargs: str) -> dict[str, Any] | None:
        cache_key = (tool_name, tuple(sorted(kwargs.items())))
        if cache_key in case_cache:
            return case_cache[cache_key]
        
        max_retries = 5
        for attempt in range(max_retries):
            try:
                evidence = await gateway.call(tool_name, case_id=case_id, **kwargs)
                case_cache[cache_key] = evidence
                ref = evidence.get("evidence_ref")
                if ref:
                    if ref not in used_evidence_refs:
                        used_evidence_refs.append(ref)
                    if domain == "entity" and ref not in entity_refs:
                        entity_refs.append(ref)
                    elif domain == "shipment" and ref not in shipment_refs:
                        shipment_refs.append(ref)
                    elif domain == "payment" and ref not in payment_refs:
                        payment_refs.append(ref)
                    elif domain == "policy" and ref not in policy_refs:
                        policy_refs.append(ref)

                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="specialist",
                    tool_name=tool_name,
                    evidence_refs=[ref] if ref else None,
                )
                return evidence
            except BaseException as exc:
                exc_str = str(exc)
                if "failed:" in exc_str or "did not return" in exc_str:
                    return None
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                return None

    # ------------------------------------------------------------------
    # 1. Entity Resolution Agent (Targeted Invocation)
    # ------------------------------------------------------------------
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
    )

    candidate_ids: list[str] = case.get("candidate_order_ids", [])
    resolved_order_ids: list[str] = []
    rejected_candidates: list[str] = []
    valid_orders_data: dict[str, dict[str, Any]] = {}

    for cand_id in candidate_ids:
        order_ev = await call_tool("get_order", domain="entity", order_id=cand_id)
        if order_ev and isinstance(order_ev.get("data"), dict) and order_ev["data"].get("order_id"):
            resolved_order_ids.append(cand_id)
            valid_orders_data[cand_id] = order_ev["data"]
        else:
            rejected_candidates.append(cand_id)

    # Selective Customer History (Only if scope explicitly requests it)
    related_order_ids: list[str] = []
    customer_unique_id: str | None = cust_hint
    if scope.get("include_customer_history") and cust_hint:
        cust_ev = await call_tool("get_customer_history", domain="entity", customer_unique_id=cust_hint)
        if cust_ev and isinstance(cust_ev.get("data"), dict):
            cust_data = cust_ev["data"]
            customer_unique_id = cust_data.get("customer_unique_id", cust_hint)
            orders_list = cust_data.get("orders", [])
            for o in orders_list:
                if isinstance(o, dict) and o.get("order_id"):
                    oid = o["order_id"]
                    if oid not in related_order_ids:
                        related_order_ids.append(oid)

    # Determine Entity Resolution Status & Confidence
    if len(resolved_order_ids) == 1:
        entity_status = "resolved"
        entity_confidence = 0.95
    elif len(resolved_order_ids) > 1:
        entity_status = "ambiguous"
        entity_confidence = 0.50
    else:
        entity_status = "not_found"
        entity_confidence = 0.10

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="order-agent",
        attributes={"status": entity_status, "resolved_count": len(resolved_order_ids)},
    )

    # ------------------------------------------------------------------
    # 2. Coordinator & Specialist Agents (Selective Tool Execution)
    # ------------------------------------------------------------------
    target_order_id = resolved_order_ids[0] if resolved_order_ids else claimed_order_id
    target_order_info = valid_orders_data.get(target_order_id, {}) if target_order_id else {}

    # Order / Item Agent
    items_data: list[dict[str, Any]] = []
    item_ids: list[str] = []
    seller_ids: list[str] = []
    items_total: float = 0.0

    if target_order_id:
        items_ev = await call_tool("get_order_items", domain="entity", order_id=target_order_id)
        if items_ev and isinstance(items_ev.get("data"), list):
            items_data = items_ev["data"]
            for item in items_data:
                iid = item.get("order_item_id")
                if iid and iid not in item_ids:
                    item_ids.append(iid)
                sid = item.get("seller_id")
                if sid and sid not in seller_ids:
                    seller_ids.append(sid)
                price = float(item.get("price", 0.0))
                freight = float(item.get("freight_value", 0.0))
                items_total += (price + freight)

        # Selective Product Context
        if scope.get("include_product_context"):
            await call_tool("get_product_context", domain="entity", order_id=target_order_id)

        # Selective Sellers Call (Only if seller_ids not retrieved from order_items)
        if not seller_ids:
            sellers_ev = await call_tool("get_sellers", domain="entity", order_id=target_order_id)
            if sellers_ev and isinstance(sellers_ev.get("data"), list):
                for s in sellers_ev["data"]:
                    if s.get("seller_id") and s["seller_id"] not in seller_ids:
                        seller_ids.append(s["seller_id"])

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order-agent",
        target="shipment-agent",
    )

    # ------------------------------------------------------------------
    # 3. Shipment Agent Analysis
    # ------------------------------------------------------------------
    shipment_verdict = "insufficient_evidence"
    late_seller_ids: list[str] = []
    carrier_name: str | None = None
    timeline_complete = False

    # Call shipment summary if target_order_id exists
    if target_order_id:
        shipment_ev = await call_tool("get_shipment_summary", domain="shipment", order_id=target_order_id)
        if shipment_ev and isinstance(shipment_ev.get("data"), dict):
            ship_data = shipment_ev["data"]
            delivered_customer_at = ship_data.get("delivered_customer_at")
            estimated_delivery_at = ship_data.get("estimated_delivery_at")
            delivered_carrier_at = ship_data.get("delivered_carrier_at")
            shipping_limits = ship_data.get("shipping_limits", [])
            carrier_name = ship_data.get("carrier_name")

            if delivered_customer_at and estimated_delivery_at:
                timeline_complete = True
                if delivered_customer_at <= estimated_delivery_at:
                    shipment_verdict = "on_time"
                else:
                    # Delivered late -> evaluate seller delay vs logistics delay
                    is_seller_late = False
                    for limit in shipping_limits:
                        limit_at = limit.get("shipping_limit_at")
                        seller_id = limit.get("seller_id")
                        if delivered_carrier_at and limit_at and delivered_carrier_at > limit_at:
                            is_seller_late = True
                            if seller_id and seller_id not in late_seller_ids:
                                late_seller_ids.append(seller_id)
                    if is_seller_late:
                        shipment_verdict = "seller_delay"
                    else:
                        shipment_verdict = "logistics_delay"
            elif ship_data.get("order_status") in ("canceled", "unavailable"):
                timeline_complete = True
                shipment_verdict = "returned"
            elif delivered_carrier_at:
                shipment_verdict = "on_time"

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="shipment-agent",
        target="payment-agent",
    )

    # ------------------------------------------------------------------
    # 4. Payment / Refund Agent Analysis (Selective Timeline Tool Calls)
    # ------------------------------------------------------------------
    payment_verdict = "insufficient_evidence"
    captured_total_brl: float | None = None
    refunded_total_brl: float | None = None
    refundable_total_brl: float | None = None

    if target_order_id:
        payments_ev = await call_tool("get_order_payments", domain="payment", order_id=target_order_id)
        pmts: list[dict[str, Any]] = []
        if payments_ev and isinstance(payments_ev.get("data"), list):
            pmts = payments_ev["data"]
            if pmts:
                captured_total_brl = sum(float(p.get("payment_value", 0.0)) for p in pmts)
                refundable_total_brl = captured_total_brl
                refunded_total_brl = 0.0

        # Selective Payment Timeline (Only if payment claims exist or captured != items_total)
        is_payment_claim = any(t in ("payment_mismatch", "duplicate_charge", "valid_split_payment") for t in claim_topics)
        is_amount_mismatch = (captured_total_brl is not None and items_total > 0 and abs(captured_total_brl - items_total) > 0.01)

        pay_timeline_ev = None
        if is_payment_claim or is_amount_mismatch or scope.get("include_payment_timeline"):
            pay_timeline_ev = await call_tool("get_payment_timeline", domain="payment", order_id=target_order_id)

        if pay_timeline_ev and isinstance(pay_timeline_ev.get("data"), dict):
            pt_events = pay_timeline_ev["data"].get("events", [])
            has_duplicate_event = any(
                e.get("status") in ("duplicate", "duplicate_charge") or e.get("event_type") in ("duplicate_charge", "duplicate_capture")
                for e in pt_events
            )
            has_mismatch_event = any(e.get("status") == "mismatch" for e in pt_events)

            if has_duplicate_event:
                payment_verdict = "duplicate_capture"
            elif has_mismatch_event or is_amount_mismatch:
                payment_verdict = "capture_mismatch"

        # Selective Refund Timeline (Only if refund claims exist or order is canceled/unavailable)
        order_status = target_order_info.get("order_status") if target_order_info else None
        is_refund_claim = any(t in ("refund_pending", "refund_failed", "requested_full_refund") for t in claim_topics)
        is_canceled_or_unavailable = order_status in ("canceled", "unavailable")

        if is_refund_claim or is_canceled_or_unavailable or scope.get("include_refund_timeline"):
            ref_ev = await call_tool("get_refund_timeline", domain="payment", order_id=target_order_id)
            if ref_ev and isinstance(ref_ev.get("data"), dict):
                events = ref_ev["data"].get("events", [])
                for ev in events:
                    if ev.get("status") == "failed":
                        payment_verdict = "refund_failed"
                    elif ev.get("status") == "pending":
                        payment_verdict = "refund_pending"
                    elif ev.get("status") in ("completed", "refunded"):
                        payment_verdict = "refunded"
                        if ev.get("amount_brl"):
                            refunded_total_brl = (refunded_total_brl or 0.0) + float(ev["amount_brl"])

        # Default Reconciled check when captured matches items_total
        if payment_verdict == "insufficient_evidence":
            if captured_total_brl is not None and items_total > 0 and abs(captured_total_brl - items_total) < 0.01:
                payment_verdict = "reconciled"

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="payment-agent",
        target="policy-agent",
    )

    # ------------------------------------------------------------------
    # 5. Policy Agent & Primary Issue Hierarchy
    # ------------------------------------------------------------------
    policy_ev = await call_tool("get_policy", domain="policy", policy_version=policy_version)
    policy_rules: dict[str, Any] = {}
    if policy_ev and isinstance(policy_ev.get("data"), dict):
        policy_rules = policy_ev["data"].get("rules", {})

    order_status = target_order_info.get("order_status") if target_order_info else None
    has_paid_evidence = captured_total_brl is not None and captured_total_brl > 0

    data_conflicts: list[dict[str, Any]] = []

    # Hierarchy for Primary Issue
    if entity_status == "not_found":
        primary_issue = "insufficient_evidence"
    elif order_status == "canceled":
        if has_paid_evidence:
            if payment_verdict == "refund_failed":
                primary_issue = "refund_failed"
            elif payment_verdict == "refund_pending":
                primary_issue = "refund_pending"
            else:
                primary_issue = "canceled_order_paid"
        else:
            primary_issue = "insufficient_evidence"
    elif order_status == "unavailable":
        if has_paid_evidence:
            if payment_verdict == "refund_failed":
                primary_issue = "refund_failed"
            elif payment_verdict == "refund_pending":
                primary_issue = "refund_pending"
            else:
                primary_issue = "unavailable_order_paid"
        else:
            primary_issue = "insufficient_evidence"
    elif payment_verdict == "duplicate_capture":
        primary_issue = "duplicate_charge"
    elif payment_verdict == "refund_failed":
        primary_issue = "refund_failed"
    elif payment_verdict == "refund_pending":
        primary_issue = "refund_pending"
    elif payment_verdict == "capture_mismatch":
        primary_issue = "payment_mismatch"
    elif shipment_verdict == "seller_delay":
        primary_issue = "late_delivery_seller"
        if "late_delivery_logistics" in claim_topics:
            data_conflicts.append({
                "field": "delay_responsibility",
                "sources": ["customer_claim", "shipment_timeline"],
                "selected_source": "shipment_timeline",
                "resolution_code": "SELLER_LIMIT_EXCEEDED",
            })
    elif shipment_verdict == "logistics_delay":
        primary_issue = "late_delivery_logistics"
        if "late_delivery_seller" in claim_topics:
            data_conflicts.append({
                "field": "delay_responsibility",
                "sources": ["customer_claim", "shipment_timeline"],
                "selected_source": "shipment_timeline",
                "resolution_code": "CARRIER_DELAY_VERIFIED",
            })
    elif "valid_split_payment" in claim_topics and payment_verdict == "reconciled":
        primary_issue = "valid_split_payment"
    elif shipment_verdict == "on_time" or payment_verdict == "reconciled":
        # Evidence disproves claim (e.g. claimed delay or payment mismatch but evidence shows on_time / reconciled)
        primary_issue = "unsupported_claim"
    else:
        primary_issue = "insufficient_evidence"

    rule_config = policy_rules.get(primary_issue, {})

    # ------------------------------------------------------------------
    # 6. Domain Evidence Mapping & Claim Assessment
    # ------------------------------------------------------------------
    claim_assessments: list[dict[str, Any]] = []
    for claim in customer_claims:
        cid = claim.get("claim_id")
        topic = claim.get("topic")
        if not cid:
            continue
        
        # Select domain-specific evidence refs for this claim
        if topic in ("late_delivery_seller", "late_delivery_logistics"):
            domain_refs = (shipment_refs + entity_refs)[:5] or used_evidence_refs[:5]
        elif topic in ("duplicate_charge", "payment_mismatch", "valid_split_payment", "refund_pending", "refund_failed"):
            domain_refs = (payment_refs + entity_refs)[:5] or used_evidence_refs[:5]
        else:
            domain_refs = (policy_refs + entity_refs)[:5] or used_evidence_refs[:5]

        if topic == "requested_full_refund":
            is_eligible = rule_config.get("refund_brl", 0.0) > 0 or primary_issue in (
                "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
                "late_delivery_logistics", "duplicate_charge", "payment_mismatch", "refund_failed"
            )
            verdict = "supported" if is_eligible and primary_issue != "unsupported_claim" else "unsupported"
            claim_assessments.append({
                "claim_id": cid,
                "verdict": verdict,
                "confidence": entity_confidence,
                "evidence_refs": domain_refs,
            })
        elif topic == primary_issue:
            verdict = "supported" if primary_issue != "unsupported_claim" else "unsupported"
            claim_assessments.append({
                "claim_id": cid,
                "verdict": verdict,
                "confidence": entity_confidence,
                "evidence_refs": domain_refs,
            })
        else:
            # Claim contradicted by verified finding
            claim_assessments.append({
                "claim_id": cid,
                "verdict": "unsupported",
                "confidence": entity_confidence,
                "evidence_refs": domain_refs,
            })

    # Policy Decision Resolution
    case_status = rule_config.get("case_status", "no_action" if primary_issue == "unsupported_claim" else "action_required")
    rec_action = rule_config.get("recommended_action", "document_no_action")
    rec_refund = float(rule_config.get("refund_brl", 0.0))
    if rec_refund == 0.0 and primary_issue in ("canceled_order_paid", "unavailable_order_paid", "duplicate_charge"):
        rec_refund = captured_total_brl or 0.0

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary_issue.upper(),
        evidence_refs=policy_refs or used_evidence_refs[:5],
    )

    # ------------------------------------------------------------------
    # 7. Root Cause & Responsible Parties (Strict Schema Enum)
    # ------------------------------------------------------------------
    responsible_parties: list[dict[str, Any]] = []

    if primary_issue == "late_delivery_seller":
        pid = late_seller_ids[0] if late_seller_ids else (seller_ids[0] if seller_ids else None)
        responsible_parties.append({"party_type": "seller", "party_id": pid})
    elif primary_issue == "late_delivery_logistics":
        responsible_parties.append({"party_type": "logistics_provider", "party_id": carrier_name})
    elif primary_issue in ("duplicate_charge", "payment_mismatch", "refund_failed", "refund_pending", "valid_split_payment"):
        responsible_parties.append({"party_type": "payment_provider", "party_id": None})
    elif primary_issue in ("canceled_order_paid", "unavailable_order_paid"):
        pid = seller_ids[0] if seller_ids else None
        responsible_parties.append({"party_type": "seller", "party_id": pid})
    elif primary_issue == "unsupported_claim":
        responsible_parties.append({"party_type": "customer", "party_id": customer_unique_id})
    else:
        responsible_parties.append({"party_type": "unknown", "party_id": None})

    refund_lines: list[dict[str, Any]] = []
    if rec_refund > 0:
        refund_lines.append({
            "reason_code": primary_issue.upper(),
            "amount_brl": rec_refund,
            "entity_id": target_order_id,
        })

    # ------------------------------------------------------------------
    # 8. Real Independent Verifier Invariants
    # ------------------------------------------------------------------
    verification_passed = True

    if scope.get("require_independent_verification"):
        # Entity invariant
        if entity_status == "resolved" and not resolved_order_ids:
            verification_passed = False
        # Shipment invariants
        if primary_issue == "late_delivery_seller" and shipment_verdict != "seller_delay":
            verification_passed = False
        if primary_issue == "late_delivery_logistics" and shipment_verdict != "logistics_delay":
            verification_passed = False
        # Payment invariants
        if primary_issue == "duplicate_charge" and payment_verdict != "duplicate_capture":
            verification_passed = False
        if primary_issue == "payment_mismatch" and payment_verdict != "capture_mismatch":
            verification_passed = False
        if primary_issue == "refund_failed" and payment_verdict != "refund_failed":
            verification_passed = False
        if primary_issue == "refund_pending" and payment_verdict != "refund_pending":
            verification_passed = False
        # Order status invariants
        if primary_issue == "canceled_order_paid" and order_status != "canceled":
            verification_passed = False
        if primary_issue == "unavailable_order_paid" and order_status != "unavailable":
            verification_passed = False
        # Evidence invariant
        if not used_evidence_refs:
            verification_passed = False

        if verification_passed:
            trace.emit(
                case_id=case_id,
                event_type="verification_completed",
                actor="verifier",
                decision_code="PASSED",
                evidence_refs=used_evidence_refs[:10],
            )
        else:
            case_status = "needs_investigation"

    # ------------------------------------------------------------------
    # 9. Dynamic Calibration & Confidence Assessment
    # ------------------------------------------------------------------
    calibrated_conf = entity_confidence
    if entity_status != "resolved":
        calibrated_conf = min(calibrated_conf, 0.50)

    if primary_issue == "insufficient_evidence":
        calibrated_conf = min(calibrated_conf, 0.35)
        case_status = "needs_investigation"
    elif primary_issue == "unsupported_claim":
        calibrated_conf = min(calibrated_conf, 0.90)
    else:
        if primary_issue in ("late_delivery_seller", "late_delivery_logistics") and not timeline_complete:
            calibrated_conf *= 0.85
        if primary_issue in ("duplicate_charge", "payment_mismatch") and payment_verdict == "insufficient_evidence":
            calibrated_conf *= 0.70

    if data_conflicts:
        calibrated_conf *= 0.80

    if not verification_passed:
        calibrated_conf *= 0.60

    final_confidence = round(max(0.10, min(0.99, calibrated_conf)), 2)

    # ------------------------------------------------------------------
    # 10. Final L3B Output Contract Assembly
    # ------------------------------------------------------------------
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": [],
            "case_status": case_status,
            "confidence": final_confidence,
        },
        "affected_entities": {
            "order_ids": resolved_order_ids,
            "item_ids": list(dict.fromkeys(item_ids)),
            "seller_ids": list(dict.fromkeys(seller_ids)),
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": entity_status,
            "resolved_order_ids": resolved_order_ids,
            "rejected_candidates": rejected_candidates,
            "confidence": entity_confidence,
        },
        "customer_context": {
            "customer_unique_id": customer_unique_id,
            "related_order_ids": list(dict.fromkeys(related_order_ids)),
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": list(dict.fromkeys(late_seller_ids)),
            "timeline_complete": timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": captured_total_brl,
            "refunded_total_brl": refunded_total_brl,
            "refundable_total_brl": refundable_total_brl,
        },
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": primary_issue.upper(), "rank": 1}
            ],
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": used_evidence_refs[:30],
        "data_conflicts": data_conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": rec_refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [rec_action],
    }



