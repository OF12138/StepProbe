# StepProbe

数学推理的过程评估与错误定位工具，运行在 WorkBuddy 中，模型能力由 Hy3 提供。

演示视频：[demo.mp4](demo.mp4)（1 分 51 秒）

## 项目简介

给定一道数学题和一条解题过程，StepProbe 判断这条推理链是否成立，找出第一个出错的步骤，给出错误类型，并识别「答案对、过程不成立」的情况。

只看最终答案会高估模型的推理能力。在 ProcessBench 全部 3400 条解答上统计，答案正确但过程有误的比例随难度明显上升：

| 难度层 | 题源 | 答案对但过程错 |
|---|---|---|
| T1 | GSM8K | 1.75% |
| T2 | MATH | 9.40% |
| T3 | OlympiadBench | 16.10% |
| T4 | Omni-MATH | 25.90% |

到了 Omni-MATH 这一层，大约每四个「答对」里就有一个推导错误。

## 主要结果

**L1 符号校验**（447 条样本，3172 步）：触发率 6.0%，触发时误报率 3.7%（1/27）。

**完整评估器**（60 条样本，盲评后与 ProcessBench 专家标注对照）：

| 指标 | 按原标注计算 | 人工抽检修正后 |
|---|---|---|
| 检出率 | 0.806 | 0.857 |
| 首错步精确定位 | 0.611 | 0.714 |
| 首错步 ±1 定位 | 0.750 | 0.829 |
| 误报率 | 0.083（2/24） | 0.000（0/23） |
| E9 召回 | 0.533 | 0.643 |

评估器与原标注不一致的 16 条样本经过人工逐条复核：10 条是评估器判错，4 条是原标注有误，2 条属于判定口径不同。右列按复核结论重算，两套数字都保留在报告里。误报率 0/23 的 95% 置信区间上界为 0.143。

两轮独立盲评的一致率：「是否有错」为 0.933，「错在哪一步、属于哪一类」为 0.667。

**Max Mode 消融**（160 道题，开、关两种设置下同题配对）：答案正确率 0.981 对 0.961，McNemar 检验 p = 0.250；两组过程成立率都是 1.000。在这批题目上没有测出差异，主要原因是题目集偏易，分析见报告 §6。

完整分析见 [docs/report.md](docs/report.md)。

## 工作原理

评估分四层，前两层不调用模型：

- **L0 步骤切分**：把解答切成编号步骤。
- **L1 符号校验**：用 SymPy 检查步骤里的等式，结果分为成立、不成立、无法判定三种。
- **L2 分步审查**：Hy3 只审 L1 无法判定的步骤，看定理用法是否正确、有没有跳步。
- **L3 全局复核**：Hy3 检查循环论证、条件遗漏和「答案对、过程错」。前面各层全部通过时也会执行。

等式成不成立交给 SymPy 计算，模型只处理需要语义理解的部分。这样做一方面避免评估器自己算错，另一方面减少了模型调用轮次。L1 在第 k 步发现错误时直接停止逐步审查，只保留 L3。

```
WorkBuddy（Hy3）
├─ Skills    /solve  /evaluate  /validate  /ablate
├─ L2 分步审查    只审 L1 无法判定的步骤
└─ L3 全局复核    循环论证、条件遗漏、答案对过程错
        │
        │ MCP
        ▼
StepProbe MCP Server（Python，不调用模型）
├─ L0 步骤切分    solution_segment
├─ L1 符号校验    check_step_symbolic  check_answer  check_format
└─ 数据与结果      dataset_next_batch  verdict_record  metrics_compute
                   report_export  p6_next_batch  p6_record

解答 ─ L0 ─ steps ─ L1 ─┬─ 无法判定的步骤 ─ L2 ─┐
                        └───────────────────────┴─ L3 ─ 评判结果（含证据）
```

MCP Server 提供 10 个工具。评判落盘时，判定为「有误」的结果必须附带 `evidence`，指明具体哪个论断不成立，否则工具拒绝写入。

四个 Skill 的分工：

| Skill | 用途 |
|---|---|
| `/solve` | 让 Hy3 输出结构化的分步解答 |
| `/evaluate` | 对一条推理链执行 L0–L3 评估 |
| `/validate` | 在带标注的数据上批量验证评估器 |
| `/ablate` | Max Mode 开关的同题配对实验 |

## 错误分类

| 编号 | 类型 | 判定依据 | 判定层 |
|---|---|---|---|
| E1 | 题意误读 | 解答求解的目标与题干所问不一致 | L3 |
| E2 | 概念 / 定理误用 | 引用定理的前提在本题不成立 | L2 |
| E3 | 计算错误 | 该步等式经符号或数值校验不成立 | L1 |
| E4 | 条件遗漏 | 题干条件未被使用，且影响结果 | L3 |
| E5 | 跳步推导 | 相邻步骤之间缺少必要推导 | L2 |
| E6 | 循环论证 | 论证依赖了待证结论本身 | L3 |
| E7 | 幻觉引用 | 引用了不存在的定理、公式或已知量 | L2 |
| E8 | 格式不符 | 最终答案形式不满足题目要求 | L1 |
| E9 | 答案对但过程不成立 | 答案校验通过，但存在 E1–E7 之一 | L3 |

