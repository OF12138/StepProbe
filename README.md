# StepProbe · 步验

> **面向可验证场景的过程评估与错误定位系统**
> 基于 WorkBuddy + Hy3 · Process-level evaluation and error localization for verifiable reasoning

---

## ⚠️ 声明

**本项目为个人参赛作品**，属于「犀牛鸟开源 - 混元大语言模型项目」实战任务的个人 / 活动提交，**与腾讯官方无关，并非腾讯官方发布的产品或工具**。

项目通过 **WorkBuddy 内的 Hy3 模型**调用大模型能力，**不涉及任何模型训练或微调**。

---

## 1. 项目简介

大模型在数学推理上"答对"越来越容易，但"答得对不对得住"却很少被检验。同一个正确答案，背后可能是一条严谨的推导链，也可能是猜选项、数值巧合、或误用定理却恰好抵消。

**StepProbe 做两件事：**

1. **应用侧** —— 在 WorkBuddy 中用 Hy3 解数学题，输出**结构化的完整解题过程**，而非仅有最终答案。
2. **评估侧** —— 构建一套过程评估器，判断推理链是否成立、**定位首个出错步骤**、**归类错误类型**，并识别**「答案正确但过程不成立」**的样本。

核心约束：**评估器本身必须先被证明可靠**，它对 Hy3 的评判才有意义。因此实验拆为两段 —— 先用带人工标注的公开数据验证评估器，再用验证过的评估器去评测 Hy3。

---

## 2. 为什么做过程评估

| 只看最终答案 | 看推理过程 |
|---|---|
| 选择题猜对 = 满分 | 能识别"蒙对" |
| 两个符号错误互相抵消 = 满分 | 能定位到第一个符号错误 |
| 跳过关键论证直接给结论 = 满分 | 能标记为跳步推导 |
| 无法回答"模型哪里不行" | 给出错误类型分布与能力边界 |

**目标用户**：需要把大模型接入教育测评、自动批改、推理能力诊断的开发者与研究者。

**待解决问题**：在有标准答案的可验证领域，如何低成本、可复现地判断"过程质量"，而不是只统计 pass@1。

**引入大模型的必要性**：步骤是否"跳步"、定理前提是否成立、论证是否循环 —— 这些判断依赖自然语言语义理解，规则引擎无法覆盖；但同时又存在大量可被符号计算确定判定的部分。本项目的价值正在于把两者切开（见 §3）。

---

## 3. 系统架构

WorkBuddy 是可执行本地任务的 Agent 工作站，支持通过 **MCP Server** 接入自定义工具，并支持 **Skills** 扩展。StepProbe 因此不是"调用大模型 API 的脚本"，而是**一组挂载到 WorkBuddy 上的确定性工具 + 一套评估 Skill**，由 WorkBuddy 中的 Hy3 驱动整个评测流程。

```
┌──────────────────────────────────────────────────────────────────┐
│                      WorkBuddy (Agent 运行时)                     │
│                        模型能力：Hy3                               │
│                                                                   │
│    Skills:  /solve    /evaluate    /validate                     │
│       │                                                           │
│       │   推理层 —— 交给 Hy3，需要语义理解的部分                    │
│       │     · L0  解题过程生成 / 步骤切分                          │
│       │     · L2  分步审查：给定前缀 steps[0:i]，step[i] 是否成立    │
│       │     · L3  全局复核：循环论证 / 跳步 / 条件遗漏 /            │
│       │                   「答案对但过程不成立」                     │
│       │                                                           │
└───────┼──────────────────────  MCP  ──────────────────────────────┘
        │
┌───────▼──────────────────────────────────────────────────────────┐
│            StepProbe MCP Server（纯 Python，零 LLM 调用）          │
│                                                                   │
│    确定性层 —— 不会"看走眼"，且成本为零                             │
│      · dataset.next_batch()      取分层样本                        │
│      · solution.segment()        结构化步骤切分                     │
│      · check.answer()            最终答案三级校验                   │
│      · check.step_symbolic()     步骤等式符号 / 数值代入校验         │
│      · verdict.record()          评判结果落盘                       │
│      · metrics.compute()         定位准确率 / 误报率 / 分层统计      │
│      · report.export()           结果表格导出                       │
└───────────────────────────────────────────────────────────────────┘
```

