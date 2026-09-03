"""MCP 工具的纯函数实现。

与 MCP 协议解耦：这里是普通 Python 函数，`__main__.py` 只负责把它们注册成
MCP 工具。这样单元测试不需要起 MCP 服务，也能完整覆盖工具逻辑。
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict

from ..checkers.answer import check_answer, check_format
from ..checkers.segment import to_steps
from ..checkers.symbolic import check_steps
from ..metrics.core import compute, stability
from ..schema import LabelClass, Sample, Verdict
from . import store


# ---------------------------------------------------------------------------
# dataset.next_batch
# ---------------------------------------------------------------------------

def dataset_next_batch(
    n: int = 20,
    tier: str | None = None,
    label_class: str | None = None,
    run_id: str | None = None,
    seed: int | None = None,
    include_labels: bool = False,
) -> dict:
    """取一批待评估样本。

    **默认剥离 label 字段。** 一旦 ground truth 进入模型上下文，本次验证即作废
    （见 skills/validate）。`include_labels=True` 仅供调试，且会在返回值里显式
    标记 `labels_included`，让下游能识别出这批结果不可用于验证。
    """
    samples = store.load_samples()

    pool = [s for s in samples if tier is None or s.tier.value == tier]
    if label_class:
        pool = [
            s for s in pool if s.label is not None and s.label.label_class.value == label_class
        ]

    if not pool:
        return {"error": f"没有匹配的样本（tier={tier}, label_class={label_class}）"}

    run_id = run_id or store.new_run_id()
    already = set(store.read_batch(run_id)["sample_ids"]) if _has_batch(run_id) else set()
    fresh = [s for s in pool if s.id not in already]

    rng = random.Random(seed if seed is not None else hash(run_id) & 0xFFFFFFFF)
    picked = fresh[:n] if len(fresh) <= n else rng.sample(fresh, n)
    picked = sorted(picked, key=lambda s: s.id)

    store.write_batch(
        run_id,
        sorted(already | {s.id for s in picked}),
        {"tier": tier, "label_class": label_class, "labels_included": include_labels},
    )

    payload = [
        (s if include_labels else s.without_label()).model_dump(exclude_none=True)
        for s in picked
    ]
    return {
        "run_id": run_id,
        "n": len(payload),
        "remaining": len(fresh) - len(payload),
        "labels_included": include_labels,
        "samples": payload,
    }


def _has_batch(run_id: str) -> bool:
    return (store.run_dir(run_id) / "batch.json").exists()


# ---------------------------------------------------------------------------
# solution.segment
# ---------------------------------------------------------------------------

def solution_segment(text: str | list[str]) -> dict:
    """把解答切分为 1-based 编号的步骤。

    传入 list 时原样保留不重切 —— 公开数据集的步号必须与标注对齐。
    """
    steps = to_steps(text)
    return {
        "n_steps": len(steps),
        "resegmented": not isinstance(text, list),
        "steps": [s.model_dump() for s in steps],
    }


# ---------------------------------------------------------------------------
# check.answer / check.format / check.step_symbolic
# ---------------------------------------------------------------------------

def check_answer_tool(pred: str, gold: str) -> dict:
    r = check_answer(pred, gold)
    return {
        "verdict": r.verdict.value,
        "correct": r.correct,
        "decided_by": r.level,
        "detail": r.detail,
        "normalized_pred": r.normalized_pred,
        "normalized_gold": r.normalized_gold,
    }


def check_format_tool(answer: str, requirements: dict | None = None) -> dict:
    r = check_format(answer, requirements)
    return {"ok": r.ok, "violations": r.violations}


def check_step_symbolic(steps: list[str]) -> dict:
    """逐步做确定性校验。

    返回每步的 FALSE / UNKNOWN / TRUE。调用方（Skill）只需对 UNKNOWN 的步骤
    做语义审查 —— 这是省下模型轮次的关键。

    注意 TRUE **不代表整条链成立**：每步算对不等于论证成立，E9 正是这么产生的。
    """
    checks = check_steps(steps)
    first_false = next(
        (i for i, c in enumerate(checks, 1) if c.verdict.value == "FALSE"), None
    )
    return {
        "n_steps": len(checks),
        "first_false_step": first_false,
        "needs_llm_review": [
            i for i, c in enumerate(checks, 1) if c.verdict.value == "UNKNOWN"
        ],
        "steps": [
            {
                "index": i,
                "verdict": c.verdict.value,
                "detail": c.detail,
                "equations": [
                    {"lhs": e.lhs, "rhs": e.rhs, "verdict": e.verdict.value} for e in c.equations
                ],
            }
            for i, c in enumerate(checks, 1)
        ],
        "note": "TRUE 不代表整条链成立；E9 需在全局复核阶段判定",
    }


# ---------------------------------------------------------------------------
# verdict.record
# ---------------------------------------------------------------------------

def verdict_record(run_id: str, verdict: dict) -> dict:
    """记录一条评判结果。

    走 Verdict 模型校验 —— 判错却没填 evidence 会在这里被拒绝，
    而不是等到写报告时才发现证据是空的。
    """
    try:
        v = Verdict.model_validate(verdict)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"评判结果不合法：{exc}"}

    resolved = store.resolve_run(run_id)
    total = store.append_verdict(resolved, v)
    return {"ok": True, "run_id": resolved, "total_recorded": total}


# ---------------------------------------------------------------------------
# metrics.compute
# ---------------------------------------------------------------------------

def metrics_compute(run_id: str | None = None) -> dict:
    """把评判结果与人工标注对齐，算出全部核心指标。

    ground truth 在这一步才从样本集读取 —— 评估阶段发出的是剥离标注的副本。
    """
    resolved = store.resolve_run(run_id)
    verdicts = store.read_verdicts(resolved)
    if not verdicts:
        return {"error": f"运行 {resolved} 尚无任何评判结果"}

    samples = store.load_samples()
    report = compute(samples, verdicts).to_dict()
    report["stability"] = stability(verdicts)
    report["run_id"] = resolved

    store.write_json(resolved, "metrics.json", report)
    return report


# ---------------------------------------------------------------------------
# report.export
# ---------------------------------------------------------------------------

def report_export(run_id: str | None = None, kind: str = "summary") -> dict:
    """导出结果表格或人工抽检清单。

    kind="human_audit" 生成误报样本的待检清单 —— 任务书要求的人工抽检产物。
    """
    resolved = store.resolve_run(run_id)
    verdicts = store.read_verdicts(resolved)
    samples = {s.id: s for s in store.load_samples()}

    if kind == "human_audit":
        rows = _audit_rows(verdicts, samples)
        path = store.write_json(resolved, "human_audit.json", {"rows": rows})
        _write_audit_csv(resolved, rows)
        by_kind = Counter(r["disagreement_kind"] for r in rows)
        return {
            "run_id": resolved,
            "kind": kind,
            "n_rows": len(rows),
            "by_disagreement_kind": dict(by_kind),
            "path": str(path),
            "csv": str(store.run_dir(resolved) / "audit_sheet.csv"),
            "note": (
                "请人工在 human_verdict 列填写：评估器正确 / 评估器错误 / 标注有误。"
                "0 行说明评估器与人工标注完全一致，属正常结果，不是导出失败。"
            ),
        }

    rows = [
        {
            "sample_id": v.sample_id,
            "tier": samples[v.sample_id].tier.value if v.sample_id in samples else None,
            "process_valid": v.process_valid,
            "first_error_step": v.first_error_step,
            "error_type": v.error_type,
            "layer": v.layer,
        }
        for v in verdicts
    ]
    path = store.write_json(resolved, "summary.json", {"rows": rows})
    dist = Counter(v.error_type for v in verdicts if v.error_type)
    return {
        "run_id": resolved,
        "kind": kind,
        "n_rows": len(rows),
        "error_type_distribution": dict(dist),
        "path": str(path),
    }


def _disagreement_kind(v: Verdict, s: Sample) -> str | None:
    """判断评估器与人工标注的分歧类型。一致则返回 None。"""
    label = s.label
    assert label is not None

    if label.process_correct and not v.process_valid:
        return "false_positive"      # 标注无误，评估器判错 → 误报候选
    if not label.process_correct and v.process_valid:
        return "missed"              # 标注有误，评估器漏过 → 漏报
    if not label.process_correct and not v.process_valid:
        if v.first_error_step != label.first_error_step:
            return "mislocalized"    # 都判有误，但步号不同 → 定位偏差
    return None


def _audit_rows(verdicts: list[Verdict], samples: dict[str, Sample]) -> list[dict]:
    """人工抽检清单：评估器与人工标注不一致的全部样本。

    覆盖三类分歧，而不只是误报：

      false_positive  标注无误、评估器判错 —— 任务书要求抽检的核心类别
      missed          标注有误、评估器漏过
      mislocalized    都判有误但步号不同

    只收误报会有个实际问题：误报率低到 0 时清单为空，人工抽检就无从做起
    （首轮 20 条实跑正是如此，误报率 0/8）。而漏报与定位偏差同样需要人工
    确认「究竟是评估器错了，还是原标注有问题」—— 这两类反而更常出现。

    同一样本多次评判时只取最后一次，避免稳定性重复评估把清单撑成 N 倍。
    """
    latest: dict[str, Verdict] = {}
    for v in verdicts:
        latest[v.sample_id] = v

    rows = []
    for sid, v in sorted(latest.items()):
        s = samples.get(sid)
        if s is None or s.label is None:
            continue
        kind = _disagreement_kind(v, s)
        if kind is None:
            continue

        step_idx = v.first_error_step or s.label.first_error_step
        rows.append(
            {
                "disagreement_kind": kind,
                "sample_id": sid,
                "tier": s.tier.value,
                "gold_process": "无误" if s.label.process_correct else "有误",
                "gold_step": s.label.first_error_step,
                "evaluator_process": "无误" if v.process_valid else "有误",
                "evaluator_step": v.first_error_step,
                "evaluator_error_type": v.error_type,
                "evidence": v.evidence,
                "step_content": (
                    s.solution_steps[step_idx - 1][:500]
                    if step_idx and step_idx <= s.n_steps
                    else ""
                ),
                "human_verdict": "",   # 待人工填：评估器正确 / 评估器错误 / 标注有误
                "human_note": "",
            }
        )
    return rows


def _write_audit_csv(run_id: str, rows: list[dict]) -> None:
    import csv

    path = store.run_dir(run_id) / "audit_sheet.csv"
    fields = [
        "disagreement_kind", "sample_id", "tier",
        "gold_process", "gold_step",
        "evaluator_process", "evaluator_step", "evaluator_error_type",
        "evidence", "step_content", "human_verdict", "human_note",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# 工具清单（供 __main__ 注册与测试引用）
# ---------------------------------------------------------------------------

TOOLS = {
    "dataset_next_batch": dataset_next_batch,
    "solution_segment": solution_segment,
    "check_answer": check_answer_tool,
    "check_format": check_format_tool,
    "check_step_symbolic": check_step_symbolic,
    "verdict_record": verdict_record,
    "metrics_compute": metrics_compute,
    "report_export": report_export,
}
