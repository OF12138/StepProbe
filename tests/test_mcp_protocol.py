"""MCP 协议层冒烟测试。

tools.py 的逻辑由 test_mcp_tools.py 覆盖；这里只验证一件事：
**工具确实能通过 MCP 协议被调用到**，不会因为 SDK 版本或签名问题挂掉。

未安装 mcp 时整体跳过 —— 它是可选依赖，数据管线与校验器不需要它。
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp", reason="mcp 为可选依赖")

from stepprobe.mcp_server.__main__ import build_server  # noqa: E402
from stepprobe.mcp_server.tools import TOOLS  # noqa: E402


def _unwrap(result) -> dict:
    """兼容 mcp 1.x / 2.x 的结果字段命名。"""
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    if structured:
        return structured
    return json.loads(result.content[0].text)


@pytest.fixture(scope="module")
def server():
    return build_server()


def test_all_tools_registered(server) -> None:
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == set(TOOLS), f"注册的工具与 TOOLS 清单不一致：{names ^ set(TOOLS)}"


def test_every_tool_has_description(server) -> None:
    for tool in asyncio.run(server.list_tools()):
        assert (tool.description or "").strip(), f"{tool.name} 缺少说明"


def test_call_check_step_symbolic(server) -> None:
    res = asyncio.run(
        server.call_tool("check_step_symbolic", {"steps": ["2 + 3 = 5", "5 * 2 = 11", "结论成立"]})
    )
    data = _unwrap(res)
    assert data["first_false_step"] == 2
    assert 3 in data["needs_llm_review"]


def test_call_check_answer(server) -> None:
    res = asyncio.run(server.call_tool("check_answer", {"pred": "1/2", "gold": "0.5"}))
    assert _unwrap(res)["correct"] is True


def test_call_solution_segment(server) -> None:
    res = asyncio.run(
        server.call_tool("solution_segment", {"text": "Step 1: 化简\nStep 2: 求解"})
    )
    assert _unwrap(res)["n_steps"] == 2