### 设计依据：为什么这样切分

**把能确定性判定的事情从 LLM 手里拿走。** 纯 LLM-as-judge 有个根本弱点 —— 评估器自己也会算错。`2x+3=7 ⟹ x=2` 这类判断应该由 SymPy 给出，而不是由另一个大模型"觉得"它对不对。因此：

- **能被符号计算判定的 → MCP 工具**（计算错误、答案校验、格式校验）：确定、免费、可复现
- **需要语义理解的 → Hy3**（定理是否适用、是否跳步、是否循环论证）：LLM 不可替代
- **先跑确定性层，再跑推理层**：大量计算类错误在 L1 即被拦下，显著减少昂贵的模型调用轮次

这一切分同时缓解了 WorkBuddy 无批量 API 带来的吞吐问题（见 §10）。

评估方法的完整设计与提示词见 [`docs/method.md`](docs/method.md)。

---

## 4. 错误分类体系

| 编号 | 错误类型 | 判定依据（可操作） | 主判定层 |
|---|---|---|---|
| E1 | 题意误读 | 解答求解的目标与题干所问不一致 | L3 |
| E2 | 概念 / 定理误用 | 引用定理的前提在本题不成立 | L2 |
| E3 | 计算错误 | 该步等式经符号或数值校验不成立 | **L1（确定性）** |
| E4 | 条件遗漏 | 题干给定条件未被使用，且该条件影响结果 | L3 |
| E5 | 跳步推导 | 相邻步骤间缺少必要中间推导，无法由前推出后 | L2 |
| E6 | 循环论证 | 该步的论证依赖了待证结论本身 | L3 |
| E7 | 幻觉引用 | 引用了不存在的定理 / 公式 / 已知量 | L2 |
| E8 | 格式不符 | 最终答案形式不满足题目要求 | **L1（确定性）** |
| E9 | **答案正确但过程不成立** | 最终答案校验通过，但存在 E1–E7 之一 | L3 |

完整判定标准（含正例 / 反例）见 [`configs/taxonomy.yaml`](configs/taxonomy.yaml)。

> 本体系覆盖任务书点名的错误类型（题意误读、概念理解错误、计算错误、条件遗漏、跳步推导、格式不符），并可与 PRMBench 的九类标注维度做映射，从而直接用公开人工标注验证分类一致性。

---

## 5. 评测数据

**题目与过程标注均取自公开数据集，不做大规模自行人工标注。**

| 数据集 | 用途 | 提供的标注 |
|---|---|---|
| **ProcessBench** | 评估器有效性验证（主） | 人工专家标注的**最早出错步骤**；含"全部步骤正确"样本，用于误报率测量 |
| **PRMBench** | 错误类型一致性验证 | 步骤级标注 + 显式错误类型维度 |
| **DeltaBench** | 跨域泛化验证（次） | 长 CoT 分段标注：首个出错步号 + 解释 + 修正 |
| **自建中文子集** | 评测执行 | 中文高考 / 竞赛题，标准答案可自动校验 |

### 难度分层与分层依据

ProcessBench 的题目来源本身构成一条难度阶梯，直接作为分层依据：

| 层级 | 来源 | 说明 |
|---|---|---|
| T1 基础 | GSM8K | 小学应用题，2–8 步算术推理 |
| T2 中等 | MATH | 高中竞赛题，含代数 / 几何 / 数论 |
| T3 困难 | OlympiadBench | 奥赛级别 |
| T4 极难 | Omni-MATH | 高难度竞赛题集 |

样本来源、构造方式、分层依据与抽样方案详见 [`docs/dataset.md`](docs/dataset.md)。

---

## 6. 有效性验证方案

评估器在用于评测 Hy3 之前，先在带人工标注的数据上证明可靠。

### 6.1 定位准确率（在答案错误的样本上）

