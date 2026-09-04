"""P6 消融的两条关键性质。

**一、解题时绝不能看到标准答案。** 看到了，这一轮的答案正确率就没有意义，
而且从结果上完全看不出来 —— 与 `test_no_label_leak.py` 守的是同一类问题。

**二、评分脚本必须能把「更严谨」和「只是更容易蒙对」区分开。** 这是 P6 存在的
全部理由。这里构造一个「答案正确率上升但过程成立率下降」的合成场景，验证
E9 比例确实随之上升 —— 如果这个判别力没了，P6 就退化成一张只会说
「Max Mode 准确率更高」的表。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from stepprobe.mcp_server import store, tools

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("p6_score", ROOT / "scripts" / "p6_score.py")
p6_score = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(p6_score)

_GOLD_TOKENS = ("gold_answer", "gold", "n_agree", "generators")


@pytest.mark.skipif(
    not (ROOT / "data" / "p6_problems.jsonl").exists(), reason="尚未构建 P6 题目集"
)
def test_p6_batch_never_leaks_the_gold_answer() -> None:
    batch = tools.p6_next_batch(n=5, tier="T2")
    assert batch["n"] > 0
    blob = json.dumps(batch["problems"], ensure_ascii=False)
    for token in _GOLD_TOKENS:
        assert token not in blob, f"P6 批次泄漏了 {token}"
    for p in batch["problems"]:
        assert set(p) <= store.P6_SAFE_KEYS


def test_p6_rejects_unknown_arm() -> None:
    assert "error" in tools.p6_next_batch(arm="turbo")
    assert "error" in tools.p6_record("r", "turbo", {})


def test_p6_record_requires_the_fields_scoring_needs() -> None:
    r = tools.p6_record("r", "max_on", {"problem_id": "x"})
    assert "error" in r and "steps" in r["error"] and "final_answer" in r["error"]


def _write(run: Path, name: str, rows: list[dict]) -> None:
    with (run / name).open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_scoring_separates_more_rigorous_from_merely_luckier(tmp_path: Path) -> None:
    """合成「答对更多但过程更差」的场景，E9 比例必须把它抓出来。"""
    data = tmp_path / "data"
    data.mkdir()
    gold = [
        {"id": f"p{i}", "problem": "q", "tier": "T2", "gold_answer": str(i), "n_agree": 1}
        for i in range(6)
    ]
    _write(data, "p6_problems.jsonl", gold)

    run = tmp_path / "runs" / "p6-x"
    run.mkdir(parents=True)
    # 关：3 题答对且过程都成立。开：5 题答对，但只有 2 题过程成立。
    _write(run, "solutions_max_off.jsonl", [
        {"problem_id": g["id"], "steps": ["s"], "final_answer": g["gold_answer"] if i < 3 else "999"}
        for i, g in enumerate(gold)
    ])
    _write(run, "solutions_max_on.jsonl", [
        {"problem_id": g["id"], "steps": ["s"], "final_answer": g["gold_answer"] if i < 5 else "999"}
        for i, g in enumerate(gold)
    ])
    _write(run, "verdicts.jsonl", [
        v
        for i, g in enumerate(gold)
        for v in (
            {"sample_id": f"max_off:{g['id']}", "process_valid": i < 3, "evidence": "x"},
            {"sample_id": f"max_on:{g['id']}", "process_valid": i < 2, "evidence": "x"},
        )
    ])

    p6_score.main(["--run", str(run), "--data-dir", str(data)])
    out = json.loads((run / "p6_comparison.json").read_text(encoding="utf-8"))

    off, on = out["metrics"]["max_off"], out["metrics"]["max_on"]
    assert on["answer_accuracy"]["value"] > off["answer_accuracy"]["value"]
    assert on["process_validity"]["value"] < off["process_validity"]["value"]
    # 判别信号：答对的题里过程不成立的比例显著上升
    assert on["e9_ratio_among_correct"]["value"] > off["e9_ratio_among_correct"]["value"]
    assert out["paired_tests"]["answer_correct"]["net"] == 2
    assert out["paired_tests"]["process_valid"]["net"] == -1


def test_unanswered_counts_as_wrong_not_as_missing(tmp_path: Path) -> None:
    """解不出来（final_answer=null）算答错，不能悄悄从分母里消失。"""
    assert p6_score._answer_correct(None, "5") is False
    assert p6_score._answer_correct("", "5") is False
    assert p6_score._answer_correct("5", "5") is True
