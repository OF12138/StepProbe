"""P2 L1 确定性校验器测试。

除常规正确性外，重点覆盖两条设计红线：
  1. 含自由符号的等式**永不判 FALSE**（解方程的每一步都会被误报）
  2. 判不出来一律 UNKNOWN，不判错（宁可漏报，不可误报）
"""

from __future__ import annotations

import pytest

from stepprobe.checkers.answer import Equivalence, check_answer, check_format
from stepprobe.checkers.latex import normalize, to_expr
from stepprobe.checkers.segment import segment, to_steps
from stepprobe.checkers.symbolic import (
    StepVerdictValue,
    check_equation,
    check_step,
    extract_equations,
    first_false_step,
)


# ---------------------------------------------------------------------------
# LaTeX 规范化
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected_value"),
    [
        (r"\frac{1}{2}", 0.5),
        (r"\dfrac{3}{4}", 0.75),
        (r"\frac{\frac{1}{2}}{2}", 0.25),      # 嵌套
        (r"\sqrt{4}", 2),
        (r"\sqrt[3]{8}", 2),
        (r"\boxed{7}", 7),
        (r"\text{5}", 5),
        (r"2 \cdot 3", 6),
        (r"10 \div 2", 5),
        (r"$12$", 12),
        (r"1,234", 1234),                       # 千位分隔符
        (r"\left( 2 + 3 \right)", 5),
    ],
)
def test_normalize_and_parse(raw: str, expected_value: float) -> None:
    expr = to_expr(raw)
    assert expr is not None, f"{raw!r} 应能解析，规范化结果为 {normalize(raw)!r}"
    assert abs(float(expr.evalf()) - expected_value) < 1e-9


def test_unparseable_returns_none() -> None:
    """解析不了必须返回 None，让上层判 UNKNOWN，绝不猜。"""
    assert to_expr("这是一段纯中文叙述") is None
    assert to_expr("") is None


# ---------------------------------------------------------------------------
# 答案三级校验
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("pred", "gold"),
    [
        ("1/2", "0.5"),
        (r"\frac{1}{2}", "0.5"),
        (r"\frac{1}{2}", "1/2"),
        ("(x+1)^2", "x^2+2x+1"),               # 符号等价
        ("2x+3", "3+2*x"),
        (r"\sqrt{2}", "2^(1/2)"),
        ("pi/4", r"\frac{\pi}{4}"),
        ("A", "(A)"),                           # 选择题
        ("yes", "是"),
        ("(1, 2)", "(1,2)"),                    # 元组
        ("{1,2,3}", "{3,2,1}"),                 # 集合：顺序无关
        (r"\boxed{42}", "42"),
    ],
)
def test_answer_equal(pred: str, gold: str) -> None:
    r = check_answer(pred, gold)
    assert r.verdict is Equivalence.EQUAL, f"{pred!r} vs {gold!r} → {r.verdict} ({r.detail})"


@pytest.mark.parametrize(
    ("pred", "gold"),
    [
        ("1/2", "1/3"),
        ("(x+1)^2", "x^2+1"),
        ("A", "B"),
        ("(1, 2)", "(1, 3)"),
        ("{1,2}", "{1,2,3}"),                   # 分量个数不同
        ("42", "43"),
    ],
)
def test_answer_not_equal(pred: str, gold: str) -> None:
    r = check_answer(pred, gold)
    assert r.verdict is Equivalence.NOT_EQUAL, f"{pred!r} vs {gold!r} → {r.verdict}"


@pytest.mark.parametrize("pred,gold", [("", "5"), ("5", ""), ("纯中文答案", "另一段中文")])
def test_answer_unknown_not_wrong(pred: str, gold: str) -> None:
    """判不出来必须是 UNKNOWN，不能是 NOT_EQUAL。"""
    assert check_answer(pred, gold).verdict is Equivalence.UNKNOWN


def test_answer_result_correct_property() -> None:
    assert check_answer("1/2", "0.5").correct is True
    assert check_answer("1/2", "1/3").correct is False
    assert check_answer("", "").correct is False   # UNKNOWN 不算正确


# ---------------------------------------------------------------------------
# 等式判定 —— 核心红线
# ---------------------------------------------------------------------------

