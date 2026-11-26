"""通过 MCP 协议（内存传输）调用，验证工具 schema 与结构化返回。"""
import anyio

from cadmcp.gateway.server import init
from cylinder_station import BASE_PLATE


def test_mcp_tools(svc):
    server = init(svc)

    async def run():
        tools = await server.list_tools()
        names = {t.name for t in tools}
        assert {"cad_build_part", "cad_build_drawing", "cad_change_commit"} <= names
        bp = next(t for t in tools if t.name == "cad_build_part")
        assert "spec" in bp.input_schema["properties"]
        assert bp.annotations.read_only_hint is False
        await server.call_tool("cad_change_begin", {"project": "MCP"})
        res = await server.call_tool("cad_build_part", {"spec": BASE_PLATE})
        return res

    res = anyio.run(run)
    assert res.is_error is False
    data = res.structured_content
    assert data["ok"] is True and data["name"] == "CS01-001_BasePlate"
    assert data["rules"]["passed"] is True
