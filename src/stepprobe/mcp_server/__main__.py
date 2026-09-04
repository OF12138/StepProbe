"""StepProbe MCP Server（stdio）。

注册到 WorkBuddy：编辑 `~/.workbuddy/mcp.json` 或 `<项目目录>/.workbuddy/mcp.json`

    {
      "mcpServers": {
        "stepprobe": {
          "command": "python",
          "args": ["-m", "stepprobe.mcp_server"],
          "env": {
            "STEPPROBE_DATA_DIR": "./data",
            "STEPPROBE_RESULTS_DIR": "./results"
          }
        }
      }
    }

本模块只做协议适配 —— 工具逻辑全在 tools.py，可脱离 MCP 单独测试。
"""

from __future__ import annotations

import sys

from . import tools


def _server_class():
    """兼容 mcp 1.x 与 2.x。

    2.x 把 FastMCP 改名为 MCPServer（`mcp.server.mcpserver`），装饰器与 run()
    的用法保持一致。WorkBuddy 用户机器上两个版本都可能存在，这里都支持，
    免得因为 SDK 版本对不上而白折腾。
    """
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2.0

        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP  # mcp < 2.0

        return FastMCP
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "缺少 mcp 依赖，或版本不受支持。请安装：pip install mcp\n"
            f"（原始错误：{exc}）"
        ) from exc


def build_server():
    """构建 MCP 服务实例。

    延迟导入 mcp，使得未安装该可选依赖时其余功能（数据管线、校验器）仍可用。
    """
    mcp = _server_class()("stepprobe")

    @mcp.tool()
    def dataset_next_batch(
        n: int = 20,
        tier: str | None = None,
        label_class: str | None = None,
        run_id: str | None = None,
        seed: int | None = None,
    ) -> dict:
        """取一批待评估样本（已剥离人工标注）。

        Args:
            n: 样本数量
            tier: 难度层 T1/T2/T3/T4，留空则不限
            label_class: faulty_wrong / clean_correct / e9，留空则不限
            run_id: 续用已有运行；留空则新建
            seed: 抽样随机种子
        """
        return tools.dataset_next_batch(
            n=n, tier=tier, label_class=label_class, run_id=run_id, seed=seed
        )

    @mcp.tool()
    def solution_segment(text: str) -> dict:
        """把解答文本切分为 1-based 编号的步骤。"""
        return tools.solution_segment(text)

    @mcp.tool()
    def check_answer(pred: str, gold: str) -> dict:
        """比对最终答案。三级校验：规范化 → 符号等价 → 数值代入。

        判不出来返回 UNKNOWN，不返回错 —— 请勿用自己的计算覆盖本结论。
        """
        return tools.check_answer_tool(pred, gold)

    @mcp.tool()
    def check_format(answer: str, requirements: dict | None = None) -> dict:
        """按题目明确提出的形式要求校验答案（E8）。

        requirements 支持：decimals / simplified_fraction / unit / shape。
        题目未提形式要求时不要调用。
        """
        return tools.check_format_tool(answer, requirements)

    @mcp.tool()
    def check_step_symbolic(steps: list[str]) -> dict:
        """逐步做确定性等式校验，返回 FALSE / UNKNOWN / TRUE。

        只需对 needs_llm_review 里列出的步骤做语义审查。
        注意 TRUE 不代表整条链成立 —— E9 仍需全局复核。
        """
        return tools.check_step_symbolic(steps)

    @mcp.tool()
    def verdict_record(run_id: str, verdict: dict) -> dict:
        """记录一条评判结果。

        verdict 需含 sample_id、process_valid；判错时必须填 evidence
        与 first_error_step，否则会被拒绝。
        """
        return tools.verdict_record(run_id, verdict)

    @mcp.tool()
    def metrics_compute(run_id: str | None = None) -> dict:
        """计算定位准确率、误报率、错误类型一致性、稳定性与分层结果。"""
        return tools.metrics_compute(run_id)

    @mcp.tool()
    def report_export(run_id: str | None = None, kind: str = "summary") -> dict:
        """导出结果表格（kind=summary）或人工抽检清单（kind=human_audit）。"""
        return tools.report_export(run_id, kind)

    @mcp.tool()
    def p6_next_batch(
        n: int = 20,
        tier: str | None = None,
        arm: str = "max_off",
        run_id: str | None = None,
    ) -> dict:
        """取一批 P6 消融题目。**不返回标准答案** —— 解题时看到答案则本轮作废。

        Args:
            n: 题目数量
            tier: 难度层 T1/T2/T3/T4，留空则不限
            arm: max_on / max_off，对应 Max Mode 开与关两个实验臂
            run_id: 续用已有运行；留空则新建
        """
        return tools.p6_next_batch(n=n, tier=tier, arm=arm, run_id=run_id)

    @mcp.tool()
    def p6_record(run_id: str, arm: str, solution: dict) -> dict:
        """记录一条 P6 解答。

        solution 需含 problem_id、steps（1-based 步骤文本列表）、final_answer。
        解不出来时 final_answer 填 null，不要编造。
        """
        return tools.p6_record(run_id, arm, solution)

    return mcp


def main() -> int:
    if "--list-tools" in sys.argv:
        for name in tools.TOOLS:
            print(name)
        return 0
    build_server().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
