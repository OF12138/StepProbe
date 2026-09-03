"""统一数据 Schema。

各数据集经 loader 规整为这里定义的结构，下游（抽样、校验、评估、指标）
只面向这一套模型编程，不再关心原始数据集的字段差异。

步号约定
--------
本项目**统一使用 1-based 步号**（第一步是 1，不是 0），与 skills/solve
的输出规范一致。各数据集原始索引在 loader 中完成转换。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class Tier(str, Enum):
    """难度层。分层依据见 docs/dataset.md §3。"""

    T1 = "T1"  # GSM8K —— 小学应用题
    T2 = "T2"  # MATH —— 高中竞赛
    T3 = "T3"  # OlympiadBench —— 奥赛
    T4 = "T4"  # Omni-MATH —— 高难度竞赛


class LabelClass(str, Enum):
    """样本的标注类别，抽样分层的依据。

    这是三分类而非二分类 —— E9（答案对但过程错）必须单独成类，
    否则它会被淹没在多数类里。实测 ProcessBench 中 E9 仅占约 1.75%，
    详见 docs/plan.md「重要修正」。
    """

    FAULTY_WRONG = "faulty_wrong"    # 过程有误 + 答案错误 → 用于定位准确率
    CLEAN_CORRECT = "clean_correct"  # 过程无误 + 答案正确 → 用于误报率
    E9 = "e9"                        # 过程有误 + 答案正确 → E9 识别能力
    OTHER = "other"                  # 过程无误 + 答案错误（罕见，应人工核查）


class Step(BaseModel):
    """解题过程中的一步。"""

    index: int = Field(..., ge=1, description="1-based 步号，连续不跳号")
    content: str
    equations: list[str] = Field(
        default_factory=list,
        description="该步涉及的等式，SymPy 可解析形式。L1 校验的输入。",
    )
    justification: str | None = Field(
        default=None, description="本步依据（定理名 / 公式名 / 变形手段）"
    )


class Label(BaseModel):
    """人工标注的 ground truth。仅验证集有，评测集为 None。"""

    process_correct: bool
    first_error_step: int | None = Field(
        default=None, ge=1, description="1-based 首个出错步号；过程无误时为 None"
    )
    answer_correct: bool | None = None
    error_type: str | None = Field(
        default=None, description="错误类型编号 E1–E9，见 configs/taxonomy.yaml"
    )
    annotator: str = "human"

    @model_validator(mode="after")
    def _check_consistency(self) -> Label:
        # 过程无误就不该有出错步号；过程有误就必须指出是哪一步。
        # 这个约束能在 loader 转换出错时立刻暴露问题，而不是让它悄悄传到指标里。
        if self.process_correct and self.first_error_step is not None:
            raise ValueError("process_correct=True 时 first_error_step 必须为 None")
        if not self.process_correct and self.first_error_step is None:
            raise ValueError("process_correct=False 时必须给出 first_error_step")
        return self

    @property
    def label_class(self) -> LabelClass:
        """按 (过程是否正确, 答案是否正确) 归入抽样类别。"""
        if self.process_correct:
            return LabelClass.CLEAN_CORRECT if self.answer_correct else LabelClass.OTHER
        return LabelClass.E9 if self.answer_correct else LabelClass.FAULTY_WRONG


class Sample(BaseModel):
    """一条评测样本：题目 + 待评估的解题过程 (+ 可选标注)。"""

    id: str
    source: str = Field(..., description="ProcessBench / DeltaBench / cn_custom ...")
    tier: Tier
    problem: str
    gold_answer: str | None = Field(
        default=None,
        description="标准答案。ProcessBench 不提供该字段，只给 answer_correct 布尔值。",
    )
    solution_steps: list[str]
    label: Label | None = None
    meta: dict = Field(default_factory=dict)

    @property
    def n_steps(self) -> int:
        return len(self.solution_steps)

    def without_label(self) -> Sample:
        """剥离标注后的副本。

        评估阶段**必须**用这个方法取样本 —— 一旦 ground truth 进入模型上下文，
        本次验证就作废了。MCP 工具 dataset.next_batch() 默认走这条路径。
        """
        return self.model_copy(update={"label": None})


class StepVerdict(BaseModel):
    """单步评判结果。"""

    index: int = Field(..., ge=1)
    valid: bool | None = Field(
        default=None, description="None 表示无法判定（L1 的 UNKNOWN），需交给上层"
    )
    error_type: str | None = None
    evidence: str | None = None
    layer: str | None = Field(default=None, description="做出判定的层：L1 / L2 / L3")


class Verdict(BaseModel):
    """一条推理链的最终评判结果。"""

    sample_id: str
    answer_correct: bool | None = None
    process_valid: bool
    first_error_step: int | None = Field(default=None, ge=1)
    error_type: str | None = None
    evidence: str | None = None
    layer: str | None = None
    e9_signals: list[str] = Field(default_factory=list)
    steps: list[StepVerdict] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_evidence(self) -> Verdict:
        # 判错必须举证。无证据的判错不可信，见 configs/taxonomy.yaml principles。
        if not self.process_valid and not (self.evidence or "").strip():
            raise ValueError("判定过程有误时 evidence 不得为空")
        return self
