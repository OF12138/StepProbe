r"""LaTeX → SymPy 可解析文本的规范化。

不使用 sympy.parsing.latex —— 它依赖 antlr4，为一个可选功能引入额外依赖不划算。
这里用规则改写覆盖数学题答案里的常见写法，解析交给 parse_expr 的
implicit_multiplication + convert_xor 变换（`2x` → `2*x`，`x^2` → `x**2`）。

设计取向：**解析不了就返回 None，绝不猜。** 上层拿到 None 会判 UNKNOWN 而非
判错 —— 宁可漏报，不可误报。

处理顺序有讲究（都踩过坑）：
  1. \dfrac / \tfrac 必须在展开之前统一成 \frac
  2. 排版命令（\left \right）必须在符号替换之前删掉，否则 \le 会把 \left
     截成 "<=ft"
  3. 含中日韩文字的串直接判定为自然语言，不交给 parse_expr —— 否则每个汉字
     都会变成一个自由符号，"解析成功"出一堆垃圾
"""

from __future__ import annotations

import re

import sympy
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

TRANSFORMATIONS = standard_transformations + (
    implicit_multiplication_application,
    convert_xor,
)

#: 直接删除的排版命令（不影响数学含义）。在符号替换之前执行。
_STRIP_COMMANDS = [
    r"\left", r"\right", r"\!", r"\,", r"\;", r"\:", r"\quad", r"\qquad",
    r"\displaystyle", r"\limits", r"$", r"\(", r"\)", r"\[", r"\]",
]

#: 符号替换。用 \b 词边界，避免前缀吃掉更长的命令名。
_REPLACEMENTS: list[tuple[str, str]] = [
    (r"\^\\circ\b", "*pi/180"),
    (r"\\degree\b", "*pi/180"),
    (r"\\cdot\b", "*"),
    (r"\\times\b", "*"),
    (r"\\div\b", "/"),
    (r"\\infty\b", "oo"),
    (r"\\pi\b", "pi"),
    (r"\\geq?\b", ">="),
    (r"\\leq?\b", "<="),
    (r"\\neq\b", "!="),
    (r"\\lbrace\b", "{"),
    (r"\\rbrace\b", "}"),
    (r"\\%", "/100"),
    (r"%", "/100"),
]

_GREEK = [
    "alpha", "beta", "gamma", "delta", "epsilon", "theta", "lambda",
    "mu", "sigma", "phi", "omega", "rho", "tau",
]

_FUNCS = [
    "sin", "cos", "tan", "cot", "sec", "csc",
    "log", "exp", "min", "max", "gcd", "lcm",
]

#: 中日韩文字 —— 出现即视为自然语言叙述
_CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")

#: \u5df2\u77e5\u53ef\u5b89\u5168\u89c4\u8303\u5316\u7684 LaTeX \u547d\u4ee4\u767d\u540d\u5355\u3002
#:
#: \u6539\u7528\u767d\u540d\u5355\u662f P2 \u5b9e\u6d4b\u540e\u7684\u5fc5\u8981\u4fee\u6b63\u3002\u539f\u5148\u300c\u4e0d\u8ba4\u8bc6\u7684\u547d\u4ee4\u5c31\u5265\u6389\u7ee7\u7eed\u89e3\u6790\u300d\uff0c
#: \u7ed3\u679c\u89e3\u6790\u51fa\u5783\u573e\u5374\u300c\u6210\u529f\u300d\u4e86\uff0c\u5236\u9020\u4e86\u5927\u91cf\u8bef\u62a5\uff1a
#:     \varphi(41) = 41 - 1   \u2192  \u5265\u6389 \varphi  \u2192  (41) = 40    \u2192  \u5224 FALSE
#:     \binom{40}{17}         \u2192  \u5265\u6389 \binom   \u2192  (40)(17)=680 \u2192  \u5224 FALSE
#: \u4e8c\u8005\u6570\u5b66\u4e0a\u90fd\u5b8c\u5168\u6b63\u786e\u3002\u9047\u5230\u767d\u540d\u5355\u5916\u7684\u547d\u4ee4\u4e00\u5f8b\u8fd4\u56de None\uff08\u4e0a\u5c42\u5224 UNKNOWN\uff09\uff0c
#: \u4e0e\u6a21\u5757\u5f00\u5934\u300c\u89e3\u6790\u4e0d\u4e86\u5c31\u8fd4\u56de None\uff0c\u7edd\u4e0d\u731c\u300d\u7684\u53d6\u5411\u4e00\u81f4\u3002
_KNOWN_COMMANDS: frozenset[str] = frozenset(
    [
        "frac", "dfrac", "tfrac", "sqrt",
        "cdot", "times", "div",
        "infty", "pi", "circ", "degree",
        "left", "right", "quad", "qquad", "displaystyle", "limits",
        "boxed", "text", "mathrm", "mathbf", "textbf", "operatorname",
        "lbrace", "rbrace",
        "le", "leq", "ge", "geq", "neq",
        "ln", "log", "exp",
        "sin", "cos", "tan", "cot", "sec", "csc",
        "min", "max", "gcd", "lcm",
        *_GREEK,
    ]
)

