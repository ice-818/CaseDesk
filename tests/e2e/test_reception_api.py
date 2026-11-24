from fastapi.testclient import TestClient

from app.config.settings import clear_settings_cache
from app.main import create_app


def test_reception_lifecycle_api(tmp_path, monkeypatch, auth_headers):
    clear_settings_cache()
    try:
        with TestClient(create_app(), headers=auth_headers("owner")) as client:
            response = client.post("/api/v1/chat", json={"message": "你好", "user_id": "owner"})
            assert response.status_code == 200
            session_id = response.json()["session_id"]
            path = f"/api/v1/sessions/{session_id}"
            status = client.get(f"{path}/reception", params={"user_id": "owner"})
            assert status.json()["status"] == "active"
            assert status.json()["resolved"] is False
            feedback_path = f"{path}/feedback"
            feedback = {
                "user_id": "owner",
                "score": 2,
                "resolved": False,
                "reason": "答复没解决问题",
            }
            assert client.post(feedback_path, json=feedback).status_code == 409
            assert client.get(f"{path}/reception", params={"user_id": "owner"}, headers=auth_headers("other")).status_code == 404
            assert (
                client.post(
                    f"{path}/close", json={"user_id": "owner", "reason": "timeout"}
                ).status_code
                == 422
            )
            closed = client.post(f"{path}/close", json={"user_id": "owner", "reason": "resolved"})
            assert closed.status_code == 200
            assert closed.json()["resolved"] is True
            assert closed.json()["end_reason"] == "resolved"
            again = client.post(f"{path}/close", json={"user_id": "owner", "reason": "user_closed"})
            assert again.json()["closed_at"] == closed.json()["closed_at"]
            assert again.json()["end_reason"] == "resolved"
            assert (
                client.post(feedback_path, json={**feedback, "user_id": "owner"}, headers=auth_headers("other")).status_code == 403
            )
            assert client.post(feedback_path, json={**feedback, "score": 6}).status_code == 422
            saved = client.post(feedback_path, json=feedback)
            assert saved.status_code == 200
            assert saved.json()["last_assistant_message_id"] is not None
            assert client.post(feedback_path, json=feedback).json() == saved.json()
            assert client.post(feedback_path, json={**feedback, "score": 5}).status_code == 409
            status = client.get(f"{path}/reception", params={"user_id": "owner"}).json()
            assert status["feedback"]["score"] == 2
            assert status["feedback"]["resolved"] is False
            assert status["end_reason"] == "resolved"
            rejected = client.post(
                "/api/v1/chat",
                json={
                    "message": "再聊聊",
                    "user_id": "owner",
                    "session_id": session_id,
                },
            )
            assert rejected.status_code == 409
            rejected_stream = client.post(
                "/api/v1/chat/stream",
                json={
                    "message": "再聊聊",
                    "user_id": "owner",
                    "session_id": session_id,
                },
            )
            assert rejected_stream.status_code == 409
            assert (
                client.post(
                    "/api/v1/chat", json={"message": "你好", "user_id": "owner"}
                ).status_code
                == 200
            )
    finally:
        clear_settings_cache()
