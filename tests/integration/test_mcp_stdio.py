"""Real subprocess/protocol tests, with no LLM or external business services."""

import asyncio
import sys

import pytest
from mcp import StdioServerParameters
from mcp.types import CallToolResult, TextContent

from app.core.data_scope import bind_data_scope, scope_from_identity
from app.core.exceptions import InvalidRequestError, ToolExecutionError
from app.mcp.client import StdioMCPClient, decode_result
from app.mcp.stdio_adapter import register_stdio_tools
from app.schemas.auth import AuthIdentity
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_discovery_and_registry_execution() -> None:
    client = StdioMCPClient()
    specs = await client.list_tools()
    assert [spec.name for spec in specs] == ["mcp_store_hours"]
    registry = ToolRegistry()
    await register_stdio_tools(registry, client)
    tool = registry.get("mcp_store_hours")
    schema = tool.openai_schema()["function"]["parameters"]
    assert schema == specs[0].inputSchema
    assert schema["properties"]["day"]["enum"] == ["all", "weekday", "weekend"]
    context = ToolContext(user_id="u", session_id="s", trace_id="t")
    result = await tool.run({"day": "weekend"}, context)
    assert result.success
    assert result.output["hours"] == "10:00-18:00"
    assert result.output["source"] == "mcp_stdio_demo"
    assert result.output["night_refund_review"] is False
    with pytest.raises(InvalidRequestError):
        await tool.run({"day": "someday"}, context)
    with pytest.raises(InvalidRequestError, match="duplicate MCP tool"):
        await register_stdio_tools(registry, client)


@pytest.mark.asyncio
async def test_default_call_and_server_errors() -> None:
    client = StdioMCPClient()
    assert (await client.call_tool("mcp_store_hours", {}))["weekday"] == "09:00-21:00"
    with pytest.raises(ToolExecutionError):
        await client.call_tool("missing_tool", {})
    with pytest.raises(ToolExecutionError):
        await client.call_tool("mcp_store_hours", {"day": "someday"})


@pytest.mark.asyncio
async def test_broken_server() -> None:
    client = StdioMCPClient(StdioServerParameters(command=sys.executable, args=["-c", "pass"]))
    with pytest.raises(ToolExecutionError):
        await client.list_tools()


@pytest.mark.asyncio
async def test_unresponsive_server_times_out() -> None:
    client = StdioMCPClient(
        StdioServerParameters(command=sys.executable, args=["-c", "import time; time.sleep(60)"]),
        timeout=0.5,
    )
    async with asyncio.timeout(10):
        with pytest.raises(ToolExecutionError):
            await client.list_tools()


def test_error_and_text_result_conversion() -> None:
    with pytest.raises(ToolExecutionError):
        decode_result(CallToolResult(isError=True, content=[]))
    assert decode_result(
        CallToolResult(content=[TextContent(type="text", text='{"hours":"10:00-18:00"}')])
    ) == {"hours": "10:00-18:00"}
    converted = decode_result(CallToolResult(content=[TextContent(type="text", text="hello")]))
    assert converted["content"][0]["text"] == "hello"


@pytest.fixture(autouse=True)
def trusted_buyer_scope():
    with bind_data_scope(scope_from_identity(AuthIdentity("u", "buyer"))):
        yield
