import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.time import utcnow
from app.main import create_app
from tests.auth_support import TEST_PASSWORD


def test_login_logout_wrong_credentials_and_safe_responses(auth_headers):
    with TestClient(create_app()) as client:
        for uid, password in [("u_demo", "wrong"), ("unknown", TEST_PASSWORD)]:
            response = client.post(
                "/api/v1/auth/login", json={"user_id": uid, "password": password}
            )
            assert response.status_code == 401
            assert password not in response.text
        response = client.post(
            "/api/v1/auth/login", json={"user_id": "u_demo", "password": TEST_PASSWORD}
        )
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        body = response.json()
        assert "password" not in json.dumps(body) and "digest" not in json.dumps(body)
        headers = {"Authorization": "Bearer " + body["access_token"]}
        assert client.get("/api/v1/auth/me", headers=headers).json()["user_id"] == "u_demo"
        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
        assert (
            client.get("/api/v1/auth/me", headers={"Authorization": "Bearer forged"}).status_code
            == 401
        )
        invalid = client.post(
            "/api/v1/auth/login", json={"user_id": "u_demo", "password": {"secret": "do-not-echo"}}
        )
        assert invalid.status_code == 422 and "do-not-echo" not in invalid.text


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/chat", {"message": "你好", "user_id": "u_demo"}),
        ("post", "/chat/stream", {"message": "你好"}),
        ("get", "/sessions/fake/reception?user_id=u_demo", None),
        ("post", "/sessions/fake/close", {"reason": "resolved"}),
        ("post", "/sessions/fake/feedback", {"score": 5, "resolved": True}),
        ("get", "/sessions/fake/shopping-checkpoint", None),
        ("post", "/shopping-checkpoints/fake/resume", {}),
    ],
)
def test_all_business_endpoints_require_bearer(method, path, body):
    with TestClient(create_app()) as client:
        response = client.request(
            method.upper(), "/api/v1" + path, **({"json": body} if body is not None else {})
        )
        assert response.status_code == 401


def test_management_auth_priority_and_disable_protections(auth_headers, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "legacy-test-admin")
    with TestClient(create_app()) as client:
        path = "/api/v1/admin/tools"
        assert client.get(path).status_code == 401
        assert client.get(path, headers=[(b"x-admin-token", b"\xff")]).status_code == 401
        assert client.get(path, headers={"x-admin-token": "legacy-test-admin"}).status_code == 200
        assert (
            client.get(
                path,
                headers={"Authorization": "Bearer forged", "x-admin-token": "legacy-test-admin"},
            ).status_code
            == 401
        )
        assert (
            client.get(
                path, headers={**auth_headers(), "x-admin-token": "legacy-test-admin"}
            ).status_code
            == 403
        )
        assert client.get(path, headers=auth_headers("op")).status_code == 200
        assert (
            client.post("/api/v1/admin/users/op/disable", headers=auth_headers("op")).status_code
            == 403
        )
        assert (
            client.post("/api/v1/admin/users/op2/disable", headers=auth_headers("op")).status_code
            == 204
        )
        assert (
            client.post(
                "/api/v1/admin/users/op/disable", headers={"x-admin-token": "legacy-test-admin"}
            ).status_code
            == 403
        )


def test_expired_token_and_query_credentials_are_rejected_without_echo(auth_headers):
    with TestClient(create_app()) as client:
        headers = auth_headers()
        token = headers["Authorization"].split()[1]
        response = client.get("/api/v1/auth/me?access_token=" + token)
        assert response.status_code == 400 and token not in response.text
        response = client.get(
            "/api/v1/auth/me", headers={**headers, "x-trace-id": token, "x-request-id": token}
        )
        assert (
            response.status_code == 200
            and token not in str(response.headers)
            and token not in response.text
        )
        client.app.state.container.auth_service.now = lambda: utcnow() + timedelta(days=9)
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_two_http_instances_observe_committed_revocation():
    with TestClient(create_app()) as first, TestClient(create_app()) as second:
        login = first.post(
            "/api/v1/auth/login", json={"user_id": "u_demo", "password": TEST_PASSWORD}
        )
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        assert second.get("/api/v1/auth/me", headers=headers).status_code == 200
        assert first.post("/api/v1/auth/logout", headers=headers).status_code == 204
        assert second.get("/api/v1/auth/me", headers=headers).status_code == 401
