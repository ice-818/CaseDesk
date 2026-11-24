import pytest

from app.core.data_scope import bind_data_scope, scope_from_identity
from app.mcp.adapter import merge_mcp_tools
from app.mcp.runtime import LOCAL_CATALOG, call_mcp_tool
from app.schemas.auth import AuthIdentity
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_local_mcp_executes() -> None:
    output = await call_mcp_tool(
        name="mcp_store_hours",
        arguments={},
        transport="local",
    )
    assert output["weekday"] == "09:00-21:00"
    assert output["source"] == "mcp_local"


@pytest.mark.asyncio
async def test_mcp_proxy_is_function_calling_tool() -> None:
    registry = ToolRegistry()
    merge_mcp_tools(registry, list(LOCAL_CATALOG))
    tool = registry.get("mcp_warehouse_cutoff")
    schema = tool.openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "mcp_warehouse_cutoff"
    result = await tool.run({}, ToolContext(user_id="u", session_id="s", trace_id="t"))
    assert result.success is True
    assert result.output["cutoff"] == "16:00"


@pytest.fixture(autouse=True)
def trusted_buyer_scope():
    with bind_data_scope(scope_from_identity(AuthIdentity("u", "buyer"))):
        yield
