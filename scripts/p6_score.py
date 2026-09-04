"""P6 评分：Max Mode 开 / 关的同题配对对比。

回答 `configs/default.yaml` 里写下的那个问题：

    **Max Mode 是让推导更严谨，还是只是让答案更容易蒙对？**

这两件事只有拆开测才分得出来，因为它们在「答案正确率」这一个数字上长得一模一样：

    答案正确率 ↑ 且 过程正确率 ↑     推导确实更严谨
    答案正确率 ↑ 但 过程正确率 ↔     只是更容易蒙对 —— E9 比例会同步上升

所以本脚本同时算三件事，并且**只看配对差分**：

    answer_accuracy   答案正确率（gold 由 build_p6_set.py 反推，见该脚本说明）
    process_validity  过程成立率（由已验证的评估器判定，run 的 verdicts）
    e9_ratio          答案对但过程不成立的比例 —— 判别「蒙对」的直接指标

**为什么必须配对。** 两个 arm 解的是同一批题，所以可以对每道题算差分，再对
差分做统计。这样题目难度、标准答案抽取误差这些成对出现的偏置会被抵消掉 ——
`build_p6_set.py` 里接受约 2% 的标准答案噪声，靠的正是这一点。

用法：

    python scripts/p6_score.py --run p6-20260904T061014Z

输入（该 run 目录下）：
    solutions_max_off.jsonl / solutions_max_on.jsonl   两臂的解答
    verdicts.jsonl                                     评估器对这些解答的评判
输出：
    p6_comparison.json / p6_comparison.md
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from stepprobe.checkers.answer import Equivalence, check_answer
from stepprobe.metrics.core import Proportion, wilson

ARMS = ("max_off", "max_on")


def load_gold(data_dir: Path) -> dict[str, dict]:
    out = {}
    with (data_dir / "p6_problems.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                d = json.loads(line)
                out[d["id"]] = d
    return out


def load_arm(run_dir: Path, arm: str) -> dict[str, dict]:
    """同一题多次记录取最后一次。"""
    path = run_dir / f"solutions_{arm}.jsonl"
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                d = json.loads(line)
                out[d["problem_id"]] = d
    return out


def load_verdicts(run_dir: Path) -> dict[tuple[str, str], dict]:
    """(arm, problem_id) → verdict。评估器落盘时 sample_id 用 `<arm>:<problem_id>`。"""
    path = run_dir / "verdicts.jsonl"
    out: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            v = json.loads(line)
            sid = v["sample_id"]
            arm, _, pid = sid.partition(":")
            if arm in ARMS and pid:
                out[(arm, pid)] = v
    return out


def _answer_correct(pred: str | None, gold: str) -> bool | None:
    """None 表示判不出来 —— 不算对也不算错，从分母中剔除。"""
    if not pred:
        return False  # 解不出来就是没答对
    r = check_answer(pred, gold)
    if r.verdict is Equivalence.UNKNOWN:
        return None
    return r.verdict is Equivalence.EQUAL


def _mcnemar(b: int, c: int) -> float | None:
    """配对二分类的 McNemar 精确检验（双侧 p）。

    b = 只有 arm A 对的题数，c = 只有 arm B 对的题数。两臂都对或都错的题
    不携带差异信息，正是配对设计要甩掉的那部分方差。
    """
    n = b + c
    if n == 0:
        return None
    from math import comb

    tail = sum(comb(n, k) for k in range(0, min(b, c) + 1))
    return min(1.0, 2 * tail / (2**n))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P6 Max Mode 消融评分")
    ap.add_argument("--run", required=True)
    ap.add_argument("--results-dir", type=Path, default=Path("results/runs"))
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument(
        "--cross-validated-only",
        action="store_true",
        help="只用 n_agree>=2（标准答案有交叉印证）的题目算绝对准确率",
    )
    args = ap.parse_args(argv)

    run_dir = Path(args.run) if Path(args.run).is_dir() else args.results_dir / args.run
    gold = load_gold(args.data_dir)
    sols = {arm: load_arm(run_dir, arm) for arm in ARMS}
    verdicts = load_verdicts(run_dir)

    paired = sorted(set(sols["max_off"]) & set(sols["max_on"]))
    if not paired:
        raise SystemExit(
            "两个 arm 没有共同解过的题目。请确认 solutions_max_off.jsonl 与 "
            "solutions_max_on.jsonl 都已生成，且用的是同一批题。"
        )
    if args.cross_validated_only:
        paired = [p for p in paired if gold.get(p, {}).get("n_agree", 1) >= 2]

    # 逐题记录两臂的三个结果
    per_problem: dict[str, dict] = {}
    for pid in paired:
        g = gold.get(pid)
        if g is None:
            continue
        row: dict = {"tier": g["tier"], "n_agree": g.get("n_agree", 1)}
        for arm in ARMS:
            v = verdicts.get((arm, pid))
            row[arm] = {
                "answer_correct": _answer_correct(sols[arm][pid].get("final_answer"), g["gold_answer"]),
                "process_valid": None if v is None else v["process_valid"],
            }
        per_problem[pid] = row

    def _prop(arm: str, field: str, want: bool = True) -> Proportion:
        vals = [r[arm][field] for r in per_problem.values() if r[arm][field] is not None]
        return Proportion(sum(1 for v in vals if v is want), len(vals))

    def _e9(arm: str) -> Proportion:
        """E9 比例：在**答对的题**里，过程不成立的占比。"""
        rows = [
            r for r in per_problem.values()
            if r[arm]["answer_correct"] is True and r[arm]["process_valid"] is not None
        ]
        return Proportion(sum(1 for r in rows if not r[arm]["process_valid"]), len(rows))

    metrics = {
        arm: {
            "answer_accuracy": _prop(arm, "answer_correct").to_dict(),
            "process_validity": _prop(arm, "process_valid").to_dict(),
            "e9_ratio_among_correct": _e9(arm).to_dict(),
        }
        for arm in ARMS
    }

    # 配对检验
    paired_tests = {}
    for field in ("answer_correct", "process_valid"):
        b = sum(
            1 for r in per_problem.values()
            if r["max_on"][field] is True and r["max_off"][field] is False
        )
        c = sum(
            1 for r in per_problem.values()
            if r["max_on"][field] is False and r["max_off"][field] is True
        )
        paired_tests[field] = {
            "max_on_only": b,
            "max_off_only": c,
            "net": b - c,
            "mcnemar_p": _mcnemar(b, c),
        }

    by_tier: dict[str, dict] = defaultdict(dict)
    for tier in sorted({r["tier"] for r in per_problem.values()}):
        rows = {k: v for k, v in per_problem.items() if v["tier"] == tier}
        for arm in ARMS:
            vals = [r[arm]["answer_correct"] for r in rows.values() if r[arm]["answer_correct"] is not None]
            by_tier[tier][arm] = Proportion(sum(vals), len(vals)).to_dict()

    payload = {
        "run_id": run_dir.name,
        "n_paired_problems": len(per_problem),
        "cross_validated_only": args.cross_validated_only,
        "metrics": metrics,
        "paired_tests": paired_tests,
        "answer_accuracy_by_tier": dict(by_tier),
        "per_problem": per_problem,
    }
    (run_dir / "p6_comparison.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def _f(d: dict) -> str:
        if d["value"] is None:
            return "—"
        lo, hi = d["ci95"]
        return f"{d['value']:.3f} ({d['k']}/{d['n']}, CI [{lo:.3f}, {hi:.3f}])"

    md = [
        "# P6：Max Mode 消融",
        "",
        f"运行 `{run_dir.name}`，同题配对 {len(per_problem)} 道"
        + ("（仅标准答案有交叉印证的题）" if args.cross_validated_only else ""),
        "",
        "| 指标 | Max Mode 关 | Max Mode 开 |",
        "| --- | --- | --- |",
    ]
    for name, key in (
        ("答案正确率", "answer_accuracy"),
        ("过程成立率", "process_validity"),
        ("答对题中过程不成立的比例（E9）", "e9_ratio_among_correct"),
    ):
        md.append(f"| {name} | {_f(metrics['max_off'][key])} | {_f(metrics['max_on'][key])} |")

    md += ["", "## 配对检验（McNemar）", "",
           "| 口径 | 仅开启时成立 | 仅关闭时成立 | 净变化 | p |",
           "| --- | --- | --- | --- | --- |"]
    for field, label in (("answer_correct", "答案正确"), ("process_valid", "过程成立")):
        t = paired_tests[field]
        p = "—" if t["mcnemar_p"] is None else f"{t['mcnemar_p']:.3f}"
        md.append(f"| {label} | {t['max_on_only']} | {t['max_off_only']} | {t['net']:+d} | {p} |")

    md += [
        "",
        "## 怎么读这张表",
        "",
        "- **答案正确率与过程成立率同升** → Max Mode 确实让推导更严谨",
        "- **答案正确率升、过程成立率不动、E9 比例升** → 只是更容易蒙对",
        "- **两者都不显著（p 大）** → 在本题目集上看不出差别；这也是结论，"
        "如实写，不要挑口径凑显著",
        "",
        "> 绝对准确率受题目集偏易影响（题目都是至少被一个模型答对过的），"
        "不与其他工作横向比较；配对差分不受该偏置影响。",
        "",
    ]
    (run_dir / "p6_comparison.md").write_text("\n".join(md), encoding="utf-8")

    print(f"同题配对 {len(per_problem)} 道")
    for name, key in (
        ("答案正确率", "answer_accuracy"),
        ("过程成立率", "process_validity"),
        ("E9 比例", "e9_ratio_among_correct"),
    ):
        print(f"  {name:<10} 关 {_f(metrics['max_off'][key])}")
        print(f"  {'':<10} 开 {_f(metrics['max_on'][key])}")
    print()
    for field, label in (("answer_correct", "答案正确"), ("process_valid", "过程成立")):
        t = paired_tests[field]
        p = "—" if t["mcnemar_p"] is None else f"{t['mcnemar_p']:.3f}"
        print(f"  McNemar［{label}］净 {t['net']:+d}（开 {t['max_on_only']} / 关 {t['max_off_only']}），p={p}")
    print()
    print(f"写入 {run_dir / 'p6_comparison.json'}")
    print(f"写入 {run_dir / 'p6_comparison.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