- `Detection Rate` —— 判定"过程存在问题"的比例
- `Exact Localization Acc` —— 定位步号与标注完全一致
- `Tolerant Localization Acc (±1)` —— 允许 ±1 步偏差（步骤切分粒度差异所致）

### 6.2 误报率（在答案正确且标注为过程无误的样本上）

- `False Positive Rate`
- **人工抽检**：对 ≥50 条误报样本逐条人工复核，区分**真实问题**（原标注漏标，评估器其实是对的）与**真误报**，记录留存于 `results/human_audit/`

### 6.3 错误类型一致性

映射到 PRMBench 标注维度后，计算 Accuracy / Macro-F1 / 混淆矩阵。

### 6.4 稳定性（附加）

同一条推理链重复评估 5 次，统计首个错误步号的众数占比与错误类型的一致率，用以说明评估结论不是随机波动的产物。

---

## 7. 评测执行与结果分析

用验证后的评估器对 Hy3 生成的解题过程做一次完整评测，输出：

- **最终答案准确率 × 过程正确率**的交叉分布，重点关注**「答案对但过程错」象限**的占比
- **错误类型分布**
- **难度分层结果**，指出模型表现开始明显下降的难度区间
- **典型 case 归因分析**
- **Max Mode 消融**：WorkBuddy 提供 Max Mode 开关。分别在**开启 / 关闭** Max Mode 下跑同一批题目，比较最终答案准确率与**过程正确率**的变化 —— 检验更强的推理配置究竟是提升了推导严谨性，还是只是把答案蒙得更准。这是本项目唯一可控的模型侧变量，作为独立一节分析。

---

## 8. 环境要求

- **WorkBuddy 桌面端**（开发时版本 v5.3.14），已登录且可使用 **Hy3** 模型
- **Python ≥ 3.10**（运行 StepProbe MCP Server）
- 依赖见 [`requirements.txt`](requirements.txt)

> **无需任何大模型 API Key。** 模型能力全部经由 WorkBuddy 调用，本项目不持有、不存储、不传输任何模型密钥。

---

## 9. 快速开始

### 9.1 安装

```bash
git clone https://github.com/OF12138/StepProbe.git
cd stepprobe
pip install -r requirements.txt
```

### 9.2 拉取并规整评测数据

```bash
cp .env.example .env      # 按需修改路径配置
python -m stepprobe.data.build --config configs/default.yaml
```

### 9.3 注册 MCP Server 到 WorkBuddy

编辑配置文件（二选一）：

- **用户级**（跨项目复用）：`~/.workbuddy/mcp.json`
  Windows 为 `%USERPROFILE%\.workbuddy\mcp.json`
- **项目级**（仅当前项目生效）：`<项目目录>/.workbuddy/mcp.json`

```json
{
  "mcpServers": {
    "stepprobe": {
      "command": "python",
      "args": ["-m", "stepprobe.mcp_server"],
      "env": {
        "STEPPROBE_DATA_DIR": "./data",
        "STEPPROBE_RESULTS_DIR": "./results"
      }
    }
  }
}
```

在 WorkBuddy 中：**侧边栏「插件」→ 右上角「MCP 服务器」→「配置 MCP」**，粘贴上述配置。

保存后查看状态指示灯：**🟢 绿色 = 连接成功**；🔴 红色 = 检查配置内容、命令环境或路径是否正确。

> 配置格式请以你所用 WorkBuddy 版本的官方文档为准，不同版本字段可能有差异。
>
> **密钥管理**：所有配置通过环境变量或 `.env` 传入，仓库仅提供 `.env.example`，`.env` 已在 `.gitignore` 中忽略，**不硬编码任何密钥**。

### 9.4 安装 Skills

将 [`skills/`](skills/) 下的 `solve` / `evaluate` / `validate` 导入 WorkBuddy，随后在对话框中以 `/` 调用。

### 9.5 运行

在 WorkBuddy 中依次执行：

```
/validate  --split processbench --n 400
    → 在人工标注数据上验证评估器，产出定位准确率与误报率

/solve     --tier all --n 200 --max-mode on
    → 用 Hy3 生成结构化解题过程

/evaluate  --run latest
    → 对生成结果做过程评估，产出完整结果表格与错误类型分布
```

