"""After-sale status transitions and the return-to-refund closed loop."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.data_scope import bind_data_scope, scope_from_identity
from app.core.enums import AftersaleStatus, OrderStatus
from app.core.exceptions import ForbiddenError, InvalidRequestError, ResourceNotFoundError
from app.core.request_scope import request_session
from app.repositories.order_repository import OrderRepository
from app.schemas.auth import AuthIdentity
from app.services.seed import seed_demo_data
from app.tools.aftersale_tools import ProcessRefundTool, ProcessReturnTool
from app.tools.base import ToolContext
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
async def test_refund_full_flow_ends_refunded(session_factory) -> None:
    async with request_session(session_factory) as session:
        repo = OrderRepository(session)
        created = await repo.create_refund(order_id="ORD10001", reason="不想要了", trace_id="t0")
        approved = await repo.transition_refund(
            refund_id=created["refund_id"], target=AftersaleStatus.APPROVED, trace_id="t1"
        )
        assert approved["status"] == AftersaleStatus.APPROVED.value
        completed = await repo.transition_refund(
            refund_id=created["refund_id"], target=AftersaleStatus.COMPLETED, trace_id="t2"
        )
        assert completed["order_status"] == OrderStatus.REFUNDED.value
        assert (await repo.get_order("ORD10001")).status == OrderStatus.REFUNDED


@pytest.mark.asyncio
async def test_refund_rejection_restores_shipped_order(session_factory) -> None:
    async with request_session(session_factory) as session:
        repo = OrderRepository(session)
        created = await repo.create_refund(order_id="ORD10001", reason="x", trace_id="t0")
        result = await repo.transition_refund(
            refund_id=created["refund_id"], target=AftersaleStatus.REJECTED, trace_id="t1"
        )
        assert result["order_status"] == OrderStatus.SHIPPED.value


@pytest.mark.asyncio
async def test_requested_cannot_jump_to_completed(session_factory) -> None:
    async with request_session(session_factory) as session:
        repo = OrderRepository(session)
        created = await repo.create_refund(order_id="ORD10003", reason="x", trace_id="t0")
        with pytest.raises(InvalidRequestError):
            await repo.transition_refund(
                refund_id=created["refund_id"], target=AftersaleStatus.COMPLETED, trace_id="t1"
            )


@pytest.mark.asyncio
async def test_terminal_status_cannot_transition_again(session_factory) -> None:
    async with request_session(session_factory) as session:
        repo = OrderRepository(session)
        created = await repo.create_refund(order_id="ORD10003", reason="x", trace_id="t0")
        await repo.transition_refund(
            refund_id=created["refund_id"], target=AftersaleStatus.APPROVED, trace_id="t1"
        )
        await repo.transition_refund(
            refund_id=created["refund_id"], target=AftersaleStatus.COMPLETED, trace_id="t2"
        )
        with pytest.raises(InvalidRequestError):
            await repo.transition_refund(
                refund_id=created["refund_id"], target=AftersaleStatus.APPROVED, trace_id="t3"
            )


@pytest.mark.asyncio
async def test_return_completion_auto_creates_completed_refund(session_factory) -> None:
    async with request_session(session_factory) as session:
        repo = OrderRepository(session)
        created = await repo.create_return(order_id="ORD10003", reason="尺码不合适", trace_id="t0")
        await repo.transition_return(
            return_id=created["return_id"], target=AftersaleStatus.APPROVED, trace_id="t1"
        )
        result = await repo.transition_return(
            return_id=created["return_id"], target=AftersaleStatus.COMPLETED, trace_id="t2"
        )
        assert result["order_status"] == OrderStatus.REFUNDED.value
        refunds = await repo.get_refunds("ORD10003")
        assert any(r.status == AftersaleStatus.COMPLETED.value for r in refunds)
        assert (await repo.get_order("ORD10003")).status == OrderStatus.REFUNDED


@pytest.mark.parametrize("user_id,role,merchant_id", [
    ("op", "operator", None),
    ("agent", "service_agent", "m_demo"),
])
@pytest.mark.asyncio
async def test_process_refund_tool_runs_approved(
    session_factory, user_id, role, merchant_id
) -> None:
    with bind_data_scope(
        scope_from_identity(AuthIdentity(user_id, role, merchant_id=merchant_id))
    ):
        async with request_session(session_factory) as session:
            repo = OrderRepository(session)
            created = await repo.create_refund(order_id="ORD10003", reason="x", trace_id="t0")
            tool = ProcessRefundTool(repo)
            ctx = ToolContext(user_id=user_id, session_id="s", trace_id="t1")
            result = await tool.run(
                {"refund_id": created["refund_id"], "action": "approve"}, ctx
            )
            assert result.success
            assert result.output["status"] == AftersaleStatus.APPROVED.value


@pytest.mark.asyncio
async def test_process_return_tool_rejects_unknown_return(session_factory) -> None:
    with bind_data_scope(scope_from_identity(AuthIdentity("op", "operator"))):
        async with request_session(session_factory) as session:
            repo = OrderRepository(session)
            tool = ProcessReturnTool(repo)
            ctx = ToolContext(user_id="op", session_id="s", trace_id="t1")
            with pytest.raises(ResourceNotFoundError):
                await tool.run(
                    {"return_id": "rt_missing", "action": "approve"}, ctx
                )


@pytest.mark.regression
@pytest.mark.parametrize("tool_name,handle_attr", [
    ("process_refund", "refund_id"),
    ("process_return", "return_id"),
])
@pytest.mark.asyncio
async def test_buyer_cannot_process_aftersale(
    session_factory, tool_name, handle_attr
) -> None:
    # autouse trusted_buyer_scope is u_demo/buyer.
    async with request_session(session_factory) as session:
        repo = OrderRepository(session)
        if tool_name == "process_refund":
            created = await repo.create_refund(
                order_id="ORD10003", reason="x", trace_id="t0"
            )
        else:
            created = await repo.create_return(
                order_id="ORD10003", reason="x", trace_id="t0"
            )
        handle = created[handle_attr]
        tool = (
            ProcessRefundTool(repo)
            if tool_name == "process_refund"
            else ProcessReturnTool(repo)
        )
        ctx = ToolContext(user_id="u_demo", session_id="s", trace_id="t1")
        with pytest.raises(ForbiddenError):
            await tool.run({handle_attr: handle, "action": "approve"}, ctx)