判定只针对步骤内的实质错误。「未证唯一性」「论证不完整」这类问题不计入过程错误。完整标准和正反例见 [configs/taxonomy.yaml](configs/taxonomy.yaml)。

## 快速开始

需要 WorkBuddy 桌面端（开发时使用 v5.3.14）并能使用 Hy3，以及 Python 3.10 或更高版本。不需要任何模型 API Key。

**1. 安装**

```bash
git clone https://github.com/OF12138/StepProbe.git
cd StepProbe
pip install -e ".[mcp,dev]"
```

要以包的形式安装。WorkBuddy 启动 MCP Server 时工作目录不固定，装成包之后 `python -m stepprobe.mcp_server` 在任何目录下都能运行。

**2. 准备数据**

```bash
cp .env.example .env
python -m stepprobe.data.build --config configs/default.yaml
```

数据会写入 `data/`，不入库。国内网络可以在 `.env` 里设置 `HF_ENDPOINT` 使用镜像。

**3. 在 WorkBuddy 中注册 MCP Server**

把 `.workbuddy/mcp.json.example` 复制为项目目录下的 `.workbuddy/mcp.json`（或用户目录下的 `~/.workbuddy/mcp.json`），将其中两个路径改成本机的绝对路径。然后在 WorkBuddy 侧边栏「插件」→「MCP 服务器」→「配置 MCP」中加载，状态灯变绿即连接成功。

路径需要写绝对路径，写成 `./data` 会因为工作目录不确定而找不到数据。

**4. 导入 Skills**

把本仓库选为 WorkBuddy 的 workspace，`skills/` 下的四个 Skill 会被自动识别，也可以手动导入。之后在对话框里用 `/evaluate` 等命令调用。

**5. 运行**

评估单条推理链：在 WorkBuddy 中输入 `/evaluate`，附上题目和解题步骤。

验证评估器并做人工抽检：

```bash
# 在 WorkBuddy 中执行 /validate，得到 run_id 后：
python scripts/pre_audit.py --run <run_id>     # 生成分歧样本的判断包和抽检表
# 在 results/runs/<run_id>/audit_sheet.csv 的 human_verdict 列填写复核结论
python scripts/apply_audit.py --run <run_id>   # 按复核结论重算指标
```

Max Mode 消融：

```bash
python scripts/build_p6_set.py                 # 构建 160 道题的题目集
# 在 WorkBuddy 中执行 /ablate，完成后：
python scripts/p6_check.py --run <run_id>      # 检查进度与数据格式
python scripts/p6_score.py --run <run_id>      # 配对评分与 McNemar 检验
```

单独评测 L1：`python scripts/eval_l1.py`。运行测试：`pytest`（191 项）。

WorkBuddy 没有批量推理接口，评测需要在对话中分批发起，每批 20 到 40 条。中断后用同一个 `run_id` 续跑即可，已处理的条目会被跳过。各阶段可直接粘贴的提示词见 [docs/runbook.md](docs/runbook.md)。

## 目录结构

```
StepProbe/
├── demo.mp4                  演示视频
├── configs/
│   ├── default.yaml          数据、抽样与运行配置
│   └── taxonomy.yaml         错误分类与判定口径
├── src/stepprobe/
│   ├── mcp_server/           MCP 工具注册、实现与结果存储
│   ├── checkers/             答案校验、等式校验（L1）、LaTeX 规范化、步骤切分
│   ├── data/                 ProcessBench / DeltaBench 加载与分层抽样
│   ├── metrics/              定位准确率、误报率、一致性、稳定性
│   └── schema.py             统一数据模型与标注剥离
├── skills/                   solve / evaluate / validate / ablate
├── scripts/                  L1 评测、人工抽检、P6 题目集构建与评分
├── tests/
├── results/runs/<run_id>/    每次运行的评判、指标、报告与抽检表
└── docs/
```

## 文档

| 文档 | 内容 |
|---|---|
| [report.md](docs/report.md) | 分析报告：验证结果、人工抽检、Max Mode 消融、局限 |
| [method.md](docs/method.md) | 评估方法、提示词设计、判定口径、指标定义 |
| [dataset.md](docs/dataset.md) | 数据来源、难度分层、抽样方案 |
| [plan.md](docs/plan.md) | 各阶段的实施记录 |
| [runbook.md](docs/runbook.md) | WorkBuddy 中的分批操作提示词 |
| [demo.md](docs/demo.md) | 演示视频脚本 |

## 局限

- 完整评估器只在 60 条样本上验证过，置信区间较宽。
- 判定口径只覆盖步骤内的实质错误，「每一步都对但整体论证有缺口」不在覆盖范围内。
- Max Mode 消融使用的题目集偏易，过程维度没有产生差异，需要更难且标准答案可靠的题目集重做。

## 参考

- [WorkBuddy 文档](https://www.workbuddy.ai/docs/zh/workbuddy/)
- [Hy3](https://github.com/Tencent-Hunyuan/Hy3)
- [ProcessBench](https://arxiv.org/abs/2412.06559)
- [DeltaBench](https://huggingface.co/datasets/OpenStellarTeam/DeltaBench)

数据集的使用遵循各自的原始许可，见 [docs/dataset.md](docs/dataset.md)。

## 许可

[MIT](LICENSE)
