import pytest
from sqlalchemy import func, select

from app.agents.nodes.executor import executor_node
from app.config.settings import Settings
from app.core.auth_context import bind_auth_checker
from app.core.exceptions import ForbiddenError, ResourceNotFoundError
from app.core.request_scope import request_session
from app.repositories.models import MerchantRow, OrderRow, RefundRow, UserRow
from app.repositories.order_repository import OrderRepository
from app.schemas.agent import AgentPlan, PlanStep
from app.schemas.auth import AuthIdentity
from app.services.auth_service import AuthService
from app.tools.aftersale_tools import CreateRefundRequestTool
from app.tools.base import ToolContext
from app.tools.product_tools import GetProductDetailTool
from app.tools.registry import ToolRegistry
from tests.auth_support import TEST_PASSWORD
from tests.merchant_support import actor, database


@pytest.mark.asyncio
async def test_tool_spoofing_and_cached_write_still_reauthorizes_target(tmp_path):
    engine, factory = await database(tmp_path / "tool.db")
    identity = AuthIdentity("agent", "service_agent", merchant_id="m_demo")

    async def check():
        return identity

    try:
        with actor(), bind_auth_checker(check):
            async with request_session(factory) as db:
                tool = GetProductDetailTool(OrderRepository(db))
                context = ToolContext(user_id="agent", session_id="au_demo", trace_id="t")
                result = await tool.run(
                    {"sku": "a-shoe", "merchant_id": "m_second", "shop_id": "m_second"}, context
                )
                assert result.output["merchant_id"] == "m_demo"
                with pytest.raises(ResourceNotFoundError):
                    await tool.run({"sku": "b-shoe"}, context)
            registry = ToolRegistry()
            registry.register(CreateRefundRequestTool())
            state = {
                "user_id": "agent",
                "session_id": "au_demo",
                "trace_id": "t",
                "user_query": "refund",
                "confirm_action_id": "same",
                "plan": AgentPlan(
                    steps=[
                        PlanStep(
                            step_id=1,
                            goal="refund",
                            tool_name="create_refund_request",
                            arguments={"order_id": "au_demo", "reason": "r"},
                        )
                    ]
                ),
            }
            async with request_session(factory) as db:
                assert (await executor_node(state, registry=registry, settings=Settings()))[
                    "tool_results"
                ][0].success
                await db.commit()
            async with factory.begin() as db:
                (await db.get(OrderRow, "au_demo")).merchant_id = "m_second"
            async with request_session(factory):
                with pytest.raises(ForbiddenError):
                    await executor_node(state, registry=registry, settings=Settings())
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(RefundRow)) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_merchant_and_rebinding_invalidate_running_scope(tmp_path):
    engine, factory = await database(tmp_path / "identity.db")
    service = AuthService(factory, Settings())
    try:
        token = (await service.login("agent", TEST_PASSWORD)).access_token
        old = await service.authenticate(token)
        async with factory.begin() as db:
            (await db.get(UserRow, "agent")).merchant_id = "m_second"
        with pytest.raises(ForbiddenError):
            await service.revalidate(old)
        assert (await service.authenticate(token)).merchant_id == "m_second"
        async with factory.begin() as db:
            (await db.get(MerchantRow, "m_second")).status = "disabled"
        with pytest.raises(ForbiddenError):
            await service.authenticate(token)
    finally:
        await engine.dispose()
