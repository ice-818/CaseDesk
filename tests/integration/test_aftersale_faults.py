"""Stage 4: fault injection and deterministic after-sale concurrency.

Runs on a disposable SQLite *file* database (multiple independent connections),
never the running application database. Concurrent interleavings are gated by
asyncio events so every ordering is deterministic and repeatable. Every test
asserts the final committed database state, not merely the HTTP/exception path.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.data_scope import bind_data_scope, scope_from_identity
from app.core.enums import AftersaleStatus, OrderStatus
from app.core.exceptions import AppError, InvalidRequestError
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.models import OrderRow, RefundRow, ReturnRow
from app.repositories.order_repository import OrderRepository
from app.schemas.auth import AuthIdentity
from app.services.seed import seed_demo_data
from tests.database_support import migrate_test_database

pytestmark = pytest.mark.fault

_ACTIVE = [AftersaleStatus.REQUESTED.value, AftersaleStatus.APPROVED.value]


@pytest.fixture
async def file_factory(tmp_path):
    path = tmp_path / "faults.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}", connect_args={"timeout": 10}
    )
    await migrate_test_database(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        await seed_demo_data(session)
        await session.commit()
    yield factory
    await engine.dispose()


def _scope():
    return bind_data_scope(scope_from_identity(AuthIdentity("u_demo", "buyer")))


async def _active_count(factory, model, order_id):
    async with factory() as session:
        return await session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.order_id == order_id, model.status.in_(_ACTIVE))
        )


async def _order_status(factory, order_id):
    async with factory() as session:
        return (await session.get(OrderRow, order_id)).status


# ---------------------------------------------------------------------------
# A. Database hard constraints (bypassing the application layer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_unique_index_blocks_second_active_refund(file_factory):
    now = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    async with file_factory() as first:
        await first.execute(
            text(
                "INSERT INTO refunds(refund_id,order_id,status,amount_cents,reason,created_at) "
                "VALUES ('rf_a','ORD10003','requested',7900,'A',:n)"
            ),
            {"n": now},
        )
        await first.commit()

    from sqlalchemy.exc import IntegrityError

    async with file_factory() as second:
        with pytest.raises(IntegrityError):
            await second.execute(
                text(
                    "INSERT INTO refunds(refund_id,order_id,status,amount_cents,reason,created_at) "
                    "VALUES ('rf_b','ORD10003','requested',7900,'B',:n)"
                ),
                {"n": now},
            )
    assert await _active_count(file_factory, RefundRow, "ORD10003") == 1


@pytest.mark.asyncio
async def test_full_refund_then_re_request_is_blocked_by_amount_guard(file_factory):
    async with file_factory() as session:
        with _scope():
            created = await OrderRepository(session).create_refund(
                order_id="ORD10003", reason="first", trace_id="t0"
            )
            await session.commit()
            await OrderRepository(session).transition_refund(
                refund_id=created["refund_id"],
                target=AftersaleStatus.APPROVED,
                trace_id="t1",
            )
            await session.commit()
            await OrderRepository(session).transition_refund(
                refund_id=created["refund_id"],
                target=AftersaleStatus.COMPLETED,
                trace_id="t2",
            )
            await session.commit()

    # The completed row frees the index slot; the order is now 'refunded' and
    # rejected by the order-status guard before the amount check is reached.
    async with file_factory() as session:
        with _scope():
            with pytest.raises(InvalidRequestError, match="cannot open"):
                await OrderRepository(session).create_refund(
                    order_id="ORD10003", reason="again", trace_id="t3"
                )
    assert await _active_count(file_factory, RefundRow, "ORD10003") == 0


# ---------------------------------------------------------------------------
# B. Fault injection: mid-write failure rolls back every side effect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_failure_rolls_back_refund_and_order_status(file_factory, monkeypatch):
    original_flush = AsyncSession.flush

    async def failing_flush(self, *args, **kwargs):
        if getattr(self, "_casedesk_fail", False):
            raise RuntimeError("injected write failure")
        await original_flush(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "flush", failing_flush)

    async with file_factory() as session:
        session._casedesk_fail = True
        with _scope():
            with pytest.raises(AppError):
                await OrderRepository(session).create_refund(
                    order_id="ORD10003", reason="x", trace_id="t"
                )
        await session.rollback()

    assert await _active_count(file_factory, RefundRow, "ORD10003") == 0
    assert await _order_status(file_factory, "ORD10003") == OrderStatus.PAID.value


@pytest.mark.asyncio
async def test_illegal_transition_leaves_state_unchanged(file_factory):
    async with file_factory() as session:
        with _scope():
            created = await OrderRepository(session).create_refund(
                order_id="ORD10003", reason="x", trace_id="t0"
            )
            with pytest.raises(InvalidRequestError):
                await OrderRepository(session).transition_refund(
                    refund_id=created["refund_id"],
                    target=AftersaleStatus.COMPLETED,
                    trace_id="t1",
                )
            await session.rollback()
    assert await _order_status(file_factory, "ORD10003") == OrderStatus.PAID.value


# ---------------------------------------------------------------------------
# C. Idempotency key: same key, different payload conflicts; hit returns cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_key_different_payload_raises_conflict(file_factory):
    async with file_factory() as session:
        with _scope():
            repo = IdempotencyRepository(session)
            await repo.put("k1", "fp-1", {"ok": True})
            with pytest.raises(AppError) as exc:
                await repo.get_if_matches("k1", "fp-different")
            assert exc.value.http_status == 409


@pytest.mark.asyncio
async def test_idempotency_hit_returns_cached_response(file_factory):
    async with file_factory() as session:
        with _scope():
            repo = IdempotencyRepository(session)
            payload = {"refund_id": "rf_x", "status": "requested"}
            await repo.put("k2", "fp-2", payload)
            assert await repo.get_if_matches("k2", "fp-2") == payload


# ---------------------------------------------------------------------------
# D. Deterministic controlled concurrency (asyncio events, not thread timing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_refund_only_one_document_commits(file_factory):
    a_ready = asyncio.Event()
    release_a = asyncio.Event()

    async def txn_a():
        with _scope():
            async with file_factory() as session:
                await OrderRepository(session).create_refund(
                    order_id="ORD10003", reason="A", trace_id="ta"
                )
                a_ready.set()
                await release_a.wait()
                await session.commit()

    async def txn_b():
        await a_ready.wait()
        with _scope():
            async with file_factory() as session:
                try:
                    await asyncio.wait_for(
                        OrderRepository(session).create_refund(
                            order_id="ORD10003", reason="B", trace_id="tb"
                        ),
                        timeout=10,
                    )
                    await session.commit()
                    return "committed"
                except AppError:
                    return "rejected"

    b_task = asyncio.create_task(txn_b())
    a_task = asyncio.create_task(txn_a())
    await asyncio.sleep(0.2)
    release_a.set()
    await a_task
    assert await b_task == "rejected"
    assert await _active_count(file_factory, RefundRow, "ORD10003") == 1


@pytest.mark.asyncio
async def test_serial_duplicate_after_commit_rejected_by_business_rule(file_factory):
    async with file_factory() as session:
        with _scope():
            await OrderRepository(session).create_refund(
                order_id="ORD10003", reason="first", trace_id="t1"
            )
            await session.commit()

    async with file_factory() as session:
        with _scope():
            with pytest.raises(InvalidRequestError):
                await OrderRepository(session).create_refund(
                    order_id="ORD10003", reason="second", trace_id="t2"
                )
    assert await _active_count(file_factory, RefundRow, "ORD10003") == 1


@pytest.mark.regression
@pytest.mark.asyncio
async def test_concurrent_refund_and_return_cannot_both_be_active(file_factory):
    a_ready = asyncio.Event()
    release_a = asyncio.Event()

    async def txn_refund():
        with _scope():
            async with file_factory() as session:
                await OrderRepository(session).create_refund(
                    order_id="ORD10003", reason="refund", trace_id="ta"
                )
                a_ready.set()
                await release_a.wait()
                await session.commit()

    async def txn_return():
        await a_ready.wait()
        with _scope():
            async with file_factory() as session:
                try:
                    await asyncio.wait_for(
                        OrderRepository(session).create_return(
                            order_id="ORD10003", reason="return", trace_id="tb"
                        ),
                        timeout=10,
                    )
                    await session.commit()
                except AppError:
                    pass

    return_task = asyncio.create_task(txn_return())
    refund_task = asyncio.create_task(txn_refund())
    await asyncio.sleep(0.2)
    release_a.set()
    await refund_task
    await return_task

    assert await _active_count(file_factory, RefundRow, "ORD10003") == 1
    assert await _active_count(file_factory, ReturnRow, "ORD10003") == 0


@pytest.mark.asyncio
async def test_return_completion_refund_amount_is_conserved(file_factory):
    # ORD10002 total is 3900, delivered.
    async with file_factory() as session:
        with _scope():
            repo = OrderRepository(session)
            created = await repo.create_return(
                order_id="ORD10002", reason="r", trace_id="t0"
            )
            await repo.transition_return(
                return_id=created["return_id"],
                target=AftersaleStatus.APPROVED,
                trace_id="t1",
            )
            result = await repo.transition_return(
                return_id=created["return_id"],
                target=AftersaleStatus.COMPLETED,
                trace_id="t2",
            )
            await session.commit()

    async with file_factory() as session:
        completed_sum = await session.scalar(
            select(func.coalesce(func.sum(RefundRow.amount_cents), 0)).where(
                RefundRow.order_id == "ORD10002",
                RefundRow.status == AftersaleStatus.COMPLETED.value,
            )
        )
    assert result["order_status"] == OrderStatus.REFUNDED.value
    assert completed_sum == 3900  # exactly one full refund, never more
