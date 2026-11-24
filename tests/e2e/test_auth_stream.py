import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app


@pytest.mark.parametrize("stream", [True, False])
def test_revocation_cancels_long_execution_and_never_returns_answer(auth_headers, stream):
    entered, cancelled = threading.Event(), threading.Event()

    class SlowGraph:
        async def ainvoke(self, state):
            assert "token_digest" not in state and "auth" not in state
            entered.set()
            try:
                await asyncio.sleep(3600)
            finally:
                cancelled.set()

        async def astream(self, state, **kwargs):
            assert "token_digest" not in state and "auth" not in state
            entered.set()
            try:
                yield {"classify_intent": {}}
                await asyncio.sleep(3600)
            finally:
                cancelled.set()

    with TestClient(create_app()) as client, ThreadPoolExecutor(max_workers=1) as pool:
        client.app.state.container.chat_service._graph = SlowGraph()
        headers = auth_headers()
        future = pool.submit(
            client.post,
            "/api/v1/chat" + ("/stream" if stream else ""),
            headers=headers,
            json={"message": "long request"},
        )
        assert entered.wait(5)
        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
        response = future.result(timeout=8)
        assert cancelled.wait(1)
        if stream:
            assert response.status_code == 200
            events = [
                json.loads(line[5:])
                for line in response.text.splitlines()
                if line.startswith("data:")
            ]
            assert events[-1]["event"] == "error" and events[-1]["status_code"] == 401
            assert not any(event["event"] == "answer" for event in events)
        else:
            assert response.status_code == 401


def test_auth_database_error_never_uses_dev_admin_bypass(auth_headers):
    with TestClient(create_app()) as client:

        async def break_auth_storage():
            async with client.app.state.container.engine.begin() as connection:
                await connection.execute(text("DROP TABLE auth_session"))

        client.portal.call(break_auth_storage)
        headers = auth_headers()
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 503
        assert client.get("/api/v1/admin/tools", headers=headers).status_code == 503
        assert (
            client.post("/api/v1/chat", headers=headers, json={"message": "你好"}).status_code
            == 503
        )