_COMMAND_RE = re.compile(r"\\([a-zA-Z]+)")

#: \u542b\u8fd9\u4e9b\u7ed3\u6784\u65f6\u8868\u8fbe\u5f0f\u542b\u4e49\u8d85\u51fa\u672c\u6a21\u5757\u80fd\u529b\uff0c\u76f4\u63a5\u653e\u5f03\uff08\u800c\u4e0d\u662f\u300c\u5265\u6389\u518d\u7b97\u300d\uff09\u3002
_UNSAFE_PATTERNS = re.compile(
    r"\\(equiv|pmod|bmod|implies|iff|Rightarrow|Leftarrow|ldots|dots|cdots"
    r"|tag|binom|choose|sum|prod|int|lim|partial|nabla|forall|exists"
    r"|approx|sim|propto|subset|supset|cup|cap|begin|end|matrix|cases"
    r"|overline|underline|widehat|vec|bar|floor|lfloor|rfloor|lceil|rceil"
    r"|pm|mp)\b"
)

#: \u5e26\u5206\u6570 3\frac{4}{5}\uff1a\u6570\u5b66\u4e0a\u662f 3+4/5\uff0c\u4f46\u9690\u5f0f\u4e58\u6cd5\u4f1a\u89e3\u6790\u6210 3*(4/5)\u3002
#: \u8bed\u6cd5\u4e0a\u65e0\u6cd5\u533a\u5206\u610f\u56fe\uff0c\u4e00\u5f8b\u653e\u5f03\u3002
_MIXED_NUMBER_RE = re.compile(r"\d\s*\\[dt]?frac")


def is_parseable(text: str) -> bool:
    """\u5224\u65ad\u8fd9\u6bb5\u6587\u672c\u662f\u5426\u843d\u5728\u672c\u6a21\u5757\u80fd\u529b\u8303\u56f4\u5185\u3002

    \u8d85\u51fa\u8303\u56f4\u8fd4\u56de False \u2014\u2014 \u4e0a\u5c42\u636e\u6b64\u5224 UNKNOWN\uff0c\u800c\u4e0d\u662f\u5224\u9519\u3002
    """
    if not text:
        return False
    s = str(text)
    if _CJK_RE.search(s) or _UNSAFE_PATTERNS.search(s) or _MIXED_NUMBER_RE.search(s):
        return False
    return all(c in _KNOWN_COMMANDS for c in _COMMAND_RE.findall(s))


def decimal_places(text: str) -> int | None:
    """\u6587\u672c\u4e2d\u51fa\u73b0\u7684\u6700\u5c11\u5c0f\u6570\u4f4d\u6570\uff0c\u7528\u4e8e\u6309\u663e\u793a\u7cbe\u5ea6\u8bbe\u7f6e\u5bb9\u5dee\u3002

    \u9898\u89e3\u91cc\u5e38\u5199\u622a\u65ad\u503c\uff08`2235/360 = 6.208333`\uff09\uff0c\u6309 1e-9 \u6bd4\u4f1a\u5168\u5224\u9519\u3002
    """
    places = [len(m) for m in re.findall(r"\d+\.(\d+)", str(text))]
    return min(places) if places else None


def _match_braces(text: str, open_idx: int) -> tuple[str | None, int]:
    """从 text[open_idx] == '{' 开始找配对的 '}'，返回 (内容, 右括号后一位)。"""
    if open_idx >= len(text) or text[open_idx] != "{":
        return None, open_idx
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i], i + 1
    return None, open_idx


