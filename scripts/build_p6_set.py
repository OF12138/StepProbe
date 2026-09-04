"""构建 P6 评测题目集：从 ProcessBench 反推出可信的标准答案。

**为什么需要这一步。** P6 要回答的问题是「Max Mode 是让推导更严谨，还是只是让
答案更容易蒙对」。要把这两件事分开，就必须同时测得**答案正确率**与**过程正确率**
—— 而 ProcessBench 只给 `final_answer_correct` 布尔值，**没有任何一列是标准答案**
（4 个 parquet 的列全部是 id / generator / problem / steps / final_answer_correct /
label）。没有标准答案，Hy3 自己解出来的答案就无从判分。

**做法。** ProcessBench 里同一道题往往有多个模型的解答，其中被标为
`final_answer_correct=True` 的那些，其最终答案按数据集的断言就是对的。于是：

    对每道题，取全部 answer-correct 的解答 → 抽出各自的 \boxed{...} → 作为标准答案

**抽取可信吗。** 单条抽取可能抽错（抽到中间步骤的 boxed、括号不配对、只抽到多解中的
一半）。ProcessBench 里有 110 道题被两个以上不同模型答对过，它们提供了一个天然的
**交叉印证集**：对这些题分别抽取，看两个独立抽取结果是否等价（用本项目自己的
`check_answer` 判定，不做字符串比较）。

    实测：boxed 抽取率 90.1%（1532/1700），交叉印证一致率 **95.5%**（105/110）

这个 95.5% 就是抽取器的**实测**可靠性，不是假设的。（第一版一致率只有 79.1%，
逐条看下去发现 23 处不一致里 20 处根本不是答案不同，而是 `check_answer` 对
「x = sqrt2 vs sqrt2」「4 度 vs 4」这类装饰差异过严 —— 已修，见
`checkers/answer.py` 的 `_strip_decoration`。**交叉印证集顺带验出了校验器的 bug。**）

**为什么默认 min-agree=1。** 若只保留有交叉印证的题目，T2 与 T4 一道都剩不下 ——
ProcessBench 各子集大多「一题一模型」，只有 olympiadbench 大量复用题目，结果会是个
只有 T3 的畸形集合，分层对比无从谈起。因此默认接受单条抽取（约 2% 的标准答案可能有误），
理由是：

    **P6 是同题配对对比。** 同一道题在 Max Mode 开与关两种设置下各解一次，比的是
    两者之差。标准答案抽错时两个 arm 被同等惩罚，**误差在配对差分中抵消**。

标准答案的噪声因此只影响**绝对准确率**，不影响 P6 真正要回答的那个问题。输出保留
`n_agree` 字段（该题有几个独立抽取互相印证），报绝对准确率时应单独看 n_agree >= 2 的子集。

**已知偏置（必须写进报告）**：能这样反推出答案的题目，都是**至少被一个模型答对过的**，
系统性偏易。绝对准确率不可与其他工作横向比较。

    python scripts/build_p6_set.py [--min-agree 1] [--per-tier 40]
"""

from __future__ import annotations

import argparse
import glob
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from stepprobe.checkers.answer import Equivalence, check_answer

#: ProcessBench 子集 → 本项目难度层
TIER = {"gsm8k": "T1", "math": "T2", "olympiadbench": "T3", "omnimath": "T4"}

_BOXED = re.compile(r"\\boxed\s*\{")


