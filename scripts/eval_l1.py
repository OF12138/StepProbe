"""L1 确定性层的实测脚本。

在带人工标注的样本集上跑纯 L1，测量它单独能达到什么水平。

这不是评估器的最终成绩 —— L1 只处理计算类错误，语义类错误由 L2/L3 负责。
这里关心的是一个更基本的问题：**L1 判 FALSE 的时候到底准不准。**

L1 是整个设计的地基（「把能确定判定的事从 LLM 手里拿走」），如果它自己就
会误报，那这个论证就不成立。

    python scripts/eval_l1.py [--data data/sample_set.jsonl] [--dump-fp N]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from stepprobe.checkers.symbolic import StepVerdictValue, check_steps


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% 置信区间。小样本下比正态近似可靠。"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="L1 确定性层实测")
    ap.add_argument("--data", type=Path, default=Path("data/sample_set.jsonl"))
    ap.add_argument("--dump-fp", type=int, default=0, help="打印前 N 条误报供人工诊断")
    ap.add_argument("--out", type=Path, default=Path("results/validation/l1_report.json"))
    args = ap.parse_args(argv)

    samples = [json.loads(line) for line in args.data.open(encoding="utf-8") if line.strip()]

    step_stat: Counter[str] = Counter()
    outcome: Counter[str] = Counter()
    false_positives: list[dict] = []
    fired = 0

    for s in samples:
        checks = check_steps(s["solution_steps"])
        for c in checks:
            step_stat[c.verdict.value] += 1

        first = next(
            (i for i, c in enumerate(checks, 1) if c.verdict is StepVerdictValue.FALSE), None
        )
        gold = s["label"]["first_error_step"]

        if first is None:
            outcome["未触发"] += 1
            continue

        fired += 1
        if gold is None:
            outcome["误报（标注过程无误）"] += 1
            false_positives.append(
                {
                    "id": s["id"],
                    "tier": s["tier"],
                    "step": first,
                    "content": s["solution_steps"][first - 1][:300],
                    "detail": checks[first - 1].detail,
                }
            )
        elif first == gold:
            outcome["精确命中"] += 1
        elif abs(first - gold) <= 1:
            outcome["±1 命中"] += 1
        elif first < gold:
            outcome["早于标注"] += 1
        else:
            outcome["晚于标注"] += 1

    total_steps = sum(step_stat.values())
    fp = outcome["误报（标注过程无误）"]
    exact = outcome["精确命中"]
    tol1 = exact + outcome["±1 命中"]

    print(f"样本 {len(samples)} 条，步骤 {total_steps} 步\n")
    print("步骤级判定分布")
    for k in ("FALSE", "TRUE", "UNKNOWN"):
        print(f"  {k:<8} {step_stat[k]:>6}  {step_stat[k] / max(total_steps, 1):>7.2%}")

    print(f"\n样本级：L1 触发 {fired}/{len(samples)} = {fired / len(samples):.1%}")
    for k, v in sorted(outcome.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<20} {v:>4}")

    if fired:
        lo, hi = wilson(fp, fired)
        print(f"\n触发时精确率  {exact}/{fired} = {exact / fired:.1%}")
        print(f"触发时 ±1 精确率 {tol1}/{fired} = {tol1 / fired:.1%}")
        print(f"触发时误报率  {fp}/{fired} = {fp / fired:.1%}  (95% CI {lo:.1%}–{hi:.1%})")

    report = {
        "n_samples": len(samples),
        "n_steps": total_steps,
        "step_verdicts": dict(step_stat),
        "fired": fired,
        "outcome": dict(outcome),
        "precision_exact": exact / fired if fired else None,
        "precision_tol1": tol1 / fired if fired else None,
        "false_positive_rate": fp / fired if fired else None,
        "false_positive_ci": wilson(fp, fired) if fired else None,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告 → {args.out}")

    for i, case in enumerate(false_positives[: args.dump_fp], 1):
        print(f"\n### 误报 {i} | {case['id']} 第 {case['step']} 步")
        print(f"  {case['content'][:220]}")
        print(f"  判定依据: {case['detail']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