def _strip_wrappers(text: str) -> str:
    r"""剥掉 \boxed{}、\text{} 等外壳，保留其中内容。"""
    for cmd in ("boxed", "text", "mathrm", "mathbf", "textbf", "operatorname"):
        pattern = re.compile(r"\\" + cmd + r"\s*\{")
        guard = 0
        while guard < 32:
            guard += 1
            m = pattern.search(text)
            if not m:
                break
            inner, end = _match_braces(text, m.end() - 1)
            if inner is None:
                text = text[: m.start()] + text[m.end() :]
                continue
            text = text[: m.start()] + inner + text[end:]
    return text


def _expand_frac(text: str) -> str:
    r"""\frac{a}{b} → ((a)/(b))，支持嵌套。"""
    pattern = re.compile(r"\\frac\s*")
    guard = 0
    while guard < 64:
        guard += 1
        m = pattern.search(text)
        if not m:
            break
        num, after_num = _match_braces(text, m.end())
        if num is None:
            # \frac12 这种省略花括号的写法
            tail = text[m.end() :]
            if len(tail) >= 2 and tail[0].isdigit() and tail[1].isdigit():
                text = f"{text[: m.start()]}(({tail[0]})/({tail[1]})){tail[2:]}"
                continue
            text = text[: m.start()] + text[m.end() :]
            continue
        den, after_den = _match_braces(text, after_num)
        if den is None:
            text = text[: m.start()] + f"({num})" + text[after_num:]
            continue
        text = text[: m.start()] + f"(({num})/({den}))" + text[after_den:]
    return text


def _expand_sqrt(text: str) -> str:
    r"""\sqrt[n]{a} → ((a)**(1/(n)))，\sqrt{a} → sqrt(a)。"""
    pattern = re.compile(r"\\sqrt\s*(\[([^\]]*)\])?\s*")
    guard = 0
    while guard < 64:
        guard += 1
        m = pattern.search(text)
        if not m:
            break
        root = m.group(2)
        body, end = _match_braces(text, m.end())
        if body is None:
            tail = text[m.end() :]
            if tail and (tail[0].isdigit() or tail[0].isalpha()):
                body, end = tail[0], m.end() + 1
            else:
                text = text[: m.start()] + text[m.end() :]
                continue
        repl = f"(({body})**(1/({root})))" if root else f"sqrt({body})"
        text = text[: m.start()] + repl + text[end:]
    return text


def normalize(text: str) -> str:
    """把一段 LaTeX / 自由文本规范化为 SymPy 友好的表达式串。"""
    if text is None:
        return ""
    s = str(text).strip()

    s = _strip_wrappers(s)

    # 1) 分数变体统一
    s = re.sub(r"\\[dt]frac\b", r"\\frac", s)
    s = _expand_frac(s)
    s = _expand_sqrt(s)

    # 2) 排版命令先删（必须早于符号替换，见模块 docstring）
    for cmd in _STRIP_COMMANDS:
        s = s.replace(cmd, " ")

    # 3) 符号替换
    for pattern, new in _REPLACEMENTS:
        s = re.sub(pattern, new, s)

    # 4) 希腊字母与函数名去掉反斜杠
    s = re.sub(r"\\ln\b", "log", s)
    for name in _GREEK + _FUNCS:
        s = re.sub(r"\\" + name + r"\b", name, s)

    # 5) 千位分隔符 1,234 → 1234（前后都是数字才算，避免误伤元组 (1, 2)）
    s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)

    # 6) 清掉残留的未识别命令与花括号
    s = re.sub(r"\\[a-zA-Z]+", " ", s)
    s = s.replace("\\", " ").replace("{", "(").replace("}", ")")
    s = re.sub(r"\s+", " ", s).strip()

    return s.rstrip(".。,，;；:：")


def to_expr(text: str):
    """解析为 SymPy 表达式；失败返回 None（由上层判 UNKNOWN）。"""
    if not is_parseable(text):
        return None
    normalized = normalize(text)
    if not normalized or _CJK_RE.search(normalized):
        return None
    try:
        expr = parse_expr(normalized, transformations=TRANSFORMATIONS, evaluate=True)
    except Exception:  # noqa: BLE001 —— 解析失败的形态太多，一律退化为 None
        return None
    return expr if isinstance(expr, sympy.Basic) else None
