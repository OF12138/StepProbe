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
# 答案装饰的剥离
# ---------------------------------------------------------------------------
#
# 同一个答案的写法差异极大。构建 P6 题目集时做了一次交叉印证（同一道题、
# 两个模型各自写出的正确答案互相比对），23 处不一致里有 20 处根本不是答案不同，
# 而是**装饰不同**：
#
#     x = \sqrt{2}   vs  \sqrt{2}          带变量名前缀
#     f(x) = x + 22  vs  x + 22            带函数名前缀
#     4^\circ        vs  4                 带角度单位
#     17.5 \%        vs  17.5              带百分号
#     1 \text{and} 3 vs  1, 3              用 and 而非逗号分隔
#
# 这类差异如果不处理，P6 里 Hy3 的答案会因为「写法不同」被大量误判为答错，
# 答案正确率被系统性低估 —— 而 P6 恰恰要靠答案正确率与过程正确率的对比
# 来回答「Max Mode 是让推导更严谨，还是只是让答案更容易蒙对」。
#
# **剥离是兜底，不是默认路径。** 先按原样比，判定 EQUAL 就直接返回；只有在
# 原样比不出相等时才剥掉装饰重试。这样保证剥离只可能把「本该相等」救回来，
# 不可能把「本来不等」洗成相等。

#: 连接词当分隔符：1 and 3 → 1, 3
_CONJUNCTION_RE = re.compile(r"\\text\s*\{\s*(and|or|、|和|或)\s*\}|\s+(and|or)\s+", re.IGNORECASE)

#: 量纲 / 单位后缀。答案本身相等时这些不该造成差异。
_UNIT_RE = re.compile(
    r"\^\s*\{?\s*\\?(?:circ|degree)\s*\}?"      # 4^\circ / 4^{\circ}
    r"|\\%|%"                                    # 17.5\% / 17.5%
    r"|\\text\s*\{[^{}]*\}"                     # \text{ cm} 等单位
    r"|\\,|\\;|\\!|\\quad|\\qquad"              # LaTeX 间距
)

#: 裸写的单位词。**白名单而非「任何结尾的词」** —— 后者会把
#: `10 apples` 与 `10 oranges` 洗成相等。这些是 GSM8K / MATH 里实际出现的量纲。
_BARE_UNIT_RE = re.compile(
    r"\s*\b(?:"
    r"hours?|hrs?|minutes?|mins?|seconds?|secs?|days?|weeks?|months?|years?"
    r"|degrees?|radians?"
    r"|cm|mm|km|m|meters?|metres?|inches|inch|feet|foot|ft|yards?|miles?|mph"
    r"|grams?|kg|kilograms?|pounds?|lbs?|ounces?|oz"
    r"|dollars?|cents?|units?|points?|times|percent"
    r"|square\s+\w+|cubic\s+\w+"
    r"|小时|分钟|秒|天|周|个?月|年|度|米|厘米|千米|公里|元|个"
    r")\b\.?\s*$",
    re.IGNORECASE,
)

#: 形如 "x =" / "f(x) =" / "\sin\theta =" 的左端前缀
_LHS_PREFIX_RE = re.compile(
    r"^\s*\\?[A-Za-z][A-Za-z0-9_]*"      # 变量名或 \sin 这类命令
    r"(?:\s*_\s*\{?[A-Za-z0-9]+\}?)?"    # 下标
    r"(?:\s*\\?[A-Za-z][A-Za-z0-9_]*)?"  # \sin \theta 这种两段
    r"(?:\s*\([^()=]*\))?"               # 函数参数 f(x)
    r"\s*=\s*(?!=)"
)


#: ±x / \pm x → 展开成 x, -x。`±1, ±7` 与 `-7, -1, 1, 7` 是同一个解集，
#: 不展开就会比出「分量个数不同」（2 vs 4）而判定答错。
_PLUSMINUS_RE = re.compile(r"(?:±|\\pm)\s*([0-9A-Za-z\\{}^_./]+)")


def _strip_decoration(text: str) -> str:
    """剥掉不改变答案含义的装饰。仅供兜底比较使用。"""
    s = _PLUSMINUS_RE.sub(lambda m: f"{m.group(1)}, -{m.group(1)}", text)
    s = _CONJUNCTION_RE.sub(", ", s)
    s = _UNIT_RE.sub("", s)
    s = _BARE_UNIT_RE.sub("", s)
    # 只剥一层前缀，且剥完必须还剩东西、且不再含 "="
    #（"x = 1, y = 2" 这类多变量答案剥了会变味，交给复合结构分支处理）
    stripped = _LHS_PREFIX_RE.sub("", s, count=1)
    if stripped.strip() and "=" not in stripped:
        s = stripped
    return s.strip()


# ---------------------------------------------------------------------------
# 第 2 / 3 级：符号与数值
# ---------------------------------------------------------------------------

#: 集合构造式与自然语言描述的标志。命中时本工具无权判「不等」。
_DECLARATIVE_RE = re.compile(
    r"\\mid|\\in\b|\\text\s*\{|(?<![<>=!])\|(?!\|)"      # {x | P(x)}、\mid、\in
    r"|\bsuch\s+that\b|\bfor\s+(?:any|all|every)\b|\bwhere\b|\bexcept\b"
    r"|\ball\s+(?:positive\s+|non-?negative\s+)?integers?\b"
    r"|\bif\s+and\s+only\s+if\b|\bi\.e\.|\bany\b.*\binteger\b"
    r"|[，。、]|\b满足\b|所有|任意",
    re.IGNORECASE,
)


