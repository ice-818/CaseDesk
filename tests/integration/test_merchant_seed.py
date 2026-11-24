"""Controlled bootstrap tests on disposable 0009 databases; no online writes."""

import pytest
from sqlalchemy import func, select

from app.config.settings import Settings
from app.repositories.db import create_engine, create_session_factory
from app.repositories.models import MerchantRow, OrderItemRow, OrderRow, ProductRow, UserRow
from app.repositories.order_repository import OrderRepository
from app.services.seed import (
    DEMO_MERCHANT_ID,
    ROLE_MERCHANTS,
    SECOND_MERCHANT_ID,
    SeedConflictError,
    merchant_comparison_rows,
    seed_demo_data,
)
from scripts import seed_merchants as entry
from scripts.seed_users import ROLE_MERCHANTS as ACCOUNT_ROLES, seed_users
from tests.auth_support import TEST_PASSWORD, prepare_auth_database
from tests.database_support import migrate_test_database
from tests.merchant_support import actor


@pytest.fixture
async def seeded_factory(tmp_path):
    path = tmp_path / "seed-merchants.db"
    prepare_auth_database(path)
    engine = create_engine(Settings(database_url=f"sqlite+aiosqlite:///{path}"))
    factory = create_session_factory(engine)
    async with factory.begin() as db:
        existing = await db.get(UserRow, "u_demo")
        db.add_all(
            [
                UserRow(
                    user_id="u_service_agent",
                    role="service_agent",
                    merchant_id=ROLE_MERCHANTS["service_agent"],
                    password_hash=existing.password_hash,
                ),
                UserRow(
                    user_id="u_operator", role="operator", password_hash=existing.password_hash
                ),
            ]
        )
    yield engine, factory
    await engine.dispose()


async def snapshot(factory):
    async with factory() as db:
        return {
            model.__tablename__: [
                dict(row) for row in (await db.execute(select(model.__table__))).mappings()
            ]
            for model in (MerchantRow, ProductRow, OrderRow, OrderItemRow, UserRow)
        }


@pytest.mark.asyncio
async def test_two_shop_counts_same_buyer_and_identical_search_adversary(seeded_factory):
    _, factory = seeded_factory
    assert ACCOUNT_ROLES is ROLE_MERCHANTS
    assert (await seed_users(factory, {}))["created"] == []
    report = await entry.seed_merchants(factory)
    assert report["conflicts"] == []
    expected = {
        DEMO_MERCHANT_ID: {
            "merchants": 1,
            "products": 4,
            "orders": 4,
            "order_items": 4,
            "refunds": 0,
            "returns": 0,
        },
        SECOND_MERCHANT_ID: {
            "merchants": 1,
            "products": 1,
            "orders": 1,
            "order_items": 1,
            "refunds": 0,
            "returns": 0,
        },
    }
    assert report["counts"] == expected
    pair = merchant_comparison_rows()[0]
    assert pair[0].sku != pair[1].sku
    assert (pair[0].name, pair[0].category, pair[0].price_cents, pair[0].tags) == (
        pair[1].name,
        pair[1].category,
        pair[1].price_cents,
        pair[1].tags,
    )
    async with factory() as db:
        for merchant in (DEMO_MERCHANT_ID, SECOND_MERCHANT_ID):
            with actor(merchant, merchant=merchant):
                results = await OrderRepository(db).find_matching_products(
                    query=pair[0].name,
                    category=pair[0].category,
                    max_price_cents=29900,
                    color=pair[0].tags.split(",")[0].split(":")[1],
                    size="42",
                    limit=1,
                )
                assert len(results) == 1 and results[0].merchant_id == merchant
        with actor("u_demo", "buyer", None):
            orders = await OrderRepository(db).list_orders_for_user("u_demo")
            assert len(orders) == 5 and {o.merchant_id for o in orders} == {
                DEMO_MERCHANT_ID,
                SECOND_MERCHANT_ID,
            }
            assert len(await OrderRepository(db).find_matching_products(query=pair[0].name)) == 2