结果写入 `results/`，报告由 `metrics.compute()` 与 `report.export()` 生成。

---

## 10. 已知约束：吞吐

WorkBuddy 是交互式 Agent 工作站，**没有批量推理 API**，所有模型调用都发生在 Agent 轮次中。这是本项目最主要的工程约束，应对方式：

1. **确定性层前置** —— 计算类错误由 SymPy 拦截，不消耗模型轮次
2. **整链单次评审** —— 一次调用评审整条推理链并返回结构化 JSON，而非逐步骤各调用一次
3. **分层抽样** —— ProcessBench 全量 3,400 条不全跑，按四个难度层**分层抽样约 400 条**，抽样方案与置信区间见 `docs/dataset.md`
4. **利用 WorkBuddy 的并行多 Agent 执行**分担长任务

评测规模因此以**分层抽样集**为准，报告中明确标注样本量与抽样方式，**不夸大覆盖范围**。

---

## 11. 目录结构

```
stepprobe/
├── README.md
├── LICENSE
├── .env.example              # 配置样例（不含真实密钥）
├── .gitignore
├── requirements.txt
├── configs/
│   ├── default.yaml          # 数据与运行配置
│   └── taxonomy.yaml         # 错误分类体系定义（含判定标准）
├── src/stepprobe/
│   ├── mcp_server/           # MCP Server 入口与工具注册
│   │   ├── __main__.py
│   │   └── tools/            # dataset / check / verdict / metrics / report
│   ├── checkers/
│   │   ├── answer.py         # 最终答案三级校验
│   │   ├── symbolic.py       # 步骤等式符号与数值校验
│   │   └── segment.py        # 步骤切分
│   ├── data/                 # 各数据集 → 统一 schema
│   └── metrics/              # 定位准确率、误报率、一致性
├── skills/                   # WorkBuddy Skills
│   ├── solve/                # 应用侧：结构化解题
│   ├── evaluate/             # L2 分步审查 + L3 全局复核
│   └── validate/             # 有效性验证流程编排
├── data/                     # 规整后的评测数据（不入库）
├── results/
│   ├── validation/           # 有效性验证结果
│   ├── eval/                 # 完整评测结果表格
│   └── human_audit/          # 人工抽检记录
└── docs/
    ├── dataset.md            # 样本来源、构造方式、分层依据、抽样方案
    ├── method.md             # 评估方法设计依据与提示词设计
    └── report.md             # 分析报告（评测完成后产出）
```

---

## 12. 项目状态

> **当前处于设计完成阶段，代码尚未实现。** 下表为实现进度。

| 模块 | 状态 |
|---|---|
| 方案设计与错误分类体系 | ✅ 已完成 |
| 数据说明与抽样方案 | ✅ 已完成 |
| 评估方法与提示词设计 | ✅ 已完成 |
| 数据管线 | ⬜ 未开始 |
| MCP Server 骨架 | ⬜ 未开始 |
| L1 确定性校验工具 | ⬜ 未开始 |
| solve / evaluate / validate Skills | ⬜ 未开始 |
| 有效性验证实验 | ⬜ 未开始 |
| 完整评测与分析报告 | ⬜ 未开始 |
| Demo 视频 / GIF | ⬜ 未开始 |

---

## 13. 参考

- WorkBuddy 官方文档 —— https://www.workbuddy.ai/docs/zh/workbuddy/
- WorkBuddy MCP 指南 —— https://www.workbuddy.ai/docs/zh/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/MCP-Guide
- Hy3 —— https://github.com/Tencent-Hunyuan/Hy3
- ProcessBench —— https://arxiv.org/abs/2412.06559
- PRMBench —— https://arxiv.org/abs/2501.03124
- DeltaBench —— https://huggingface.co/datasets/OpenStellarTeam/DeltaBench
- BIG-Bench Mistake —— https://github.com/WHGTyen/BIG-Bench-Mistake

各数据集的使用均遵循其原始许可协议，详见 [`docs/dataset.md`](docs/dataset.md)。

## License

MIT
