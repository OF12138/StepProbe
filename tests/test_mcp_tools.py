"""P3 MCP 工具测试。

最关键的一条：`dataset.next_batch` **绝不能**泄漏 ground truth。
一旦标注进了模型上下文，整轮有效性验证就作废了 —— 而且不会报错。
"""

from __future__ import annotations

import json

import pytest

from stepprobe.mcp_server import store, tools
from stepprobe.schema import Label, Sample, Tier


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """构造一个隔离的数据 / 结果目录，避免污染真实产物。"""
    data, results = tmp_path / "data", tmp_path / "results"
    data.mkdir()
    results.mkdir()

    # 三个标注类别各占三分之一，保证按类别筛选时都能取到样本
    combos = [
        (False, False),  # faulty_wrong
        (True, True),    # clean_correct
        (False, True),   # e9：过程有误但答案正确
    ]
    samples = []
    for tier in (Tier.T1, Tier.T2):
        for i in range(6):
            process_correct, answer_correct = combos[i % 3]
            samples.append(
                Sample(
                    id=f"{tier.value}-{i:02d}",
                    source="ProcessBench",
                    tier=tier,
                    problem=f"题目 {i}",
                    solution_steps=["2 + 3 = 5", "5 * 2 = 11", "结论成立"],
                    label=Label(
                        process_correct=process_correct,
                        first_error_step=None if process_correct else 2,
                        answer_correct=answer_correct,
                    ),
                )
            )
    with (data / "sample_set.jsonl").open("w", encoding="utf-8") as fh:
        for s in samples:
            fh.write(s.model_dump_json() + "\n")

    monkeypatch.setenv("STEPPROBE_DATA_DIR", str(data))
    monkeypatch.setenv("STEPPROBE_RESULTS_DIR", str(results))
    store._CACHE.clear()
    yield tmp_path
    store._CACHE.clear()


# ---------------------------------------------------------------------------
# dataset.next_batch —— 标注泄漏是红线
# ---------------------------------------------------------------------------

def test_next_batch_strips_labels(workspace) -> None:
    batch = tools.dataset_next_batch(n=4)
    assert batch["n"] == 4
    assert batch["labels_included"] is False

    blob = json.dumps(batch, ensure_ascii=False)
    for leak in ("first_error_step", "process_correct", "answer_correct", '"label"'):
        assert leak not in blob, f"批次数据泄漏了 ground truth 字段：{leak}"


def test_next_batch_filters_by_tier(workspace) -> None:
    batch = tools.dataset_next_batch(n=10, tier="T1")
    assert all(s["tier"] == "T1" for s in batch["samples"])


def test_next_batch_filters_by_label_class(workspace) -> None:
    batch = tools.dataset_next_batch(n=10, label_class="clean_correct")
    assert batch["n"] > 0
    assert "first_error_step" not in json.dumps(batch)


def test_next_batch_does_not_repeat_within_run(workspace) -> None:
    first = tools.dataset_next_batch(n=4)
    run_id = first["run_id"]
    second = tools.dataset_next_batch(n=4, run_id=run_id)

    ids_a = {s["id"] for s in first["samples"]}
    ids_b = {s["id"] for s in second["samples"]}
    assert not (ids_a & ids_b), "同一运行内不应重复发放样本"


def test_next_batch_reports_no_match(workspace) -> None:
    assert "error" in tools.dataset_next_batch(n=5, tier="T9")


def test_include_labels_is_flagged(workspace) -> None:
    """调试用的 include_labels 必须显式标记，让下游知道这批不可用于验证。"""
    batch = tools.dataset_next_batch(n=6, include_labels=True)
    assert batch["labels_included"] is True
    blob = json.dumps(batch)
    assert '"label"' in blob and "process_correct" in blob


# ---------------------------------------------------------------------------
# 校验类工具
# ---------------------------------------------------------------------------

def test_check_answer_tool(workspace) -> None:
    r = tools.check_answer_tool(r"\frac{1}{2}", "0.5")
    assert r["correct"] is True
    assert r["decided_by"] in {"normalize", "sympy", "numeric"}
    assert tools.check_answer_tool("1/2", "1/3")["correct"] is False


def test_check_step_symbolic_flags_unknown_steps(workspace) -> None:
    r = tools.check_step_symbolic(["2 + 3 = 5", "5 * 2 = 11", "结论成立"])
    assert r["first_false_step"] == 2
    assert 3 in r["needs_llm_review"], "纯文字步骤必须交给 LLM 审查"


def test_solution_segment_keeps_list_intact(workspace) -> None:
    r = tools.solution_segment(["一步。含句号。", "第二步"])
    assert r["n_steps"] == 2
    assert r["resegmented"] is False


# ---------------------------------------------------------------------------
# verdict.record —— 判错必须举证
# ---------------------------------------------------------------------------

