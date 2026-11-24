"""Regression guard for sse_starlette's process-wide AppStatus event.

The library keeps ``AppStatus.should_exit_event`` at class scope. It binds to
the event loop of the first SSE response, while each ``TestClient`` owns a
fresh loop. Reusing the stale event raises
``RuntimeError: ... bound to a different event loop`` and turns a request that
should be a clean 401/200 into an error (sse-starlette issue #59).
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from sse_starlette.sse import AppStatus

pytestmark = pytest.mark.regression


def _stream_answer(client, headers):
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": "你好", "user_id": "u_demo"},
        headers=headers,
    ) as response:
        assert response.status_code == 200
        return [line for line in response.iter_lines() if line]


async def _wait_stale_event():
    await AppStatus.should_exit_event.wait()


def test_sse_appstatus_event_does_not_leak_across_event_loops(auth_headers):
    headers = auth_headers()

    # A real SSE stream creates the process-wide event bound to this client's loop.
    with TestClient(create_app()) as first:
        lines = _stream_answer(first, headers)
    assert AppStatus.should_exit_event is not None
    assert any("answer" in line for line in lines)

    # The stale event cannot be awaited on a fresh event loop.
    fresh_loop = asyncio.new_event_loop()
    try:
        with pytest.raises(RuntimeError, match="bound to a different event loop"):
            fresh_loop.run_until_complete(_wait_stale_event())
    finally:
        fresh_loop.close()

    # After resetting, the next client's loop builds a new event and streams fine.
    AppStatus.should_exit_event = None
    with TestClient(create_app()) as clean:
        lines = _stream_answer(clean, headers)
    assert any("answer" in line for line in lines)
