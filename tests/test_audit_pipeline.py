"""人工抽检落地流程的测试。

这里守的是一条**诚信约束**而非普通逻辑：修正后的指标必须真的来自人工结论，
不能让预审意见混进去冒充人工抽检。`apply_audit` 的校验
一旦失效，报告里的数字就会失去它宣称的来源，而且从结果上看不出来。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from stepprobe.schema import Label, Sample, Tier

import importlib.util

_SPEC = importlib.util.spec_from_file_location(
    "apply_audit", Path(__file__).resolve().parents[1] / "scripts" / "apply_audit.py"
)
apply_audit = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(apply_audit)

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "results" / "runs" / "run-20260903T141258Z"
CORRECTIONS = ROOT / "scripts" / "audit_corrections.json"


def _sample(sid: str, *, process_correct: bool, step: int | None, answer: bool) -> Sample:
    return Sample(
        id=sid,
        source="ProcessBench",
        tier=Tier.T1,
        problem="p",
        solution_steps=["a", "b", "c"],
        label=Label(
            process_correct=process_correct,
            first_error_step=step,
            answer_correct=answer,
        ),
    )


def test_relabel_rewrites_label_and_recomputes_class() -> None:
    """改写标注要连带改变 label_class —— 892 正是靠这一点从「无误」变成 E9。"""
    s = _sample("x", process_correct=True, step=None, answer=True)
    fixed, excluded = apply_audit.apply_corrections(
        [s], {"x": {"action": "relabel", "process_correct": False, "first_error_step": 2}}
    )
    assert excluded == []
    label = fixed[0].label
    assert label is not None
    assert (label.process_correct, label.first_error_step) == (False, 2)
    assert label.annotator == "human_audit"
    # 过程有误 + 答案正确 → E9，由 label_class 自动推出，不需要手写
    assert label.label_class.value == "e9"


def test_exclude_drops_sample_entirely() -> None:
    """criterion_gap 是剔除，不是改判 —— 剔除后它既不进分子也不进分母。"""
    keep = _sample("keep", process_correct=True, step=None, answer=True)
    drop = _sample("drop", process_correct=False, step=1, answer=True)
    fixed, excluded = apply_audit.apply_corrections(
        [keep, drop], {"drop": {"action": "exclude"}}
    )
    assert excluded == ["drop"]
    assert [s.id for s in fixed] == ["keep"]


def test_untouched_samples_pass_through_unchanged() -> None:
    s = _sample("x", process_correct=False, step=3, answer=False)
    fixed, excluded = apply_audit.apply_corrections([s], {})
    assert excluded == []
    assert fixed[0].label == s.label


@pytest.mark.skipif(not CORRECTIONS.exists(), reason="尚无修正文件")
def test_every_correction_is_backed_by_a_human_verdict() -> None:
    """核心约束：修正文件里的每一条，都必须在抽检表里有人工结论。

    否则就是拿预审意见改指标 —— 脚本会拒绝执行，这里把它钉死。
    """
    corrections = json.loads(CORRECTIONS.read_text(encoding="utf-8"))["corrections"]
    with (RUN_DIR / "audit_sheet.csv").open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    judged = {r["sample_id"] for r in rows if (r.get("human_verdict") or "").strip()}
    assert set(corrections) <= judged, f"无人工结论支撑：{sorted(set(corrections) - judged)}"


@pytest.mark.skipif(not CORRECTIONS.exists(), reason="尚无修正文件")
def test_corrections_match_the_human_verdicts_they_claim() -> None:
    """修正的方向必须与人工结论一致，不能反着来。

    evaluator_right → 原标注有问题 → relabel
    criterion_gap   → 双方都没错   → exclude
    evaluator_wrong → 标注正确     → 根本不该出现在修正文件里
    """
    corrections = json.loads(CORRECTIONS.read_text(encoding="utf-8"))["corrections"]
    with (RUN_DIR / "audit_sheet.csv").open(encoding="utf-8-sig", newline="") as fh:
        verdicts = {r["sample_id"]: r["human_verdict"] for r in csv.DictReader(fh)}

    expected = {"evaluator_right": "relabel", "criterion_gap": "exclude"}
    for sid, c in corrections.items():
        hv = verdicts[sid]
        assert hv in expected, f"{sid} 的人工结论是 {hv}，不应出现在修正文件中"
        assert c["action"] == expected[hv], f"{sid}：{hv} 应对应 {expected[hv]}"