def extract_boxed(text: str) -> str | None:
    """取**最后一个** \boxed{...} 的内容，按括号配对截断。

    用最后一个而非第一个：解答中途可能 box 出阶段性结果，最终答案在末尾。
    手写括号配对而不用正则，因为答案里常有嵌套花括号（\frac{a}{b}）。
    """
    last = None
    for m in _BOXED.finditer(text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        if depth == 0:
            last = text[m.end() : i - 1].strip()
    return last or None


def _agree(a: str, b: str) -> bool:
    """两个抽取结果是否等价。用本项目的答案校验器，不做字符串比较。

    **只有明确判定 EQUAL 才算印证。** UNKNOWN 不算 —— 这里在挑选可信的标准答案，
    「判不出来」不能当成「一致」。
    """
    return check_answer(a, b).verdict is Equivalence.EQUAL


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="构建 P6 评测题目集")
    ap.add_argument(
        "--min-agree",
        type=int,
        default=1,
        help="每道题至少需要多少个 generator 的抽取结果互相印证（默认 1，理由见模块文档）",
    )
    ap.add_argument("--per-tier", type=int, default=40)
    ap.add_argument("--seed", type=int, default=20260903)
    ap.add_argument("--out", type=Path, default=Path("data/p6_problems.jsonl"))
    args = ap.parse_args(argv)

    import pandas as pd

    # (tier, problem) → {generator: 抽取出的答案}
    cand: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    n_correct = n_boxed = 0
    for f in sorted(glob.glob("data/raw_cache/Qwen__ProcessBench__*.parquet")):
        sub = f.split("__")[-1].replace(".parquet", "")
        df = pd.read_parquet(f)
        for _, r in df.iterrows():
            if not r["final_answer_correct"]:
                continue
            n_correct += 1
            ans = extract_boxed("\n".join(list(r["steps"])[-4:]))
            if ans is None:
                continue
            n_boxed += 1
            cand[(TIER[sub], r["problem"])][str(r["generator"])] = ans

    # 交叉印证。
    #
    # **一致率必须只在「有两个以上独立抽取」的题目上统计**，与 --min-agree 无关：
    # 单条抽取跟自己比必然一致，把它算进去会得到一个自证的漂亮数字
    #（min-agree=1 时会显示 99.6%），而它对抽取器的可靠性毫无信息量。
    kept: dict[str, list[dict]] = defaultdict(list)
    n_multi = n_agreed = 0
    for (tier, problem), by_gen in cand.items():
        vals = list(by_gen.values())
        agreed = all(_agree(vals[0], v) for v in vals[1:])
        if len(by_gen) >= 2:
            n_multi += 1
            n_agreed += agreed
        if len(by_gen) < args.min_agree or not agreed:
            continue
        kept[tier].append(
            {
                "problem": problem,
                "gold_answer": vals[0],
                "tier": tier,
                "n_agree": len(by_gen),
                "generators": sorted(by_gen),
            }
        )

    print(f"answer-correct 解答 {n_correct} 条，其中抽出 boxed {n_boxed} 条"
          f"（{n_boxed / max(n_correct, 1):.1%}）")
    print(f"交叉印证集（被 ≥2 个模型答对过的题）{n_multi} 道，其中两次独立抽取等价 "
          f"{n_agreed} 道 → **抽取器一致率 {n_agreed / max(n_multi, 1):.1%}**")
    print(f"（该比率与 --min-agree 无关：单条抽取跟自己比必然一致，不计入）")
    print()

    import random

    rng = random.Random(args.seed)
    out = []
    for tier in ("T1", "T2", "T3", "T4"):
        pool = sorted(kept.get(tier, []), key=lambda d: d["problem"])
        rng.shuffle(pool)
        take = pool[: args.per_tier]
        for i, d in enumerate(take):
            out.append({"id": f"p6-{tier.lower()}-{i:03d}", **d})
        print(f"  {tier}: 可用 {len(pool)} 道，取 {len(take)} 道")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for d in out:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
    print()
    print(f"共 {len(out)} 道 → {args.out}")
    print("分布：", dict(Counter(d["tier"] for d in out)))
    print()
    n_cross = sum(1 for d in out if d["n_agree"] >= 2)
    print(f"其中有交叉印证（n_agree ≥ 2）的 {n_cross} 道 —— 报绝对准确率时用这个子集。")
    print()
    print("⚠ 这些题都是至少被一个模型答对过的，系统性偏易。P6 只做 Max Mode "
          "开/关的同题配对对比，不与其他工作横向比较绝对准确率。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
