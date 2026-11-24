from fastapi.testclient import TestClient

from app.config.settings import clear_settings_cache
from app.core.data_scope import bind_data_scope, scope_from_identity
from app.main import create_app
from app.repositories.models import ProductRow
from app.schemas.auth import AuthIdentity


def test_multiturn_shopping_is_persisted_and_does_not_leak_across_tasks(tmp_path, monkeypatch, auth_headers):
    clear_settings_cache()
    app = create_app()
    try:
        with TestClient(app, headers=auth_headers("u")) as client:

            async def seed():
                async with app.state.container.session_factory() as db:
                    for sku, color, size, price in [
                        ("A-black", "黑色", "42", 29900),
                        ("B-white", "白色", "42", 30000),
                        ("C-too-expensive", "白色", "42", 35000),
                        ("D-wrong-size", "白色", "43", 25000),
                    ]:
                        db.add(
                            ProductRow(merchant_id="m_demo", 
                                sku=sku,
                                name=f"{color}{size}码鞋",
                                category="鞋",
                                price_cents=price,
                                inventory=1,
                                tags=f"color:{color},size:{size}",
                            )
                        )
                    await db.commit()

            client.portal.call(seed)
            first = client.post(
                "/api/v1/chat", json={"message": "推荐300元以内黑色42码的鞋", "user_id": "u"}
            )
            assert first.status_code == 200
            session_id = first.json()["session_id"]
            assert "A-black" in first.json()["answer"]
            second = client.post(
                "/api/v1/chat",
                json={"message": "白色也可以", "session_id": session_id, "user_id": "u"},
            )
            assert second.status_code == 200
            assert "A-black" in second.json()["answer"] and "B-white" in second.json()["answer"]
            assert "C-too-expensive" not in second.json()["answer"]
            assert "D-wrong-size" not in second.json()["answer"]
            third = client.post(
                "/api/v1/chat",
                json={"message": "换成白色", "session_id": session_id, "user_id": "u"},
            )
            assert "B-white" in third.json()["answer"] and "A-black" not in third.json()["answer"]
            fourth = client.post(
                "/api/v1/chat",
                json={"message": "再推荐一个耳机", "session_id": session_id, "user_id": "u"},
            )
            assert fourth.status_code == 200

            async def load():
                from app.repositories.session_repository import SessionRepository

                async with app.state.container.session_factory() as db:
                    with bind_data_scope(scope_from_identity(AuthIdentity("u", "buyer"))):
                        task = await SessionRepository(db).shopping_task(session_id)
                    assert task.task_query == "耳机"
                    assert task.constraints == {}
                    with bind_data_scope(scope_from_identity(AuthIdentity("u", "buyer"))):
                        repo = SessionRepository(db)
                        await repo.get_or_create("other-session", "u")
                        assert await repo.shopping_task("other-session") is None

            client.portal.call(load)
    finally:
        clear_settings_cache()
