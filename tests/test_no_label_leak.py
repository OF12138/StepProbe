"""标注泄漏专项测试。

设这个独立文件的原因：泄漏这类 bug 会**静默**作废整轮验证 —— 不报错、
不崩溃，指标照样算得出来，只是全都不可信。

而且它已经真实发生过一次：loader 把 ProcessBench 的 `raw_label`（0-based
首错步号）塞进了 `meta`，`without_label()` 只清 `label` 不清 `meta`，
ground truth 就这么发了出去。当时 test_mcp_tools.py 里的夹具 `meta` 是空的，
**测试全绿而真实数据在泄漏**。

所以这里坚持两条：
  1. 用**真实数据集**测（data/sample_set.jsonl 存在时），不只用夹具
  2. 白名单校验，而非逐个字段黑名单 —— 新增 meta 字段默认视为泄漏
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stepprobe.schema import SAFE_META_KEYS, Label, Sample, Tier

REAL_DATA = Path("data/sample_set.jsonl")

#: 带值出现即视为泄漏的字段名。
#: raw_label 是踩过的坑：它等于 first_error_step - 1。
#:
#: 不含 "label" 本身 —— `model_dump_json()` 会保留 `"label": null` 这个键名，
#: 值是 null 并非泄漏。键名的存在与否用结构化断言检查，比子串匹配可靠。
LEAK_TOKENS = [
    "first_error_step",
    "process_correct",
    "answer_correct",
    "raw_label",
    "annotator",
]


def assert_no_leak(sample: Sample, context: str = "") -> None:
    """结构化断言：剥离后的样本不得携带任何 ground truth。"""
    stripped = sample.without_label()
    prefix = f"{context}{sample.id}: " if context or sample.id else ""

    assert stripped.label is None, f"{prefix}label 未剥离"
    extra = set(stripped.meta) - SAFE_META_KEYS
    assert not extra, f"{prefix}meta 含白名单外字段 {extra}"

    blob = stripped.model_dump_json()
    for token in LEAK_TOKENS:
        assert token not in blob, f"{prefix}序列化结果含泄漏字段 {token}"


def _sample_with_dirty_meta() -> Sample:
    return Sample(
        id="x-1",
        source="ProcessBench",
        tier=Tier.T2,
        problem="题目",
        solution_steps=["一步", "二步"],
        label=Label(process_correct=False, first_error_step=2, answer_correct=True),
        meta={
            "original_id": "math-1",
            "split": "math",
            "raw_label": 1,          # ← 就是这个字段泄漏过
            "generator": "SomeModel",
            "future_field": "谁知道以后会加什么",
        },
    )


def test_without_label_strips_raw_label() -> None:
    stripped = _sample_with_dirty_meta().without_label()
    assert "raw_label" not in stripped.meta, "raw_label 等于 first_error_step-1，必须剥离"


def test_without_label_uses_whitelist_not_blacklist() -> None:
    """未知的新增 meta 字段必须默认剥离，而不是默认放行。"""
    stripped = _sample_with_dirty_meta().without_label()
    assert "future_field" not in stripped.meta
    assert set(stripped.meta) <= SAFE_META_KEYS


def test_without_label_keeps_safe_meta() -> None:
    stripped = _sample_with_dirty_meta().without_label()
    assert stripped.meta.get("original_id") == "math-1"
    assert stripped.meta.get("split") == "math"


def test_without_label_does_not_mutate_original() -> None:
    s = _sample_with_dirty_meta()
    s.without_label()
    assert s.label is not None
    assert s.meta["raw_label"] == 1


def test_serialized_form_has_no_leak_tokens() -> None:
    assert_no_leak(_sample_with_dirty_meta())


# ---------------------------------------------------------------------------
# 真实数据 —— 夹具测不出来的那一类问题
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not REAL_DATA.exists(), reason="需先运行 stepprobe.data.build")
def test_real_dataset_has_no_leak_after_stripping() -> None:
    """对真实样本集全量做剥离，逐条检查泄漏字段。"""
    with REAL_DATA.open(encoding="utf-8") as fh:
        samples = [Sample.model_validate_json(ln) for ln in fh if ln.strip()]

    assert samples, "样本集为空"
    for s in samples:
        assert_no_leak(s, context="真实数据 ")


@pytest.mark.skipif(not REAL_DATA.exists(), reason="需先运行 stepprobe.data.build")
def test_real_batch_from_mcp_tool_has_no_leak(monkeypatch) -> None:
    """走 MCP 工具的真实路径 —— 这是模型实际拿到的数据。"""
    monkeypatch.setenv("STEPPROBE_DATA_DIR", str(REAL_DATA.parent.resolve()))
    from stepprobe.mcp_server import store, tools

    store._CACHE.clear()
    try:
        for tier in ("T1", "T2", "T3", "T4"):
            batch = tools.dataset_next_batch(n=15, tier=tier)
            blob = json.dumps(batch, ensure_ascii=False)
            for token in LEAK_TOKENS:
                assert token not in blob, f"{tier} 批次泄漏 {token}"
    finally:
        store._CACHE.clear()
