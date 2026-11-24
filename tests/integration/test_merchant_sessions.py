import pytest

from app.agents.product_requirements import ProductRequirements
from app.core.exceptions import ForbiddenError, ResourceNotFoundError
from app.repositories.session_repository import SessionRepository
from app.schemas.reception import ReceptionFeedbackRequest
from tests.merchant_support import actor, database


@pytest.mark.asyncio
async def test_all_session_children_and_shop_owner_are_enforced(tmp_path):
    engine, factory = await database(tmp_path / "s.db")
    try:
        async with factory() as db:
            with actor():
                repo = SessionRepository(db)
                new = await repo.get_or_create("new", "forged")
                assert (new.user_id, new.channel, new.merchant_id) == ("agent", "shop", "m_demo")
                assert (await repo.get_or_create("aother", "forged")).user_id == "other"
                assert (await repo.recent_messages("aother"))[0].content == "private"
                req = ProductRequirements(source_query="shoe", active=True)
                await repo.save_shopping_task("aother", req)
                assert await repo.shopping_task("aother") is not None
                await repo.save_summary("aother", "ok")
                await repo.append_message("aother", "assistant", "ok")
                await repo.close_reception("aother", "forged", "resolved", idle_seconds=1800)
                assert (
                    await repo.submit_feedback(
                        "aother",
                        ReceptionFeedbackRequest(score=5, resolved=True),
                        user_id="forged",
                        idle_seconds=1800,
                    )
                ).score == 5
                for sid in ("platform", "bu_demo"):
                    for work in [
                        repo.recent_messages(sid),
                        repo.shopping_task(sid),
                        repo.feedback(sid),
                        repo.reception(sid, "u_demo", idle_seconds=1800),
                    ]:
                        with pytest.raises(ResourceNotFoundError):
                            await work
                    for work in [
                        repo.get_or_create(sid, "forged"),
                        repo.ensure_active(sid, "x", idle_seconds=1800),
                        repo.append_message(sid, "assistant", "bad"),
                        repo.save_summary(sid, "bad"),
                        repo.save_shopping_task(sid, req),
                        repo.close_reception(sid, "x", "resolved", idle_seconds=1800),
                        repo.submit_feedback(
                            sid,
                            ReceptionFeedbackRequest(score=5, resolved=True),
                            user_id="x",
                            idle_seconds=1800,
                        ),
                    ]:
                        with pytest.raises(ForbiddenError):
                            await work
    finally:
        await engine.dispose()