def test_closed_form_false_is_detected() -> None:
    """闭式数值不相等 → FALSE，这是 E3 的判定依据。"""
    assert check_equation("2+3", "6").verdict is StepVerdictValue.FALSE
    assert check_equation("194 - 11*17", "8").verdict is StepVerdictValue.FALSE


def test_closed_form_true() -> None:
    assert check_equation("2+3", "5").verdict is StepVerdictValue.TRUE
    assert check_equation("11*17+9", "196").verdict is StepVerdictValue.TRUE  # 187+9=196


def test_identity_is_true() -> None:
    assert check_equation("(x+1)^2", "x^2+2x+1").verdict is StepVerdictValue.TRUE


def test_conditional_equation_never_false() -> None:
    """红线：含自由符号的等式永不判 FALSE。

    `x = 5` 在解方程语境下完全正确，但语法上与恒等式声明无法区分。
    若判 FALSE，解方程的每一步都会被误报为计算错误。
    """
    for lhs, rhs in [("x", "5"), ("2x+3", "7"), ("y", "x+1"), ("a^2", "16")]:
        v = check_equation(lhs, rhs).verdict
        assert v is not StepVerdictValue.FALSE, f"{lhs} = {rhs} 被误判为 FALSE"


# ---------------------------------------------------------------------------
# 等式抽取
# ---------------------------------------------------------------------------

def test_extract_chained_equation() -> None:
    pairs = extract_equations("2 + 3 = 5 = 10/2")
    assert ("2 + 3", "5") in pairs
    assert ("5", "10/2") in pairs


@pytest.mark.parametrize(
    "text",
    [
        "设 x = 5，则原式成立",          # 定义，不是待验证等式
        "Let y = 3 and proceed",
        "因为 a ≈ 3.14 所以近似成立",     # 近似
        "由此可知 x >= 5",               # 不等式
        "这一步纯粹是文字叙述没有任何公式",
    ],
)
def test_extract_skips_non_assertions(text: str) -> None:
    assert check_step(text).verdict is StepVerdictValue.UNKNOWN


def test_check_step_reports_evidence() -> None:
    chk = check_step("计算得 2 + 3 = 6")
    assert chk.verdict is StepVerdictValue.FALSE
    assert "2 + 3" in chk.detail


def test_first_false_step_is_one_based() -> None:
    steps = ["先有 1 + 1 = 2", "再算 2 * 3 = 7", "于是 4 - 1 = 3"]
    assert first_false_step(steps) == 2


def test_first_false_step_none_when_clean() -> None:
    assert first_false_step(["1 + 1 = 2", "2 * 3 = 6"]) is None


# ---------------------------------------------------------------------------
# 格式校验 (E8)
# ---------------------------------------------------------------------------

def test_format_no_requirement_never_violates() -> None:
    """题目没提形式要求就不判 E8。"""
    assert check_format("3.14159", None).ok is True
    assert check_format("3.14159", {}).ok is True


def test_format_decimals() -> None:
    assert check_format("3.14", {"decimals": 2}).ok is True
    assert check_format("3.14159", {"decimals": 2}).ok is False


def test_format_simplified_fraction() -> None:
    assert check_format("1/2", {"simplified_fraction": True}).ok is True
    assert check_format("2/4", {"simplified_fraction": True}).ok is False


def test_format_unit() -> None:
    assert check_format("5 m/s", {"unit": "m/s"}).ok is True
    assert check_format("5", {"unit": "m/s"}).ok is False


# ---------------------------------------------------------------------------
# 步骤切分
# ---------------------------------------------------------------------------

def test_segment_by_explicit_numbering() -> None:
    text = "Step 1: 先化简\nStep 2: 再求解\nStep 3: 得出答案"
    assert segment(text) == ["先化简", "再求解", "得出答案"]


def test_segment_by_chinese_numbering() -> None:
    assert len(segment("步骤1：化简\n步骤2：求解")) == 2


def test_segment_by_circled_numbers() -> None:
    assert segment("①先化简 ②再求解") == ["先化简", "再求解"]


def test_segment_by_paragraph() -> None:
    assert len(segment("第一段内容\n\n第二段内容")) == 2


