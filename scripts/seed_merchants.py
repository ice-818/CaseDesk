"""Explicit, idempotent two-shop bootstrap. Never called by online routes."""

import asyncio
import json

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.config.settings import get_settings
from app.repositories.db import create_engine, create_session_factory, init_schema
from app.repositories.models import (
    MerchantRow,
    OrderItemRow,
    OrderRow,
    ProductRow,
    RefundRow,
    ReturnRow,
    UserRow,
)
from app.services.seed import (
    MERCHANT_SEEDS,
    ROLE_MERCHANTS,
    demo_rows,
    insert_missing_demo_rows,
    merchant_comparison_rows,
    seed_conflicts,
)


async def merchant_counts(db):
    result = {}
    for merchant_id in MERCHANT_SEEDS:
        values = {
            "merchants": int(
                await db.scalar(
                    select(func.count())
                    .select_from(MerchantRow)
                    .where(MerchantRow.merchant_id == merchant_id)
                )
                or 0
            )
        }
        for label, model in (("products", ProductRow), ("orders", OrderRow)):
            values[label] = int(
                await db.scalar(
                    select(func.count()).select_from(model).where(model.merchant_id == merchant_id)
                )
                or 0
            )
        for label, model in (
            ("order_items", OrderItemRow),
            ("refunds", RefundRow),
            ("returns", ReturnRow),
        ):
            values[label] = int(
                await db.scalar(
                    select(func.count())
                    .select_from(model)
                    .join(OrderRow, model.order_id == OrderRow.order_id)
                    .where(OrderRow.merchant_id == merchant_id)
                )
                or 0
            )
        result[merchant_id] = values
    return result


async def seed_merchants(factory):
    """All-or-nothing preflight. Existing values, bindings and passwords are untouched."""
    await init_schema(factory.kw["bind"])
    try:
        async with factory() as db:
            if db.bind.dialect.name == "sqlite":
                await db.execute(text("BEGIN IMMEDIATE"))
            # Existing merchant rows serialize concurrent executions on PostgreSQL.
            merchants = {
                row.merchant_id: row
                for row in await db.scalars(
                    select(MerchantRow)
                    .where(MerchantRow.merchant_id.in_(MERCHANT_SEEDS))
                    .order_by(MerchantRow.merchant_id)
                    .with_for_update()
                )
            }
            classic = demo_rows()
            pair = merchant_comparison_rows()
            products, orders, items = [
                list(a) + list(b) for a, b in zip(classic, pair, strict=True)
            ]
            conflicts = await seed_conflicts(db, products, orders, items)
            buyer = await db.get(UserRow, "u_demo")
            if (
                buyer is None
                or buyer.role != "buyer"
                or buyer.merchant_id != ROLE_MERCHANTS["buyer"]
            ):
                conflicts.append(
                    "users:u_demo must already be a platform buyer; run the account seed explicitly"
                )
            if conflicts:
                return {
                    "created": [],
                    "skipped": [],
                    "conflicts": sorted(conflicts),
                    "counts": await merchant_counts(db),
                }
            created, skipped = [], []
            for merchant_id, name in MERCHANT_SEEDS.items():
                if merchant_id not in merchants:
                    db.add(MerchantRow(merchant_id=merchant_id, name=name))
                    created.append("merchants:" + merchant_id)
                else:
                    skipped.append("merchants:" + merchant_id)
            await db.flush()
            inserted, unchanged = await insert_missing_demo_rows(db, products, orders, items)
            counts = await merchant_counts(db)
            await db.commit()
            return {
                "created": created + inserted,
                "skipped": skipped + unchanged,
                "conflicts": [],
                "counts": counts,
            }
    except IntegrityError:
        # A racing bootstrap never turns into overwrite/upsert or a partial commit.
        return {
            "created": [],
            "skipped": [],
            "conflicts": ["concurrent seed collision; no writes committed"],
            "counts": {},
        }


async def main():
    engine = create_engine(get_settings())
    try:
        report = await seed_merchants(create_session_factory(engine))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report["conflicts"]:
            raise SystemExit(1)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
