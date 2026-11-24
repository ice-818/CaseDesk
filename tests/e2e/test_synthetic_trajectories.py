"""32 frozen trajectories (16 shopping + 16 after-sale) over real HTTP and storage.

Only the LLM provider is replaced. Manual run:
python -m pytest -q tests/e2e/test_synthetic_trajectories.py
"""

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.product_requirements import matches_requirements
from app.core.data_scope import bind_data_scope, scope_from_identity
from app.core.exceptions import ForbiddenError
from app.core.request_scope import request_session
from app.main import create_app
from app.providers.base import LLMResponse
from app.repositories.models import OrderRow, ProductRow, RefundRow, ReturnRow, UserRow
from app.schemas.auth import AuthIdentity
from app.schemas.common import TokenUsage
from app.services.seed import seed_demo_data
from app.tools.aftersale_tools import ProcessRefundTool
from app.tools.base import ToolContext

pytestmark = pytest.mark.trajectory
DATA = json.loads((Path(__file__).resolve().parent / "fixtures/trajectory_scripts.json").read_text(encoding="utf-8"))
PREFIXES = {
    "intent": "你是电商客服意图分类器", "complexity": "你是检索复杂度分类器",
    "rewrite": "你改写电商检索查询", "grade": "你评估检索结果",
    "planner": "你是电商客服规划器", "answer": "你是电商平台客服助手",
    "faithfulness": "你检测客服回答是否忠实", "self_correct": "你是电商客服改写器",
}


class ScriptedLLMProvider:
    def __init__(self):
        self.nodes = {}
        self.calls = []
        self.tool_outputs = []

    async def ping(self):
        return True

    async def generate(self, messages, *, temperature=0, max_tokens=1024, response_format=None):
        system = messages[0]["content"]
        kind = next((key for key, prefix in PREFIXES.items() if system.startswith(prefix)), "unknown")
        self.calls.append(kind)
        fallback = {"label": "faq_policy", "confidence": 0.9, "rationale": "safe", "prompt_version": "intent_v3"}
        payload = deepcopy(self.nodes.get(kind, fallback))
        if kind == "answer":
            text = messages[-1]["content"]
            if "工具结果：" in text:
                raw = text.split("工具结果：", 1)[1].strip()
                if raw.startswith("["):
                    outputs, _ = json.JSONDecoder().raw_decode(raw)
                    self.tool_outputs.extend(outputs)
                    payload["answer"] = json.dumps(outputs, ensure_ascii=False)
            payload["citation_chunk_ids"] = re.findall(r"(?m)^\[([^\]]+)\]", text)
        return LLMResponse(json.dumps(payload, ensure_ascii=False), TokenUsage(total_tokens=8), "scripted")

    async def stream(self, messages, *, temperature=0, max_tokens=1024, response_format=None):
        response = await self.generate(messages, temperature=temperature, max_tokens=max_tokens, response_format=response_format)
        yield response.content


async def database_state(factory):
    async with factory() as db:
        return {
            table.__tablename__: sorted(
                (tuple(row) for row in (await db.execute(select(table.__table__))).all()),
                key=repr,
            )
            for table in (OrderRow, RefundRow, ReturnRow, UserRow)
        }


def resolve(value, bindings):
    if isinstance(value, str):
        for key, replacement in bindings.items():
            value = value.replace("$" + key, replacement)
        return value
    if isinstance(value, list):
        return [resolve(item, bindings) for item in value]
    if isinstance(value, dict):
        return {key: resolve(item, bindings) for key, item in value.items()}
    return value


async def records(factory):
    async with factory() as db:
        refunds = list((await db.scalars(select(RefundRow).order_by(RefundRow.created_at))).all())
        returns = list((await db.scalars(select(ReturnRow).order_by(ReturnRow.created_at))).all())
        order = await db.get(OrderRow, "ORD10003")
        return {
            "refund_count": len(refunds), "return_count": len(returns),
            "active_count": sum(row.status in {"requested", "approved"} for row in refunds + returns),
            "refund_status": refunds[-1].status if refunds else None,
            "return_status": returns[-1].status if returns else None,
            "total": sum(row.amount_cents for row in refunds if row.status == "completed"),
            "order_status": order.status,
        }, {
            **({"refund_id": refunds[-1].refund_id} if refunds else {}),
            **({"return_id": returns[-1].return_id} if returns else {}),
        }


