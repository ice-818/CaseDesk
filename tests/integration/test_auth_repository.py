import asyncio

import pytest
from sqlalchemy import select

from app.config.settings import Settings
from app.core.exceptions import AppError, ForbiddenError, UnauthorizedError
from app.repositories.db import create_engine, create_session_factory
from app.repositories.models import AuthSessionRow
from app.schemas.auth import AuthIdentity
from app.services.auth_service import AuthService
from tests.auth_support import TEST_PASSWORD, prepare_auth_database


@pytest.mark.asyncio
async def test_two_instances_share_revocation_and_disable_all_sessions(tmp_path):
    path = tmp_path / "auth.db"
    prepare_auth_database(path)
    settings = Settings(database_url=f"sqlite+aiosqlite:///{path}")
    engines = [create_engine(settings), create_engine(settings)]
    first, second = [AuthService(create_session_factory(e), settings) for e in engines]
    try:
        one = await first.login("u_demo", TEST_PASSWORD)
        two = await first.login("u_demo", TEST_PASSWORD)
        identity = await second.authenticate(one.access_token)
        await first.logout(identity)
        with pytest.raises(UnauthorizedError):
            await second.authenticate(one.access_token)
        operator = await first.authenticate((await first.login("op", TEST_PASSWORD)).access_token)
        await first.disable_user("u_demo", operator)
        with pytest.raises(UnauthorizedError):
            await second.authenticate(two.access_token)
        with pytest.raises(UnauthorizedError):
            await second.login("u_demo", TEST_PASSWORD)
        with pytest.raises(ForbiddenError):
            await first.disable_user("op", operator)
        await first.disable_user("op2", operator)
        with pytest.raises(ForbiddenError):
            await first.disable_user(
                "op", AuthIdentity("system:admin-token", "operator", "admin_token")
            )
        async with first.factory() as db:
            rows = list((await db.scalars(select(AuthSessionRow))).all())
            assert rows and all(len(row.token_digest) == 64 for row in rows)
            assert one.access_token not in str([row.__dict__ for row in rows])
    finally:
        for engine in engines:
            await engine.dispose()


@pytest.mark.asyncio
async def test_storage_failure_denies_authentication(tmp_path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'missing.db'}")
    engine = create_engine(settings)
    auth = AuthService(create_session_factory(engine), settings)
    try:
        with pytest.raises(AppError) as error:
            await auth.authenticate("cds_" + "a" * 43)
        assert error.value.http_status == 503
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_disables_cannot_remove_both_operators(tmp_path):
    path = tmp_path / "operators.db"
    prepare_auth_database(path)
    settings = Settings(database_url=f"sqlite+aiosqlite:///{path}")
    engines = [create_engine(settings), create_engine(settings)]
    first, second = [AuthService(create_session_factory(engine), settings) for engine in engines]
    actor = AuthIdentity("system:admin-token", "operator", "admin_token")
    try:
        results = await asyncio.gather(
            first.disable_user("op", actor),
            second.disable_user("op2", actor),
            return_exceptions=True,
        )
        assert sum(value is None for value in results) == 1
        assert sum(isinstance(value, ForbiddenError) for value in results) == 1
    finally:
        for engine in engines:
            await engine.dispose()


@pytest.mark.asyncio
async def test_unexpected_session_query_failure_also_denies_access():
    def broken_factory():
        raise RuntimeError("session storage failed")

    auth = AuthService(broken_factory, Settings())
    with pytest.raises(AppError) as error:
        await auth.authenticate("cds_" + "a" * 43)
    assert error.value.http_status == 503
