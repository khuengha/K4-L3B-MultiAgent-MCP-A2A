from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import Investigation, _payment, solve_case


class Gateway:
    def __init__(self, data):
        self.data = data
        self.calls = []

    async def list_tools(self):
        return list(self.data)

    async def call(self, tool_name, *, case_id, **arguments):
        self.calls.append((tool_name, case_id, arguments))
        value = self.data[tool_name]
        if isinstance(value, Exception):
            raise value
        return {"evidence_ref": f"ev_{len(self.calls):020d}", "data": copy.deepcopy(value)}


@pytest.fixture
def setup(tmp_path):
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    case = {
        "case_id": "CASE_001",
        "customer_unique_id_hint": "customer-1",
        "candidate_order_ids": ["order-1", "wrong-order"],
        "policy_version": "P1",
        "customer_request": {"claims": [{"claim_id": "claim-1", "topic": "duplicate_charge"}]},
        "investigation_scope": {"include_product_context": True},
    }
    data = {
        "get_customer_history": {
            "customer_unique_id": "customer-1",
            "orders": [{"order_id": "order-1"}],
        },
        "get_order": {"order_id": "order-1", "order_status": "delivered"},
        "get_order_items": [
            {
                "order_item_id": "item-1",
                "seller_id": "seller-1",
                "price": "80",
                "freight_value": "9",
            }
        ],
        "get_shipment_summary": {
            "order_id": "order-1",
            "delivered_customer_at": "2018-01-10",
            "estimated_delivery_at": "2018-01-11",
            "delivered_carrier_at": "2018-01-02",
            "shipping_limits": [{"seller_id": "seller-1", "shipping_limit_at": "2018-01-03"}],
            "events": [],
        },
        "get_payment_timeline": {
            "order_id": "order-1",
            "payments": [{"payment_sequential": "1"}, {"payment_sequential": "2"}],
            "events": [
                {"event_type": "captured", "amount_brl": "44.50", "status": "confirmed"},
                {"event_type": "captured", "amount_brl": "44.50", "status": "confirmed"},
            ],
        },
        "get_refund_timeline": {"order_id": "order-1", "events": []},
        "get_product_context": [],
        "get_policy": {
            "rules": {
                "valid_split_payment": {
                    "case_status": "no_action",
                    "recommended_action": "document_no_action",
                    "refund_brl": 0,
                    "responsible_parties": [],
                }
            }
        },
    }
    return case, Gateway(data), trace


def test_split_payment_is_not_duplicate_charge(setup):
    case, gateway, trace = setup
    output = asyncio.run(solve_case(case, gateway, trace))
    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["claim_assessments"][0]["verdict"] == "unsupported"
    assert output["entity_resolution"]["rejected_candidates"] == ["wrong-order"]
    assert output["payment_analysis"]["captured_total_brl"] == 89
    assert len(gateway.calls) == 8
    assert all(call[1] == case["case_id"] for call in gateway.calls)
    trace.contracts.validate_output(output, "test")


def test_refund_failure_never_invents_zero_balance(setup):
    case, gateway, trace = setup
    gateway.data["get_refund_timeline"] = RuntimeError("unavailable")
    output = asyncio.run(solve_case(case, gateway, trace))
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["payment_analysis"]["refunded_total_brl"] is None
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_ambiguous_entity_does_not_investigate_arbitrary_candidate(setup):
    case, gateway, trace = setup
    gateway.data["get_customer_history"]["orders"].append({"order_id": "wrong-order"})
    output = asyncio.run(solve_case(case, gateway, trace))
    assert output["entity_resolution"]["status"] == "ambiguous"
    assert output["affected_entities"]["order_ids"] == []
    assert {call[0] for call in gateway.calls} == {"get_customer_history", "get_policy"}


def test_conflicting_items_block_refund(setup):
    case, gateway, trace = setup
    gateway.data["get_order"]["order_status"] = "canceled"
    gateway.data["get_order_items"].append(
        {"order_item_id": "item-1", "seller_id": "seller-1", "price": "90", "freight_value": "9"}
    )
    gateway.data["get_policy"]["rules"]["canceled_order_paid"] = {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "refund_brl": 89,
    }
    output = asyncio.run(solve_case(case, gateway, trace))
    assert output["data_conflicts"]
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_case_local_cache_and_failed_call_cache(setup):
    case, gateway, trace = setup

    async def run():
        ctx = Investigation(case, gateway, trace)
        ctx.tools = set(await gateway.list_tools())
        first = await ctx.fetch("entity-agent", "get_order", order_id="order-1")
        assert first == await ctx.fetch("entity-agent", "get_order", order_id="order-1")

    asyncio.run(run())
    assert len(gateway.calls) == 1


def test_refund_completed_supersedes_pending():
    payment = {"events": [{"event_type": "captured", "amount_brl": "89", "status": "confirmed"}]}
    refund = {
        "events": [
            {"event_type": "refund_requested", "status": "pending", "event_at": "2018-01-01"},
            {
                "event_type": "refunded",
                "status": "confirmed",
                "event_at": "2018-01-02",
                "amount_brl": "89",
            },
        ]
    }
    result = _payment(payment, refund, [])
    assert result["verdict"] == "refunded"
    assert result["refundable_total_brl"] == 0


def test_policy_refund_is_capped_by_remaining_capture(setup):
    case, gateway, trace = setup
    gateway.data["get_order"]["order_status"] = "canceled"
    gateway.data["get_policy"]["rules"]["canceled_order_paid"] = {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "refund_brl": 100,
    }
    output = asyncio.run(solve_case(case, gateway, trace))
    assert output["financial_resolution"]["recommended_refund_brl"] == 89


def test_cross_order_evidence_is_rejected(setup):
    case, gateway, trace = setup
    gateway.data["get_order"]["order_id"] = "another-order"
    output = asyncio.run(solve_case(case, gateway, trace))
    assert output["entity_resolution"]["resolved_order_ids"] == []
    assert output["assessment"]["case_status"] == "needs_investigation"


def test_nested_cross_order_event_is_rejected(setup):
    case, gateway, trace = setup
    gateway.data["get_payment_timeline"]["events"][0]["order_id"] = "another-order"
    output = asyncio.run(solve_case(case, gateway, trace))
    assert output["payment_analysis"]["captured_total_brl"] is None
    assert output["assessment"]["case_status"] == "needs_investigation"


def test_malformed_refund_payload_is_not_zero_refund(setup):
    case, gateway, trace = setup
    gateway.data["get_refund_timeline"] = {}
    output = asyncio.run(solve_case(case, gateway, trace))
    assert output["payment_analysis"]["refunded_total_brl"] is None
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_gateway_accepts_mcp_v2_snake_case_result():
    from types import SimpleNamespace

    from student_agent.mcp_gateway import EvidenceGateway

    evidence = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_" + "a" * 20,
        "result_hash": "sha256:" + "0" * 64,
        "domain": "order",
        "data": {"order_id": "order-1"},
    }

    class Session:
        async def call_tool(self, tool_name, arguments):
            return SimpleNamespace(is_error=False, structured_content=evidence, content=[])

    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
    gateway = EvidenceGateway(Session(), contracts)
    assert (
        asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="order-1")) == evidence
    )
