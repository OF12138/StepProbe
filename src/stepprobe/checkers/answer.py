"""最终答案三级校验（L1 确定性层）。

三级依次尝试，任一级判定相等即通过：

    1. 规范化后字符串比对      1/2 vs 0.5 vs \\frac{1}{2}
    2. SymPy 符号等价          (x+1)^2 vs x^2+2x+1
    3. 随机数值代入 + 容差      符号化简超时或失败时的兜底

设计依据见 docs/method.md §4.1。LaTeX 答案形式高度自由，单纯字符串比对会
产生大量假阴性；纯符号化简在复杂表达式上又可能超时。三级级联兼顾两者。

判不出来返回 UNKNOWN，不返回 NOT_EQUAL —— 宁可漏报，不可误报。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

import sympy

from .latex import normalize, to_expr

#: 数值比较容差
DEFAULT_TOLERANCE = 1e-9
#: 数值代入的采样点数
DEFAULT_SAMPLES = 8
#: 符号化简超时（秒）。SymPy 无内建超时，这里靠表达式规模粗筛来规避。
MAX_EXPR_OPS = 5000


class Equivalence(str, Enum):
    EQUAL = "EQUAL"
    NOT_EQUAL = "NOT_EQUAL"
    UNKNOWN = "UNKNOWN"


@dataclass
class AnswerResult:
    verdict: Equivalence
    level: str | None = None  # 由哪一级判定：normalize / sympy / numeric
    detail: str = ""
    normalized_pred: str = ""
    normalized_gold: str = ""

    @property
    def correct(self) -> bool:
        return self.verdict is Equivalence.EQUAL


# ---------------------------------------------------------------------------
# 第 1 级：规范化字符串比对
# ---------------------------------------------------------------------------

_CHOICE_RE = re.compile(r"^\(?([A-Ea-e])\)?[.、)]?$")
_YESNO = {
    "yes": "true", "true": "true", "是": "true", "对": "true",
    "no": "false", "false": "false", "否": "false", "错": "false",
}


def _canonical_string(text: str) -> str:
    s = normalize(text).lower().replace(" ", "")
    s = s.strip("()") if s.startswith("(") and s.endswith(")") and "," not in s else s
    return _YESNO.get(s, s)


def _as_choice(text: str) -> str | None:
    m = _CHOICE_RE.match(normalize(text).strip())
    return m.group(1).upper() if m else None


# ---------------------------------------------------------------------------
# 第 2 / 3 级：符号与数值
# ---------------------------------------------------------------------------

def _split_tuple(text: str) -> list[str] | None:
    """把 (1, 2)、{1,2,3}、1;2 拆成分量。不是复合结构返回 None。"""
    s = normalize(text)
    if not s:
        return None
    inner = s
    if (s.startswith("(") and s.endswith(")")) or (s.startswith("[") and s.endswith("]")):
        inner = s[1:-1]
    if "," not in inner and ";" not in inner:
        return None
    parts = [p.strip() for p in re.split(r"[,;]", inner)]
    return parts if len(parts) > 1 and all(parts) else None


def _too_large(expr) -> bool:
    try:
        return sympy.count_ops(expr) > MAX_EXPR_OPS
    except Exception:  # noqa: BLE001
        return True


def _symbolic_equal(a, b) -> Equivalence:
    """符号等价判定。判不了返回 UNKNOWN。"""
    if _too_large(a) or _too_large(b):
        return Equivalence.UNKNOWN
    try:
        diff = sympy.simplify(a - b)
        if diff == 0:
            return Equivalence.EQUAL
        if diff.is_number and diff.is_zero is False:
            return Equivalence.NOT_EQUAL
    except Exception:  # noqa: BLE001
        pass
    try:
        eq = a.equals(b)
        if eq is True:
            return Equivalence.EQUAL
        if eq is False:
            return Equivalence.NOT_EQUAL
    except Exception:  # noqa: BLE001
        pass
    return Equivalence.UNKNOWN


def _numeric_equal(
    a, b, samples: int = DEFAULT_SAMPLES, tol: float = DEFAULT_TOLERANCE
) -> Equivalence:
    """随机数值代入比对。仅在两侧自由符号一致时可用。"""
    syms = sorted(a.free_symbols | b.free_symbols, key=str)
    if len(syms) > 4:
        return Equivalence.UNKNOWN

    try:
        if not syms:
            va, vb = complex(a.evalf()), complex(b.evalf())
            if abs(va - vb) <= tol * max(1.0, abs(va), abs(vb)):
                return Equivalence.EQUAL
            return Equivalence.NOT_EQUAL
    except Exception:  # noqa: BLE001
        return Equivalence.UNKNOWN

    import random

    rng = random.Random(12345)  # 固定种子：同一对表达式每次判定结果一致
    checked = 0
    for _ in range(samples * 4):
        subs = {s: sympy.Float(rng.uniform(0.31, 2.87)) for s in syms}
        try:
            va, vb = complex(a.subs(subs).evalf()), complex(b.subs(subs).evalf())
        except Exception:  # noqa: BLE001
            continue
        if any(x != x or abs(x) == float("inf") for x in (va.real, vb.real)):
            continue  # 取到定义域外的点，换一个
        if abs(va - vb) > 1e-6 * max(1.0, abs(va), abs(vb)):
            return Equivalence.NOT_EQUAL
        checked += 1
        if checked >= samples:
            return Equivalence.EQUAL
    return Equivalence.UNKNOWN


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def check_answer(
    pred: str | None,
    gold: str | None,
    *,
    samples: int = DEFAULT_SAMPLES,
    tolerance: float = DEFAULT_TOLERANCE,
) -> AnswerResult:
    """比对预测答案与标准答案。

    返回 AnswerResult；无法判定时 verdict 为 UNKNOWN（不算错）。
    """
    np_, ng = normalize(pred or ""), normalize(gold or "")
    base = {"normalized_pred": np_, "normalized_gold": ng}

    if not np_ or not ng:
        return AnswerResult(Equivalence.UNKNOWN, detail="答案为空", **base)

    # --- 第 1 级：规范化字符串 ---
    if _canonical_string(pred or "") == _canonical_string(gold or ""):
        return AnswerResult(Equivalence.EQUAL, "normalize", "规范化后完全一致", **base)

    cp, cg = _as_choice(pred or ""), _as_choice(gold or "")
    if cp and cg:
        verdict = Equivalence.EQUAL if cp == cg else Equivalence.NOT_EQUAL
        return AnswerResult(verdict, "normalize", f"选择题选项 {cp} vs {cg}", **base)

    # --- 复合结构（元组 / 集合 / 多解）逐分量比对 ---
    tp, tg = _split_tuple(pred or ""), _split_tuple(gold or "")
    if tp is not None and tg is not None:
        if len(tp) != len(tg):
            return AnswerResult(
                Equivalence.NOT_EQUAL, "normalize", f"分量个数不同：{len(tp)} vs {len(tg)}", **base
            )
        # 集合语义：顺序无关，做一次贪心配对
        remaining = list(tg)
        for item in tp:
            for cand in list(remaining):
                if check_answer(item, cand, samples=samples, tolerance=tolerance).correct:
                    remaining.remove(cand)
                    break
            else:
                return AnswerResult(
                    Equivalence.NOT_EQUAL, "normalize", f"分量 {item!r} 无匹配", **base
                )
        return AnswerResult(Equivalence.EQUAL, "normalize", "分量逐一匹配", **base)

    # --- 第 2 级：符号等价 ---
    ep, eg = to_expr(pred or ""), to_expr(gold or "")
    if ep is None or eg is None:
        return AnswerResult(Equivalence.UNKNOWN, detail="无法解析为表达式", **base)

    sym = _symbolic_equal(ep, eg)
    if sym is not Equivalence.UNKNOWN:
        return AnswerResult(sym, "sympy", "SymPy 符号等价判定", **base)

    # --- 第 3 级：数值代入 ---
    num = _numeric_equal(ep, eg, samples=samples, tol=tolerance)
    if num is not Equivalence.UNKNOWN:
        return AnswerResult(num, "numeric", "随机数值代入判定", **base)

    return AnswerResult(Equivalence.UNKNOWN, detail="三级均无法判定", **base)


# ---------------------------------------------------------------------------
# 格式校验（E8）
# ---------------------------------------------------------------------------

@dataclass
class FormatResult:
    ok: bool
    violations: list[str] = field(default_factory=list)


def check_format(answer: str | None, requirements: dict | None) -> FormatResult:
    """按题目明确提出的形式要求做规则校验。

    **只在题目明确要求时适用** —— 题目没提形式要求就不判 E8，
    见 configs/taxonomy.yaml E8 的反例。
    """
    if not requirements:
        return FormatResult(True)

    s = normalize(answer or "")
    violations: list[str] = []

    decimals = requirements.get("decimals")
    if decimals is not None:
        m = re.search(r"\d+\.(\d+)", s)
        if not m or len(m.group(1)) != int(decimals):
            violations.append(f"要求保留 {decimals} 位小数")

    if requirements.get("simplified_fraction"):
        m = re.fullmatch(r"\(?\s*(-?\d+)\s*/\s*(\d+)\s*\)?", s)
        if m:
            num, den = int(m.group(1)), int(m.group(2))
            if sympy.gcd(abs(num), den) != 1:
                violations.append("分数未化为最简")

    unit = requirements.get("unit")
    if unit and unit.lower() not in (answer or "").lower():
        violations.append(f"缺少单位 {unit}")

    shape = requirements.get("shape")  # interval / set / tuple
    if shape == "interval" and not re.search(r"[\[(].*,.*[\])]", s):
        violations.append("要求区间形式")
    if shape == "set" and not (s.startswith("(") or s.startswith("{")):
        violations.append("要求集合形式")

    return FormatResult(not violations, violations)
