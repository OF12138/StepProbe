# 实现计划

**原则：人工环节压到最小。** 凡是能由代码或 Claude Code 完成的，都不留给人工；只有三类事情必须人工做 —— 在 WorkBuddy 界面里发起任务（无 API，无法自动化）、人工抽检的最终确认（任务书硬性要求）、录制 demo。

---

## 阶段划分

| 阶段 | 内容 | 谁做 | 状态 |
|---|---|---|---|
| **P0** | 方案设计、错误分类体系、数据方案 | Claude | ✅ 完成 |
| **P1** | 数据管线：拉取、规整、分层抽样 | Claude | ✅ 完成 |
| **P2** | L1 确定性校验器（答案 / 等式 / 格式）+ 单元测试 | Claude | ⬜ |
| **P3** | MCP Server：工具注册与 stdio 服务 | Claude | ⬜ |
| **P4** | Skills 落地 + WorkBuddy 联调 | Claude 写，**人工接入** | ⬜ |
| **P5** | 有效性验证实验 | **人工发起**，Claude 分析 | ⬜ |
| **P6** | 评测执行 + Max Mode 消融 | **人工发起**，Claude 分析 | ⬜ |
| **P7** | 人工抽检（Claude 预审 + 人工确认） | Claude 预审，**人工确认** | ⬜ |
| **P8** | 分析报告 + Demo 录制 | Claude 写报告，**人工录屏** | ⬜ |

---

## 各阶段详情

### P1 数据管线 ✅

- 直接下载 HuggingFace parquet（总计约 3.3 MB），**不依赖 `datasets` 库**
- 各数据集 → 统一 Schema，本地缓存
- 三分类分层抽样（见下方「P1 实测发现」）
- `python -m stepprobe.data.build` 一键完成

### P2 L1 确定性校验器

- `checkers/answer.py` —— 三级校验：规范化 → SymPy 符号等价 → 随机数值代入
- `checkers/symbolic.py` —— 步骤等式抽取与判定，返回 `FALSE` / `UNKNOWN` / `TRUE`
- `checkers/segment.py` —— 步骤切分（公开数据集沿用原索引，不重新切）
- **自带测试集**：手工构造约 40 条已知答案的等式对（含 LaTeX 变体、超时用例、无法解析用例），保证校验器本身可回归

### P3 MCP Server

- stdio 传输，注册 7 个工具（README §3）
- `dataset.next_batch()` **必须剥离 `label` 字段** —— 防止评估时泄漏 ground truth
- 全部工具写单元测试，Claude 自测通过后再交付

### P4 Skills 落地与联调

Claude 完成：Skill 定义、输出 JSON Schema 校验、失败重试逻辑。

**人工环节（约 15 分钟，一次性）**：
1. 把 `mcp.json` 配置粘进 WorkBuddy（侧边栏「插件」→「MCP 服务器」→「配置 MCP」）
2. 确认指示灯变绿
3. 导入 `skills/` 下三个 Skill
4. 跑一条样例题验证链路通畅

### P5 有效性验证实验

**人工环节**：在 WorkBuddy 中执行 `/validate --split processbench --n 400`，等待跑完。

预计耗时受吞吐限制（README §10），建议分批跑。跑完后 Claude 负责读取 `results/validation/`、计算指标、分析未达标项。

### P6 评测执行 + Max Mode 消融

**人工环节**：执行 `/solve` 与 `/evaluate`，Max Mode 开 / 关各跑一轮。

### P7 人工抽检 —— 如何把人工降到最低

任务书要求「经人工抽检确认其中属于真实问题与属于误报的比例」，**这一步无法用自动方法替代**。但可以把它从「逐条判断」降为「逐条确认」：

1. Claude 对全部误报样本做一次**独立复核**，逐条给出：建议判断（真实问题 / 真误报）、理由、原标注、评估器判断
2. 生成 `results/human_audit/audit_sheet.csv`，每行只需人工填一个「同意 / 不同意」
3. 人工只需扫 50 行确认，**预计 30–45 分钟**，而非逐条从头分析

