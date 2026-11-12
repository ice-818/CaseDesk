"""Map dynamically discovered MCP tools to the existing ToolRegistry."""

from __future__ import annotations

from typing import Any

from jsonschema import ValidationError, validate
from mcp.types import Tool as MCPTool
from pydantic import BaseModel, ConfigDict

from app.core.enums import ToolRisk
from app.core.exceptions import InvalidRequestError
from app.mcp.client import StdioMCPClient
from app.schemas.agent import ToolResult
from app.tools.base import Tool, ToolContext
from app.tools.registry import ToolRegistry


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="allow")


class StdioMCPToolProxy(Tool):
    def __init__(self, spec: MCPTool, client: StdioMCPClient) -> None:
        self.name = spec.name
        self.description = spec.description or ""
        self.risk = ToolRisk.EXTERNAL
        self.input_model = _Arguments
        self._schema = spec.inputSchema
        self._client = client

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._schema,
            },
        }

    def parse(self, arguments: dict[str, Any]) -> BaseModel:
        try:
            validate(arguments, self._schema)
        except ValidationError as exc:
            raise InvalidRequestError(f"invalid arguments for {self.name}") from exc
        return super().parse(arguments)

    async def execute(self, parsed: BaseModel, context: ToolContext) -> ToolResult:
        output = await self._client.call_tool(self.name, parsed.model_dump())
        return ToolResult(name=self.name, success=True, output=output, risk=self.risk)


async def register_stdio_tools(
    registry: ToolRegistry, client: StdioMCPClient | None = None
) -> None:
    client = client or StdioMCPClient()
    specs = await client.list_tools()
    # Validate all names before mutating the registry; never silently shadow tools.
    names = registry.names()
    for spec in specs:
        if spec.name in names:
            raise InvalidRequestError(f"duplicate MCP tool: {spec.name}")
        names.append(spec.name)
    for spec in specs:
        registry.register(StdioMCPToolProxy(spec, client))
