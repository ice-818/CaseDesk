import pytest

from app.core.enums import OrderStatus
from app.core.exceptions import ForbiddenError, ResourceNotFoundError, UnauthorizedError
from app.repositories.order_repository import OrderRepository
from app.repositories.platform_repository import PlatformRepository
from tests.merchant_support import actor, database


@pytest.mark.asyncio
async def test_buyer_cross_shop_operator_and_agent_sql_filters_before_limit(tmp_path):
    engine, factory = await database(tmp_path / "r.db")
    try:
        async with factory() as db:
            with pytest.raises(UnauthorizedError):
                OrderRepository(db)
            for prefix, merchant in (("a", "m_demo"), ("b", "m_second")):
                with actor(prefix, merchant=merchant):
                    repo = OrderRepository(db)
                    assert [p.sku for p in await repo.find_matching_products(limit=1)] == [
                        prefix + "-shoe"
                    ]
                    assert [p.sku for p in await repo.search_products("shoe")] == [prefix + "-shoe"]
                    assert (
                        await repo.get_visible_order(prefix + "other", "forged")
                    ).user_id == "other"
                    with pytest.raises(ResourceNotFoundError):
                        await repo.get_visible_order(("b" if prefix == "a" else "a") + "u_demo")
                    with pytest.raises(ForbiddenError):
                        PlatformRepository(factory)
            with actor("u_demo", "buyer", None):
                repo = OrderRepository(db)
                assert len(await repo.find_matching_products()) == 2
                assert len(await repo.list_orders_for_user("u_demo")) == 2
                assert await repo.list_orders_for_user("other") == []
            with actor("op", "operator", None):
                assert len(await OrderRepository(db).list_orders_for_user("other")) == 2
                assert await PlatformRepository(factory).datasets() == {"datasets": []}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_every_order_write_rechecks_scope_without_tool_precheck(tmp_path):
    engine, factory = await database(tmp_path / "w.db")
    try:
        async with factory() as db:
            with actor():
                repo = OrderRepository(db)
                for work in [
                    repo.update_status("bu_demo", OrderStatus.CANCELLED),
                    repo.create_refund(order_id="bu_demo", reason="r", trace_id="t"),
                    repo.create_return(order_id="bu_demo", reason="r", trace_id="t"),
                    repo.cancel_order("bu_demo", trace_id="t"),
                    repo.write_audit("t", "bad", "bu_demo"),
                ]:
                    with pytest.raises(ForbiddenError):
                        await work
                assert (await repo.create_refund(order_id="au_demo", reason="r", trace_id="t"))[
                    "order_id"
                ] == "au_demo"
                assert (await repo.create_return(order_id="aother", reason="r", trace_id="t"))[
                    "order_id"
                ] == "aother"
                assert (await repo.cancel_order("aother", trace_id="t"))["status"] == "cancelled"
                assert len(await repo.get_refunds("au_demo")) == 1
                with pytest.raises(ResourceNotFoundError):
                    await repo.get_refunds("bu_demo")
                await db.commit()
    finally:
        await engine.dispose()