诚实说明：Claude 的预审意见会写进报告，并注明「人工确认了其中 N 条、推翻了 M 条」——**不把 Claude 的复核伪装成人工抽检**。

### P8 报告与 Demo

Claude 写完整 `docs/report.md`。**人工环节**：按 Claude 给出的分镜脚本录制 2 分钟内的 demo。

---

## P1 实测发现（四处修正了原设计）

原设计基于论文描述与部分抽样，P1 实现时核实了全量真实数据，有四处需要修正。

### 修正一：E9 并不稀缺，且随难度急剧上升

**这是本项目目前最有价值的发现。**

先前只看了 gsm8k（400 条）就下结论说 E9（答案对但过程错）仅占 1.75%、稀缺到必须全量取用。跑完全量 3400 条后，这个判断是**错的**：

| 难度层 | 题源 | 总数 | E9 数量 | **E9 占比** |
|---|---|---|---|---|
| T1 | GSM8K | 400 | 7 | **1.75%** |
| T2 | MATH | 1000 | 94 | **9.40%** |
| T3 | OlympiadBench | 1000 | 161 | **16.10%** |
| T4 | Omni-MATH | 1000 | 259 | **25.90%** |
| **合计** | | **3400** | **521** | **15.32%** |

**从 T1 到 T4，E9 占比翻了近 15 倍。**

这个梯度本身就是一条值得写进报告的结论：**题目越难，「答案对但推理站不住」的比例越高**。换句话说，只统计最终答案准确率，在难题上会系统性高估模型能力 —— 而这正是本项目立论的实证支撑。它同时直接回答了任务书要求的「模型能力边界与临界点分析」。

相应地，抽样策略改回三类等量（`per_cell: 40`），不再对 E9 特殊照顾；`TAKE_ALL` 机制保留但置空，留给以后自建中文子集中可能真正稀缺的类别。

抽样集实际规模：**447 条**（T1/e9 仅 7 条不足 40，按规则全取）。

### 修正二：ProcessBench 不提供标准答案

原 Schema 设计里有 `gold_answer` 字段。实测发现 ProcessBench **没有**这个字段，只提供 `final_answer_correct` 布尔值。

影响：`check.answer()` 无法在 ProcessBench 上验证 —— 只能直接采用它给出的布尔值。答案校验器的正确性改由 P2 自建的等式测试集回归。`gold_answer` 在 Schema 中保留为可选字段，供 DeltaBench 与自建中文子集使用。

### 修正三：DeltaBench 只能验证定位能力

实测 DeltaBench 的 math + code 子集共 935 条，**`final_correct` 全部为 0** —— 即全是答案错误的样本。

影响：DeltaBench **无法**用于测量误报率，也不含任何 E9 样本。它的用途收窄为**定位能力的跨域泛化验证**（数学 562 + 代码 373）。误报率与 E9 相关指标只在 ProcessBench 上给出。

另：其分段数据在 `sections` 字段（dict 列表），不是 `sections_content`（那是拼接后的单个字符串）。

### 修正四：步号基准

ProcessBench 的 `label` 是 **0-based**，`-1` 表示全部步骤正确；本项目统一 **1-based**。

转换：`first_error_step = label + 1 if label >= 0 else None`

这是容易悄悄出错的地方 —— 差一位不报错，只会让定位准确率被系统性低估。已由单元测试覆盖，且 loader 中加了越界断言。

---

## 依赖精简

原计划依赖 `datasets` 库（体积大、装起来慢）。实测 ProcessBench 的 parquet 文件四个 split 合计仅约 3.3 MB，因此改为**直接用 urllib 下载 parquet + pandas 读取**，去掉该依赖。

保留依赖：`sympy`、`pydantic`、`pyyaml`、`pandas`、`pyarrow`、`mcp`、`tqdm`、`statsmodels`。
