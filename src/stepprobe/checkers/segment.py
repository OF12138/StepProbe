"""步骤切分（L0）。

目标：把自由文本解答规整为可索引的原子步骤，使「第 k 步出错」有确切含义。

**关键约束：切分粒度必须与标注对齐。** 若把标注中的一步拆成两步，步号就整体
错位，定位准确率会被系统性低估 —— 而且不报错。因此对公开数据集**一律沿用其
原有步骤索引，绝不重新切分**；本模块只用于自建中文子集与 Hy3 新生成的解答。

见 docs/method.md §3。
"""

from __future__ import annotations

import re

from ..schema import Step

#: 显式步骤编号：Step 1 / 步骤1 / 第1步 / ①  / (1) / 1.
_NUMBERED_PATTERNS = [
    re.compile(r"^\s*(?:step|Step|STEP)\s*(\d+)\s*[:：.、)]?\s*", re.MULTILINE),
    re.compile(r"^\s*(?:步骤|第)\s*(\d+)\s*步?\s*[:：.、)]?\s*", re.MULTILINE),
    re.compile(r"^\s*\((\d+)\)\s*", re.MULTILINE),
    re.compile(r"^\s*(\d+)\s*[.、)]\s+", re.MULTILINE),
]

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _split_by_numbering(text: str) -> list[str] | None:
    """按显式编号切分。没有编号返回 None。"""
    for pattern in _NUMBERED_PATTERNS:
        matches = list(pattern.finditer(text))
        if len(matches) < 2:
            continue
        # 编号必须大致递增，否则多半是误匹配（比如正文里的 "1. " 列表）
        nums = [int(m.group(1)) for m in matches]
        if nums != sorted(nums):
            continue
        chunks = []
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            chunks.append(text[m.end() : end].strip())
        chunks = [c for c in chunks if c]
        if len(chunks) >= 2:
            return chunks

    if sum(ch in text for ch in _CIRCLED) >= 2:
        parts = re.split(f"[{_CIRCLED}]", text)
        chunks = [p.strip() for p in parts[1:] if p.strip()]
        if len(chunks) >= 2:
            return chunks

    return None


def _split_by_paragraph(text: str) -> list[str] | None:
    chunks = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return chunks if len(chunks) >= 2 else None


def _split_by_line(text: str) -> list[str] | None:
    chunks = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return chunks if len(chunks) >= 2 else None


def _split_by_sentence(text: str) -> list[str]:
    # 公式块内的句号不能当断句点，先把 $...$ 挖出来保护起来
    placeholders: list[str] = []

    def _stash(m: re.Match) -> str:
        placeholders.append(m.group(0))
        return f"\x00{len(placeholders) - 1}\x00"

    protected = re.sub(r"\$[^$]*\$|\\\[[\s\S]*?\\\]", _stash, text)
    parts = re.split(r"(?<=[。！？])|(?<=[.!?])\s+", protected)

    def _restore(s: str) -> str:
        return re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], s)

    return [_restore(p).strip() for p in parts if p and p.strip()]


def segment(text: str) -> list[str]:
    """按优先级切分：显式编号 → 段落 → 换行 → 句子。"""
    if not text or not text.strip():
        return []
    for splitter in (_split_by_numbering, _split_by_paragraph, _split_by_line):
        chunks = splitter(text)
        if chunks:
            return chunks
    chunks = _split_by_sentence(text)
    return chunks or [text.strip()]


def to_steps(text_or_steps: str | list[str]) -> list[Step]:
    """转成 Step 列表，1-based 连续编号。

    传入 list 时**原样保留，不重新切分** —— 这正是公开数据集的处理路径。
    """
    raw = text_or_steps if isinstance(text_or_steps, list) else segment(text_or_steps)
    return [
        Step(index=i, content=str(c).strip())
        for i, c in enumerate(raw, start=1)
        if str(c).strip()
    ]
