from __future__ import annotations

import logging
from typing import Any

from solvyn.mcp.tool import MCPTool, MCPToolSchema
from solvyn.tools.exceptions import ToolNotFoundError

logger = logging.getLogger(__name__)


class MCPServerManager:
    """Central registry for MCP tools. Supports discovery, validation, and invocation."""

    def __init__(self) -> None:
        self._tools: dict[str, MCPTool] = {}

    def register_tool(self, tool: MCPTool) -> None:
        self._tools[tool.schema.name] = tool
        logger.info(f"Registered MCP tool: {tool.schema.name} v{tool.schema.version}")

    def register_tool_from_schema(
        self,
        schema: MCPToolSchema,
        handler: Any,
        auth_required: bool = False,
        auth_provider: str = "",
    ) -> MCPTool:
        tool = MCPTool(
            schema=schema,
            handler=handler,
            auth_required=auth_required,
            auth_provider=auth_provider,
        )
        self.register_tool(tool)
        return tool

    def get_tool(self, name: str) -> MCPTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(name)
        return tool

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def list_tools(self) -> list[MCPToolSchema]:
        return [tool.schema for tool in self._tools.values()]

    def discover_for_capabilities(self, capabilities: list[str]) -> list[MCPTool]:
        if not capabilities:
            return list(self._tools.values())
        matching = []
        for tool in self._tools.values():
            tool_caps = set(tool.schema.required_capabilities)
            if not tool_caps or tool_caps.intersection(set(capabilities)):
                matching.append(tool)
        return matching

    async def invoke(self, tool_name: str, parameters: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        tool = self.get_tool(tool_name)
        return await tool.execute(parameters, context)

    def get_stats(self) -> dict[str, dict[str, Any]]:
        return {name: tool.get_stats() for name, tool in self._tools.items()}


mcp_server_manager = MCPServerManager()
