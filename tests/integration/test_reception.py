from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.data_scope import bind_data_scope, scope_from_identity
from app.core.exceptions import AppError
from app.repositories.models import MessageRow
from app.repositories.session_repository import SessionRepository
from app.schemas.auth import AuthIdentity
from app.schemas.reception import ReceptionFeedbackRequest
from tests.database_support import migrate_test_database


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await migrate_test_database(engine)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["resolved", "user_closed"])
async def test_close_is_durable_idempotent_and_preserves_history(db, reason):
    repo = SessionRepository(db)
    await repo.get_or_create("s", "u")
    await repo.ensure_active("s", "u", idle_seconds=1800)
    await repo.append_message("s", "user", "你好")
    first = await repo.close_reception("s", "u", reason, idle_seconds=1800)
    timestamp = first.closed_at
    await db.commit()
    second = await repo.close_reception("s", "u", "user_closed", idle_seconds=1800)
    assert second.status == "closed"
    assert second.end_reason == reason
    assert second.closed_at == timestamp
    assert await db.scalar(select(func.count()).select_from(MessageRow)) == 1
    with pytest.raises(AppError) as error:
        await repo.ensure_active("s", "u", idle_seconds=1800)
    assert error.value.http_status == 409


@pytest.mark.asyncio
async def test_timeout_boundary_and_status_polling_do_not_extend_reception(db):
    repo = SessionRepository(db)
    session = await repo.get_or_create("s", "u")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    session.updated_at = start
    row = await repo.reception("s", "u", idle_seconds=60, now=start + timedelta(seconds=59))
    assert row.status == "active"
    assert row.last_activity_at.replace(tzinfo=UTC) == start
    row = await repo.reception("s", "u", idle_seconds=60, now=start + timedelta(seconds=60))
    assert row.status == "closed"
    assert row.end_reason == "timeout"
    assert row.closed_at.replace(tzinfo=UTC) == start + timedelta(seconds=60)
    row = await repo.close_reception("s", "u", "resolved", idle_seconds=60)
    assert row.end_reason == "timeout"
    feedback = await repo.submit_feedback(
        "s",
        ReceptionFeedbackRequest(
            user_id="u",
            score=5,
            resolved=True,
        ),
        idle_seconds=60,
        user_id="u",
    )
    assert feedback.resolved is True
    assert row.end_reason == "timeout"
    assert row.status == "closed"


@pytest.mark.asyncio
async def test_activity_refreshes_deadline_and_expiry_survives_rejection(db):
    repo = SessionRepository(db)
    await repo.get_or_create("s", "u")
    row = await repo.reception("s", "u", idle_seconds=60)
    row.last_activity_at = datetime(2020, 1, 1, tzinfo=UTC)
    await repo.append_message("s", "user", "新消息")
    assert (await repo.reception("s", "u", idle_seconds=60)).status == "active"
    row.last_activity_at = datetime(2020, 1, 1, tzinfo=UTC)
    with pytest.raises(AppError):
        await repo.ensure_active("s", "u", idle_seconds=60)
    await db.rollback()
    assert (await repo.reception("s", "u", idle_seconds=60)).end_reason == "timeout"


@pytest.mark.asyncio
async def test_foreign_and_unknown_sessions_cannot_be_read_closed_or_reused(db):
    with bind_data_scope(scope_from_identity(AuthIdentity("owner", "buyer"))):
        await SessionRepository(db).get_or_create("s", "owner")
    with bind_data_scope(scope_from_identity(AuthIdentity("other", "buyer"))):
        repo = SessionRepository(db)
        for session_id in ("s", "missing"):
            with pytest.raises(AppError) as error:
                await repo.reception(session_id, "other", idle_seconds=60)
            assert error.value.http_status == 404
            with pytest.raises(AppError) as error:
                await repo.close_reception(session_id, "other", "resolved", idle_seconds=60)
            assert error.value.http_status == 403
        with pytest.raises(AppError):
            await repo.get_or_create("s", "other")


@pytest.fixture(autouse=True)
def trusted_buyer_scope():
    with bind_data_scope(scope_from_identity(AuthIdentity("u", "buyer"))):
        yield
