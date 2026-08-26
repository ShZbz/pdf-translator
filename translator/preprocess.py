"""P4: preprocess.py — D5 水印两层策略(文字层 wmremover 式清理)。

三层递进(SCHEME D5,复用 wmremover.py 逻辑 + P4 实测修复):
  1. 独立短流(<500B)含水印关键词 → 清空
  2. 长流/Form XObject 中斜切/旋转矩阵 Tm + 水印关键词的 BT..ET 块 → 移除块
     (P4 实测:水印常藏在 Form XObject 里;斜切矩阵形如 "1 0 0.21256 1",
      原"四小数"正则漏检 → 放宽为允许 b/c 为 0)
  3. Watermark/Stamp 注释 → 删除
cv2.inpaint 仅用于内嵌扫描图 — v1 不实现(SCHEME:严禁整页 inpaint)。
"""
from __future__ import annotations

import re

import pymupdf

# 水印关键词:多词短语用子串匹配(误伤率低);单词词(preprint/confidential/draft)
# 必须带词边界,防误伤 "arXiv preprint arXiv:1234" 引文、"confidentiality statement" 等
TEXT_WM_KEYWORDS = [
    "accepted manuscript",
    "author submitted manuscript",
    "author manuscript",
    "unpublished manuscript",
    "manuscript version",
]
# 单词级:仅限短流/整块匹配场景,且要求两侧非字母数字(词边界)
_TEXT_WM_WORDS = [
    "preprint",
    "do not distribute",
    "for review only",
]

# 白名单:含这些子串的流即使命中关键词也不清(保护合法引用/声明)
_TEXT_WM_WHITELIST = [
    "arxiv:",          # arXiv 引文
    "arxiv.org",
    "doi:",
]

# Tm 的 a b c d:允许整数/小数/负号;斜切或旋转 = |b|>0 或 |c|>0
_TM_PAT = re.compile(
    r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)"
)

_WORD_RE_CACHE: dict[str, re.Pattern] = {}


def _hit_watermark(norm_text: str) -> bool:
    """双列表判定:短语子串 + 单词词边界;命中白名单直接放行。"""
    if any(w in norm_text for w in _TEXT_WM_WHITELIST):
        return False
    if any(kw in norm_text for kw in TEXT_WM_KEYWORDS):
        return True
    for w in _TEXT_WM_WORDS:
        pat = _WORD_RE_CACHE.get(w)
        if pat is None:
            pat = re.compile(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])")
            _WORD_RE_CACHE[w] = pat
        if pat.search(norm_text):
            return True
    return False


def _normalize_for_match(s: str) -> str:
    """关键词匹配归一化：明文 + UTF-16BE hex/CID 字节两种视角。

    1.pdf 实测:水印文本是 CID 编码,流里形如 (\\x00H\\x00W\\x00...)Tj,
    明文小写匹配打不中 → 死代码根因。UTF-16 视角把 \\x00H\\x00W 还原为 'HW'。
    """
    views = [s.lower()]
    # UTF-16BE: \x00H\x00W\x00... → 'HW...'（去 NUL 后即明文）
    if "\\x00" in s or "\x00" in s:
        views.append(s.replace("\x00", "").lower())
    return "\n".join(views)


def _strip_skewed_wm_blocks(stream: bytes) -> tuple[bytes | None, int]:
    """从内容流字节中剔除斜切/旋转矩阵 + 水印关键词的 BT..ET 块。

    v0.1.1 重写：不再逐行解析(漏检 Tm (text) Tj 同行式/BT..ET 同行式)，
    改为整块正则扫描；关键词匹配走 _normalize_for_match 双视角。
    返回 (新流, 删除块数)；无删除返回 (None, 0)。
    """
    decoded = stream.decode("latin-1", errors="replace")
    norm = _normalize_for_match(decoded)
    if not _hit_watermark(norm):
        return None, 0

    removed = 0
    out = decoded
    # 整块 BT..ET 匹配(non-greedy, 跨行)：含斜切/旋转 Tm 且含水印词 → 整块删
    bt_re = re.compile(r"BT\b.*?\bET", re.S)
    tm_re = re.compile(
        r"(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+[-\d.]+\s+[-\d.]+\s+Tm")

    def _block_hit(m: re.Match) -> str:
        nonlocal removed
        blk = m.group(0)
        tmm = tm_re.search(blk)
        if tmm:
            b_, c_ = float(tmm.group(2)), float(tmm.group(3))
            skewed = abs(b_) > 1e-3 or abs(c_) > 1e-3
        else:
            skewed = False
        if not skewed:
            return blk                      # 非斜切块不动
        blk_norm = _normalize_for_match(blk)
        if _hit_watermark(blk_norm):
            removed += 1
            return ""                       # 命中：整块移除
        return blk

    new_text = bt_re.sub(_block_hit, out)
    if not removed:
        return None, 0
    if not new_text.strip():
        return None, removed
    return new_text.encode("latin-1", errors="replace"), removed


def remove_watermarks(doc: pymupdf.Document) -> dict:
    """原地清理文字层水印。返回统计。"""
    stats = {
        "streams_cleaned": 0,
        "text_blocks_removed": 0,
        "annots_removed": 0,
    }

    # ---- 收集本页全部相关流:page contents + Form XObjects ----
    def _streams_of(page) -> list[int]:
        xrefs = list(page.get_contents() or [])
        seen = set()
        for xo in page.get_xobjects() or []:
            xr = xo[0]
            if xr not in seen:
                seen.add(xr)
                try:
                    if doc.xref_is_stream(xr):
                        xrefs.append(xr)
                except Exception:
                    continue
        return xrefs

    all_xrefs: dict[int, int] = {}   # xref -> page_no
    for pno, page in enumerate(doc):
        for xr in _streams_of(page):
            all_xrefs.setdefault(xr, pno)

    # ---- 1. 独立短流清空 ----
    for xr in all_xrefs:
        stream = doc.xref_stream(xr)
        if not stream or len(stream) >= 500:
            continue
        low = _normalize_for_match(stream.decode("latin-1", errors="replace"))
        if _hit_watermark(low):
            doc.update_stream(xr, b" ")
            stats["streams_cleaned"] += 1

    # ---- 2. 长流中的斜切/旋转水印块 ----
    for xr in all_xrefs:
        stream = doc.xref_stream(xr)
        if not stream or len(stream) < 500:
            continue
        cleaned, n = _strip_skewed_wm_blocks(stream)
        if cleaned is not None and cleaned != stream:
            doc.update_stream(xr, cleaned)
            stats["text_blocks_removed"] += n

    # ---- 3. 注释水印 ----
    for page in doc:
        annots = page.annots()
        if not annots:
            continue
        for a in list(annots):
            atype = a.type
            tn = atype[0] if isinstance(atype, tuple) else atype
            name = (a.info.get("name") or "").lower()
            if (
                tn == getattr(pymupdf, "PDF_ANNOT_WATERMARK", -1)
                or "watermark" in name
                or "draft" in name
            ):
                page.delete_annot(a)
                stats["annots_removed"] += 1

    return stats