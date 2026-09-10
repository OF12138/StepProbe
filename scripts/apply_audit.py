"""把人工抽检结论落到指标上，产出修正后的指标与对照表。

原始指标以 ProcessBench 标注为准；人工抽检发现该标注本身有一部分是错的，
因此需要一个**可复核**的修正流程 —— 而不是在报告里手写一句「如果人工确认，
误报率将降为 ...」。

    python scripts/apply_audit.py --run <run_id>

输入：
    results/runs/<run>/audit_sheet.csv    人工填写的 human_verdict
    scripts/audit_corrections.json        据此写成的标注修正（含理由）
输出（写入该 run 目录）：
    metrics_corrected.json                修正后指标
    audit_summary.md                      确认/推翻计数 + 修正前后对照

**边界**：只有 human_verdict 非空的行才会被采纳。修正文件里的每条都必须
在 audit_sheet.csv 中有对应的人工结论，否则报错退出 —— 防止把预审意见
当成人工结论悄悄用上。
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from stepprobe.metrics.core import compute
from stepprobe.schema import Label, Sample, Verdict

#: 人工结论 → 该条是否说明「原标注有问题」
_ANNOTATION_FAULTY = {"evaluator_right"}


def load_sheet(run_dir: Path) -> list[dict]:
    with (run_dir / "audit_sheet.csv").open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_samples(data_dir: Path, wanted: set[str]) -> list[Sample]:
    out = []
    with (data_dir / "sample_set.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            d = json.loads(line)
            if d["id"] in wanted:
                out.append(Sample.model_validate(d))
    return out


def load_verdicts(run_dir: Path) -> list[Verdict]:
    out = []
    with (run_dir / "verdicts.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(Verdict.model_validate_json(line))
    return out


def apply_corrections(
    samples: list[Sample], corrections: dict
) -> tuple[list[Sample], list[str]]:
    """返回 (修正后样本, 被剔除的样本 id)。"""
    excluded: list[str] = []
    out: list[Sample] = []
    for s in samples:
        c = corrections.get(s.id)
        if c is None:
            out.append(s)
            continue
        if c["action"] == "exclude":
            excluded.append(s.id)
            continue
        assert s.label is not None, f"{s.id} 无标注却要求 relabel"
        new_label = Label(
            process_correct=c["process_correct"],
            first_error_step=c["first_error_step"],
            answer_correct=s.label.answer_correct,
            error_type=s.label.error_type if not c["process_correct"] else None,
            annotator="human_audit",
        )
        out.append(s.model_copy(update={"label": new_label}))
    return out, excluded


def _fmt(p: dict) -> str:
    if p["value"] is None:
        return "—"
    lo, hi = p["ci95"]
    return f"{p['value']:.3f} ({p['k']}/{p['n']}, CI [{lo:.3f}, {hi:.3f}])"


def _rows(before: dict, after: dict) -> list[tuple[str, str, str]]:
    picks = [
        ("检出率", ("localization", "detection_rate")),
        ("精确定位准确率", ("localization", "exact_localization_acc")),
        ("±1 容忍定位准确率", ("localization", "tolerant_localization_acc_pm1")),
        ("误报率", ("false_positive_rate",)),
        ("E9 召回率", ("e9_recall",)),
    ]
    out = []
    for name, path in picks:
        b, a = before, after
        for k in path:
            b, a = b[k], a[k]
        out.append((name, _fmt(b), _fmt(a)))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="应用人工抽检结论并重算指标")
    ap.add_argument("--run", required=True)
    ap.add_argument("--results-dir", type=Path, default=Path("results/runs"))
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--corrections", type=Path, default=Path("scripts/audit_corrections.json"))
    args = ap.parse_args(argv)

    run_dir = Path(args.run) if Path(args.run).is_dir() else args.results_dir / args.run
    sheet = load_sheet(run_dir)
    payload = json.loads(args.corrections.read_text(encoding="utf-8"))
    corrections = payload["corrections"]

    human = {r["sample_id"]: r for r in sheet if (r.get("human_verdict") or "").strip()}
    if not human:
        raise SystemExit("audit_sheet.csv 中没有任何 human_verdict，无法应用。")

    # 每条修正都必须有对应的人工结论 —— 不允许拿预审意见充数
    orphan = set(corrections) - set(human)
    if orphan:
        raise SystemExit(f"以下修正没有对应的人工结论：{sorted(orphan)}")

    confirmed = [r for r in sheet if r["human_verdict"] == r["pre_audit_verdict"]]
    overridden = [
        r for r in sheet
        if r.get("human_verdict") and r["pre_audit_verdict"] != "needs_human"
        and r["human_verdict"] != r["pre_audit_verdict"]
    ]
    independent = [r for r in sheet if r["pre_audit_verdict"] == "needs_human" and r.get("human_verdict")]

    verdicts = load_verdicts(run_dir)
    ids = {v.sample_id for v in verdicts}
    samples = load_samples(args.data_dir, ids)
    before = compute(samples, verdicts).to_dict()

    fixed, excluded = apply_corrections(samples, corrections)
    kept = [v for v in verdicts if v.sample_id not in set(excluded)]
    after = compute(fixed, kept).to_dict()

    (run_dir / "metrics_corrected.json").write_text(
        json.dumps(
            {
                "policy": payload["_policy"],
                "n_corrections": len(corrections),
                "n_excluded": len(excluded),
                "excluded_ids": excluded,
                "metrics_before": before,
                "metrics_after": after,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    md = [
        "# 人工抽检结果",
        "",
        f"运行：`{run_dir.name}`　分歧 {len(sheet)} 条，人工已判 {len(human)} 条",
        "",
        "## 预审 vs 人工",
        "",
        f"- 确认预审意见：**{len(confirmed)}** 条",
        f"- 推翻预审意见：**{len(overridden)}** 条"
        + (f"（{', '.join(r['sample_id'] + '：' + r['pre_audit_verdict'] + ' → ' + r['human_verdict'] for r in overridden)}）" if overridden else ""),
        f"- 预审无法定论、由人工独立判断：**{len(independent)}** 条"
        + (f"（{', '.join(r['sample_id'] + ' → ' + r['human_verdict'] for r in independent)}）" if independent else ""),
        "",
        "## 判定口径",
        "",
        payload["_policy"]["note"],
        "",
        "## 标注修正",
        "",
        f"人工确认 {len(corrections)} 条原标注需要处理：改写 "
        f"{len(corrections) - len(excluded)} 条，按口径剔除 {len(excluded)} 条。",
        "",
        "| 样本 | 处理 | 依据 |",
        "| --- | --- | --- |",
    ]
    for sid, c in corrections.items():
        act = "剔除" if c["action"] == "exclude" else "改写标注"
        md.append(f"| `{sid}` | {act} | {c['effect']} |")

    md += ["", "## 指标：修正前 → 修正后", "",
           "| 指标 | 以原标注为准 | 人工抽检修正后 |", "| --- | --- | --- |"]
    for name, b, a in _rows(before, after):
        md.append(f"| {name} | {b} | {a} |")
    md += [
        "",
        "修正后指标以人工抽检确认的标注为准。二者都保留：前者可与 ProcessBench "
        "上的其他工作直接比较，后者才是评估器的真实表现。",
        "",
    ]
    (run_dir / "audit_summary.md").write_text("\n".join(md), encoding="utf-8")

    print(f"确认 {len(confirmed)} / 推翻 {len(overridden)} / 独立判断 {len(independent)}")
    print(f"修正 {len(corrections) - len(excluded)} 条，剔除 {len(excluded)} 条")
    print()
    for name, b, a in _rows(before, after):
        print(f"  {name:<18} {b}  →  {a}")
    print()
    print(f"写入 {run_dir / 'metrics_corrected.json'}")
    print(f"写入 {run_dir / 'audit_summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
