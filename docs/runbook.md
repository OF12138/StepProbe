# WorkBuddy 执行手册（P6 消融 / P5 扩样）

直接复制方框里的内容粘进 WorkBuddy 对话框。**每一步都记下返回的 `run_id`** ——
后续所有批次都要用同一个，写错了数据就归不到一起。

> **前置**：`skills/` 下四个 Skill 都要在 WorkBuddy 里可用。之前手动传过
> `solve` / `evaluate` / `validate` 三个，**`ablate` 是新增的，需要补传**；
> 或者直接把本仓库选作 workspace，WorkBuddy 会自动识别项目层级 Skills。
>
> 开工前确认 MCP 状态灯是 🟢。

---

## A. Max Mode 消融（P6）

四个阶段。**A1 与 A2 之间只许改 Max Mode 这一个设置**，其余一律不动 ——
提示词、批大小、Skill 用法全部保持一致，否则测出的差异归因不到 Max Mode 上。

### A0　先确认题目集在位

在终端（不是 WorkBuddy）跑一次：

```bash
python scripts/build_p6_set.py
```

应输出 `共 160 道 → data\p6_problems.jsonl`。已经跑过就跳过。

---

### A1　Max Mode **关闭**，解题

**先在 WorkBuddy 界面把 Max Mode 关掉。** 然后粘贴：

```
执行 /ablate 的阶段 A。当前 Max Mode 已关闭。

1. 调用 p6_next_batch(n=20, arm="max_off")，记下返回的 run_id 并告诉我。
2. 对返回的每一道题解题，输出格式严格遵守 /solve 的规范：
   steps 为 1-based 连续编号的步骤文本列表，一步只做一件事；
   final_answer 只写答案本身，不含"答案是"之类前缀。
3. 每解完一道，立刻调用
   p6_record(run_id="<刚才的 run_id>", arm="max_off",
             solution={"problem_id": "...", "steps": [...], "final_answer": "..."})
4. 全部 20 道落盘后，报告：本批完成数、p6_next_batch 返回的 remaining。

约束：
- 解不出来就把 final_answer 填 null，并在最后一步说明卡在哪里。不要编造答案。
- 不要为了让过程"看起来严谨"补充实际没用到的步骤。
- 这一批只解题，不要做任何过程评估。
```

拿到 `run_id` 后，**后续每一批**都粘这个（把 `<RUN_ID>` 换成实际值）：

```
继续 /ablate 阶段 A，run_id = <RUN_ID>，arm = max_off，Max Mode 保持关闭。

调用 p6_next_batch(n=20, arm="max_off", run_id="<RUN_ID>")，
按上一批同样的规范解题并逐条 p6_record。
完成后报告本批完成数与 remaining。
```

重复到 `remaining` 为 0（共 8 批）。

---

### A2　Max Mode **开启**，解同一批题

**先在 WorkBuddy 界面把 Max Mode 打开。** 其他什么都别改。

```
执行 /ablate 的阶段 B。当前 Max Mode 已开启，run_id = <RUN_ID>（沿用阶段 A 的）。

调用 p6_next_batch(n=20, arm="max_on", run_id="<RUN_ID>")，
用与阶段 A **完全相同**的方式解题，逐条
p6_record(run_id="<RUN_ID>", arm="max_on", solution={...})。

重要：
- 不要去看阶段 A 的解答，也不要参考 results/runs/<RUN_ID>/solutions_max_off.jsonl。
  两臂必须各自独立解题，否则配对对比失去意义。
- 解题方式、步骤粒度、失败处理与阶段 A 保持一致。

完成后报告本批完成数与 remaining。
```

同样重复到 `remaining` 为 0。

---

### A3　评估全部 320 条解答

这一步的输入是磁盘上的解答文件，不走 `p6_next_batch`。

```
执行 /ablate 的阶段 C：评估解答。run_id = <RUN_ID>。

1. 读取 results/runs/<RUN_ID>/solutions_max_off.jsonl，取前 20 条尚未评估的。
2. 对每一条执行 /evaluate 的完整流程：
   - 先调 check_step_symbolic(steps=[...])，只对返回的 needs_llm_review 里的
     步骤做语义审查，不要用自己的计算覆盖工具已判定的步骤；
   - 再做全局复核（E1/E4/E6/E9），E9 复核无条件执行；
   - 判错必须填 evidence，指向具体不成立的论断。
3. 落盘时 sample_id 必须写成 "max_off:<problem_id>"，例如
   verdict_record(run_id="<RUN_ID>", verdict={
     "sample_id": "max_off:p6-t2-000",
     "process_valid": false, "first_error_step": 3,
     "error_type": "E2", "evidence": "...", "layer": "L2"})
   过程成立时 first_error_step 与 error_type 填 null。
   本项目题目集没有标准答案，answer_correct 一律填 null，不要自行揣测答案对错。

评审纪律（重要）：
- 不要去关心这条解答来自哪个 arm，也不要拿两个 arm 的解答互相比较。
  逐条独立评，就像评一条陌生的推理链。知道"这条来自 Max Mode"会让判断
  偏向预期结论 —— 这是消融实验最容易自我实现的地方。
- 只判实质错误：某一步的计算、代数变形、定理适用条件或概念使用本身不成立。
  "未证唯一性 / 论证不完整 / 表述含混 / 步骤冗余"都不算错，不要判错。
- 默认成立，举证判错。不确定就判成立。

完成后报告本批评估数量。
```

后续批次把 `solutions_max_off.jsonl` 换成 `solutions_max_on.jsonl`、`sample_id`
前缀换成 `max_on:`，其余不变。两个文件各 8 批，共 16 批。

