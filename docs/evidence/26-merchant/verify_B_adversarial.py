import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.data_scope import DataScope, bind_data_scope
from app.core.exceptions import ForbiddenError, ResourceNotFoundError, UnauthorizedError
from app.core.time import utcnow
from app.repositories.models import OrderRow, ProductRow
from app.repositories.order_repository import OrderRepository
from tests.database_support import migrate_test_database


async def main() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await migrate_test_database(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Two shops: same name/price, different SKU; one order each.
    async with factory() as s:
        s.add_all([
            ProductRow(sku="SKU-A", name="same", category="c", price_cents=100, inventory=5, tags="", description="", merchant_id="m_demo"),
            ProductRow(sku="SKU-B", name="same", category="c", price_cents=100, inventory=5, tags="", description="", merchant_id="m_second"),
            OrderRow(order_id="ORD-A", user_id="u_demo", status="paid", total_cents=100, created_at=utcnow(), merchant_id="m_demo"),
            OrderRow(order_id="ORD-B", user_id="u_demo", status="paid", total_cents=100, created_at=utcnow(), merchant_id="m_second"),
        ])
        await s.commit()

    agent = DataScope("u_service_agent", "service_agent", "shop", "m_demo")
    buyer = DataScope("u_demo", "buyer", "platform", None)

    # 1. Repository without a bound scope must be rejected.
    try:
        async with factory() as s:
            OrderRepository(s)
        print("FAIL: repo without scope accepted")
    except UnauthorizedError:
        print("OK: no-scope repository rejected")

    with bind_data_scope(agent):
        async with factory() as s:
            repo = OrderRepository(s)
            # 2. Direct write-path target in another shop -> 403
            try:
                await repo.get_visible_order("ORD-B", write=True)
                print("FAIL: cross-shop write target accepted")
            except ForbiddenError:
                print("OK: cross-shop write target blocked (403)")
            # 3. Bypass the tool layer entirely: call write method directly
            try:
                await repo.create_refund(order_id="ORD-B", reason="x", trace_id="t")
                print("FAIL: direct cross-shop refund accepted")
            except (ForbiddenError, ResourceNotFoundError):
                print("OK: direct create_refund cross-shop blocked")
            # 4. Read path hides existence; product visibility per shop
            print("other shop product hidden:", await repo.get_product("SKU-B") is None)
            print("own shop product visible:", await repo.get_product("SKU-A") is not None)
            _hidden = await repo.get_visible_order("ORD-B") if False else None
            try:
                await repo.get_visible_order("ORD-B")
                print("FAIL: cross-shop read visible")
            except ResourceNotFoundError:
                print("OK: cross-shop read hidden (404)")

    # 5. Buyer on platform scope still reads own orders across shops
    with bind_data_scope(buyer):
        async with factory() as s:
            repo = OrderRepository(s)
            print("buyer reads ORD-A:", (await repo.get_visible_order("ORD-A")).order_id)
            print("buyer reads ORD-B:", (await repo.get_visible_order("ORD-B")).order_id)

    await engine.dispose()
    print("DONE")


asyncio.run(main())
