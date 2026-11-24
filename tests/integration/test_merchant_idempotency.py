import pytest

from app.core.exceptions import AppError, ForbiddenError
from app.repositories.idempotency import IdempotencyRepository
from tests.merchant_support import actor, database


@pytest.mark.asyncio
async def test_same_key_separates_shops_and_preserves_conflict_check(tmp_path):
    engine, factory = await database(tmp_path / "i.db")
    try:
        async with factory() as db:
            for merchant in ("m_demo", "m_second"):
                with actor(merchant, merchant=merchant):
                    repo = IdempotencyRepository(db)
                    assert await repo.get_if_matches("same", "fp") is None
                    await repo.put("same", "fp", {"shop": merchant})
                    await db.flush()
                    assert await repo.get_if_matches("same", "fp") == {"shop": merchant}
                    with pytest.raises(AppError) as error:
                        await repo.put("same", "changed", {})
                    assert error.value.http_status == 409
            with actor():
                old = IdempotencyRepository(db)
            with actor("b", merchant="m_second"):
                with pytest.raises(ForbiddenError):
                    await old.get_if_matches("same", "fp")
    finally:
        await engine.dispose()
