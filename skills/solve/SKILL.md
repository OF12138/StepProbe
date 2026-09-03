---
name: solve
description: 用 Hy3 解数学题并输出结构化的完整解题过程（而非仅最终答案），供 StepProbe 过程评估使用。
---

# /solve —— 结构化解题

## 用途

对给定题目产出**完整、可逐步索引的解题过程**。本 Skill 的输出是过程评估的输入，因此**结构比文采重要**。

## 参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `--tier` | 难度层 `T1/T2/T3/T4/all` | `all` |
| `--n` | 题目数量 | `50` |
| `--max-mode` | `on` / `off`，用于消融对比 | 跟随当前设置 |
| `--source` | `processbench` / `cn_custom` | `cn_custom` |

## 流程

1. 调用 MCP 工具 `dataset.next_batch(source, tier, n)` 取一批题目。
2. 对每道题，按下面的输出规范解题。
3. 调用 `verdict.record(sample_id, solution)` 落盘。

## 输出规范

**必须返回严格的 JSON，不得包含 JSON 之外的任何文字。**

```json
{
  "problem_id": "cn-gaokao-2023-17",
  "steps": [
    {
      "index": 1,
      "content": "由题意，设 f(x) = x^2 - 2x + 3，对称轴为 x = 1。",
      "equations": ["f(x) = x^2 - 2*x + 3"],
      "justification": "配方法"
    }
  ],
  "final_answer": "[2, +\\infty)",
  "answer_format": "interval"
}
```

### 字段要求

- **`steps[].index`** —— 从 1 开始连续编号，不得跳号。
- **`steps[].content`** —— 一步只做一件事。若一段话包含两个独立推导，拆成两步。
- **`steps[].equations`** —— 该步涉及的等式，**写成 SymPy 可解析的形式**（`2*x` 而非 `2x`，`**` 表示乘方）。这是 L1 确定性校验的输入，缺失会导致该步只能退回 LLM 判断。无等式时给空数组。
- **`steps[].justification`** —— 本步依据（定理名、公式名、代数变形手段）。用于 L2 审查定理前提是否成立。
- **`final_answer`** —— 只写答案本身，不含"答案是"等前缀。
- **`answer_format`** —— `number` / `expression` / `interval` / `set` / `tuple`。

## 关键约束

**不要为了让过程"看起来严谨"而补充实际未使用的步骤。** 本项目要评估的是真实推理过程；虚构中间步骤会让评测结果失真，也会污染 E9（答案对但过程不成立）的判定。

**如果没有把握，如实写出不确定的地方**，不要用模糊表述掩盖跳步。写"此处直接引用结论，未验证前提"比写一段似是而非的论证更有价值。

**允许失败。** 解不出来就在 `final_answer` 填 `null` 并在最后一步说明卡在哪里。强行编造答案会严重污染评测数据。
