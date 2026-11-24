"""pytest bootstrap: never call real LLM/DB from developer .env."""

import os

import pytest

os.environ["APP_ENV"] = "testing"
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["CROSS_ENCODER_FORCE"] = "false"
os.environ["LOCAL_EMBEDDING_FORCE"] = "false"
os.environ["UNSTRUCTURED_FORCE"] = "false"
os.environ["EMBEDDING_API_KEY"] = ""
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./data/local/test.db"
os.environ["REDIS_URL"] = ""
os.environ["QDRANT_URL"] = ""
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["ADMIN_API_TOKEN"] = ""

from pathlib import Path

Path("data/local").mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def _auth_database(request, tmp_path, monkeypatch):
    if "e2e" not in request.node.path.parts:
        yield None
        return
    from sse_starlette.sse import AppStatus

    from tests.auth_support import prepare_auth_database, test_headers
    # Each TestClient owns a fresh event loop. The dependency's process-wide
    # exit event must not leak from a previous client's closed loop.
    monkeypatch.setattr(AppStatus, "should_exit_event", None)
    path = tmp_path / "authenticated.db"
    prepare_auth_database(path)
    tokens = test_headers(path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    from app.config.settings import clear_settings_cache
    clear_settings_cache()
    yield tokens
    clear_settings_cache()


@pytest.fixture
def auth_headers(_auth_database):
    def headers(user_id="u_demo"):
        return {"Authorization": "Bearer " + _auth_database[user_id]}
    return headers