def test_verdict_record_rejects_missing_evidence(workspace) -> None:
    run_id = tools.dataset_next_batch(n=2)["run_id"]
    r = tools.verdict_record(
        run_id, {"sample_id": "T1-00", "process_valid": False, "first_error_step": 2}
    )
    assert r["ok"] is False
    assert "evidence" in r["error"] or "不合法" in r["error"]


def test_verdict_record_accepts_valid(workspace) -> None:
    run_id = tools.dataset_next_batch(n=2)["run_id"]
    r = tools.verdict_record(
        run_id,
        {
            "sample_id": "T1-00",
            "process_valid": False,
            "first_error_step": 2,
            "error_type": "E3",
            "evidence": "5 * 2 = 11 不成立",
            "layer": "L1",
        },
    )
    assert r["ok"] is True
    assert r["total_recorded"] == 1


# ---------------------------------------------------------------------------
# metrics —— 严口径
# ---------------------------------------------------------------------------

def _record_all(run_id: str, *, perfect: bool) -> None:
    for s in store.load_samples():
        label = s.label
        assert label is not None
        if perfect:
            faulty = not label.process_correct
            payload = {
                "sample_id": s.id,
                "process_valid": not faulty,
                "first_error_step": label.first_error_step,
                "error_type": "E3" if faulty else None,
                "evidence": "命中" if faulty else None,
            }
        else:
            payload = {"sample_id": s.id, "process_valid": True}
        tools.verdict_record(run_id, payload)


def test_metrics_perfect_evaluator(workspace) -> None:
    run_id = tools.dataset_next_batch(n=1)["run_id"]
    _record_all(run_id, perfect=True)
    m = tools.metrics_compute(run_id)

    assert m["localization"]["detection_rate"]["value"] == 1.0
    assert m["localization"]["exact_localization_acc"]["value"] == 1.0
    assert m["false_positive_rate"]["value"] == 0.0


def test_metrics_all_pass_evaluator(workspace) -> None:
    """全判「无误」的评估器：误报率 0，但检出率也是 0 —— 没有区分力。"""
    run_id = tools.dataset_next_batch(n=1)["run_id"]
    _record_all(run_id, perfect=False)
    m = tools.metrics_compute(run_id)

    assert m["localization"]["detection_rate"]["value"] == 0.0
    assert m["false_positive_rate"]["value"] == 0.0


def test_localization_denominator_counts_misses(workspace) -> None:
    """未检出必须记为定位失败，不能只在检出样本里算准确率。"""
    run_id = tools.dataset_next_batch(n=1)["run_id"]
    samples = store.load_samples()
    faulty = [s for s in samples if s.label and not s.label.process_correct]

    # 只对第一条有误样本给出正确定位，其余全部漏报
    tools.verdict_record(
        run_id,
        {
            "sample_id": faulty[0].id,
            "process_valid": False,
            "first_error_step": faulty[0].label.first_error_step,
            "evidence": "命中",
        },
    )
    for s in faulty[1:]:
        tools.verdict_record(run_id, {"sample_id": s.id, "process_valid": True})

    m = tools.metrics_compute(run_id)
    exact = m["localization"]["exact_localization_acc"]
    assert exact["n"] == len(faulty), "分母必须是全部有误样本"
    assert exact["value"] == pytest.approx(1 / len(faulty))


def test_metrics_includes_confidence_interval(workspace) -> None:
    run_id = tools.dataset_next_batch(n=1)["run_id"]
    _record_all(run_id, perfect=True)
    m = tools.metrics_compute(run_id)
    ci = m["localization"]["detection_rate"]["ci95"]
    assert ci is not None and len(ci) == 2 and ci[0] <= ci[1]


def test_metrics_without_verdicts(workspace) -> None:
    run_id = tools.dataset_next_batch(n=2)["run_id"]
    assert "error" in tools.metrics_compute(run_id)


# ---------------------------------------------------------------------------
# report.export
# ---------------------------------------------------------------------------

def test_report_human_audit_sheet(workspace) -> None:
    """人工抽检清单只收「评估器判错、标注为无误」的样本。"""
    run_id = tools.dataset_next_batch(n=1)["run_id"]
    clean = [s for s in store.load_samples() if s.label and s.label.process_correct]

    tools.verdict_record(
        run_id,
        {
            "sample_id": clean[0].id,
            "process_valid": False,
            "first_error_step": 2,
            "error_type": "E3",
            "evidence": "疑似算错",
        },
    )
    r = tools.report_export(run_id, kind="human_audit")

    assert r["n_rows"] == 1
    csv_text = (store.run_dir(run_id) / "audit_sheet.csv").read_text(encoding="utf-8-sig")
    assert "human_verdict" in csv_text
    assert clean[0].id in csv_text


def test_report_summary(workspace) -> None:
    run_id = tools.dataset_next_batch(n=1)["run_id"]
    tools.verdict_record(
        run_id,
        {"sample_id": "T1-00", "process_valid": False, "first_error_step": 2,
         "error_type": "E3", "evidence": "x"},
    )
    r = tools.report_export(run_id, kind="summary")
    assert r["n_rows"] == 1
    assert r["error_type_distribution"] == {"E3": 1}


