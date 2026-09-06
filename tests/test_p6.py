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

_SPEC2 = importlib.util.spec_from_file_location("p6_check", ROOT / "scripts" / "p6_check.py")
p6_check = importlib.util.module_from_spec(_SPEC2)
assert _SPEC2.loader is not None
_SPEC2.loader.exec_module(p6_check)

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


def _minimal_run(tmp_path: Path, verdicts: list[dict]) -> tuple[Path, Path]:
    """两题两臂的最小运行，verdicts 由调用方决定覆盖到哪。"""
    data = tmp_path / "data"
    data.mkdir()
    gold = [
        {"id": "p0", "problem": "q", "tier": "T1", "gold_answer": "1", "n_agree": 1},
        {"id": "p1", "problem": "q", "tier": "T4", "gold_answer": "2", "n_agree": 1},
    ]
    _write(data, "p6_problems.jsonl", gold)
    run = tmp_path / "runs" / "p6-y"
    run.mkdir(parents=True)
    for arm in ("max_off", "max_on"):
        _write(run, f"solutions_{arm}.jsonl", [
            {"problem_id": g["id"], "steps": ["s"], "final_answer": g["gold_answer"]} for g in gold
        ])
    _write(run, "verdicts.jsonl", verdicts)
    return run, data


def test_partial_run_declares_itself_incomplete(tmp_path: Path) -> None:
    """中途评分产出的报告必须自曝不完整。

    守的是一个**看不出来的错**：评估做到一半跑评分，得到的
    `p6_comparison.md` 与最终版格式完全一致，只是过程类指标仅覆盖已评估的
    那部分 —— 而评估通常按 tier 顺序推进，已评估的部分系统性偏易。这种报告
    一旦被引用，结论会朝「过程成立率很高」偏，且无从察觉。
    """
    run, data = _minimal_run(tmp_path, [
        {"sample_id": "max_off:p0", "process_valid": True},
    ])
    p6_score.main(["--run", str(run), "--data-dir", str(data)])
    out = json.loads((run / "p6_comparison.json").read_text(encoding="utf-8"))

    assert out["complete"] is False
    assert out["verdict_coverage"]["max_off"]["evaluated"] == 1
    assert out["verdict_coverage"]["max_on"]["evaluated"] == 0
    # 偏易必须体现在 tier 分布上，而不只是一个总数
    assert out["verdict_coverage"]["max_off"]["by_tier"] == {"T1": 1}
    md = (run / "p6_comparison.md").read_text(encoding="utf-8")
    assert "不可引用" in md


def test_complete_run_carries_no_warning(tmp_path: Path) -> None:
    run, data = _minimal_run(tmp_path, [
        {"sample_id": f"{arm}:{pid}", "process_valid": True}
        for arm in ("max_off", "max_on")
        for pid in ("p0", "p1")
    ])
    p6_score.main(["--run", str(run), "--data-dir", str(data)])
    out = json.loads((run / "p6_comparison.json").read_text(encoding="utf-8"))

    assert out["complete"] is True
    assert "不可引用" not in (run / "p6_comparison.md").read_text(encoding="utf-8")


def test_check_catches_the_drifts_that_scoring_would_swallow(tmp_path: Path) -> None:
    """体检脚本必须抓住三类静默漂移。

    这三类都不会让评分脚本报错，只会让某一臂的样本数悄悄少一截：
    忘写 arm 前缀、把评判落到没有解答的题上、同一题重复落盘。
    """
    run, data = _minimal_run(tmp_path, [
        {"sample_id": "p0", "process_valid": True},                    # 缺前缀
        {"sample_id": "max_off:p9", "process_valid": True},            # 题号不存在
        {"sample_id": "max_on:p1", "process_valid": True},
        {"sample_id": "max_on:p1", "process_valid": True},             # 重复
    ])
    rc = p6_check.main(["--run", str(run), "--data-dir", str(data)])
    assert rc == 1


def test_check_passes_on_a_clean_run(tmp_path: Path) -> None:
    run, data = _minimal_run(tmp_path, [
        {"sample_id": f"{arm}:{pid}", "process_valid": True}
        for arm in ("max_off", "max_on")
        for pid in ("p0", "p1")
    ])
    assert p6_check.main(["--run", str(run), "--data-dir", str(data)]) == 0