def assert_expect(expect, body, state, provider):
    assert body.get("answer", "").strip() if "answer" in body else body.get("error_code")
    if "pending" in expect:
        assert bool(body.get("pending_action")) == expect["pending"]
    if "refused" in expect:
        assert body["refused"] == expect["refused"]
    for key in ("refund_count", "return_count", "active_count", "refund_status", "return_status", "total", "order_status"):
        if key in expect:
            assert state[key] == expect[key], (key, state, expect)
    if expect.get("citations"):
        assert body["citations"]
    view = body.get("shopping_view")
    products = (view or {}).get("products", [])
    if "count" in expect:
        assert len(products) == expect["count"]
    if "min_count" in expect:
        assert len(products) >= expect["min_count"]
    if expect.get("hard"):
        assert view
        constraints = expect["constraints"]
        assert all(view["conditions"].get(key) == value for key, value in constraints.items())
        assert all(matches_requirements(p, constraints) and p["inventory"] > 0 for p in products)
    if "contains" in expect:
        assert expect["contains"] in body["answer"]
    for expected, field in (("selected", "selected_sku"), ("index", "display_index"), ("compare", "compare_skus")):
        if expected in expect:
            assert view[field] == expect[expected]
    for name in expect.get("calls", []):
        assert name in provider.calls
    if "answer_tool" in expect:
        assert provider.tool_outputs
        assert any(
            output["success"] and all(output["output"].get(key) == value for key, value in expect["answer_tool"].items())
            for output in provider.tool_outputs
        )


REGRESSIONS = {"AFTER-01", "AFTER-07", "AFTER-09", "AFTER-14", "AFTER-15", "AFTER-16"}


@pytest.mark.parametrize("trajectory", [
    pytest.param(item, id=item["trajectory_id"],
                 marks=pytest.mark.regression if item["trajectory_id"] in REGRESSIONS else ())
    for item in DATA["trajectories"]
])
def test_fixed_trajectory(trajectory, monkeypatch, auth_headers):
    provider = ScriptedLLMProvider()
    monkeypatch.setattr("app.services.container.OfflineLLMProvider", lambda: provider)
    with TestClient(create_app()) as client:
        factory = client.app.state.container.session_factory

        async def seed():
            async with factory.begin() as db:
                await seed_demo_data(db)
                for values in DATA["catalog"]:
                    db.add(ProductRow(**values))
        client.portal.call(seed)
        sessions = {}
        pending = {}
        last_confirm = {}
        previous_body = None
        for original in trajectory["turns"]:
            _, bindings = client.portal.call(records, factory)
            turn = resolve(original, bindings)
            user = turn.get("user", "u_demo")
            provider.nodes = turn.get("nodes", provider.nodes)
            provider.calls.clear()
            provider.tool_outputs.clear()
            if turn.get("repeat_confirmation"):
                payload = last_confirm[user]
            elif turn.get("confirm_pending"):
                assert user in pending
                payload = {**pending[user], "confirm_action_id": pending[user]["action_id"]}
                payload.pop("action_id")
                last_confirm[user] = payload
            else:
                payload = {"message": turn["message"]}
                if user in sessions:
                    payload["session_id"] = sessions[user]
            before = client.portal.call(database_state, factory)
            response = client.post("/api/v1/chat", json=payload, headers=auth_headers(user))
            assert response.status_code == turn.get("expect_status", 200), response.text
            body = response.json()
            assert not re.search(r"cds_[A-Za-z0-9_-]{43}", response.text)
            state, _ = client.portal.call(records, factory)
            assert_expect(turn["expect"], body, state, provider)
            if turn["expect"].get("no_business_change"):
                assert client.portal.call(database_state, factory) == before
            if turn["expect"].get("same_previous"):
                assert body["answer"] == previous_body["answer"]
                assert body["shopping_view"] == previous_body["shopping_view"]
            if turn["expect"].get("direct_denied"):
                async def denied(refund_id, session_id):
                    with bind_data_scope(scope_from_identity(AuthIdentity("u_demo", "buyer"))):
                        async with request_session(factory):
                            with pytest.raises(ForbiddenError):
                                await ProcessRefundTool().run(
                                    {"refund_id": refund_id, "action": "approve"},
                                    ToolContext(user_id="u_demo", session_id=session_id, trace_id="trajectory"),
                                )
                client.portal.call(denied, bindings["refund_id"], sessions[user])
                assert client.portal.call(database_state, factory) == before
            if response.status_code == 200:
                sessions[user] = body["session_id"]
                if body.get("pending_action"):
                    pending[user] = {
                        "message": payload["message"], "session_id": body["session_id"],
                        "action_id": body["pending_action"]["action_id"],
                    }
                previous_body = body


# 32 trajectories: 16 shopping + 16 after-sale.
# Release invariants: no unauthorized business writes/leaks, no duplicate refunds,
# and no hard-constraint violations in verified shopping results.