**每隔几批在终端跑一次体检**，不要等 320 条全做完：

```bash
python scripts/p6_check.py --run <RUN_ID>
```

它查的是「已落盘的东西能不能被评分脚本正确归位」—— 前缀漏写、arm 写反、
同一题重复评、判错没填 evidence。这几类都不会报错，只会让某一臂的样本数
悄悄少一截；换文件那一步（`max_off` → `max_on`）尤其容易漂移。发现得早，
返工的是几条；发现得晚，返工的是几十条人工评估。

---

### A4　评分

回终端：

```bash
python scripts/p6_score.py --run <RUN_ID>
```

产出 `p6_comparison.md`。把输出发我，我来写进报告。

中途也可以跑，脚本会自己在报告开头标注「数据不完整、不可引用」并列出各 tier
的评估覆盖度 —— 因为部分数据产出的报告和最终版格式完全一样，而评估按 tier
顺序推进，中途的过程成立率系统性偏高。**答案正确率不受影响**，160 道题解完
就已经是终值了。

**怎么读**（`p6_comparison.md` 里也写了）：

| 观察到的模式 | 结论 |
|---|---|
| 答案正确率 ↑ **且** 过程成立率 ↑ | Max Mode 确实让推导更严谨 |
| 答案正确率 ↑ **但** 过程成立率不动、E9 比例 ↑ | 只是更容易蒙对 |
| 两者都不显著（p 大） | 在这批题上看不出差别 —— **这也是结论，如实写** |

---

## B. 扩样验证（P5，60 → 447）

**开一个全新的 run_id**，不要续用 60 条那次 —— 那一轮已经做完人工抽检并写进报告，
混进新数据会让两套指标对不上。

### B1　首批

```
执行 /validate。这是一次全新的完整验证，目标是跑完 447 条抽样集。

1. 调用 dataset_next_batch(n=40)，记下返回的 run_id 并告诉我。
2. 对每条样本执行 /evaluate 的完整流程，逐条 verdict_record 落盘。

硬性约束：
- 返回的样本已剥离标注。**不要以任何方式去查 ground truth**，特别是不要读
  data/sample_set.jsonl 或 results/runs/ 下的既有结果。看到了这一轮就作废。
- 判错必须填 evidence，指向具体不成立的论断；无证据的判错会被工具拒绝。
- 只判实质错误（同 A3 的口径）：未证唯一性、论证不完整、表述含混、步骤冗余
  都不算过程错误。
- 默认成立，举证判错。宁可漏报，不可误报。
- E9 复核无条件执行，不要依赖 check_answer —— 本数据集没有标准答案，
  answer_correct 一律填 null。判据是"结论是否由前序步骤逻辑推出"。
- 特别注意藏在文字论断里的错误（不在任何等式中，L1 看不见）：
  样本空间/分母的界定、计数与周期的断言、大小比较、枚举的完备性、定义域。
  对每一个这样的断言问：它是被证明的，还是被声称的？

完成后报告本批数量与 remaining。
```

### B2　续批（重复约 11 次）

```
继续 /validate，run_id = <RUN_ID>。

dataset_next_batch(n=40, run_id="<RUN_ID>")，
按上一批同样的规范逐条评估并 verdict_record。
完成后报告本批数量与 remaining。
```

### B3　稳定性（在 447 条跑完之后）

稳定性必须是**独立重评**，不能重复落盘同一结果 —— 首轮就是在这里翻过车，
把同一条 verdict 存了 5 遍得到"满分"。

**开一个新会话**（清空上下文），然后：

```
稳定性复评，run_id = <RUN_ID>。

从 results/runs/<RUN_ID>/verdicts.jsonl 里取前 30 个不同的 sample_id，
只取 sample_id 列表，**不要读它们已有的判定结果**。

然后对这 30 条重新调 dataset_next_batch 取样本原文，从头独立评估一遍，
用同一个 run_id 落盘。

这是独立重评：你必须像第一次见到这些推理链一样评判。若你已经知道之前的结论，
请直接告诉我，这一轮作废。
```

### B4　出指标

```
metrics_compute(run_id="<RUN_ID>")
report_export(run_id="<RUN_ID>", kind="summary")
report_export(run_id="<RUN_ID>", kind="human_audit")
```

把 `metrics_compute` 的输出发我。之后走一次和上次一样的抽检流程：

```bash
python scripts/pre_audit.py --run <RUN_ID>     # 生成判断包
# 人工填 audit_sheet.csv 的 human_verdict
python scripts/apply_audit.py --run <RUN_ID>   # 重算修正后指标
```

> 447 条的分歧数会比 60 条那轮多（按比例约 100+ 条）。**不必全审** ——
> 任务书要求的是"抽检"。建议按分歧类型分层各抽 15–20 条，总量控制在 50 条以内，
> 并在报告里写明抽检比例与抽样方式。告诉我你想抽多少，我改 `pre_audit.py`
> 加一个分层抽样开关。

---

## 中断了怎么办

两个流程都是断点续跑的：`p6_next_batch` 和 `dataset_next_batch` 都会跳过该
`run_id` 下已经处理过的条目。中断后只要拿着同一个 `run_id` 重发对应的"续批"
提示词即可，不会重复也不会漏。

想确认进度（P6）：

```bash
python scripts/p6_check.py --run <RUN_ID>
```

会按 arm 和 tier 打印已完成数，并顺带做一遍格式体检。

P5 那边直接看行数：

```bash
wc -l results/runs/<RUN_ID>/verdicts.jsonl
```