def test_list_input_is_never_resegmented() -> None:
    """红线：公开数据集的原有索引必须原样保留，重切会让步号整体错位。"""
    raw = ["一步。里面。有很多。句号。", "第二步"]
    steps = to_steps(raw)
    assert len(steps) == 2
    assert steps[0].content == raw[0]
    assert [s.index for s in steps] == [1, 2]


def test_to_steps_indices_are_one_based_and_contiguous() -> None:
    steps = to_steps("Step 1: a\nStep 2: b\nStep 3: c")
    assert [s.index for s in steps] == [1, 2, 3]


def test_segment_empty() -> None:
    assert segment("") == []
    assert to_steps([]) == []


# ---------------------------------------------------------------------------
# P2 实测后补充的回归用例
#
# 下面每一条都对应一个在真实 ProcessBench 数据上抓到的误报。
# 全部要求判 UNKNOWN 而非 FALSE —— 它们数学上都是对的。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "why"),
    [
        (r"\varphi(41) = 41 - 1", "未知命令 \varphi 被剥掉后变成 (41)=40"),
        (r"\binom{40}{17} = \binom{40}{23}", "\binom 被剥掉后变成 (40)(17)=680"),
        (r"f(2) = 4(2) + 6 = 8 + 6 = 14 \tag{7}", "\tag{7} 被当成乘 7"),
        (r"4(-1) = -4 \equiv -1 \equiv 2 \pmod{3}", "同余式不是普通等式"),
        (r"3\frac{4}{5} = \frac{3 \cdot 5 + 4}{5}", "带分数被隐式乘法解析成 3*(4/5)"),
        (r"3970 \div 8 = 496 \text{ remainder } 2", "带余除法：右侧是商+余数"),
        (r"49 \div 9 = 5", "整除取整惯用法"),
        (r"\log_{10} 8 / \log_{10} 2 = 3", "函数下标底数无法解析"),
        (r"0 \implies x = \pm 1", "推导箭头不是等式"),
        (r"2235^\circ \div 360^\circ = 6.208333\ldots", "截断小数 + 省略号"),
    ],
)
def test_real_world_false_positives_are_unknown(text: str, why: str) -> None:
    verdict = check_step(text).verdict
    assert verdict is not StepVerdictValue.FALSE, f"误报回归：{why}"


def test_display_math_blocks_are_split() -> None:
    r"""\[ \] 必须先切开，否则跨块会拼出假等式。

    不切的话 "143 \] \[ 143 + 73" 会被当成一个等式左侧。
    """
    text = r"\[ 71 + 72 = 143 \] \[ 143 + 73 = 216 \] \[ 216 + 74 = 290 \]"
    assert check_step(text).verdict is StepVerdictValue.TRUE


def test_truncated_decimal_tolerated() -> None:
    """按显示的小数位数设容差，截断值不算错。"""
    assert check_equation("2235/360", "6.208333").verdict is StepVerdictValue.TRUE


def test_genuine_integer_division_error_still_caught() -> None:
    """整除惯用法的豁免不能宽到放过真错：49//9 是 5，不是 6。"""
    assert check_equation(r"49 \div 9", "6").verdict is StepVerdictValue.FALSE


# ---------------------------------------------------------------------------
# 答案装饰的剥离
# ---------------------------------------------------------------------------
#
# 这批用例不是想出来的，是从 P6 题目集的交叉印证里**捞出来的**：同一道题被两个
# 模型答对，两份正确答案却被判为不等。逐条看下去，23 处不一致里 20 处是装饰差异，
# 不是答案差异。修完交叉印证一致率从 79.1% 升到 95.5%。


@pytest.mark.parametrize(
    ("pred", "gold"),
    [
        (r"x = \sqrt{2}", r"\sqrt{2}"),           # 变量名前缀
        ("y = 12", "12"),
        (r"f(x) = x + 22", "x + 22"),             # 函数名前缀
        (r"\sin \theta = \frac{-1+\sqrt{5}}{2}", r"\frac{-1 + \sqrt{5}}{2}"),
        (r"4^\circ", "4"),                        # 角度单位
        (r"4^{\circ}", "4"),
        (r"17.5 \%", "17.5"),                     # 百分号
        ("1, 3", r"1 \text{ and } 3"),            # 连接词当分隔符
        ("0.01, 100", r"0.01 \text{ and } 100"),
    ],
)
def test_decoration_does_not_make_equal_answers_unequal(pred: str, gold: str) -> None:
    assert check_answer(pred, gold).correct, f"{pred!r} vs {gold!r} 应判相等"


