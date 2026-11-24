from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.product_requirements import update_requirements
from app.core.data_scope import bind_data_scope, scope_from_identity
from app.core.exceptions import AppError
from app.core.time import utcnow
from app.repositories.session_repository import SessionRepository
from app.repositories.shopping_checkpoint import ShoppingCheckpointRepository
from app.schemas.auth import AuthIdentity
from tests.database_support import migrate_test_database


@pytest.mark.asyncio
async def test_running_checkpoint_requires_staleness_and_completed_cannot_resume():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await migrate_test_database(engine)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await SessionRepository(db).get_or_create("s", "u")
            requirements = update_requirements("推荐黑色42码鞋")
            repo = ShoppingCheckpointRepository(db)
            await repo.save(
                {
                    "shopping_checkpoint_id": "cp",
                    "session_id": "s",
                    "user_id": "u",
                    "product_requirements": requirements,
                }
            )
            await db.commit()
            with pytest.raises(AppError) as error:
                await repo.recover_requirements("cp", "u", "s")
            assert error.value.http_status == 409
            with bind_data_scope(scope_from_identity(AuthIdentity("other", "buyer"))), pytest.raises(AppError) as error:
                await ShoppingCheckpointRepository(db).owned("cp", "other")
            assert error.value.http_status == 404
            row = await repo.owned("cp", "u")
            row.updated_at = utcnow() - timedelta(seconds=61)
            await db.commit()
            assert (
                await repo.recover_requirements("cp", "u", "s")
            ).constraints == requirements.constraints
            with pytest.raises(AppError):
                await repo.recover_requirements("cp", "u", "different-session")
            row.status = "completed"
            await db.commit()
            with pytest.raises(AppError):
                await repo.recover_requirements("cp", "u", "s")
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def trusted_buyer_scope():
    with bind_data_scope(scope_from_identity(AuthIdentity("u", "buyer"))):
        yield
