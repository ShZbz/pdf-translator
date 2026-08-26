"""D1 渲染：redaction(保图形) + CJK TextWriter 段落回灌 + fit-to-bbox + D6 公式裁图回贴。

P0 已验证参数组合：
- apply_redactions(images=PDF_REDACT_IMAGE_NONE, graphics=PDF_REDACT_LINE_ART_NONE)
- pymupdf.Font(fontfile=SourceHanSansCN) + TextWriter.fill_textbox
"""
from __future__ import annotations

import io
import re

import pymupdf

from .layout import dominant_size

DEFAULT_FONTS = [
    "/mnt/c/Windows/Fonts/SourceHanSansCN-Normal.ttf",
    "/mnt/c/Windows/Fonts/Noto Sans SC (TrueType).otf",
    "/mnt/c/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

MIN_FONT = 6.5

_CJK_RE = None


def _has_cjk(s: str) -> bool:
    """文本是否含 CJK 字符（双语 en 层字体选择用）。"""
    global _CJK_RE
    if _CJK_RE is None:
        import re
        _CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
    return bool(_CJK_RE.search(s))


# ---- P1 渲染质量包：文本清洗 / CJK 断行 / 精确回灌 ----

_CJK_CLASS = r"\u4e00-\u9fff\u3000-\u303f\uff01-\uffee\u2018\u2019\u201c\u201d\u2026"
_INTRA_SPACE_RE = re.compile(rf"(?<=[{_CJK_CLASS}])[ \t\xa0]+(?=[{_CJK_CLASS}])")

# 避头尾标点：行首禁则字符（不可出现在行首，悬挂到上一行行尾）
_NO_LINE_START = set("，。、；：？！」』）】〉》〕〗〙％‰.,;:?!)]}%·…—~")
# 行尾禁则：开括号类不可收在行尾，压到下一行
_NO_LINE_END = set("（「『【〈《〔〖〘([{‘“")


def _clean_zh_text(text: str) -> str:
    """译文/保留原文的渲染前清洗：
    - \\xa0 → 空格；每行首尾空白剥除；空行丢弃
    - 行重组为单一流式段落（源 PDF 换行位置对译文无意义）：CJK 邻接处直连，
      其余以单空格连接
    - 删除 CJK 字符之间的句中空格（提取残留，是"提前换行"观感的根源之一）
    """
    lines = [ln.strip(" \t\xa0") for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    out = ""
    for i, ln in enumerate(lines):
        if i == 0:
            out = ln
            continue
        prev_last = out[-1]
        nxt_first = ln[0]
        boundary_cjk = (_has_cjk(prev_last) or prev_last in _NO_LINE_START) and \
                       (_has_cjk(nxt_first) or nxt_first in _NO_LINE_END)
        out += ("" if boundary_cjk else " ") + ln
    return _INTRA_SPACE_RE.sub("", out)


def _wrap_cjk(text: str, font: "pymupdf.Font", fs: float, width: float,
              first_indent: float = 0.0) -> list[str]:
    """断行分发（v0.2.2）：含 Latin 词的文本走混合断行（词边界），
    纯 CJK/标点文本走逐字贪心+避头尾。"""
    from .wrap_mixed import _has_latin
    if _has_latin(text):
        return wrap_mixed_lines(text, font, fs, width, first_indent)
    lines: list[str] = []
    cur = ""
    cur_w = 0.0
    avail = width - first_indent
    for ch in text:
        w = font.text_length(ch, fontsize=fs)
        if cur and cur_w + w > avail:
            if ch in _NO_LINE_START:
                cur += ch          # 悬挂禁则标点（允许轻微溢出 ≤1 字宽）
                cur_w += w
                continue
            if cur[-1] in _NO_LINE_END:
                moved = cur[-1]    # 行尾开括号压到下一行
                cur = cur[:-1]
                lines.append(cur)
                cur = moved
                cur_w = font.text_length(moved, fontsize=fs)
            else:
                lines.append(cur)
                cur = ""
                cur_w = 0.0
                avail = width      # 后续行恢复全宽
            cur += ch
            cur_w += w
        else:
            if not cur:
                pass
            cur += ch
            cur_w += w
    if cur:
        lines.append(cur)
    return [ln for ln in lines if ln]


def wrap_mixed_lines(text: str, font: "pymupdf.Font", fs: float, width: float,
                     first_indent: float = 0.0) -> list[str]:
    """混合断行入口（首行缩进在首行可用宽度里扣）。"""
    from .wrap_mixed import wrap_mixed as _wm
    return _wm(text, font, fs, width, first_indent=first_indent)


def _draw_para(tw, rect: "pymupdf.Rect", txt: str, font: "pymupdf.Font",
               base_size: float, indent_chars: int,
               warnings: list[str], tag: str,
               lh_factor: float = 1.32, center: bool = False,
               min_font: float | None = None) -> None:
    """试排降字号 + 逐行精确绘制。溢出时告警（不静默丢字）。

    v0.2.2: lh_factor 由样式决定（标题紧凑正文舒展）；center=整段居中。
    min_font: 该段字号下限（缺省 MIN_FONT）。ref_entry 传 5.8——
    条目盒高被相邻条目侵入 ~7pt，6.5pt 下限×4行装不下（实测教训）。
    """
    floor = max(4.5, min_font or MIN_FONT)
    if not txt.strip():
        return
    # v0.2.4: 窄盒（<20pt，如纯数字单元格 '0.3' w=10pt）直接按原字号
    # 单行绘制——旧逻辑在此静默 return，数字列缺格（实测表 I rcc 列）
    if rect.width < 20:
        tw.append(pymupdf.Point(rect.x0, rect.y0 + font.ascender * base_size),
                  txt.strip(), font=font, fontsize=base_size)
        if rect.width < font.text_length(txt.strip(), fontsize=base_size):
            warnings.append(f"{tag}: narrow box {rect.width:.0f}pt < text width")
        return
    fs = max(base_size, floor)
    while True:
        wrapped = _wrap_cjk(txt, font, fs, rect.width,
                            first_indent=(indent_chars * fs if not center else 0))
        lh = fs * lh_factor
        total_h = len(wrapped) * lh
        if total_h <= rect.height * 1.04 or fs <= floor:
            break
        fs -= 0.25
    if total_h > rect.height * 1.04:
        warnings.append(
            f"{tag}: overflow {total_h:.0f}pt > box {rect.height:.0f}pt "
            f"@{fs:.2f}pt/{len(wrapped)} lines — text may clip")
    asc = font.ascender * fs
    y = rect.y0 + asc
    for i, ln in enumerate(wrapped):
        x = rect.x0 + (indent_chars * fs if (i == 0 and not center) else 0.0)
        if center:
            # 整行水平居中：按行宽偏移
            lw = font.text_length(ln, fontsize=fs)
            x = rect.x0 + max(0.0, (rect.width - lw) / 2.0)
        tw.append(pymupdf.Point(x, y), ln, font=font, fontsize=fs)
        y += lh


def find_cjk_font(explicit: str | None = None) -> str:
    """按显式路径 → 常见路径顺序找第一个存在的 CJK 字体文件。"""
    import os
    for p in ([explicit] if explicit else []) + DEFAULT_FONTS:
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError(
        "no CJK font found; set fonts.cjk in config.yaml "
        f"(tried: {[explicit] + DEFAULT_FONTS})")


def _fit_fontsize(rect: pymupdf.Rect, text: str, font: pymupdf.Font,
                  base_size: float) -> float:
    """试排降字号：从 base_size 起，每次 -0.25pt，直到装得下或到 MIN_FONT。"""
    fs = max(base_size, MIN_FONT)
    while fs > MIN_FONT:
        if _fits(rect, text, font, fs):
            return fs
        fs -= 0.25
    return MIN_FONT


def _fits(rect: pymupdf.Rect, text: str, font: pymupdf.Font, fs: float) -> bool:
    """粗判：文本总宽（含换行展开）在 rect 面积约束内可排。"""
    # 每行可用宽度 = rect 宽；估算行数 = ceil(total_advance / line_width)
    total = sum(font.text_length(ch, fontsize=fs) for ch in text if ch != "\n")
    nlines_needed = 0
    for para in text.split("\n"):
        w = sum(font.text_length(ch, fontsize=fs) for ch in para)
        lw = rect.width or 1.0
        nlines_needed += max(1, int(w // lw) + (1 if w % lw else 0))
    lh = fs * 1.35          # CJK 行高经验值
    return nlines_needed * lh <= rect.height * 1.02   # 2% 容差


def render_page(page, layout: dict, translated: list[dict],
                font_path: str, formula_pixmaps: dict[int, bytes] | None = None,
                bilingual: bool = False,
                warnings: list[str] | None = None,
                typography=None,
                cell_texts: dict[int, str | None] | None = None) -> None:
    """原地改造一页：redact 全部正文块 → 中文回灌 → display 公式位图回贴。

    layout:    layout_page() 输出
    translated: [{index, text}]，与 layout["paragraphs"] 等长对齐；
                text 为 None 表示该段保留原文——但原文已被 redact，
                因此保留原文段也要用原文本重灌（保证视觉完整）。
    formula_pixmaps: {formula_idx: png_bytes} 由 pipeline 预裁。
    warnings:  渲染告警收集器（溢出等），None 则内部丢弃。
    typography: Typography 实例（v0.2.2 期刊级排版）；None 时退回单字体旧行为。
    cell_texts: v0.2.3 {cell_index: 译文}——表格单元格译文原位回灌；
                None 值的单元格保留原文。
    """
    paras = layout["paragraphs"]
    tmap = {t["index"]: t["text"] for t in translated}
    warn_local: list[str] = warnings if warnings is not None else []

    # ---- 1. redaction：正文文字块（图/表/公式区/verbatim 段不动）----
    # v0.2.3: verbatim 段（Algorithm 伪代码框）不 redact——原文像素保留，
    # 重灌会破坏伪代码缩进/数学符号排版（实测 paper3 p4 框内中英混杂根因）。
    for i, p in enumerate(paras):
        if p.get("is_verbatim"):
            continue
        page.add_redact_annot(p["bbox"])
    # v0.2.3: 表格单元格文字 redact（译文回灌用）——表区边框线不受影响
    # （redaction 只删文字对象，graphics=NONE 保线段）
    cells = layout.get("tables_cells") or []
    for cell in cells:
        page.add_redact_annot(pymupdf.Rect(cell["bbox"]))
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)

    font = pymupdf.Font(fontfile=font_path)
    tw = pymupdf.TextWriter(page.rect)
    # v0.2.2: 标题用黑体族（typography 提供时）
    tw_head = pymupdf.TextWriter(page.rect)
    f_head = typography.f_head if typography else font
    # 双语 en 层字体:原文含 CJK(ZH→EN 反向翻译)时用 CJK 字体,否则 Helvetica
    en_font = font if _has_cjk("".join(p["text"] for p in paras)) else pymupdf.Font("helv")
    # D6 公式区 rects(双语 en 层防压公式位图)
    formula_rects = [pymupdf.Rect(f["bbox"]) for f in layout.get("formulas", [])]

    body_size = None
    if typography:
        from collections import Counter
        _sz: Counter = Counter()
        for p in paras:
            _sz[round(p.get("size") or dominant_size(p), 1)] += max(len(p["text"]), 1)
        body_size = _sz.most_common(1)[0][0] if _sz else None

    # ---- 2. 段落回灌（P1 清洗+CJK断行+缩进 / D8 标题加粗 / P5 双语对照）----
    # v0.2.3: verbatim 段跳过回灌（原文像素未动，无需重排）
    # v0.2.3: 表格单元格译文回灌（原位、小字号、不缩进）
    cell_map = cell_texts or {}
    for ci, cell in enumerate(cells):
        dst = cell_map.get(ci)
        if not dst or not dst.strip():
            continue
        crect = pymupdf.Rect(cell["bbox"])
        # v0.2.4: 最小宽度 15→8——纯数字单元格（'0.3'）宽仅 10pt 被旧
        # 门槛跳过，数字列缺格（实测 paper3 表 I 第三行 rcc 列）
        if crect.width < 8 or crect.height < 5:
            continue
        c_fs = min(dominant_size({"spans": cell.get("spans", [])}) or 8.0, 8.0)
        _draw_para(tw, crect, _clean_zh_text(dst), font, c_fs, 0,
                   warn_local, f"cell{ci}", lh_factor=1.25)
    for i, p in enumerate(paras):
        if p.get("is_verbatim"):
            continue   # 原文像素保留
        raw = tmap.get(i)
        if not raw:
            raw = p["text"]           # 触顶/失败降级：原文重灌保持版面完整
        txt = _clean_zh_text(raw)
        rect = pymupdf.Rect(p["bbox"])
        rect.x0 -= 0.5; rect.y0 -= 0.5     # 微扩防贴边裁字
        rect.x1 += 0.5; rect.y1 += 0.5
        if p.get("is_ref"):
            # ref 条目贴边绘制:不微扩——相邻条目共享边界,扩了会互相侵入
            rect.y0 += 0.5; rect.y1 -= 0.5
        base = p.get("size") or dominant_size(p) or 10.0
        para_min = None   # 段级字号下限（ref_entry 放宽，typography 分支赋值）
        # v0.2.2: 期刊样式解析（typography 缺省时退回旧逻辑）
        if typography is not None:
            style = typography.resolve(p, body_size)
            use_font = (f_head if style.bold else font)
            tw_target = tw_head if style.bold else tw
            from .typography import line_height_factor
            indent_chars = style.indent
            tag_extra = style.kind
            lh_factor = line_height_factor(style.kind)
            center = style.center
            # ref_entry 字号下限放宽到 5.8pt（盒高被相邻条目侵入）
            para_min = 5.8 if style.kind == "ref_entry" else None
            base = max(style.size, MIN_FONT)
        else:
            use_font = font
            tw_target = tw
            is_ref = bool(re.match(r"^\[\d+\]", txt.strip()))
            indent_chars = 0 if (p.get("is_heading") or p.get("is_caption")
                                 or is_ref or len(txt) < 40) else 2
            tag_extra = ""
            lh_factor = 1.32
            center = False
        tag = f"render p{i}{('[' + tag_extra + ']') if tag_extra else ''}"
        # P5 双语：中文只排上部 60%，底部留英文原文小字（标题段跳过）
        if bilingual and raw != p["text"] and not p.get("is_heading"):
            split_y = rect.y0 + rect.height * 0.6
            zh_rect = pymupdf.Rect(rect.x0, rect.y0, rect.x1,
                                   max(split_y, rect.y0 + MIN_FONT * 2))
            _draw_para(tw, zh_rect, txt, use_font, base, 0,
                       warn_local, f"{tag}/zh",
                       lh_factor=lh_factor, center=center)
            en_rect = pymupdf.Rect(rect.x0, split_y + 1.0, rect.x1, rect.y1)
            if en_rect.height < MIN_FONT * 1.5 or en_rect.width < 20:
                continue   # 底部空间不足，放弃原文
            en_fs = max(MIN_FONT, base * 0.75)
            # 防压公式位图:en 层若与任何公式区重叠 → 放弃英文层(中文优先)
            if any(en_rect.intersects(fr) for fr in formula_rects):
                continue
            wrapped = _wrap_to_width(p["text"], en_font, en_fs, en_rect.width)
            # 只放得下的行；超出的行截断（原文可查原 PDF）
            max_lines = int(en_rect.height / (en_fs * 1.2))
            shown = "\n".join(wrapped.split("\n")[:max(1, max_lines)])
            if shown.strip():
                tb = pymupdf.TextWriter(page.rect)
                tb.fill_textbox(en_rect, shown, fontsize=en_fs,
                                font=en_font, align=0)
                # TextWriter 无 color 参数 → write_text 后用灰色渲染整段
                tb.write_text(page, color=(0.35, 0.35, 0.35))
            continue   # 中文已单独排，跳过默认回灌
        _draw_para(tw_target, rect, txt, use_font, base, indent_chars,
                   warn_local, tag, lh_factor=lh_factor, center=center,
                   min_font=para_min)
    tw.write_text(page)
    tw_head.write_text(page)   # 黑体层（空 writer 写入无害）

    # ---- 3. D6 display 公式位图回贴（原 bbox 原位浮回）----
    if formula_pixmaps:
        for fi, png in formula_pixmaps.items():
            if fi >= len(layout["formulas"]):
                continue
            r = pymupdf.Rect(layout["formulas"][fi]["bbox"])
            page.insert_image(r, stream=png)


def _wrap_to_width(text: str, font: pymupdf.Font, fs: float, width: float) -> str:
    """贪心换行：把 text 按 font 宽度折成多行（保留原有 \\n）。"""
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            trial = f"{cur} {word}" if cur else word
            if font.text_length(trial, fontsize=fs) <= width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return "\n".join(lines)


def _bilingual_rect(rect: pymupdf.Rect, zh_fs: float, zh_len: int,
                    font: pymupdf.Font, en_len: int) -> pymupdf.Rect | None:
    """双语原文区：段落底部预留 min(需高, 段高40%)。放不下返回 None（放弃原文）。"""
    est_lines = max(1, int(en_len / max(1.0, rect.width / 6.0)) + 1)
    need_h = min(est_lines * MIN_FONT * 1.2, rect.height * 0.4)
    if need_h < MIN_FONT * 1.2:
        return None
    return pymupdf.Rect(rect.x0, rect.y1 - need_h, rect.x1, rect.y1)


def crop_formula_pixmaps(doc, page_no: int, formulas: list[dict]) -> dict[int, bytes]:
    """render 前 pre-crop：display 公式区渲染成 PNG（D6）。
    必须在 redaction 之前调用。"""
    out: dict[int, bytes] = {}
    page = doc[page_no]
    for fi, f in enumerate(formulas):
        pix = page.get_pixmap(clip=f["bbox"], dpi=300)
        out[fi] = pix.tobytes("png")
    return out