@pytest.mark.parametrize(
    ("pred", "gold"),
    [
        ("4.0", "4.2"),
        ("567", r"\frac{567}{4}"),
        ("x = 3", "4"),                # 剥掉前缀后仍然不等
        (r"5^\circ", "4"),             # 剥掉单位后仍然不等
        ("1, 3", "1, 4"),
    ],
)
def test_decoration_stripping_never_washes_unequal_into_equal(pred: str, gold: str) -> None:
    """剥离是兜底，只能把「本该相等」救回来，不能把「本来不等」洗成相等。"""
    assert not check_answer(pred, gold).correct, f"{pred!r} vs {gold!r} 不应判相等"


def test_multi_variable_answer_keeps_its_equals_signs() -> None:
    """"x = 1, y = 2" 这类多变量答案不能被剥成 "1, y = 2"。

    _strip_decoration 只在剥完不再含 "=" 时才采用剥离结果。
    """
    from stepprobe.checkers.answer import _strip_decoration

    assert _strip_decoration("x = 1, y = 2") == "x = 1, y = 2"
    assert check_answer("x = 1, y = 2", "x = 1, y = 2").correct


# ---------------------------------------------------------------------------
# 答案归一化：从 P6 两臂对比里捞出来的四个 bug
# ---------------------------------------------------------------------------
#
# 这四条都不会报错，只会让「同一个答案的两种写法」被判成不等。放在 P6 里就是
# 凭空造出配对差分 —— 而 160 道题只产生 10 条差分，噪声占了 7 条，McNemar
# 检验测的基本是格式而不是模型。


@pytest.mark.parametrize(
    ("pred", "gold", "why"),
    [
        # 1) 千位分隔符规则误吃元组里的逗号：(3,331) → 3331，而 (3, 331) 因为
        #    有空格逃过。同一个答案的两种写法被判成不等。
        ("(1,1006), (3,331), (11,31)", "(1, 1006), (3, 331), (11, 31)", "元组内逗号"),
        # 2) `\b` 在 g 与 _ 之间不成立，`\log_{10}` 的 \log 被当未识别命令删掉
        ("(4, log_10(2))", r"(4, \log_{10}(2))", "带下标的函数名"),
        ("x_1 + x_2", r"x_{1} + x_{2}", "简单下标花括号"),
        # 3) 首尾字符判包裹：`(a,b), (c,d)` 被剥成 `a,b), (c,d`，分量全错
        (
            "(22 + 8*sqrt(6), 22 - 8*sqrt(6)), (22 - 8*sqrt(6), 22 + 8*sqrt(6))",
            r"(22 + 8\sqrt{6}, 22 - 8\sqrt{6}) \text{ and } (22 - 8\sqrt{6}, 22 + 8\sqrt{6})",
            "并列元组",
        ),
    ],
)
def test_same_answer_written_differently_is_equal(pred: str, gold: str, why: str) -> None:
    assert check_answer(pred, gold).verdict is Equivalence.EQUAL, why


def test_thousands_separator_still_stripped_outside_brackets() -> None:
    """修元组不能把千位分隔符本身修没了。"""
    assert check_answer("1,234", "1234").verdict is Equivalence.EQUAL
    assert check_answer("1,234,567", "1234567").verdict is Equivalence.EQUAL


