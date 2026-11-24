"""Idempotent, validated synthetic catalog import. Never called by online routes.

The source file uses integer yuan (price_yuan); this script converts it to
price_cents. Every validation failure aborts the whole batch before any write.
"""

import argparse
import asyncio
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError

from app.config.settings import get_settings
from app.repositories.db import create_engine, create_session_factory, init_schema
from app.repositories.models import ProductRow

DEFAULT_SOURCE = Path("data/local/catalog_seed.json")
MANIFEST_PATH = Path("docs/evidence/27-guidance/catalog_seed_manifest.json")

REQUIRED_FIELDS = {
    "merchant_id",
    "sku",
    "name",
    "category",
    "price_yuan",
    "inventory",
    "tags",
    "description",
}
VALID_MERCHANTS = {"m_demo", "m_second"}
VALID_CATEGORIES = {"数码", "服饰", "家居"}
RESERVED_SKUS = {
    "SKU-HEADSET",
    "SKU-MUG-CUSTOM",
    "SKU-TEE",
    "SKU-SHOE-DEMO",
    "SKU-SHOE-SECOND",
}
PRICE_RANGE = {"数码": (99, 899), "服饰": (59, 499), "家居": (29, 299)}
EXPECTED_COUNTS = {"m_demo": 8, "m_second": 11}
EXPECTED_ZERO = {"m_demo": 1, "m_second": 2}


def is_nonneg_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def tag_tokens(tags: str) -> list[str]:
    return [token for token in tags.split(",") if token]


def validate_payload(rows: object) -> list[str]:
    """Return a list of human-readable errors; empty list means the file is valid."""
    if not isinstance(rows, list) or not rows:
        return ["payload must be a non-empty JSON array"]

    errors: list[str] = []
    seen_skus: set[str] = set()
    counts: dict[str, int] = defaultdict(int)
    zero_inventory: dict[str, int] = defaultdict(int)
    below_100: dict[str, int] = defaultdict(int)
    above_300: dict[str, int] = defaultdict(int)

    for index, row in enumerate(rows):
        prefix = f"row[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{prefix}: must be an object")
            continue

        missing = REQUIRED_FIELDS - set(row)
        if missing:
            errors.append(f"{prefix}: missing fields {sorted(missing)}")
            continue

        merchant = row["merchant_id"]
        category = row["category"]
        if not isinstance(merchant, str) or merchant not in VALID_MERCHANTS:
            errors.append(f"{prefix}: merchant_id must be one of {sorted(VALID_MERCHANTS)}")
        if not isinstance(category, str) or category not in VALID_CATEGORIES:
            errors.append(f"{prefix}: category must be one of {sorted(VALID_CATEGORIES)}")

        sku = row["sku"]
        if not isinstance(sku, str) or not sku or len(sku) > 32:
            errors.append(f"{prefix}: sku must be a non-empty string of at most 32 chars")
        elif sku in RESERVED_SKUS:
            errors.append(f"{prefix}: sku {sku} is reserved by the base seed")
        elif sku in seen_skus:
            errors.append(f"{prefix}: duplicate sku {sku} inside the file")
        else:
            seen_skus.add(sku)

        name = row["name"]
        if not isinstance(name, str) or not name or len(name) > 128:
            errors.append(f"{prefix}: name must be a non-empty string of at most 128 chars")

        if not is_nonneg_int(row["price_yuan"]):
            errors.append(f"{prefix}: price_yuan must be a non-negative integer")
        else:
            price = row["price_yuan"]
            if category in PRICE_RANGE:
                low, high = PRICE_RANGE[category]
                if not low <= price <= high:
                    errors.append(
                        f"{prefix}: price_yuan {price} is outside the {category} range {low}-{high}"
                    )
            if price < 100:
                below_100[merchant] += 1
            if price > 300:
                above_300[merchant] += 1

        if not is_nonneg_int(row["inventory"]):
            errors.append(f"{prefix}: inventory must be a non-negative integer")
        elif row["inventory"] == 0:
            zero_inventory[merchant] += 1

        tags = row["tags"]
        if not isinstance(tags, str) or not tags or len(tags) > 256:
            errors.append(f"{prefix}: tags must be a non-empty string of at most 256 chars")
        else:
            tokens = tag_tokens(tags)
            if any(any(char.isspace() for char in token) for token in tokens):
                errors.append(f"{prefix}: tags must not contain whitespace")
            if len(tokens) != len(set(tokens)):
                errors.append(f"{prefix}: duplicate tag token")
            color_values = [
                token.split(":", 1)[1] for token in tokens if token.startswith("color:")
            ]
            size_values = [
                token.split(":", 1)[1] for token in tokens if token.startswith("size:")
            ]
            if category == "服饰":
                if not color_values or any(not value for value in color_values):
                    errors.append(f"{prefix}: apparel requires a non-empty color: tag")
                if not size_values or any(not value for value in size_values):
                    errors.append(f"{prefix}: apparel requires a non-empty size: tag")
            if "color:紫" in tokens and "size:44" in tokens:
                errors.append(
                    f"{prefix}: forbidden purple/size44 combination reserved for the no-stock test"
                )

        if not isinstance(row["description"], str):
            errors.append(f"{prefix}: description must be a string")

        if merchant in VALID_MERCHANTS:
            counts[merchant] += 1

    for merchant, expected in EXPECTED_COUNTS.items():
        if counts[merchant] != expected:
            errors.append(
                f"merchant {merchant}: expected {expected} rows, found {counts[merchant]}"
            )
    for merchant, expected in EXPECTED_ZERO.items():
        if zero_inventory[merchant] != expected:
            errors.append(
                f"merchant {merchant}: expected {expected} zero-inventory rows, "
                f"found {zero_inventory[merchant]}"
            )
    for merchant in VALID_MERCHANTS:
        if below_100[merchant] < 1:
            errors.append(f"merchant {merchant}: needs at least one product below 100 yuan")
        if above_300[merchant] < 1:
            errors.append(f"merchant {merchant}: needs at least one product above 300 yuan")

    return errors