def _is_declarative(text: str) -> bool:
    """答案是不是「一句描述」而非一个可比较的值。

    `{n : n >= 1, n != 2}` 会被当成三分量的元组，与标准答案的
    `\\{n \\mid n \\geq 1 \\text{ and } n \\neq 2\\}` 比出「分量个数不同」，
    然后**自信地判定答错** —— 而两者其实是同一个集合。

    这类答案本工具判不了，正确的输出是 UNKNOWN（不进准确率分母），不是
    NOT_EQUAL。区别很实在：P6 里两臂的书写风格不同，一臂写 ASCII 集合构造式、
    另一臂写自然语言，前者被判「错」、后者被判「未知」，一个进分母一个不进 ——
    凭空造出一个配对差分。

    只用于把 NOT_EQUAL 降级为 UNKNOWN，绝不用于制造相等。
    """
    return bool(_DECLARATIVE_RE.search(text))


def _encloses_whole(s: str) -> bool:
    """首尾括号是否真的是一对、且包住了整个串。

    `(a,b), (c,d)` 也以 `(` 开头、`)` 结尾，但那是两个并列的元组 —— 按首尾字符
    判断会把它剥成 `a,b), (c,d`，之后所有分量都是错的。这类多解答案在竞赛题里
    很常见，剥错了就变成「分量个数不同」，判成答错。
    """
    pairs = {")": "(", "]": "["}
    if not s or s[0] not in "([" or s[-1] not in ")]":
        return False
    if pairs.get(s[-1]) != s[0]:
        return False
    depth = 0
    for i, ch in enumerate(s):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
            if depth == 0 and i != len(s) - 1:
                return False  # 首括号在中途就闭合了
    return depth == 0


def _split_top_level(s: str) -> list[str]:
    """只在最外层（括号深度 0）按 , 或 ; 切分。"""
    parts, buf, depth = [], [], 0
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch in ",;" and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts]


def _split_tuple(text: str) -> list[str] | None:
    """把 (1, 2)、{1,2,3}、1;2 拆成分量。不是复合结构返回 None。

    嵌套结构按**最外层**切：`(1,2), (3,4)` 得到两个元组，而不是四个数。
    分量之间的比较是递归的，所以嵌套多深都能处理。
    """
    s = normalize(text)
    if not s:
        return None
    inner = s[1:-1] if _encloses_whole(s) else s
    parts = _split_top_level(inner)
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

    原样比不出相等时，会剥掉答案装饰（变量名前缀、单位、连接词）重试一次 ——
    见 `_strip_decoration` 的说明。**剥离只能把相等救回来，不能把不等洗成相等**。
    """
    result = _with_decoration_retry(pred, gold, samples=samples, tolerance=tolerance)
    # 统一出口降级。放在这里而不是各分支里：装饰剥离那条路会把 UNKNOWN
    # 升级成 NOT_EQUAL，早退的守卫盖不住它。
    if result.verdict is Equivalence.NOT_EQUAL and (
        _is_declarative(pred or "") or _is_declarative(gold or "")
    ):
        return AnswerResult(
            Equivalence.UNKNOWN,
            result.level,
            f"集合构造式或自然语言描述，本工具无权判定不等（原判定：{result.detail}）",
            normalized_pred=result.normalized_pred,
            normalized_gold=result.normalized_gold,
        )
    return result


def _with_decoration_retry(
    pred: str | None,
    gold: str | None,
    *,
    samples: int = DEFAULT_SAMPLES,
    tolerance: float = DEFAULT_TOLERANCE,
) -> AnswerResult:
    result = _check(pred, gold, samples=samples, tolerance=tolerance)
    if result.verdict is Equivalence.EQUAL:
        return result

    sp, sg = _strip_decoration(pred or ""), _strip_decoration(gold or "")
    if (sp, sg) == ((pred or "").strip(), (gold or "").strip()):
        return result  # 没有装饰可剥，不必重试

    retry = _check(sp, sg, samples=samples, tolerance=tolerance)
    if retry.verdict is Equivalence.EQUAL:
        return AnswerResult(
            Equivalence.EQUAL,
            f"{retry.level}/decoration",
            f"剥离答案装饰后判定相等（{retry.detail}）",
            normalized_pred=result.normalized_pred,
            normalized_gold=result.normalized_gold,
        )
    # 剥离后仍不等 → 保留原判定；若原判定是 UNKNOWN 而剥离后能判不等，采用后者
    if result.verdict is Equivalence.UNKNOWN and retry.verdict is Equivalence.NOT_EQUAL:
        return AnswerResult(
            Equivalence.NOT_EQUAL,
            f"{retry.level}/decoration",
            f"剥离答案装饰后判定不等（{retry.detail}）",
            normalized_pred=result.normalized_pred,
            normalized_gold=result.normalized_gold,
        )
    return result


def _check(
    pred: str | None,
    gold: str | None,
    *,
    samples: int = DEFAULT_SAMPLES,
    tolerance: float = DEFAULT_TOLERANCE,
) -> AnswerResult:
    """三级级联本体。装饰剥离由 `check_answer` 负责，这里不管。"""
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
