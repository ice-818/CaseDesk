import asyncio
from datetime import timedelta

import pytest

from app.config.settings import Settings
from app.core.exceptions import InvalidRequestError, UnauthorizedError
from app.repositories.db import create_engine, create_session_factory
from app.services.auth_service import AuthService, hash_password, password_bytes
from tests.auth_support import TEST_PASSWORD, prepare_auth_database


@pytest.mark.asyncio
async def test_login_logout_expiry_and_unknown_user(tmp_path):
    path = tmp_path / "auth.db"
    prepare_auth_database(path)
    settings = Settings(database_url=f"sqlite+aiosqlite:///{path}")
    engine = create_engine(settings)
    auth = AuthService(create_session_factory(engine), settings)
    try:
        response = await auth.login("u_demo", TEST_PASSWORD)
        identity = await auth.authenticate(response.access_token)
        assert identity.user_id == "u_demo"
        for uid, password in [("u_demo", "wrong"), ("missing", TEST_PASSWORD)]:
            with pytest.raises(UnauthorizedError):
                await auth.login(uid, password)
        auth.now = lambda: response.expires_at
        with pytest.raises(UnauthorizedError):
            await auth.authenticate(response.access_token)
        auth.now = lambda: response.expires_at - timedelta(seconds=1)
        await auth.logout(identity)
        with pytest.raises(UnauthorizedError):
            await auth.authenticate(response.access_token)
        with pytest.raises(UnauthorizedError):
            await auth.authenticate("forged")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_hashing_is_salted_cost_12_and_threaded(monkeypatch):
    import threading

    import bcrypt

    main_thread = threading.get_ident()
    original = bcrypt.hashpw
    threads = []

    def record(*args):
        threads.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(bcrypt, "hashpw", record)
    first, second = await asyncio.gather(
        hash_password("valid password"), hash_password("valid password")
    )
    assert first != second and first.startswith("$2b$12$")
    assert all(thread != main_thread for thread in threads)
    assert password_bytes("a" * 72) == b"a" * 72
    with pytest.raises(InvalidRequestError):
        password_bytes("中" * 25)
