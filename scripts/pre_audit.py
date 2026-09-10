"""人工抽检预审：把「逐条从头分析」降为「逐条确认」。

任务书要求「经人工抽检确认其中属于真实问题与属于误报的比例」，这一步无法用
自动方法替代。但可以先做一轮预审，把每条分歧整理成**判断包**（题干、完整步骤、
标注与评估器的分歧点、L1 独立符号校验结果），并附上预审意见与置信度。

人工只需对每行确认或推翻，而不是从头读题重新分析。

**诚实边界**：预审意见只是建议，不等于人工抽检结果。
最终 CSV 保留独立的 `human_verdict` 列；报告中必须分别写明「人工确认了 N 条、
推翻了 M 条」，不把预审冒充人工抽检。

    python scripts/pre_audit.py --run <run_id> [--verdicts scripts/pre_audit_verdicts.json]

产出（写入该 run 目录）：
    audit_packets.md    每条分歧的完整判断包，供人工阅读
    audit_sheet.csv     增加 pre_audit_verdict / pre_audit_reason / pre_audit_confidence 列
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from stepprobe.checkers.symbolic import check_step

#: 预审结论的取值
VERDICTS = {
    "evaluator_right",   # 评估器正确，原标注有问题
    "evaluator_wrong",   # 评估器错了，原标注正确
    "criterion_gap",     # 双方都没错，是判定准则不同
    "needs_human",       # 预审无法定论，必须人工判断
}


def load_rows(run_dir: Path) -> list[dict]:
    payload = json.loads((run_dir / "human_audit.json").read_text(encoding="utf-8"))
    return payload["rows"]


def load_samples(data_dir: Path, wanted: set[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with (data_dir / "sample_set.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            d = json.loads(line)
            if d["id"] in wanted:
                out[d["id"]] = d
    return out


def _l1_on(step_text: str) -> str:
    """对单步跑 L1 确定性校验，给人工一个独立于两方判断的参考。"""
    chk = check_step(step_text)
    return f"{chk.verdict.value} — {chk.detail}"


def build_packet(row: dict, sample: dict, suggestion: dict | None) -> str:
    gold = row["gold_step"]
    ev = row["evaluator_step"]
    lines: list[str] = []

    lines.append(f"## {row['sample_id']}")
    lines.append("")
    lines.append(f"- 分歧类型：**{row['disagreement_kind']}**　难度层：{row['tier']}")
    lines.append(f"- 人工标注：过程{row['gold_process']}" + (f"，首错第 {gold} 步" if gold else ""))
    lines.append(
        f"- 评估器：过程{row['evaluator_process']}"
        + (f"，首错第 {ev} 步（{row['evaluator_error_type'] or '未归类'}）" if ev else "")
    )
    lines.append("")
    lines.append("### 题目")
    lines.append("")
    lines.append(sample["problem"].strip())
    lines.append("")

    lines.append("### 解题步骤")
    lines.append("")
    for i, step in enumerate(sample["solution_steps"], 1):
        marks = []
        if gold and i == gold:
            marks.append("标注首错")
        if ev and i == ev:
            marks.append("评估器首错")
        tag = f"  ← **{' / '.join(marks)}**" if marks else ""
        lines.append(f"**[{i}]**{tag}")
        lines.append("")
        lines.append(step.strip())
        lines.append("")

    lines.append("### L1 确定性校验（独立于双方判断）")
    lines.append("")
    for label, idx in (("标注首错步", gold), ("评估器首错步", ev)):
        if idx and idx <= len(sample["solution_steps"]):
            lines.append(f"- {label} [{idx}]：`{_l1_on(sample['solution_steps'][idx - 1])}`")
    lines.append("")

    lines.append("### 评估器给出的证据")
    lines.append("")
    lines.append((row.get("evidence") or "（无）").strip())
    lines.append("")

    if suggestion:
        lines.append("### 预审意见（非人工抽检结果）")
        lines.append("")
        lines.append(f"- 结论：**{suggestion['verdict']}**　置信度：{suggestion['confidence']}")
        lines.append(f"- 理由：{suggestion['reason']}")
        if suggestion.get("human_question"):
            lines.append(f"- **待人工确认**：{suggestion['human_question']}")
        lines.append("")

    lines.append("### 人工判断")
    lines.append("")
    lines.append("`human_verdict`（在 audit_sheet.csv 中填写）：")
    lines.append("`evaluator_right` / `evaluator_wrong` / `criterion_gap`")
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


FIELDS = [
    "disagreement_kind", "sample_id", "tier",
    "gold_process", "gold_step",
    "evaluator_process", "evaluator_step", "evaluator_error_type",
    "pre_audit_verdict", "pre_audit_confidence", "pre_audit_reason",
    "human_verdict", "human_note",
    "evidence", "step_content",
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="人工抽检预审")
    ap.add_argument("--run", required=True, help="run_id 或 run 目录")
    ap.add_argument("--results-dir", type=Path, default=Path("results/runs"))
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument(
        "--verdicts",
        type=Path,
        default=Path("scripts/pre_audit_verdicts.json"),
        help="预审意见文件；不存在则只生成判断包，不填意见",
    )
    args = ap.parse_args(argv)

    run_dir = Path(args.run) if Path(args.run).is_dir() else args.results_dir / args.run
    rows = load_rows(run_dir)
    samples = load_samples(args.data_dir, {r["sample_id"] for r in rows})

    suggestions: dict[str, dict] = {}
    if args.verdicts.exists():
        suggestions = json.loads(args.verdicts.read_text(encoding="utf-8"))
        bad = {v["verdict"] for v in suggestions.values()} - VERDICTS
        if bad:
            raise SystemExit(f"预审结论取值非法：{bad}")

    packets = ["# 人工抽检判断包", "",
               f"运行：`{run_dir.name}`　共 {len(rows)} 条分歧", "",
               "> 预审意见只是建议，**不等于人工抽检结果**。",
               "> 请逐条确认或推翻，并把结论填进 `audit_sheet.csv` 的 `human_verdict` 列。", "",
               "---", ""]
    csv_rows = []

    for row in sorted(rows, key=lambda r: (r["disagreement_kind"], r["sample_id"])):
        sample = samples.get(row["sample_id"])
        if sample is None:
            continue
        sug = suggestions.get(row["sample_id"])
        packets.append(build_packet(row, sample, sug))
        csv_rows.append(
            {
                **{k: row.get(k, "") for k in FIELDS if k in row},
                "pre_audit_verdict": (sug or {}).get("verdict", ""),
                "pre_audit_confidence": (sug or {}).get("confidence", ""),
                "pre_audit_reason": (sug or {}).get("reason", ""),
                "human_verdict": "",
                "human_note": "",
            }
        )

    (run_dir / "audit_packets.md").write_text("\n".join(packets), encoding="utf-8")
    with (run_dir / "audit_sheet.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(csv_rows)

    by_sug = Counter(r["pre_audit_verdict"] or "（未预审）" for r in csv_rows)
    print(f"{len(csv_rows)} 条分歧 → {run_dir}")
    print(f"  audit_packets.md  判断包")
    print(f"  audit_sheet.csv   待人工填 human_verdict")
    print()
    print("预审结论分布：")
    for k, v in by_sug.most_common():
        print(f"  {k:<18} {v}")
    pending = by_sug.get("needs_human", 0) + by_sug.get("（未预审）", 0)
    print()
    print(f"需人工独立判断：{pending} 条；其余 {len(csv_rows) - pending} 条只需确认预审意见")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
