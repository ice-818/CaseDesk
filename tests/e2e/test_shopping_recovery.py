from fastapi.testclient import TestClient

from app.config.settings import clear_settings_cache
from app.main import create_app
from app.repositories.models import ProductRow


def test_checkpoint_survives_failure_and_service_restart_and_rechecks_price(tmp_path, monkeypatch, auth_headers):
    clear_settings_cache()
    try:
        app = create_app()
        with TestClient(app, raise_server_exceptions=False, headers=auth_headers("u")) as client:

            async def seed(price):
                async with app.state.container.session_factory() as db:
                    row = await db.get(ProductRow, "SHOE")
                    if row is None:
                        db.add(
                            ProductRow(merchant_id="m_demo", 
                                sku="SHOE",
                                name="黑色鞋",
                                category="鞋",
                                price_cents=price,
                                inventory=1,
                                tags="color:黑色,size:42",
                            )
                        )
                    else:
                        row.price_cents = price
                    await db.commit()

            client.portal.call(seed, 29900)
            session_id = client.post(
                "/api/v1/chat", json={"message": "你好", "user_id": "u"}
            ).json()["session_id"]
            original = app.state.container.chat_service._invoke

            async def crash_after_tools(state):
                await original(state)
                raise RuntimeError("injected failure after durable tool checkpoints")

            monkeypatch.setattr(app.state.container.chat_service, "_invoke", crash_after_tools)
            failed = client.post(
                "/api/v1/chat",
                json={
                    "message": "推荐300元以内黑色42码鞋",
                    "user_id": "u",
                    "session_id": session_id,
                },
            )
            assert failed.status_code == 500
            path = f"/api/v1/sessions/{session_id}/shopping-checkpoint"
            saved = client.get(path, params={"user_id": "u"}).json()
            assert saved["status"] == "interrupted"
            assert saved["snapshot"]["loop"]["verified"][0]["sku"] == "SHOE"
            checkpoint_id = saved["checkpoint_id"]
            assert client.get(path, params={"user_id": "u"}, headers=auth_headers("other")).status_code == 404
            assert (
                client.post(
                    f"/api/v1/shopping-checkpoints/{checkpoint_id}/resume",
                    json={"user_id": "u"}, headers=auth_headers("other"),
                ).status_code
                == 404
            )
            client.portal.call(seed, 35000)
        # Rebuild the entire container with the same file DB: no old in-memory state.
        clear_settings_cache()
        with TestClient(create_app(), headers=auth_headers("u")) as client:
            recovered = client.post(
                f"/api/v1/shopping-checkpoints/{checkpoint_id}/resume", json={"user_id": "other"}
            )
            assert recovered.status_code == 200
            assert "未找到" in recovered.json()["answer"]
            assert "SHOE" not in recovered.json()["answer"]
            assert recovered.json()["checkpoint_id"] != checkpoint_id
            latest = client.get(path, params={"user_id": "u"}).json()
            assert latest["status"] == "completed"
            assert latest["parent_id"] == checkpoint_id
            assert (
                client.post(
                    f"/api/v1/shopping-checkpoints/{latest['checkpoint_id']}/resume",
                    json={"user_id": "u"},
                ).status_code
                == 409
            )
    finally:
        clear_settings_cache()
