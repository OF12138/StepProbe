---
name: ablate
description: Max Mode 开 / 关的同题配对消融实验，分别测答案正确率与过程成立率，判别 Max Mode 是让推导更严谨还是只是让答案更容易蒙对。
---

# /ablate —— Max Mode 消融

## 要回答的问题

> **Max Mode 是让推导更严谨，还是只是让答案更容易蒙对？**

这两件事在「答案正确率」这一个数字上长得一模一样，必须拆开测：

| 观察到的模式 | 结论 |
|---|---|
| 答案正确率 ↑ **且** 过程成立率 ↑ | 推导确实更严谨 |
| 答案正确率 ↑ **但** 过程成立率不动、E9 比例 ↑ | 只是更容易蒙对 |
| 两者都不显著 | 在本题目集上看不出差别 —— **这也是结论** |

这是本项目立论的直接检验：只看最终答案会怎样系统性地误判模型能力。

## 前提

- 题目集已构建：`python scripts/build_p6_set.py`（160 道，四层各 40）
- 评估器已通过 P5 验证（见 `docs/report.md` §4）—— 未验证的评估器测出来的
  「过程成立率」不可信，整个消融就没有意义

## 可用的 MCP 工具（stepprobe）

| 工具 | 作用 |
|---|---|
| `p6_next_batch(n, tier, arm, run_id)` | 取一批题目，**不含标准答案** |
| `p6_record(run_id, arm, solution)` | 落盘一条解答 |
| `verdict_record(run_id, verdict)` | 落盘评估器对该解答的评判 |
| `check_step_symbolic` / `solution_segment` | 同 `/evaluate` |

---

## 流程

整个实验是 **2 个 arm × 2 个阶段**。**两个 arm 必须解同一批题** —— 配对设计是
这个实验的全部统计力量来源。

### 阶段 A：Max Mode **关闭**，解题

1. 在 WorkBuddy 界面**关掉 Max Mode**
2. ```
   p6_next_batch(n=20, arm="max_off")        # 首次，返回 run_id
   p6_next_batch(n=20, arm="max_off", run_id="<上一步的 run_id>")
   ```
3. 对每道题按 `/solve` 的输出规范解题，然后
   ```
   p6_record(run_id="<run_id>", arm="max_off", solution={
     "problem_id": "p6-t2-000", "steps": [...], "final_answer": "..."
   })
   ```
4. 重复到 160 道全部解完

### 阶段 B：Max Mode **开启**，解同一批题

1. 在 WorkBuddy 界面**打开 Max Mode**
2. **用同一个 `run_id`**，把 `arm` 换成 `"max_on"`，重复阶段 A
3. `p6_next_batch` 会按 `id` 稳定排序发题，两个 arm 拿到的是同一批

> **不要在两个阶段之间改动任何其他设置**（提示词、批大小、`/solve` 的用法）。
> 消融实验只允许变一个变量，否则测出来的差异归因不到 Max Mode 上。

### 阶段 C：评估两批解答

对**全部 320 条解答**（160 题 × 2 arm）执行 `/evaluate` 的完整流程。落盘时
`sample_id` 必须写成 **`<arm>:<problem_id>`**：

```
verdict_record(run_id="<run_id>", verdict={
  "sample_id": "max_on:p6-t2-000",
  "process_valid": false, "first_error_step": 3,
  "error_type": "E2", "evidence": "...", "layer": "L2"
})
```

评分脚本靠这个前缀把评判归到对应的 arm。

> **评估时不要去看是哪个 arm 的解答，也不要去比对两个 arm。** 逐条独立评，
> 就像评任何一条陌生的推理链。知道「这条来自 Max Mode」会让判断偏向预期结论 ——
> 这正是消融实验最容易自我实现的地方。

### 阶段 D：评分

```
python scripts/p6_score.py --run <run_id>
```

产出 `p6_comparison.json` 与 `p6_comparison.md`：两臂的答案正确率、过程成立率、
E9 比例（均带 Wilson 95% CI），加上配对 McNemar 检验与分层结果。

只想看标准答案经过交叉印证的那部分题目时加 `--cross-validated-only`。

---

## 结果解读的三条纪律

1. **看配对差分，不看绝对值。** 题目集是从 ProcessBench 里「至少被一个模型答对过」
   的题反推出来的，**系统性偏易**，绝对准确率不能与其他工作横向比较。配对差分不受
   这个偏置影响（见 `scripts/build_p6_set.py` 的说明）。

2. **p 值大就如实写「看不出差别」。** 不要换口径、换子集、换指标去凑一个显著结果。
   在 160 道题上测不出差异，本身就是一个诚实且有信息量的结论。

3. **E9 比例是这个实验的主角。** 它直接回答「是不是只是蒙得更准」，而答案正确率
   单独看永远回答不了这个问题。报告里它应该和答案正确率并列，而不是放在附录。
