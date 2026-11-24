import json

import pytest
from fastapi.testclient import TestClient
from sse_starlette.sse import AppStatus

from app.config.settings import clear_settings_cache
from app.core.data_scope import bind_data_scope, scope_from_identity
from app.main import create_app
from app.repositories.models import ProductRow, ShoppingCheckpointRow
from app.repositories.shopping_checkpoint import ShoppingCheckpointRepository
from app.schemas.auth import AuthIdentity
from app.tools.product_tools import GetProductDetailTool, RecommendProductsTool
from tests.auth_support import prepare_auth_database, test_headers


@pytest.fixture
def navigation_app(tmp_path, monkeypatch):
    path = tmp_path / "navigation.db"
    prepare_auth_database(path)
    tokens = test_headers(path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(AppStatus, "should_exit_event", None)
    clear_settings_cache()
    app = create_app()
    try:
        with TestClient(app) as client:
            async def seed():
                async with app.state.container.session_factory.begin() as db:
                    for i in range(3):
                        db.add(ProductRow(
                            sku=f"NAV-{i + 1}", merchant_id="m_demo", name="通勤鞋",
                            category="服饰", inventory=10, price_cents=22500,
                            tags="color:黑色,size:42,feature:防滑",
                        ))
            client.portal.call(seed)
            yield app, client, {uid: {"Authorization": f"Bearer {token}"} for uid, token in tokens.items()}
    finally:
        clear_settings_cache()


def test_navigation_continues_across_chat_requests(navigation_app, monkeypatch):
    _, client, headers = navigation_app
    detail_calls = []
    recall_calls = []
    original_detail = GetProductDetailTool.execute
    original_recall = RecommendProductsTool.execute

    async def detail(self, parsed, context):
        detail_calls.append(parsed.sku)
        return await original_detail(self, parsed, context)

    async def recall(self, parsed, context):
        recall_calls.append(parsed.offset)
        return await original_recall(self, parsed, context)

    monkeypatch.setattr(GetProductDetailTool, "execute", detail)
    monkeypatch.setattr(RecommendProductsTool, "execute", recall)

    def chat(message, session=None):
        request = {"message": message}
        if session:
            request["session_id"] = session
        response = client.post("/api/v1/chat", json=request, headers=headers["agent"])
        assert response.status_code == 200, response.text
        return response.json()

    initial = chat("推荐300元以内黑色42码防滑通勤鞋")
    initial_view = initial["shopping_view"]
    assert initial_view["view"] == "list"
    assert [(p["sku"], p["position"]) for p in initial_view["products"]] == [
        ("NAV-1", 1), ("NAV-2", 2), ("NAV-3", 3),
    ]
    session = initial["session_id"]
    parent = initial["checkpoint_id"]
    path = f"/api/v1/sessions/{session}/shopping-checkpoint"
    for query, pointer, expected in [
        ("换一个", 1, "NAV-2"),
        ("再换一个", 2, "NAV-3"),
        ("对比第1和第3", 2, "商品对比"),
        ("选第2个", 1, "已记录"),
        ("上一个", 0, "NAV-1"),
    ]:
        answer = chat(query, session)
        assert expected in answer["answer"]
        assert answer["shopping_view"]["display_index"] == pointer
        if query.startswith("对比"):
            assert answer["shopping_view"]["compare_skus"] == ["NAV-1", "NAV-3"]
        if query.startswith("选"):
            assert answer["shopping_view"]["selected_sku"] == "NAV-2"
        saved = client.get(path, headers=headers["agent"]).json()
        snapshot = saved["snapshot"]
        loop = snapshot["loop"]
        assert saved["parent_id"] == parent
        parent = saved["checkpoint_id"]
        assert snapshot["step_count"] == 4
        assert snapshot["requirements"]["constraints"] == {"max_price_cents": 30000, "color": "黑色", "size": "42"}
        assert [p["sku"] for p in loop["candidates"]] == loop["checked_skus"] == loop["display_skus"] == [
            "NAV-1", "NAV-2", "NAV-3",
        ]
        assert loop["display_index"] == pointer
    assert loop["selected_sku"] == "NAV-2"
    assert loop["compare_skus"] == ["NAV-1", "NAV-3"]
    assert detail_calls == ["NAV-1", "NAV-2", "NAV-3"]
    assert recall_calls == [0]
    isolated = chat("换一个")
    assert "没有可继续浏览" in isolated["answer"]
    assert not isolated["checkpoint_id"]
    assert isolated["shopping_view"] is None
    unauthorized = client.post(
        "/api/v1/chat", json={"message": "换一个", "session_id": session}, headers=headers["other"],
    )
    assert unauthorized.status_code == 403
    assert "NAV-" not in unauthorized.text


def test_navigation_snapshot_scope_version_and_unfinished_lookup(navigation_app):
    app, client, headers = navigation_app
    first = client.post("/api/v1/chat", json={"message": "推荐300元以内通勤鞋"}, headers=headers["agent"]).json()

    async def verify():
        factory = app.state.container.session_factory
        actor = AuthIdentity("agent", "service_agent", merchant_id="m_demo")
        async with factory.begin() as db:
            row = await db.get(ShoppingCheckpointRow, first["checkpoint_id"])
            with bind_data_scope(scope_from_identity(actor)):
                repository = ShoppingCheckpointRepository(db)
                assert await repository.latest_unfinished(first["session_id"]) is None
                row.status = "interrupted"
                await db.flush()
                snapshot = await repository.latest_unfinished(first["session_id"])
                assert snapshot and snapshot.loop["verified"][0]["sku"] == "NAV-1"
                assert await repository.recover_navigation(first["session_id"], snapshot.requirements)
            for foreign_actor in (
                AuthIdentity("agent", "service_agent", merchant_id="m_second"),
                AuthIdentity("different-agent", "service_agent", merchant_id="m_demo"),
            ):
                with bind_data_scope(scope_from_identity(foreign_actor)):
                    foreign = ShoppingCheckpointRepository(db)
                    assert await foreign.latest_unfinished(first["session_id"]) is None
                    assert await foreign.recover_navigation(first["session_id"], snapshot.requirements) is None
            with bind_data_scope(scope_from_identity(actor)):
                row.snapshot_json = json.dumps({**json.loads(row.snapshot_json), "version": 2})
                await db.flush()
                assert await repository.latest_unfinished(first["session_id"]) is None
                assert await repository.recover_navigation(first["session_id"], snapshot.requirements) is None

    client.portal.call(verify)