async def import_catalog(factory, rows: list[dict]) -> dict:
    await init_schema(factory.kw["bind"])
    try:
        async with factory() as db:
            existing = set((await db.scalars(select(ProductRow.sku))).all())
            created: list[str] = []
            skipped: list[str] = []
            for row in rows:
                sku = row["sku"]
                if sku in existing:
                    skipped.append("products:" + sku)
                    continue
                db.add(
                    ProductRow(
                        merchant_id=row["merchant_id"],
                        sku=sku,
                        name=row["name"],
                        category=row["category"],
                        price_cents=row["price_yuan"] * 100,
                        inventory=row["inventory"],
                        tags=row["tags"],
                        description=row["description"],
                    )
                )
                created.append("products:" + sku)
            await db.commit()

        async with factory() as query:
            product_counts = {
                merchant: await query.scalar(
                    select(func.count())
                    .select_from(ProductRow)
                    .where(ProductRow.merchant_id == merchant)
                )
                for merchant in sorted(VALID_MERCHANTS)
            }
        return {
            "created": created,
            "skipped": skipped,
            "conflicts": [],
            "counts": product_counts,
        }
    except IntegrityError:
        # A racing insert never becomes a partial commit; retry the idempotent run.
        return {
            "created": [],
            "skipped": [],
            "conflicts": ["integrity collision; no writes committed"],
            "counts": {},
        }


def write_manifest(source: Path, rows: list[dict], report: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    managed_skus = [row["sku"] for row in rows]
    in_db = set(report["created"]) | set(report["skipped"])
    manifest = {
        "source_file": str(source),
        "updated_at": datetime.now(UTC).isoformat(),
        "provenance": (
            "synthetic data generated by an external model, validated and imported "
            "by scripts/seed_catalog.py; not a real business record"
        ),
        "managed_skus": managed_skus,
        "present_in_db": sorted(sku for sku in in_db if sku.startswith("products:")),
        "last_run_created": report["created"],
        "last_run_skipped": report["skipped"],
        "counts": report["counts"],
        "conflicts": report["conflicts"],
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and import synthetic products.")
    parser.add_argument("source", nargs="?", default=str(DEFAULT_SOURCE))
    args = parser.parse_args()
    source = Path(args.source)

    rows = json.loads(source.read_text(encoding="utf-8"))
    errors = validate_payload(rows)
    if errors:
        print(json.dumps({"conflicts": errors}, ensure_ascii=False, indent=2))
        raise SystemExit(1)

    settings = get_settings()
    engine = create_engine(settings)
    if settings.use_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def enable_busy_timeout(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.close()

    factory = create_session_factory(engine)
    try:
        report = await import_catalog(factory, rows)
        if not report["conflicts"]:
            write_manifest(source, rows, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report["conflicts"]:
            raise SystemExit(1)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
