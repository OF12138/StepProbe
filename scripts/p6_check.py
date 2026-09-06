"""P6 运行体检：进度 + 格式，跑评估的**中途**用。

为什么要单独一个脚本：320 条评估要分 16 批完成，中间换过文件、换过
`sample_id` 前缀。任何一处漂移（前缀写错、arm 写反、重复落盘）都不会报错，
只会在最后评分时表现为「某一臂样本数莫名其妙少了一截」。等到那时候才发现，
返工的是几十条人工评估。

所以这里只查一件事：**已落盘的东西，能不能被 p6_score.py 正确归位。**

    python scripts/p6_check.py --run <RUN_ID>
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

ARMS = ("max_off", "max_on")


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path.name} 第 {n} 行不是合法 JSON：{e}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P6 运行体检")
    ap.add_argument("--run", required=True)
    ap.add_argument("--results-dir", type=Path, default=Path("results/runs"))
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    args = ap.parse_args(argv)

    run_dir = Path(args.run) if Path(args.run).is_dir() else args.results_dir / args.run
    if not run_dir.is_dir():
        raise SystemExit(f"找不到运行目录 {run_dir}")

    problems = {
        json.loads(ln)["id"]: json.loads(ln)
        for ln in (args.data_dir / "p6_problems.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    }
    problems_by_tier = Counter(p["tier"] for p in problems.values())

    issues: list[str] = []
    print(f"运行 {run_dir.name}　题目集 {len(problems)} 道\n")

    # ---- 解答 ----
    sols: dict[str, list[dict]] = {}
    print("解答")
    for arm in ARMS:
        rows = _load(run_dir / f"solutions_{arm}.jsonl")
        sols[arm] = rows
        ids = [r.get("problem_id") for r in rows]
        dup = [k for k, c in Counter(ids).items() if c > 1]
        unknown = sorted(set(ids) - set(problems))
        unsolved = [i for r, i in zip(rows, ids) if not r.get("final_answer")]
        print(f"  {arm:<8} {len(set(ids))}/{len(problems)}　未解出 {len(unsolved)}")
        if dup:
            issues.append(f"{arm} 有重复 problem_id：{dup[:5]}")
        if unknown:
            issues.append(f"{arm} 有题目集之外的 problem_id：{unknown[:5]}")
        for r in rows:
            if not isinstance(r.get("steps"), list) or not r["steps"]:
                issues.append(f"{arm}:{r.get('problem_id')} 的 steps 为空或不是列表")

    common = set(i["problem_id"] for i in sols["max_off"]) & set(
        i["problem_id"] for i in sols["max_on"]
    )
    if sols["max_off"] and sols["max_on"]:
        print(f"  配对　　 {len(common)} 道")
        for arm in ARMS:
            only = sorted(set(r["problem_id"] for r in sols[arm]) - common)
            if only:
                issues.append(f"只有 {arm} 解了、另一臂没解：{only[:5]}（共 {len(only)}）")

    # ---- 评判 ----
    print("\n评估")
    verdicts = _load(run_dir / "verdicts.jsonl")
    keyed: dict[str, list[dict]] = {a: [] for a in ARMS}
    for v in verdicts:
        sid = str(v.get("sample_id", ""))
        arm, _, pid = sid.partition(":")
        if arm not in ARMS:
            issues.append(f"sample_id 前缀不是 max_off/max_on，无法归臂：{sid!r}")
            continue
        if pid not in problems:
            issues.append(f"sample_id 里的题号不在题目集中：{sid!r}")
            continue
        if not any(r["problem_id"] == pid for r in sols[arm]):
            issues.append(f"{sid} 有评判但该臂没有对应解答")
        keyed[arm].append({**v, "_pid": pid, "_tier": problems[pid]["tier"]})

    for arm in ARMS:
        rows = keyed[arm]
        pids = [r["_pid"] for r in rows]
        dup = [k for k, c in Counter(pids).items() if c > 1]
        tiers = Counter(r["_tier"] for r in rows)
        bar = "　".join(
            f"{t} {tiers.get(t, 0)}/{problems_by_tier[t]}" for t in sorted(problems_by_tier)
        )
        print(f"  {arm:<8} {len(set(pids))}/{len(problems)}　{bar}")
        if dup:
            issues.append(f"{arm} 有重复评判的题：{dup[:5]}（共 {len(dup)}）—— 会污染统计")

    # 字段规范
    for arm in ARMS:
        for r in keyed[arm]:
            sid = r["sample_id"]
            if not isinstance(r.get("process_valid"), bool):
                issues.append(f"{sid} 的 process_valid 不是布尔值")
            if r.get("answer_correct") is not None:
                issues.append(
                    f"{sid} 填了 answer_correct —— 题目集无标准答案，必须为 null，"
                    "答案对错由 p6_score.py 统一判定"
                )
            if r.get("process_valid") is False:
                if not r.get("evidence"):
                    issues.append(f"{sid} 判错但没有 evidence")
                if r.get("first_error_step") is None:
                    issues.append(f"{sid} 判错但没有 first_error_step")
            elif r.get("first_error_step") is not None or r.get("error_type") is not None:
                issues.append(f"{sid} 判成立却填了 first_error_step / error_type")

    done = sum(len({r['_pid'] for r in keyed[a]}) for a in ARMS)
    print(f"\n评估进度 {done}/{len(problems) * 2}")

    if issues:
        print(f"\n发现 {len(issues)} 个问题：")
        for m in issues:
            print(f"  ✗ {m}")
        return 1
    print("\n✓ 格式无问题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
