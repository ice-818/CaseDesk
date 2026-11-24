import pytest
from pydantic import BaseModel, Field

from app.agents.nodes.executor import execute_with_fallback
from app.core.auth_context import bind_auth_checker
from app.core.data_scope import bind_data_scope, scope_from_identity
from app.core.enums import ToolRisk
from app.core.exceptions import ToolExecutionError
from app.schemas.agent import PlanStep, ToolResult
from app.schemas.auth import AuthIdentity
from app.tools.base import Tool, ToolContext
from app.tools.registry import ToolRegistry


class _Query(BaseModel):
    query: str = Field(default="")


class _FailingOrder(Tool):
    name = "get_order"
    description = "fail"
    risk = ToolRisk.READ
    input_model = _Query

    async def execute(self, parsed: BaseModel, context: ToolContext) -> ToolResult:
        raise ToolExecutionError("order lookup failed")


class _Knowledge(Tool):
    name = "search_knowledge"
    description = "kb"
    risk = ToolRisk.READ
    input_model = _Query

    async def execute(self, parsed: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(name=self.name, success=True, output={"chunks": [{"content": "政策"}]})


@pytest.mark.asyncio
async def test_read_tool_falls_back_to_knowledge() -> None:
    registry = ToolRegistry()
    registry.register(_FailingOrder())
    registry.register(_Knowledge())
    async def valid_identity():
        return AuthIdentity("u", "buyer")
    with bind_auth_checker(valid_identity):
        result = await execute_with_fallback(
            registry.get("get_order"),
            PlanStep(step_id=1, goal="order", tool_name="get_order", arguments={"query": "x"}),
            ToolContext(user_id="u", session_id="s", trace_id="t"),
            registry,
            "定制杯还能退吗",
        )
    assert result.success is True
    assert result.name == "search_knowledge"
    assert result.output["fallback_from"] == "get_order"


@pytest.mark.asyncio
async def test_revoked_identity_never_runs_fallback():
    from app.core.exceptions import UnauthorizedError
    checks = 0
    registry = ToolRegistry()
    registry.register(_FailingOrder())
    registry.register(_Knowledge())

    async def revoke_before_fallback():
        nonlocal checks
        checks += 1
        if checks > 1:
            raise UnauthorizedError()
        return AuthIdentity("u", "buyer")

    with bind_auth_checker(revoke_before_fallback), pytest.raises(UnauthorizedError):
        await execute_with_fallback(registry.get("get_order"),
            PlanStep(step_id=1, goal="order", tool_name="get_order", arguments={}),
            ToolContext(user_id="u", session_id="s", trace_id="t"), registry, "query")
    assert checks == 2


@pytest.fixture(autouse=True)
def trusted_buyer_scope():
    with bind_data_scope(scope_from_identity(AuthIdentity("u", "buyer"))):
        yield
