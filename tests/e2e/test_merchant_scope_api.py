from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.models import SessionRow


def test_agent_read_write_status_and_spoofing(auth_headers):
    with TestClient(create_app()) as client:

        async def setup():
            async with client.app.state.container.session_factory.begin() as db:
                db.add_all(
                    [
                        SessionRow(
                            session_id="shop-a",
                            user_id="u_demo",
                            channel="shop",
                            merchant_id="m_demo",
                        ),
                        SessionRow(
                            session_id="shop-b",
                            user_id="u_demo",
                            channel="shop",
                            merchant_id="m_second",
                        ),
                        SessionRow(session_id="platform-buyer", user_id="u_demo"),
                    ]
                )

        client.portal.call(setup)
        headers = auth_headers("agent")
        assert client.get("/api/v1/sessions/shop-a/reception", headers=headers).status_code == 200
        for sid in ("shop-b", "platform-buyer"):
            assert (
                client.get(
                    f"/api/v1/sessions/{sid}/reception?merchant_id=m_demo", headers=headers
                ).status_code
                == 404
            )
            assert (
                client.post(
                    f"/api/v1/sessions/{sid}/close",
                    headers=headers,
                    json={
                        "reason": "resolved",
                        "merchant_id": "m_demo",
                        "shop_id": "m_demo",
                        "user_id": "u_demo",
                    },
                ).status_code
                == 403
            )
        assert (
            client.get("/api/v1/sessions/shop-b/reception", headers=auth_headers()).status_code
            == 200
        )
        assert (
            client.get("/api/v1/sessions/shop-b/reception", headers=auth_headers("op")).status_code
            == 200
        )
        response = client.post(
            "/api/v1/chat",
            headers=headers,
            json={
                "message": "hello",
                "merchant_id": "m_second",
                "shop_id": "m_second",
                "user_id": "u_demo",
            },
        )
        assert response.status_code == 200

        async def verify():
            async with client.app.state.container.session_factory() as db:
                row = await db.get(SessionRow, response.json()["session_id"])
                assert (row.user_id, row.channel, row.merchant_id) == ("agent", "shop", "m_demo")

        client.portal.call(verify)
