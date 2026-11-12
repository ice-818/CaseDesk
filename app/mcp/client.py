"""Official MCP v1 SDK client; each operation owns and closes its subprocess."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, Tool

from app.core.exceptions import ToolExecutionError


class StdioMCPClient:
    """Launch only the bundled demo server, with the current Python interpreter.

    Short-lived sessions avoid sharing SDK task/cancellation scopes between
    FastAPI lifespan and request tasks. A connection pool is deliberately absent.
    """

    def __init__(
        self, params: StdioServerParameters | None = None, *, timeout: float = 15.0
    ) -> None:
        self.params = params or StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).with_name("server.py").resolve())],
            env={"PYTHONIOENCODING": "utf-8"},
        )
        self.timeout = timeout

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        try:
            async with asyncio.timeout(self.timeout):
                async with stdio_client(self.params) as (read, write):
                    async with ClientSession(
                        read, write, read_timeout_seconds=timedelta(seconds=self.timeout)
                    ) as session:
                        await session.initialize()
                        yield session
        except Exception as exc:
            raise ToolExecutionError("MCP stdio operation failed or timed out") from exc

    async def list_tools(self) -> list[Tool]:
        async with self._session() as session:
            tools = []
            cursor = None
            seen: set[str] = set()
            while True:
                page = await session.list_tools(cursor=cursor)
                tools.extend(page.tools)
                cursor = page.nextCursor
                if cursor is None:
                    return tools
                if cursor in seen:
                    raise ToolExecutionError("MCP tools/list repeated cursor")
                seen.add(cursor)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self._session() as session:
            result = await session.call_tool(name, arguments=arguments)
        return decode_result(result)


def decode_result(result: CallToolResult) -> dict[str, Any]:
    """Never treat an MCP tool error as a successful business result."""
    if result.isError:
        raise ToolExecutionError("MCP tool returned isError=true")
    if result.structuredContent is not None:
        return result.structuredContent
    texts = [item.text for item in result.content if item.type == "text"]
    if len(texts) == 1:
        try:
            value = json.loads(texts[0])
        except ValueError:
            pass
        else:
            if isinstance(value, dict):
                return value
    return {"content": [item.model_dump(mode="json") for item in result.content]}