@pytest.mark.asyncio
async def test_repeat_does_not_overwrite_prices_status_timestamps_bindings_or_passwords(
    seeded_factory,
):
    _, factory = seeded_factory
    await entry.seed_merchants(factory)
    async with factory.begin() as db:
        (await db.get(ProductRow, "SKU-HEADSET")).price_cents = 17
        (await db.get(ProductRow, "SKU-HEADSET")).inventory = 0
        (await db.get(OrderRow, "ORD10001")).status = "cancelled"
        (await db.get(MerchantRow, DEMO_MERCHANT_ID)).name = "Existing custom name"
        agent = await db.get(UserRow, "u_service_agent")
        agent.merchant_id = SECOND_MERCHANT_ID
        agent.status = "disabled"
    before = await snapshot(factory)
    report = await entry.seed_merchants(factory)
    assert report["created"] == [] and report["conflicts"] == []
    assert await snapshot(factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["product", "legacy-product", "order-merchant", "order-owner"])
async def test_identifier_conflict_is_reported_without_any_partial_writes(seeded_factory, kind):
    _, factory = seeded_factory
    products, orders, _ = merchant_comparison_rows()
    async with factory.begin() as db:
        if "product" in kind:
            row = products[0]
            if kind == "legacy-product":
                row.sku = "SKU-HEADSET"
            row.merchant_id = SECOND_MERCHANT_ID
        else:
            row = orders[0]
            if kind == "order-merchant":
                row.merchant_id = SECOND_MERCHANT_ID
            else:
                row.user_id = "other"
        db.add(row)
    before = await snapshot(factory)
    report = await entry.seed_merchants(factory)
    assert report["conflicts"] and report["created"] == []
    assert await snapshot(factory) == before
    if kind == "legacy-product":
        async with factory() as db:
            with pytest.raises(SeedConflictError):
                await seed_demo_data(db)
        assert await snapshot(factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["foreign-sku", "duplicate-line"])
async def test_existing_item_conflicts_are_not_repaired_or_duplicated(seeded_factory, kind):
    _, factory = seeded_factory
    await entry.seed_merchants(factory)
    async with factory.begin() as db:
        item = await db.scalar(select(OrderItemRow).where(OrderItemRow.order_id == "ORD-SHOE-DEMO"))
        if kind == "foreign-sku":
            item.sku = "SKU-SHOE-SECOND"
        else:
            db.add(
                OrderItemRow(
                    order_id=item.order_id,
                    sku=item.sku,
                    name=item.name,
                    quantity=1,
                    unit_price_cents=item.unit_price_cents,
                )
            )
    before = await snapshot(factory)
    report = await entry.seed_merchants(factory)
    assert any(value.startswith("order_items:") for value in report["conflicts"])
    assert report["created"] == [] and await snapshot(factory) == before


@pytest.mark.asyncio
async def test_missing_merchant_is_created_but_existing_merchant_is_preserved(seeded_factory):
    _, factory = seeded_factory
    async with factory.begin() as db:
        await db.delete(await db.get(MerchantRow, SECOND_MERCHANT_ID))
        row = await db.get(MerchantRow, DEMO_MERCHANT_ID)
        row.name = "Custom merchant"
        row.status = "disabled"
    report = await entry.seed_merchants(factory)
    assert "merchants:" + SECOND_MERCHANT_ID in report["created"]
    async with factory() as db:
        assert (await db.get(MerchantRow, DEMO_MERCHANT_ID)).status == "disabled"
        assert (await db.get(MerchantRow, DEMO_MERCHANT_ID)).name == "Custom merchant"
        assert await db.scalar(select(func.count()).select_from(MerchantRow)) == 2


@pytest.mark.asyncio
async def test_missing_buyer_reports_prerequisite_without_creating_accounts(tmp_path):
    engine = create_engine(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"))
    try:
        await migrate_test_database(engine)
        factory = create_session_factory(engine)
        before = await snapshot(factory)
        report = await entry.seed_merchants(factory)
        assert any(value.startswith("users:u_demo") for value in report["conflicts"])
        assert report["created"] == [] and await snapshot(factory) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cli_only_reports_business_identifiers_and_no_credentials(
    seeded_factory, monkeypatch, capsys
):
    engine, factory = seeded_factory
    monkeypatch.setattr(entry, "get_settings", lambda: Settings(database_url=str(engine.url)))
    await entry.main()
    output = capsys.readouterr().out
    assert "counts" in output and TEST_PASSWORD not in output and "password_hash" not in output
    async with factory.begin() as db:
        (await db.get(UserRow, "u_demo")).role = "operator"
    with pytest.raises(SystemExit) as error:
        await entry.main()
    assert error.value.code == 1
