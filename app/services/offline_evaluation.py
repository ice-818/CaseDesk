"""Execute frozen fixtures against actual code, without LLM/API calls or live business writes."""

import hashlib
import inspect
import json
import platform
import time
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from alembic.config import Config
from app.agents.product_requirements import update_requirements
from app.core.data_scope import bind_data_scope, scope_from_identity
from app.repositories.models import ProductRow
from app.repositories.order_repository import OrderRepository
from app.schemas.auth import AuthIdentity
from app.schemas.platform import DatasetCreate, SearchCase, UpdateCase
from eval.metrics import percentile


def execution_manifest(dataset: DatasetCreate, content_hash: str) -> dict:
    import sys

    from app.agents import product_requirements
    from app.repositories import order_repository
    from app.schemas import platform as dataset_schema
    from app.tools import product_tools

    code = "\n".join(
        inspect.getsource(module)
        for module in (
            product_requirements,
            order_repository,
            dataset_schema,
            product_tools,
            sys.modules[__name__],
        )
    )
    return {
        "adapter": dataset.task,
        "adapter_version": "offline-v1",
        "code_hash": hashlib.sha256(code.encode()).hexdigest(),
        "python": platform.python_version(),
        "dataset_hash": content_hash,
        "mode": "deterministic-fixtures",
        "external_api_calls": 0,
    }


async def evaluate(dataset: DatasetCreate) -> dict:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    rows: list[dict[str, Any]] = []
    try:
        def migrate_fixture(connection):
            root = Path(__file__).resolve().parents[2]
            config = Config(str(root / "alembic.ini"))
            config.set_main_option("script_location", str(root / "alembic"))
            config.attributes.update(connection=connection, configure_logger=False)
            command.upgrade(config, "head")
        async with engine.connect() as connection:
            await connection.run_sync(migrate_fixture)
        factory = async_sessionmaker(engine)
        for sample in dataset.samples:
            started = time.perf_counter()
            actual: Any
            expected: Any
            try:
                if isinstance(sample, SearchCase):
                    async with factory() as db:
                        for product in sample.input.catalog:
                            values = product.model_dump()
                            values["merchant_id"] = values.get("merchant_id") or "m_demo"
                            values["tags"] = ",".join(product.tags)
                            db.add(ProductRow(**values))
                        await db.flush()
                        # Only this function's private in-memory fixture database is
                        # used; this identity never grants access to business storage.
                        with bind_data_scope(scope_from_identity(AuthIdentity("offline-fixture", "operator"))):
                            products = await OrderRepository(db).find_matching_products(
                                query=sample.input.query,
                                category=sample.input.category,
                                **sample.input.filters.model_dump(),
                                limit=3,
                            )
                        actual = [product.sku for product in products]
                        expected = sample.expected
                        # No commit: each case's catalog is rolled back before the next case.
                else:
                    assert isinstance(sample, UpdateCase)
                    state = None
                    for turn in sample.input.turns:
                        state = update_requirements(turn, state)
                    assert state is not None
                    actual = {
                        "constraints": state.constraints,
                        "task_query": state.task_query,
                        "active": state.active,
                        "clarification_required": bool(state.clarification),
                        "evidence_tags": state.evidence_tags,
                    }
                    expected = sample.expected.model_dump(mode="json")
                    expected["constraints"] = sample.expected.constraints.model_dump(
                        exclude_none=True
                    )
                rows.append(
                    {
                        "case_id": sample.case_id,
                        "group_id": sample.group_id,
                        "passed": actual == expected,
                        "actual": actual,
                        "expected": expected,
                        "error": None,
                        "elapsed_ms": (time.perf_counter() - started) * 1000,
                    }
                )
            except Exception as exc:
                rows.append(
                    {
                        "case_id": sample.case_id,
                        "group_id": sample.group_id,
                        "passed": False,
                        "actual": None,
                        "expected": json.loads(sample.model_dump_json())["expected"],
                        "error": type(exc).__name__,
                        "elapsed_ms": (time.perf_counter() - started) * 1000,
                    }
                )
    finally:
        await engine.dispose()
    passed = sum(row["passed"] for row in rows)
    return {
        "summary": {
            "total": len(rows),
            "passed": passed,
            "failed": len(rows) - passed,
            "errors": sum(row["error"] is not None for row in rows),
            "pass_rate": passed / len(rows),
            "p95_ms": percentile([r["elapsed_ms"] for r in rows], 95),
        },
        "cases": rows,
    }