@pytest.mark.parametrize(
    ("pred", "gold"),
    [
        ("{n : n >= 1, n != 2}", r"\{n \mid n \geq 1 \text{ and } n \neq 2\}"),
        ("{2^r | r in Z^+}", r"\{2^r \mid r \in \mathbb{Z}^+\}"),
        ("f(x) = c x (c positive integer)", r"f(x) = cx \text{ for any positive integer } c"),
    ],
)
def test_set_builder_and_prose_answers_are_unknown_not_wrong(pred: str, gold: str) -> None:
    """判不了就说判不了，不要自信地判错。

    `{n : n >= 1, n != 2}` 会被当成三分量元组，与标准答案比出「分量个数不同」
    然后判定答错 —— 但两者是同一个集合。UNKNOWN 不进准确率分母，NOT_EQUAL 进，
    这个区别在 P6 里是实打实的偏置：一臂写 ASCII 集合式被判「错」，另一臂写
    自然语言被判「未知」，一个进分母一个不进。
    """
    assert check_answer(pred, gold).verdict is Equivalence.UNKNOWN


@pytest.mark.parametrize(
    ("pred", "gold"),
    [
        ("4", "5"),
        ("1, 3", "1, 4"),
        ("(1,2)", "(1,3)"),
        ("4.0", "4.2"),
        ("x = 3", "4"),
        ("567", r"\frac{567}{4}"),
        ("(1,1006), (3,331)", "(1, 1007), (3, 331)"),
    ],
)
def test_normalization_relaxations_never_manufacture_equality(pred: str, gold: str) -> None:
    """所有放宽都必须是单向的：能把「本该相等」救回来，不能把「本来不等」洗成相等。"""
    assert check_answer(pred, gold).verdict is Equivalence.NOT_EQUAL


@pytest.mark.parametrize(
    ("pred", "gold"),
    [
        ("10", "10 hours"),          # 裸写的单位词
        ("2 hours", "2"),
        ("-7, -1, 1, 7", "±1, ±7"),  # ± 记号与展开写法
        ("1, -1", r"\pm 1"),
    ],
)
def test_bare_units_and_plusminus(pred: str, gold: str) -> None:
    assert check_answer(pred, gold).verdict is Equivalence.EQUAL


@pytest.mark.parametrize(
    ("pred", "gold"),
    [
        # 单位词用白名单而非「任何结尾的词」，否则这一条会被洗成相等
        ("10 apples", "10 oranges"),
        ("10", "11 hours"),
        ("-7, -1, 1, 7", "±1, ±8"),
    ],
)
def test_unit_and_plusminus_relaxations_stay_one_way(pred: str, gold: str) -> None:
    assert check_answer(pred, gold).verdict is Equivalence.NOT_EQUAL


# ---------------------------------------------------------------------------
# 进制标注：L1 不该判它
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lhs", "rhs"),
    [
        ("192 + 16 + 4", "212_{10}"),                          # 数学上成立
        ("2048 + 1024 + 512 + 128 + 32 + 8 + 4 + 2", "3758_{10}"),
        ("212 + 3758", "3970_{10}"),
        ("21 \times 5", "105_{10}"),
        ("3 \cdot 8^2 + 2 \cdot 8^1 + 4 \cdot 8^0", "324_8"),  # 进制转换，两侧语义不同
    ],
)
def test_base_notation_is_not_judged_by_l1(lhs: str, rhs: str) -> None:
    r"""`212_{10}` 里的下标是**进制**不是下标，SymPy 读不懂它的语义。

    这是回归测试：为了让 `\log_{10}(2)` 与 `log_10(2)` 判等，normalize 加了
    `_{X}` → `_X`，副作用是进制标注从「解析失败落到 UNKNOWN」变成「能解析并
    判 FALSE」。进制转换题里每一步都带标注，所以误判是**成串**的 —— 全抽样集
    上 L1 误报率一度从 3.6% 升到 10.0%。

    L1 的立论是「触发时几乎不会错」，语义读不懂的记号必须让给 L2。
    """
    assert check_equation(lhs, rhs).verdict is StepVerdictValue.UNKNOWN


def test_base_notation_exemption_does_not_swallow_real_errors() -> None:
    """豁免只针对带进制标注的等式，不带标注的算错照判。"""
    assert check_equation("192 + 16 + 5", "212").verdict is StepVerdictValue.FALSE
    assert check_equation("2+3", "6").verdict is StepVerdictValue.FALSE
    # 下标是真下标（变量）时不受影响
    assert check_equation("x_1 + x_1", "2*x_1").verdict is StepVerdictValue.TRUE
