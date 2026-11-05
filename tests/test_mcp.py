from __future__ import annotations

import pytest

from solvyn.mcp.server import MCPServerManager
from solvyn.mcp.tool import MCPTool, MCPToolParameter, MCPToolSchema
from solvyn.tools.exceptions import ToolExecutionError, ToolNotFoundError, ToolValidationError


class TestMCPToolSchema:
    def test_schema_creation(self) -> None:
        schema = MCPToolSchema(name="test-tool", description="A test tool")
        assert schema.name == "test-tool"
        assert schema.version == "1.0.0"

    def test_schema_with_parameters(self) -> None:
        params = [MCPToolParameter(name="query", type="string", description="Search query")]
        schema = MCPToolSchema(name="search", parameters=params)
        assert len(schema.parameters) == 1
        assert schema.parameters[0].name == "query"

    def test_to_prompt_schema(self) -> None:
        params = [MCPToolParameter(name="q", type="string", description="query")]
        schema = MCPToolSchema(name="s", description="search", parameters=params)
        prompt = schema.to_prompt_schema()
        assert "Tool: s" in prompt
        assert "q (string, required)" in prompt


class TestMCPTool:
    def test_tool_creation(self) -> None:
        schema = MCPToolSchema(name="my-tool")
        tool = MCPTool(schema=schema)
        assert tool.schema.name == "my-tool"
        assert tool.sandboxed is False

    @pytest.mark.asyncio
    async def test_tool_execute(self) -> None:
        async def handler(params: dict, ctx: dict) -> dict:
            return {"result": params.get("q", "")}

        schema = MCPToolSchema(name="echo")
        tool = MCPTool(schema=schema, handler=handler)
        result = await tool.execute({"q": "hello"})
        assert result["success"] is True
        assert result["result"]["result"] == "hello"

    @pytest.mark.asyncio
    async def test_sandboxed_tool_fails_closed_without_runtime_dispatch(self) -> None:
        called = False

        def handler(params: dict, ctx: dict) -> dict:
            nonlocal called
            called = True
            return {}

        tool = MCPTool(schema=MCPToolSchema(name="isolated", sandboxed=True), handler=handler)
        with pytest.raises(ToolExecutionError, match="refusing to execute the handler directly"):
            await tool.execute({})
        assert called is False

    @pytest.mark.asyncio
    async def test_tool_validate_missing_required(self) -> None:
        params = [MCPToolParameter(name="required_param", type="string", required=True)]
        schema = MCPToolSchema(name="strict", parameters=params)
        tool = MCPTool(schema=schema, handler=lambda p, c: {})
        with pytest.raises(ToolValidationError):
            await tool.execute({})

    @pytest.mark.asyncio
    async def test_tool_default_params(self) -> None:
        params = [MCPToolParameter(name="limit", type="int", required=False, default=10)]
        schema = MCPToolSchema(name="paged", parameters=params)
        tool = MCPTool(schema=schema, handler=lambda p, c: p)
        result = await tool.execute({})
        assert result["result"]["limit"] == 10

    def test_tool_stats(self) -> None:
        schema = MCPToolSchema(name="stat-tool")
        tool = MCPTool(schema=schema)
        stats = tool.get_stats()
        assert stats["call_count"] == 0
        assert stats["tool_name"] == "stat-tool"


class TestMCPServerManager:
    def test_register_and_list(self) -> None:
        mgr = MCPServerManager()
        schema = MCPToolSchema(name="t1")
        mgr.register_tool(MCPTool(schema=schema))
        tools = mgr.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "t1"

    def test_get_tool(self) -> None:
        mgr = MCPServerManager()
        schema = MCPToolSchema(name="t2")
        mgr.register_tool(MCPTool(schema=schema))
        tool = mgr.get_tool("t2")
        assert tool.schema.name == "t2"

    def test_get_tool_not_found(self) -> None:
        mgr = MCPServerManager()
        with pytest.raises(ToolNotFoundError):
            mgr.get_tool("nonexistent")

    def test_has_tool(self) -> None:
        mgr = MCPServerManager()
        assert not mgr.has_tool("t3")
        mgr.register_tool(MCPTool(schema=MCPToolSchema(name="t3")))
        assert mgr.has_tool("t3")

    def test_discover_for_capabilities(self) -> None:
        mgr = MCPServerManager()
        mgr.register_tool(MCPTool(schema=MCPToolSchema(name="t1", required_capabilities=["code_gen"])))
        mgr.register_tool(MCPTool(schema=MCPToolSchema(name="t2", required_capabilities=["review"])))
        mgr.register_tool(MCPTool(schema=MCPToolSchema(name="t3")))
        found = mgr.discover_for_capabilities(["code_gen"])
        assert len(found) == 2

    @pytest.mark.asyncio
    async def test_invoke(self) -> None:
        async def handler(params: dict, ctx: dict) -> dict:
            return {"echo": params.get("msg", "")}

        mgr = MCPServerManager()
        mgr.register_tool_from_schema(MCPToolSchema(name="echo"), handler)
        result = await mgr.invoke("echo", {"msg": "hi"})
        assert result["success"] is True
        assert result["result"]["echo"] == "hi"