def test_resolve_latest_run(workspace) -> None:
    run_id = tools.dataset_next_batch(n=2)["run_id"]
    assert store.resolve_run("latest") == run_id
    assert store.resolve_run(None) == run_id


# ---------------------------------------------------------------------------
# 稳定性
# ---------------------------------------------------------------------------

def test_stability_tracks_repeats(workspace) -> None:
    run_id = tools.dataset_next_batch(n=1)["run_id"]
    for step in (2, 2, 3):
        tools.verdict_record(
            run_id,
            {"sample_id": "T1-00", "process_valid": False, "first_error_step": step,
             "error_type": "E3", "evidence": "x"},
        )
    m = tools.metrics_compute(run_id)
    assert m["stability"]["n_repeated_samples"] == 1
    assert m["stability"]["step_mode_ratio"] == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# 首轮实跑（20 条）暴露的问题，回归用例
# ---------------------------------------------------------------------------

def test_audit_sheet_covers_all_disagreement_kinds(workspace) -> None:
    """抽检表要覆盖三类分歧，不能只收误报。

    首轮实跑误报率是 0/8，只收误报会导致清单为空、人工抽检无从下手，
    而漏报与定位偏差同样需要人工确认「是评估器错了还是原标注有问题」。
    """
    run_id = tools.dataset_next_batch(n=1)["run_id"]
    samples = store.load_samples()
    clean = next(s for s in samples if s.label and s.label.process_correct)
    faulty = [s for s in samples if s.label and not s.label.process_correct]

    # 误报：标注无误，评估器判错
    tools.verdict_record(run_id, {
        "sample_id": clean.id, "process_valid": False,
        "first_error_step": 2, "error_type": "E3", "evidence": "疑似算错",
    })
    # 漏报：标注有误，评估器判无误
    tools.verdict_record(run_id, {"sample_id": faulty[0].id, "process_valid": True})
    # 定位偏差：都判有误但步号不同（标注是 2）
    tools.verdict_record(run_id, {
        "sample_id": faulty[1].id, "process_valid": False,
        "first_error_step": 3, "error_type": "E5", "evidence": "跳步",
    })
    # 完全一致：不应进入清单
    tools.verdict_record(run_id, {
        "sample_id": faulty[2].id, "process_valid": False,
        "first_error_step": 2, "error_type": "E3", "evidence": "命中",
    })

    r = tools.report_export(run_id, kind="human_audit")
    assert r["by_disagreement_kind"] == {
        "false_positive": 1, "missed": 1, "mislocalized": 1
    }, r["by_disagreement_kind"]
    assert r["n_rows"] == 3, "判断一致的样本不该进抽检表"


def test_audit_sheet_dedups_repeated_verdicts(workspace) -> None:
    """稳定性重复评估不应把抽检表撑成 N 倍。"""
    run_id = tools.dataset_next_batch(n=1)["run_id"]
    clean = next(s for s in store.load_samples() if s.label and s.label.process_correct)
    for _ in range(5):
        tools.verdict_record(run_id, {
            "sample_id": clean.id, "process_valid": False,
            "first_error_step": 2, "error_type": "E3", "evidence": "疑似算错",
        })
    assert tools.report_export(run_id, kind="human_audit")["n_rows"] == 1


def test_stability_rejects_identical_resubmissions(workspace) -> None:
    """机械重复提交必须被识别，不能算出 1.0 的假满分。

    首轮实跑就是把同一条 verdict 原样重提 5 次，得到 step_mode_ratio=1.0 ——
    这个 1.0 只证明「同一份 JSON 存五遍还是它自己」。
    """
    run_id = tools.dataset_next_batch(n=1)["run_id"]
    payload = {
        "sample_id": "T1-00", "process_valid": False, "first_error_step": 2,
        "error_type": "E3", "evidence": "完全相同的证据",
    }
    for _ in range(5):
        tools.verdict_record(run_id, dict(payload))

    st = tools.metrics_compute(run_id)["stability"]
    assert st["valid"] is False
    assert st["step_mode_ratio"] is None, "无效指标必须置空，不能给出假满分"
    assert st["n_identical_resubmissions"] == 1
    assert "机械重复" in st["note"]


def test_stability_valid_when_verdicts_differ(workspace) -> None:
    """真正独立重评（结果有差异）时稳定性指标有效。"""
    run_id = tools.dataset_next_batch(n=1)["run_id"]
    for step, ev in [(2, "证据甲"), (2, "证据乙"), (3, "证据丙")]:
        tools.verdict_record(run_id, {
            "sample_id": "T1-00", "process_valid": False,
            "first_error_step": step, "error_type": "E3", "evidence": ev,
        })
    st = tools.metrics_compute(run_id)["stability"]
    assert st["valid"] is True
    assert st["step_mode_ratio"] == pytest.approx(2 / 3)
