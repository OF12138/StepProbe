"""P1 数据管线测试。

重点覆盖两处最容易悄悄出错、且出错不报警的地方：
  1. 0-based → 1-based 步号转换（差一位会让定位准确率被系统性低估）
  2. E9 类别的识别与全量取用（漏了它 E9 指标就没有样本支撑）
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stepprobe.data.loaders import _parse_listish, _to_first_error_step
from stepprobe.data.sampling import classify, stratified_sample, summarize
from stepprobe.schema import Label, LabelClass, Sample, Tier, Verdict


# --------------------------------------------------------------------------
# 步号转换
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (-1, None),  # ProcessBench 约定：-1 = 全部步骤正确
        (0, 1),      # 0-based 第 0 步 → 1-based 第 1 步
        (1, 2),
        (6, 7),
    ],
)
def test_first_error_step_conversion(raw: int, expected: int | None) -> None:
    assert _to_first_error_step(raw) == expected


# --------------------------------------------------------------------------
# Label 一致性约束
# --------------------------------------------------------------------------

def test_label_rejects_correct_process_with_error_step() -> None:
    with pytest.raises(ValidationError):
        Label(process_correct=True, first_error_step=3)


def test_label_rejects_faulty_process_without_error_step() -> None:
    with pytest.raises(ValidationError):
        Label(process_correct=False, first_error_step=None)


@pytest.mark.parametrize(
    ("process_correct", "first_error", "answer_correct", "expected"),
    [
        (False, 2, False, LabelClass.FAULTY_WRONG),
        (True, None, True, LabelClass.CLEAN_CORRECT),
        (False, 2, True, LabelClass.E9),   # 答案对但过程错
        (True, None, False, LabelClass.OTHER),
    ],
)
def test_label_class(process_correct, first_error, answer_correct, expected) -> None:
    label = Label(
        process_correct=process_correct,
        first_error_step=first_error,
        answer_correct=answer_correct,
    )
    assert label.label_class is expected


# --------------------------------------------------------------------------
# 标注剥离 —— 评估时泄漏 ground truth 会让整个验证作废
# --------------------------------------------------------------------------

def _sample(sid: str, tier: Tier, process_correct: bool, answer_correct: bool) -> Sample:
    return Sample(
        id=sid,
        source="ProcessBench",
        tier=tier,
        problem="题目",
        solution_steps=["第一步", "第二步", "第三步"],
        label=Label(
            process_correct=process_correct,
            first_error_step=None if process_correct else 2,
            answer_correct=answer_correct,
        ),
    )


def test_without_label_strips_ground_truth() -> None:
    s = _sample("x", Tier.T1, process_correct=False, answer_correct=False)
    stripped = s.without_label()

    assert stripped.label is None
    assert s.label is not None, "原样本不应被就地修改"
    assert "first_error_step" not in stripped.model_dump_json()


# --------------------------------------------------------------------------
# 分层抽样
# --------------------------------------------------------------------------

def _pool() -> list[Sample]:
    """构造一个 E9 稀缺的样本池，模拟 ProcessBench 的真实分布。"""
    pool: list[Sample] = []
    for tier in (Tier.T1, Tier.T2):
        for i in range(80):
            pool.append(_sample(f"{tier.value}-fw-{i:03d}", tier, False, False))
        for i in range(80):
            pool.append(_sample(f"{tier.value}-cc-{i:03d}", tier, True, True))
        for i in range(3):  # E9 极少
            pool.append(_sample(f"{tier.value}-e9-{i:03d}", tier, False, True))
    return pool


def test_stratified_sample_caps_common_classes() -> None:
    picked = stratified_sample(_pool(), per_cell=50, seed=42)
    stats = summarize(picked)

    assert stats["by_cell"]["T1/faulty_wrong"] == 50
    assert stats["by_cell"]["T1/clean_correct"] == 50
    assert stats["by_cell"]["T2/faulty_wrong"] == 50


def test_stratified_sample_takes_all_e9() -> None:
    """E9 必须全量取用 —— 按比例抽样会让它少到无法支撑结论。"""
    picked = stratified_sample(_pool(), per_cell=50, seed=42)
    stats = summarize(picked)

    assert stats["by_cell"]["T1/e9"] == 3
    assert stats["by_cell"]["T2/e9"] == 3
    assert stats["by_class"]["e9"] == 6


def test_stratified_sample_is_reproducible() -> None:
    ids_a = [s.id for s in stratified_sample(_pool(), per_cell=20, seed=7)]
    ids_b = [s.id for s in stratified_sample(_pool(), per_cell=20, seed=7)]
    ids_c = [s.id for s in stratified_sample(_pool(), per_cell=20, seed=8)]

    assert ids_a == ids_b, "同一种子必须给出相同抽样"
    assert ids_a != ids_c, "不同种子应给出不同抽样"


def test_stratified_sample_never_duplicates() -> None:
    picked = stratified_sample(_pool(), per_cell=50, seed=42)
    ids = [s.id for s in picked]
    assert len(ids) == len(set(ids)), "重复采样会让置信区间失真"


def test_small_bucket_taken_whole() -> None:
    """单元内样本不足时全部取用，不做重复采样。"""
    pool = [_sample(f"t1-fw-{i}", Tier.T1, False, False) for i in range(5)]
    picked = stratified_sample(pool, per_cell=50, seed=1)
    assert len(picked) == 5


def test_classify_skips_unlabeled() -> None:
    pool = _pool() + [
        Sample(
            id="unlabeled-1",
            source="cn_custom",
            tier=Tier.T1,
            problem="题目",
            solution_steps=["一步"],
        )
    ]
    total = sum(len(v) for v in classify(pool).values())
    assert total == len(pool) - 1


# --------------------------------------------------------------------------
# Verdict 举证约束
# --------------------------------------------------------------------------

def test_verdict_requires_evidence_when_faulty() -> None:
    with pytest.raises(ValidationError):
        Verdict(sample_id="x", process_valid=False, first_error_step=2, evidence="  ")


def test_verdict_allows_empty_evidence_when_valid() -> None:
    v = Verdict(sample_id="x", process_valid=True)
    assert v.evidence is None


# --------------------------------------------------------------------------
# DeltaBench 字段解析（列表被存成了字符串）
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[1, 2, 3]", [1, 2, 3]),
        ("['a', 'b']", ["a", "b"]),
        ([4, 5], [4, 5]),
        ("", []),
        ("[]", []),
        ("None", []),
        ("不是列表", []),
    ],
)
def test_parse_listish(raw, expected) -> None:
    assert _parse_listish(raw) == expected
