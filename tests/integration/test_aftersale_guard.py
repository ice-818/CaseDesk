"""Business-level after-sale guards: duplicate, cross-table, status and amount."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.data_scope import bind_data_scope, scope_from_identity
from app.core.enums import AftersaleStatus, OrderStatus
from app.core.exceptions import InvalidRequestError
from app.core.request_scope import request_session
from app.core.time import utcnow
from app.repositories.models import OrderRow, RefundRow, ReturnRow
from app.repositories.order_repository import OrderRepository
from app.schemas.auth import AuthIdentity
from app.services.seed import seed_demo_data
from tests.database_support import migrate_test_database


@pytest.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    await migrate_test_database(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        await seed_demo_data(session)
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def trusted_buyer_scope():
    with bind_data_scope(scope_from_identity(AuthIdentity("u_demo", "buyer"))):
        yield


@pytest.mark.asyncio
async def test_duplicate_refund_is_rejected(session_factory) -> None:
    async with request_session(session_factory) as session:
        repo = OrderRepository(session)
        first = await repo.create_refund(order_id="ORD10003", reason="不想要了", trace_id="t1")
        assert first["status"] == AftersaleStatus.REQUESTED.value
        with pytest.raises(InvalidRequestError):
            await repo.create_refund(order_id="ORD10003", reason="再退一次", trace_id="t2")


@pytest.mark.asyncio
async def test_refund_blocked_when_return_active(session_factory) -> None:
    async with request_session(session_factory) as session:
        repo = OrderRepository(session)
        await repo.create_return(order_id="ORD10003", reason="尺码不合适", trace_id="t1")
        with pytest.raises(InvalidRequestError):
            await repo.create_refund(order_id="ORD10003", reason="想改退款", trace_id="t2")


@pytest.mark.asyncio
async def test_duplicate_return_is_rejected(session_factory) -> None:
    async with request_session(session_factory) as session:
        repo = OrderRepository(session)
        await repo.create_return(order_id="ORD10001", reason="退货", trace_id="t1")
        with pytest.raises(InvalidRequestError):
            await repo.create_return(order_id="ORD10001", reason="再退", trace_id="t2")


@pytest.mark.asyncio
async def test_return_blocked_when_refund_active(session_factory) -> None:
    async with request_session(session_factory) as session:
        repo = OrderRepository(session)
        await repo.create_refund(order_id="ORD10003", reason="退款", trace_id="t1")
        with pytest.raises(InvalidRequestError):
            await repo.create_return(order_id="ORD10003", reason="想改退货", trace_id="t2")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [OrderStatus.CANCELLED, OrderStatus.REFUNDED, OrderStatus.CREATED])
async def test_aftersale_rejected_for_illegal_order_status(session_factory, status) -> None:
    async with request_session(session_factory) as session:
        order = await session.get(OrderRow, "ORD10003")
        order.status = status.value
        await session.flush()
        repo = OrderRepository(session)
        with pytest.raises(InvalidRequestError):
            await repo.create_refund(order_id="ORD10003", reason="x", trace_id="t")


@pytest.mark.asyncio
async def test_completed_refund_blocks_new_refund_by_amount(session_factory) -> None:
    async with request_session(session_factory) as session:
        session.add(
            RefundRow(
                refund_id="rf_old",
                order_id="ORD10003",
                status=AftersaleStatus.COMPLETED.value,
                amount_cents=7900,
                created_at=utcnow(),
            )
        )
        await session.flush()
        repo = OrderRepository(session)
        with pytest.raises(InvalidRequestError):
            await repo.create_refund(order_id="ORD10003", reason="重复退", trace_id="t")


@pytest.mark.asyncio
async def test_terminal_document_releases_slot_for_new_request(session_factory) -> None:
    async with request_session(session_factory) as session:
        session.add(
            ReturnRow(
                return_id="rt_old",
                order_id="ORD10003",
                status=AftersaleStatus.REJECTED.value,
                created_at=utcnow(),
            )
        )
        await session.flush()
        repo = OrderRepository(session)
        result = await repo.create_refund(order_id="ORD10003", reason="重新申请", trace_id="t")
        assert result["order_id"] == "ORD10003"
