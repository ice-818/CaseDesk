import json

from fastapi.testclient import TestClient

from app.core.time import utcnow
from app.main import create_app
from app.repositories.models import SessionRow, ShoppingCheckpointRow
from tests.e2e.test_data_evaluation_platform import dataset_payload


def test_client_identity_is_ignored_and_foreign_resources_stay_private(auth_headers):
    with TestClient(create_app()) as client:
        headers = auth_headers()
        first = client.post(
            "/api/v1/chat", headers=headers, json={"message": "你好", "user_id": "other"}
        )
        assert first.status_code == 200
        session_id = first.json()["session_id"]

        async def inspect_and_checkpoint():
            async with client.app.state.container.session_factory() as db:
                session = await db.get(SessionRow, session_id)
                assert session.user_id == "u_demo"
                db.add(
                    ShoppingCheckpointRow(
                        checkpoint_id="private-checkpoint",
                        session_id=session_id,
                        user_id="u_demo",
                        status="interrupted",
                        snapshot_json=json.dumps(
                            {
                                "version": 1,
                                "requirements": {"source_query": "推荐鞋", "active": True},
                            }
                        ),
                        updated_at=utcnow(),
                    )
                )
                await db.commit()

        client.portal.call(inspect_and_checkpoint)
        path = f"/api/v1/sessions/{session_id}"
        assert client.get(path + "/reception?user_id=other", headers=headers).status_code == 200
        assert (
            client.get(path + "/shopping-checkpoint?user_id=other", headers=headers).status_code
            == 200
        )
        assert (
            client.get(
                path + "/shopping-checkpoint?user_id=u_demo", headers=auth_headers("other")
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/v1/shopping-checkpoints/private-checkpoint/resume",
                headers=auth_headers("other"),
                json={"user_id": "u_demo"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                path + "/close", headers=headers, json={"user_id": "other", "reason": "resolved"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                path + "/feedback",
                headers=headers,
                json={"user_id": "other", "score": 5, "resolved": True},
            ).status_code
            == 200
        )
        assert (
            client.post(
                path + "/feedback",
                headers=auth_headers("other"),
                json={"user_id": "u_demo", "score": 5, "resolved": True},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/chat",
                headers=auth_headers("other"),
                json={"message": "你好", "session_id": session_id, "user_id": "u_demo"},
            ).status_code
            == 403
        )
        stream = client.post(
            "/api/v1/chat/stream",
            headers=headers,
            json={"message": "你好", "user_id": {"forged": "other"}},
        )
        assert stream.status_code == 200 and '"answer"' in stream.text


def test_operator_and_legacy_reviewer_cannot_be_forged(auth_headers):
    with TestClient(create_app()) as client:
        root = "/api/v1/admin/platform/datasets"
        for index, (headers, reviewer) in enumerate(
            [(auth_headers("op"), "op"), ({}, "system:dev-admin")]
        ):
            payload = dataset_payload()
            payload["version"] = f"review-{index}"
            created = client.post(root, headers=headers, json=payload)
            assert created.status_code == 201
            reviewed = client.post(
                root + "/" + created.json()["dataset_id"] + "/review",
                headers=headers,
                json={"decision": "approved", "reviewer": "forged-operator", "note": "checked"},
            )
            assert reviewed.status_code == 200 and reviewed.json()["reviewer"] == reviewer
        assert (
            client.get(
                "/api/v1/admin/tools", headers={"Authorization": "Bearer invalid"}
            ).status_code
            == 401
        )
        assert (
            client.get("/api/v1/admin/tools", headers={"x-admin-token": "wrong"}).status_code == 401
        )
        assert client.get("/api/v1/admin/tools", headers=auth_headers("agent")).status_code == 403
        # The dev admin exception never opens business endpoints.
        assert client.post("/api/v1/chat", json={"message": "你好"}).status_code == 401


def test_production_has_no_admin_bypass(auth_headers):
    with TestClient(create_app()) as client:
        client.app.state.container.settings.app_env = "production"
        assert client.get("/api/v1/admin/tools").status_code == 401


def test_openapi_deprecates_ignored_client_ids():
    schema = create_app().openapi()
    assert schema["components"]["schemas"]["ChatRequest"]["properties"]["user_id"]["deprecated"]
    assert "user_id" not in schema["components"]["schemas"]["ChatRequest"].get("required", [])
    parameters = schema["paths"]["/api/v1/sessions/{session_id}/reception"]["get"]["parameters"]
    assert next(p for p in parameters if p["name"] == "user_id")["deprecated"]
