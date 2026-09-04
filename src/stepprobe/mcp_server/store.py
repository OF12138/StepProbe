"""评测运行的状态与落盘。

MCP 工具是无状态调用，但一次评测是有状态的过程（取样 → 评判 → 汇总）。
这里用 run_id 把一次评测的产物聚在一起，全部落到磁盘 —— 进程重启不丢，
WorkBuddy 分批跑也能接上。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..schema import Sample, Verdict

_LOCK = threading.Lock()


def data_dir() -> Path:
    return Path(os.environ.get("STEPPROBE_DATA_DIR", "./data")).resolve()


def results_dir() -> Path:
    return Path(os.environ.get("STEPPROBE_RESULTS_DIR", "./results")).resolve()


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def run_dir(run_id: str, *, create: bool = True) -> Path:
    """运行目录。

    `create=False` 用于**只读**路径。默认建目录很方便，但会让「读一次不存在的
    运行」也在 results/runs/ 下留一个空目录 —— 跑一次单测就污染一批，混进真实
    评测记录里。所以每个读函数都显式传 create=False。
    """
    d = results_dir() / "runs" / run_id
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def latest_run_id() -> str | None:
    root = results_dir() / "runs"
    if not root.exists():
        return None
    runs = sorted((p.name for p in root.iterdir() if p.is_dir()), reverse=True)
    return runs[0] if runs else None


def resolve_run(run_id: str | None) -> str:
    """`latest` 或 None 都解析为最近一次运行。"""
    if run_id and run_id != "latest":
        return run_id
    resolved = latest_run_id()
    if resolved is None:
        raise ValueError("尚无任何评测运行，请先调用 dataset.next_batch")
    return resolved


# ---------------------------------------------------------------------------
# 样本集
# ---------------------------------------------------------------------------

_CACHE: dict[Path, list[Sample]] = {}


def load_samples(name: str = "sample_set.jsonl") -> list[Sample]:
    """读取规整后的样本集，带进程内缓存。"""
    path = data_dir() / name
    if path not in _CACHE:
        if not path.exists():
            raise FileNotFoundError(
                f"找不到 {path}。请先运行：python -m stepprobe.data.build"
            )
        with path.open(encoding="utf-8") as fh:
            _CACHE[path] = [Sample.model_validate_json(ln) for ln in fh if ln.strip()]
    return _CACHE[path]


# ---------------------------------------------------------------------------
# 批次与评判结果
# ---------------------------------------------------------------------------

def write_batch(run_id: str, sample_ids: list[str], meta: dict) -> None:
    """记录本次运行发出了哪些样本。

    这份清单是「评估时看没看到标注」的凭据 —— 发出的是剥离标注的副本，
    ground truth 只在 metrics 阶段从原始样本集读取。
    """
    payload = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sample_ids": sample_ids,
        **meta,
    }
    (run_dir(run_id) / "batch.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_batch(run_id: str) -> dict:
    path = run_dir(run_id, create=False) / "batch.json"
    if not path.exists():
        raise FileNotFoundError(f"运行 {run_id} 没有 batch.json")
    return json.loads(path.read_text(encoding="utf-8"))


def append_verdict(run_id: str, verdict: Verdict) -> int:
    """追加一条评判结果，返回该运行当前累计条数。

    同一 sample_id 重复写入会全部保留 —— 稳定性验证需要同一样本的多次结果。
    """
    path = run_dir(run_id) / "verdicts.jsonl"
    with _LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(verdict.model_dump_json() + "\n")
        return sum(1 for _ in path.open(encoding="utf-8"))


def read_verdicts(run_id: str) -> list[Verdict]:
    path = run_dir(run_id, create=False) / "verdicts.jsonl"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [Verdict.model_validate_json(ln) for ln in fh if ln.strip()]


def write_json(run_id: str, name: str, payload: dict) -> Path:
    path = run_dir(run_id) / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# P6 题目集（Max Mode 消融）
# ---------------------------------------------------------------------------

_P6_CACHE: dict[Path, list[dict]] = {}

#: 允许发给解题方的字段。**gold_answer 不在其中** —— 解题时看见标准答案，
#: 这一轮答案正确率就作废了。与 Sample.without_label 是同一条纪律：白名单，
#: 新增字段默认不外发。
P6_SAFE_KEYS: frozenset[str] = frozenset({"id", "problem", "tier"})


def load_p6_problems(name: str = "p6_problems.jsonl") -> list[dict]:
    path = data_dir() / name
    if path not in _P6_CACHE:
        if not path.exists():
            raise FileNotFoundError(
                f"找不到 {path}。请先运行：python scripts/build_p6_set.py"
            )
        with path.open(encoding="utf-8") as fh:
            _P6_CACHE[path] = [json.loads(ln) for ln in fh if ln.strip()]
    return _P6_CACHE[path]


def append_solution(run_id: str, arm: str, solution: dict) -> int:
    """追加一条解答。arm 区分 Max Mode 开 / 关两个实验臂。"""
    path = run_dir(run_id) / f"solutions_{arm}.jsonl"
    with _LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**solution, "arm": arm}, ensure_ascii=False) + "\n")
        return sum(1 for _ in path.open(encoding="utf-8"))


def read_solutions(run_id: str, arm: str) -> list[dict]:
    path = run_dir(run_id, create=False) / f"solutions_{arm}.jsonl"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]
