import pytest

from app.agents.product_requirements import ProductRequirements
from app.core.exceptions import ForbiddenError, ResourceNotFoundError
from app.repositories.shopping_checkpoint import ShoppingCheckpointRepository
from tests.merchant_support import actor, database


@pytest.mark.asyncio
async def test_checkpoint_parent_ownership_update_and_recovery(tmp_path):
    engine, factory = await database(tmp_path / "c.db")
    try:
        async with factory() as db:
            state = {
                "shopping_checkpoint_id": "cp",
                "session_id": "au_demo",
                "user_id": "forged",
                "product_requirements": ProductRequirements(source_query="shoe", active=True),
            }
            with actor():
                repo = ShoppingCheckpointRepository(db)
                await repo.save(state, status="interrupted")
                assert (await repo.owned("cp", "forged")).user_id == "u_demo"
                assert (await repo.latest("au_demo")).checkpoint_id == "cp"
                assert (
                    await repo.recover_requirements("cp", "forged", "au_demo")
                    == state["product_requirements"]
                )
            with actor("b", merchant="m_second"):
                repo = ShoppingCheckpointRepository(db)
                with pytest.raises(ResourceNotFoundError):
                    await repo.owned("cp")
                with pytest.raises(ForbiddenError):
                    await repo.save({**state, "session_id": "bu_demo"})
                with pytest.raises(ForbiddenError):
                    await repo.mark_interrupted("cp")
    finally:
        await engine.dispose()
