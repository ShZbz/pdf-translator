"""修复 _wrap_cjk: Latin 词不拆行(词边界断行), CJK 保持逐字断行。

v0.2.2 任务1/5 排版包:英文保留段/双语原文层被逐字拆碎的根因是
贪心逐字断行对 Latin 文本无词边界概念。改为混合策略:
- 连续 Latin/digit 串视为不可分 token
- 行首放不下整个 token 时整词压到下一行;超长 token(>行宽)硬切兜底
- CJK 字符维持逐字+避头尾
"""
from __future__ import annotations

import re

import pymupdf

# 行首禁则标点（不可出现在行首，悬挂到上一行行尾）
_NO_LINE_START = set("，。、；：？！」』）】〉》〕〗〙％‰.,;:?!)]}%·…—~")
# 行尾禁则：开括号类不可收在行尾，压到下一行
_NO_LINE_END = set("（「『【〈《〔〖〘([{‘“")

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff01-\uffee]")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\u2019\-]*|\s+")
_LATIN_DETECT_RE = re.compile(r"[A-Za-z]")


def _has_latin(text: str) -> bool:
    """文本是否含 Latin 字母（决定断行策略分发）。"""
    return bool(_LATIN_DETECT_RE.search(text))


def _tokenize(text: str) -> list[str]:
    """把文本切成原子单元：Latin 词(含连字符)/空白/CJK 单字/单个标点。"""
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if _CJK_RE.match(ch):
            tokens.append(ch)
            i += 1
            continue
        m = _TOKEN_RE.match(text, i)
        if m:
            tok = m.group(0)
            if tok.isspace():
                # 空白折叠成单个空格标记（断行点）
                tokens.append(" ")
            else:
                tokens.append(tok)
            i = m.end()
        else:
            tokens.append(ch)
            i += 1
    return tokens


def wrap_mixed(text: str, font: "pymupdf.Font", fs: float, width: float,
               first_indent: float = 0.0) -> list[str]:
    """混合断行：Latin 词边界 + CJK 逐字 + 避头尾。"""
    lines: list[str] = []
    cur = ""
    cur_w = 0.0
    avail = width - first_indent
    space_w = font.text_length(" ", fontsize=fs)

    def _flush():
        nonlocal cur, cur_w
        if cur:
            lines.append(cur.rstrip())
        cur = ""
        cur_w = 0.0

    for tok in _tokenize(text):
        if tok == " ":
            if cur:                       # 行尾空格不落墨,记挂起宽度
                cur_w_pending = True
                cur += " "
                cur_w += space_w
            continue
        w = font.text_length(tok, fontsize=fs)
        # 超长 token 硬切兜底（URL/无空格长串）
        if w > width:
            _flush()
            piece = ""
            pw = 0.0
            for ch in tok:
                cw = font.text_length(ch, fontsize=fs)
                if piece and pw + cw > width:
                    lines.append(piece)
                    piece, pw = "", 0.0
                piece += ch
                pw += cw
            cur, cur_w = piece, pw
            continue
        if cur and cur_w + w > avail:
            # 避头尾:下一行首字符若是禁则标点 → 悬挂上行
            if tok[0] in _NO_LINE_START and len(tok) == 1:
                # 悬挂后若超行宽 2% 容差 → 放弃悬挂正常换行（标点随行首下移）
                if cur_w + w <= width * 1.02:
                    cur += tok
                    cur_w += w
                    continue
                _flush()
                avail = width
                cur += tok
                cur_w += w
                continue
            # 行尾开括号压下行
            moved = ""
            while cur and cur[-1] in _NO_LINE_END:
                moved = cur[-1] + moved
                cur_w -= font.text_length(cur[-1], fontsize=fs)
                cur = cur[:-1]
            if moved:
                lines.append(cur.rstrip())
                cur = moved
                cur_w = font.text_length(moved, fontsize=fs)
            else:
                _flush()
            cur += tok
            cur_w += w
            avail = width
        else:
            cur += tok
            cur_w += w
    _flush()
    return [ln for ln in lines if ln.strip()]
