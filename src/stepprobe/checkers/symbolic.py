"""步骤等式的符号 / 数值校验（L1 确定性层）。

返回三值：FALSE / UNKNOWN / TRUE，语义见 docs/method.md §4.2。

    FALSE    等式确定不成立 → 判 E3 计算错误，短路
    UNKNOWN  无法判定（含自然语言推理、几何论述、条件等式）→ 交给 L2
    TRUE     等式成立 → 仍需交给 L3（每步算对不等于整体论证成立）

关键的保守设计：**只在等式两侧都是闭式数值时才判 FALSE。**

含自由符号的等式无法区分两种情况：
    · 恒等式声明     (x+1)^2 = x^2+2x+1   —— 不成立就是错
    · 条件等式/解方程  x = 5                —— 在解题语境中完全正确

二者语法上无法区分，若一律按恒等式判定，解方程的每一步都会被误报为计算错误。
因此含自由符号时最多判 TRUE（验证为恒等式），永不判 FALSE。

这直接服务于误报率指标 —— 见 configs/taxonomy.yaml principles。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

import sympy

from .answer import Equivalence, _numeric_equal, _symbolic_equal
from .latex import decimal_places, to_expr


class StepVerdictValue(str, Enum):
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"
    TRUE = "TRUE"


@dataclass
class EquationCheck:
    lhs: str
    rhs: str
    verdict: StepVerdictValue
    detail: str = ""


@dataclass
class StepCheck:
    verdict: StepVerdictValue
    equations: list[EquationCheck]
    detail: str = ""


#: 出现这些标记时不做等式判定 —— 它们表示近似、定义或赋值，不是等式声明
_SKIP_MARKERS = ("≈", r"\approx", "约等于", "≠", r"\neq", ":=", "?", "…", "...")

#: 带余除法惯用写法：`3970 \div 8 = 496 \text{ remainder } 2`。
#: 等号右侧是「商 + 余数」而非一个数值，按普通等式判会必然判错。
#: 这是 P2 实测中占比最高的单一误报来源（4 条误报里占 3 条）。
_REMAINDER_RE = re.compile(r"\bremainder\b|\bremain\w*\b|余数?|\bR\s*=|\bmod\b", re.IGNORECASE)

#: 函数名带下标（\log_{10}）本模块无法正确解析底数，跳过。
_FUNC_SUBSCRIPT_RE = re.compile(r"\\(log|ln|max|min)\s*_")

#: 「令 / 设 / let」引导的是定义，不是待验证的等式
_DEFINITION_RE = re.compile(
    r"(?:^|[，,。.；;])\s*(?:let|set|define|令|设|记|assume|suppose)\b",
    re.IGNORECASE,
)

#: 比较运算符 —— 含这些的不是纯等式，交给 L2
_INEQUALITY = ("<=", ">=", "<", ">", r"\le", r"\ge", r"\leq", r"\geq")

#: 公式块定界符：\[ \] \( \) $$ $。每个块内的等式互相独立，必须先切开。
_DISPLAY_MATH_RE = re.compile(r"\\\[|\\\]|\\\(|\\\)|\$\$|\$")


def extract_equations(text: str) -> list[tuple[str, str]]:
    """从步骤文本中抽取等式对。

    支持链式等式 a = b = c → [(a,b), (b,c)]。
    抽不出返回空列表（上层判 UNKNOWN）。
    """
    if not text or any(m in text for m in _SKIP_MARKERS):
        return []

    pairs: list[tuple[str, str]] = []
    # 先按公式块定界符切开。不切的话多个独立公式会被串成一个：
    #     \[ 71+72 = 143 \] \[ 143+73 = 216 \]
    # 直接 split("=") 会得到 "143 \] \[ 143+73" 这种跨块的假等式。
    blocks = _DISPLAY_MATH_RE.split(text)

    # 再按句子切，逐段找等式
    for chunk in (c for b in blocks for c in re.split(r"[。；;\n]|(?<=[a-z0-9)])\.\s", b)):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        if _DEFINITION_RE.search(chunk):
            continue
        if _REMAINDER_RE.search(chunk) or _FUNC_SUBSCRIPT_RE.search(chunk):
            continue
        if any(op in chunk for op in _INEQUALITY):
            continue
        # 去掉 == / != 之类
        if "==" in chunk or "!=" in chunk:
            continue

        # 先按中文切开：解题步骤常写成「计算得 2 + 3 = 6」，
        # 直接对整句 split("=") 会让左侧带上中文前缀而无法解析。
        for fragment in _CJK_SPLIT_RE.split(chunk):
            if "=" not in fragment:
                continue
            parts = [p.strip() for p in fragment.split("=")]
            parts = [p for p in parts if p and _looks_mathy(p)]
            if len(parts) < 2:
                continue
            for a, b in zip(parts, parts[1:]):
                pairs.append((a, b))

    return pairs


_MATHY_RE = re.compile(r"[\d\\]|[a-zA-Z]\s*[\^_(]|\bpi\b")

#: 按中日韩文字与中文标点切分，把叙述性前后缀剥掉，只留纯表达式片段。
#: 解题步骤常写成「计算得 2 + 3 = 6」，直接对整句 split("=") 会让左侧带上
#: 中文前缀而无法解析。
_CJK_SPLIT_RE = re.compile(r"[㐀-鿿぀-ヿ，、：:]+")


def _looks_mathy(text: str) -> bool:
    """粗筛：太长或不含数学记号的片段（多半是自然语言）直接排除。"""
    if len(text) > 200:
        return False
    return bool(_MATHY_RE.search(text))


_INT_DIV_RE = re.compile(r"^\s*(\d+)\s*(?:/|\\div)\s*(\d+)\s*$")


def _is_integer_division(lhs: str, rhs: str) -> bool:
    """识别「整数相除写成取整商」的惯用法。

    题解里常写 `49 \\div 9 = 5`（意思是分成 5 组、余 4），省略了余数部分。
    按普通等式判必然判错，但这不是计算错误。只有商恰好等于向下取整时才
    按惯用法处理 —— 真算错的（如 49/9 = 6）依然会被判 FALSE。
    """
    m = _INT_DIV_RE.match(lhs.strip())
    if not m:
        return False
    try:
        quotient = int(rhs.strip())
    except ValueError:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    return b != 0 and a % b != 0 and a // b == quotient


def _closed_form_verdict(a, b, rounding_tol: float) -> StepVerdictValue:
    """两侧均为闭式数值时的判定，带截断小数容差。"""
    try:
        va, vb = complex(a.evalf()), complex(b.evalf())
    except Exception:  # noqa: BLE001
        return StepVerdictValue.UNKNOWN
    if any(x != x or abs(x) == float("inf") for x in (va.real, vb.real)):
        return StepVerdictValue.UNKNOWN

    diff = abs(va - vb)
    scale = max(1.0, abs(va), abs(vb))
    if diff <= max(1e-9 * scale, rounding_tol):
        return StepVerdictValue.TRUE
    return StepVerdictValue.FALSE


def _rounding_tolerance(lhs: str, rhs: str) -> float:
    """按题解里写出的小数位数确定容差。

    题解常写截断值（`2235/360 = 6.208333`），按 1e-9 比会全判成错。
    显示到第 d 位小数，就允许 10^-d 的绝对误差。
    """
    places = [p for p in (decimal_places(lhs), decimal_places(rhs)) if p is not None]
    return 10.0 ** (-min(places)) if places else 0.0


def check_equation(lhs: str, rhs: str) -> EquationCheck:
    """判定单个等式。含自由符号时永不判 FALSE，理由见模块 docstring。"""
    a, b = to_expr(lhs), to_expr(rhs)
    if a is None or b is None:
        return EquationCheck(lhs, rhs, StepVerdictValue.UNKNOWN, "无法解析为表达式")

    has_symbols = bool(a.free_symbols or b.free_symbols)

    sym = _symbolic_equal(a, b)
    if sym is Equivalence.EQUAL:
        return EquationCheck(lhs, rhs, StepVerdictValue.TRUE, "符号等价")

    num = _numeric_equal(a, b)
    if num is Equivalence.EQUAL:
        return EquationCheck(lhs, rhs, StepVerdictValue.TRUE, "数值代入一致")

    if _is_integer_division(lhs, rhs):
        return EquationCheck(
            lhs, rhs, StepVerdictValue.UNKNOWN, "整除取整惯用法（商已隐含舍去余数）"
        )

    if not has_symbols:
        closed = _closed_form_verdict(a, b, _rounding_tolerance(lhs, rhs))
        if closed is not StepVerdictValue.UNKNOWN:
            detail = "闭式数值相等" if closed is StepVerdictValue.TRUE else "闭式数值不相等"
            return EquationCheck(lhs, rhs, closed, detail)

    if has_symbols:
        # 含自由符号且不是恒等式 —— 可能是条件等式（解方程），不判错
        return EquationCheck(
            lhs, rhs, StepVerdictValue.UNKNOWN, "含自由符号，无法区分恒等式与条件等式"
        )
    return EquationCheck(lhs, rhs, StepVerdictValue.UNKNOWN, "无法判定")


def check_step(text: str) -> StepCheck:
    """判定一个步骤。

    只要有任一等式判 FALSE，整步即 FALSE（首个不成立的等式即为证据）。
    全部 TRUE 则整步 TRUE；否则 UNKNOWN。
    """
    pairs = extract_equations(text)
    if not pairs:
        return StepCheck(StepVerdictValue.UNKNOWN, [], "未抽取到可判定的等式")

    checks = [check_equation(a, b) for a, b in pairs]

    for c in checks:
        if c.verdict is StepVerdictValue.FALSE:
            return StepCheck(
                StepVerdictValue.FALSE, checks, f"等式不成立：{c.lhs} = {c.rhs}（{c.detail}）"
            )

    if checks and all(c.verdict is StepVerdictValue.TRUE for c in checks):
        return StepCheck(StepVerdictValue.TRUE, checks, f"{len(checks)} 个等式均成立")

    return StepCheck(StepVerdictValue.UNKNOWN, checks, "存在无法判定的等式")


def check_steps(steps: list[str]) -> list[StepCheck]:
    """逐步判定。不做短路 —— 短路策略由评估流水线决定，这里只提供事实。"""
    return [check_step(s) for s in steps]


def first_false_step(steps: list[str]) -> int | None:
    """返回首个判 FALSE 的步号（1-based）；没有则 None。"""
    for i, chk in enumerate(check_steps(steps), start=1):
        if chk.verdict is StepVerdictValue.FALSE:
            return i
    return None


__all__ = [
    "EquationCheck",
    "StepCheck",
    "StepVerdictValue",
    "check_equation",
    "check_step",
    "check_steps",
    "extract_equations",
    "first_false_step",
    "sympy",
]
