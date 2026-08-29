"""D1 渲染：redaction(保图形) + CJK TextWriter 段落回灌 + fit-to-bbox + D6 公式裁图回贴。

P0 已验证参数组合：
- apply_redactions(images=PDF_REDACT_IMAGE_NONE, graphics=PDF_REDACT_LINE_ART_NONE)
- pymupdf.Font(fontfile=SourceHanSansCN) + TextWriter.fill_textbox
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pymupdf

from .langs import resolve_original_font
from .layout import dominant_size

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
# 源 PDF 列表项的项目符号（并段后残留在句中，实测 paper3 p2 '· 提出'）
_BULLET_CHARS = "•·◦‣▪●▸‧"

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
    # 行首项目符号剥离（源列表行并入段落后不再是有序列表，符号是噪声）
    lines = [ln.lstrip(_BULLET_CHARS).strip() if ln else ln for ln in lines]
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


def find_cjk_font(explicit: str | None = None, lang: str = "zh") -> str:
    """目标语言正文字体（v0.4.2 多语言化，函数名保留旧称兼容）。

    平台三通吃（Windows 原生 / WSL / Linux·macOS），候选链见 langs.py。
    非拉丁文字系统（CJK/西里尔/阿拉伯/希伯来/天城文）找不到字体文件时
    抛错（豆腐块不可接受，提示用户配置）；拉丁目标返回 ""
    （pymupdf.Font(fontfile="") = 内置 Noto Serif，覆盖拉丁-1）。
    """
    from . import langs
    body, _ = langs.resolve_output_fonts(
        lang, {"cjk": explicit} if explicit else None)
    if body:
        return body
    if langs.lang_info(lang).script != "latin":
        raise FileNotFoundError(
            f"no font found for target language {lang!r}; set fonts.body "
            f"(or fonts.cjk) in config.yaml to a font covering it "
            f"(tried dirs: {[str(d) for d in langs.font_dirs()]})")
    return ""   # 西文：内置衬线兜底（覆盖率告警由 pipeline 补发）


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
                cell_texts: dict[int, str | None] | None = None,
                renderer: str = "writer",
                lang: str = "zh",
                para_specs: list[dict] | None = None,
                cell_specs: list[dict] | None = None,
                factors: dict[str, dict] | None = None,
                fit_cfg=None,
                archive: "pymupdf.Archive | None" = None,
                font_css: str | None = None) -> None:
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
    renderer:  v0.5.1 起 "htmlbox"=insert_htmlbox HTML+CSS 排版引擎（默认，
               自带 shaping/bidi/两端对齐，段落与单元格全走该路径）；
               "writer"=TextWriter 逐字排印（遗留稳定路径，RTL/复杂整形不可用）。
    lang:      目标语言 code（v0.5.1：RTL 语言注入 direction:rtl CSS）。
    v0.6.0 排版自适配（fit.mode=auto 时由 pipeline 预计算并传入）：
    para_specs/cell_specs: collect_para_specs/collect_cell_specs 产出
               （测量 pass 与渲染 pass 必须同一份，框/字号/样式才一致）；
    factors:   {类名: {factor, lead, tracking}} 样式级统一因子；
    archive/font_css: 每文档一次的字体 Archive 复用（None 则本页自建）。
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
    # 双语原文层字体:原文含 CJK(如 zh→en 反向)用目标 CJK 字体;西文原文用
    # Times 系（覆盖西里尔/扩展拉丁，Helvetica 只有拉丁-1，俄语原文会豆腐块）
    if _has_cjk("".join(p["text"] for p in paras)):
        en_font = font
    else:
        en_path = (typography.en_body_path if typography
                   else None) or resolve_original_font()
        en_font = pymupdf.Font(fontfile=en_path) if en_path else pymupdf.Font("helv")
    # D6 公式区 rects(双语 en 层防压公式位图)
    formula_rects = [pymupdf.Rect(f["bbox"]) for f in layout.get("formulas", [])]

    body_size = None
    if typography:
        from collections import Counter
        _sz: Counter = Counter()
        for p in paras:
            _sz[round(p.get("size") or dominant_size(p), 1)] += max(len(p["text"]), 1)
        body_size = _sz.most_common(1)[0][0] if _sz else None

    # ---- 2/3. 回灌 + 公式回贴（按渲染引擎分流）----
    # v0.5.1 修复:旧版单元格先画进 writer 缓冲再判 renderer，htmlbox 分支
    # 提前 return → tw.write_text 永不执行，htmlbox 模式表格文字全部丢失
    # （redact 后空白）。现单元格与段落同引擎渲染，htmlbox 模式彻底无
    # writer 残留路径。
    if renderer == "htmlbox":
        if archive is None or font_css is None:
            archive, font_css = _build_font_archive(
                font_path, typography.heading_path if typography else None)
        # v0.6.0: 渲染规格统一在此收集（测量 pass 已预收集时直接复用——
        # 两遍必须同一份 spec，框/字号/样式才一致）
        if para_specs is None:
            para_specs = collect_para_specs(
                paras, tmap, typography, bilingual, formula_rects, lang,
                layout=layout, fit_cfg=fit_cfg, page_h=page.rect.height)
        if cell_specs is None:
            cell_specs = collect_cell_specs(
                cells, cell_texts or {}, layout.get("tables"),
                font_path, lang)
        _render_cells_htmlbox(page, cells, cell_texts or {}, font_path,
                              typography, lang, warn_local,
                              cell_specs=cell_specs, factors=factors,
                              archive=archive, font_css=font_css)
        _render_paras_htmlbox(page, paras, tmap, font_path, typography,
                              bilingual, formula_rects, lang, warn_local,
                              para_specs=para_specs, factors=factors,
                              fit_cfg=fit_cfg,
                              archive=archive, font_css=font_css,
                              layout=layout)
        if formula_pixmaps:
            _paste_formula_pixmaps(page, formula_pixmaps, layout["formulas"])
        return

    cell_map = cell_texts or {}
    for ci, cell in enumerate(cells):
        dst = cell_map.get(ci)
        # v0.4.3 修复：译文缺失（dry-run client=None / cell_texts 未覆盖）时
        # 回灌原文——旧版直接 continue，但上方已对该格 add_redact_annot，
        # 扫描/干跑输出里表格单元格文字被静默删除（实测 dry-run 表格全空）
        if dst is None:
            dst = cell.get("text") or ""
        if not dst.strip():
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
            # v0.4.2: 首行缩进 2 字符仅 CJK 目标语言（中文期刊惯例）；
            # 西文按学术惯例不缩进
            base_indent = 2 if _has_cjk(txt) else 0
            indent_chars = 0 if (p.get("is_heading") or p.get("is_caption")
                                 or is_ref or len(txt) < 40) else base_indent
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
        _paste_formula_pixmaps(page, formula_pixmaps, layout["formulas"])


def _paste_formula_pixmaps(page, formula_pixmaps: dict[int, bytes],
                          formulas: list[dict]) -> None:
    """D6 display 公式位图回贴（原 bbox 原位浮回；两渲染引擎共用）。"""
    for fi, png in formula_pixmaps.items():
        if fi >= len(formulas):
            continue
        r = pymupdf.Rect(formulas[fi]["bbox"])
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


# ---- v0.5.0: insert_htmlbox 渲染路径（v0.5.1 转默认引擎）----

def _html_escape(text: str) -> str:
    import html as _html
    return _html.escape(text, quote=False)


def _dir_css(lang: str) -> str:
    """RTL 目标语言注入 direction:rtl（htmlbox Story 引擎自带 bidi 整形）。"""
    from .langs import is_rtl
    return "direction:rtl;" if is_rtl(lang) else ""


def _build_font_archive(font_path: str, heading_path: str | None) \
        -> tuple["pymupdf.Archive | None", str]:
    """把正文字体（+可选标题字体）装进 Archive，返回 (archive, css @font-face 段)。

    字体文件缺失（西文内置兜底）返回 (None, "")，CSS 落 serif 家族链。
    v0.6.0: Archive 改为每文档一次复用（pipeline 构建后透传）——旧版
    每页重建（段落路径+单元格路径各一次），8 页文档白建 16 次。
    """
    if not font_path:
        return None, ""
    arch = pymupdf.Archive()
    faces = []
    name = "ptbody" + Path(font_path).suffix.lower()
    arch.add(font_path, name)
    faces.append(f"@font-face {{font-family: ptbody; src: url({name});}}")
    if heading_path and heading_path != font_path:
        hname = "pthead" + Path(heading_path).suffix.lower()
        arch.add(heading_path, hname)
        faces.append(f"@font-face {{font-family: pthead; src: url({hname});}}")
    return arch, "\n".join(faces)


def measure_para(rect: "pymupdf.Rect", html: str, css: str,
                 archive: "pymupdf.Archive | None") -> float:
    """测量基座（v0.6.0 任务 A）：一段 HTML 在给定 CSS/字号下需要的实际高度。

    封装 Story(html, user_css, archive).fit_height(rect.width, 0, None)，
    user_css 前缀 body{margin:1px} 与 insert_htmlbox 内部实现一致，
    测量与落墨同源。fit 子系统的高阶测量（最大字号因子）见 fit.py。
    """
    w = rect.width
    if w < 5 or not html.strip():
        return 0.0
    story = pymupdf.Story(html=html, user_css="body {margin:1px;}" + css,
                          archive=archive)
    try:
        fit = story.fit_height(w, 0, None)
        return float(fit.parameter) if fit.parameter else 0.0
    except Exception:
        return rect.height      # 测量失败按装不下处理


# ---- v0.6.0: 排版规格（测量与渲染共用）----

def _para_css(font_css: str, family: str, base: float, lh: float,
              align: str, indent_em: float, dir_css: str,
              factor: float = 1.0, lead: float = 1.0,
              tracking: float = 1.0) -> str:
    """段落 CSS 构造（唯一出口——测量与渲染必须同源）。

    factor=类字号因子（fit.compute_style_factors 产出）；lead=行距系数；
    tracking=字距系数（<1 时注入 letter-spacing 负值）。
    """
    css = (font_css +
           f" p {{font-family:{family}; font-size:{base * factor:.2f}pt;"
           f" line-height:{lh * lead:.3f}; margin:0; text-align:{align};")
    if indent_em:
        css += f"text-indent:{indent_em}em;"
    if tracking < 1.0:
        css += f"letter-spacing:{tracking - 1.0:.4f};"
    css += dir_css + "}"
    return css


def _next_below_y(rect: "pymupdf.Rect", col: int, layout: dict,
                  paras: list[dict], self_idx: int) -> float | None:
    """同栏内本段下方最近元素（段落/图区/表区/公式区）的 y0。

    扩框下探边界的依据：扩出的空间不能压到任何下邻内容的原始位置
    （段落渲染从各自 y0 顶排，重叠=叠字）。图/表/公式区按 x 重叠判
    栏归属（无 col 标注，跨栏时只要 x 有重叠就视为下邻障碍）。
    """
    cands: list[float] = []
    my_y1 = rect.y1
    for j, q in enumerate(paras):
        if j == self_idx:
            continue
        qb = pymupdf.Rect(q["bbox"])
        if qb.y0 > my_y1 - 1.0 and q.get("col", 0) == col \
                and _h_overlap_ge(rect, qb, 0.2):
            cands.append(qb.y0)
    lay_figs = layout.get("figure_regions") or []
    lay_tabs = [pymupdf.Rect(t["bbox"]) for t in (layout.get("tables") or [])]
    lay_frs = [pymupdf.Rect(f["bbox"]) for f in (layout.get("formulas") or [])]
    for r in lay_figs + lay_tabs + lay_frs:
        if r.y0 > my_y1 - 1.0 and _h_overlap_ge(rect, r, 0.2):
            cands.append(r.y0)
    return min(cands) if cands else None


def _h_overlap_ge(a: "pymupdf.Rect", b: "pymupdf.Rect", frac: float) -> bool:
    inter = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    return inter >= frac * max(min(a.width, b.width), 1.0)


def collect_para_specs(paras: list[dict], tmap: dict,
                       typography, bilingual: bool,
                       formula_rects: list["pymupdf.Rect"],
                       lang: str, layout: dict | None = None,
                       fit_cfg=None, page_h: float | None = None) -> list[dict]:
    """段落 → 渲染规格列表（测量 pass 与渲染 pass 共用，保证两遍一致）。

    每条 spec: {i, tag, kind, cls, rect(含扩框), base, family, align,
    indent, lh, html, dir_css, can_shrink, is_heading}。
    v0.6.0 任务 C：非 ref、非双语段允许向下扩框（下探不越同栏下邻元素
    y0 − 2pt，上限 expand_lines×行高）——学术 PDF 段间距通常有
    0.3~0.5 行高，向下多占 1~2 行视觉无感。
    v0.6.1: 扩框下探 clamp 到页底（page_h−2pt）——末段无下邻元素时
    旧版会把框扩进页底空白，译文渲染进页边距（实测 p1 末条目悬行）。
    """
    from .typography import line_height_factor

    heading_path = typography.heading_path if typography else None
    # 正文字体族在 spec 里只存 family 名（CSS 构造时用）
    body_family = "ptbody, serif"
    head_family = "pthead, ptbody, serif"
    d = _dir_css(lang)

    from collections import Counter
    _sz: Counter = Counter()
    for p in paras:
        _sz[round(p.get("size") or dominant_size(p), 1)] += max(len(p["text"]), 1)
    body_size = _sz.most_common(1)[0][0] if _sz else None

    expand_on = bool(fit_cfg is not None and fit_cfg.mode == "auto"
                     and getattr(fit_cfg, "expand_lines", 0) > 0
                     and not bilingual)
    specs: list[dict] = []
    for i, p in enumerate(paras):
        if p.get("is_verbatim"):
            continue
        raw = tmap.get(i) or p["text"]
        txt = _clean_zh_text(raw)
        if not txt.strip():
            continue
        rect = pymupdf.Rect(p["bbox"])
        if p.get("is_ref"):
            # ref 条目贴边排：不微扩不扩框（相邻条目共享边界）
            rect.y0 += 0.5; rect.y1 -= 0.5
        else:
            rect.x0 -= 0.5; rect.y0 -= 0.5; rect.x1 += 0.5; rect.y1 += 0.5
        base = p.get("size") or dominant_size(p) or 10.0
        if typography is not None:
            style = typography.resolve(p, body_size)
            family = head_family if style.bold else body_family
            base = max(style.size, MIN_FONT)
            align = "center" if style.center else "justify"
            indent = style.indent
            lh = line_height_factor(style.kind)
            kind = style.kind
        else:
            family = body_family
            indent = 2 if (_has_cjk(txt) and len(txt) >= 40
                           and not p.get("is_heading")
                           and not p.get("is_caption")
                           and not p.get("is_ref")) else 0
            align = "justify"
            lh = 1.32
            # 无 typography 时按元数据映射类名（caption/ref 独立成类，
            # 因子/行距不与正文混同）
            kind = ("caption" if p.get("is_caption")
                    else "ref_entry" if p.get("is_ref") else "")
        is_heading = kind in ("title", "sec_title", "subsec_title",
                              "head_plain") or bool(p.get("is_heading"))
        full_rect = pymupdf.Rect(rect)
        # ---- 任务 C：向下扩框（测量/渲染同 rect）----
        if expand_on and not p.get("is_ref"):
            below = _next_below_y(rect, p.get("col", 0), layout or {},
                                  paras, i) if layout else None
            max_y1 = rect.y1 + fit_cfg.expand_lines * base * lh
            if below is not None:
                max_y1 = min(max_y1, below - 2.0)
            if page_h is not None:
                max_y1 = min(max_y1, page_h - 2.0)   # 不进页底空白
            if max_y1 > rect.y1:
                rect = pymupdf.Rect(rect.x0, rect.y0, rect.x1, max_y1)
                full_rect = pymupdf.Rect(rect)
        # 双语模式：译文层只有上部 60%（60/40 切框是硬约束，不扩框），
        # 测量与渲染必须同一个框，类因子才不虚高
        if bilingual and raw != p["text"] and not is_heading:
            split_y = full_rect.y0 + full_rect.height * 0.6
            rect = pymupdf.Rect(full_rect.x0, full_rect.y0, full_rect.x1,
                                max(split_y, full_rect.y0 + MIN_FONT * 2))
        specs.append({
            "i": i, "tag": f"render p{i}[{kind}]" if kind else f"render p{i}",
            "kind": kind,
            "cls": kind if kind and kind != "abstract" else "body",
            "rect": rect, "full_rect": full_rect,
            "base": base, "family": family, "align": align,
            "indent": indent, "lh": lh, "dir_css": d,
            "html": f"<p>{_html_escape(txt)}</p>",
            "src_text": p["text"], "raw_translated": raw,
            "can_shrink": kind not in ("title", "sec_title", "subsec_title",
                                       "head_plain"),
            "is_heading": is_heading,
        })
    return specs


def spec_css(spec: dict, font_css: str, lead: float = 1.0,
             tracking: float = 1.0, factor: float = 1.0) -> str:
    """spec → 测量/渲染用 CSS（factor 进字号，测量与渲染同源）。"""
    return _para_css(font_css, spec["family"], spec["base"], spec["lh"],
                     spec["align"], spec["indent"], spec["dir_css"],
                     factor=factor, lead=lead, tracking=tracking)


def collect_cell_specs(cells: list[dict], cell_map: dict,
                       tables: list[dict] | None,
                       font_path: str, lang: str) -> list[dict]:
    """表格单元格 → 渲染规格（v0.6.0：表类因子 per-table 测量）。

    窄格（<20pt 数字列）不参与类因子（nowrap 语义下无重排空间，
    维持引擎 per-cell scale_low=0.3 深缩放）；表归属按 bbox 包含
    匹配（layout["tables"] 每表 cells 与平铺 tables_cells 同源，
    缓存 JSON 往返后对象身份断裂，几何匹配才稳）。
    """
    d = _dir_css(lang)
    family = "ptbody, serif" if font_path else "serif"
    tab_rects = [pymupdf.Rect(t["bbox"]) for t in (tables or [])]
    specs: list[dict] = []
    for ci, cell in enumerate(cells):
        dst = cell_map.get(ci)
        if dst is None:
            dst = cell.get("text") or ""
        if not dst.strip():
            continue
        crect = pymupdf.Rect(cell["bbox"])
        if crect.width < 8 or crect.height < 5:
            continue
        c_fs = min(dominant_size({"spans": cell.get("spans", [])}) or 8.0, 8.0)
        nowrap = crect.width < 20
        tid = -1
        for ti, tr in enumerate(tab_rects):
            if tr.contains(crect):
                tid = ti
                break
        # v0.7.0: fit 类按逻辑表 gid（跨页延续表共享父表 gid——跨页字号
        # 统一；无 gid 时退页内 tid 保持旧行为）
        gid = tid
        if 0 <= tid < len(tables or []):
            gid = (tables or [])[tid].get("gid", tid)
        specs.append({
            "i": ci, "tag": f"cell{ci}", "kind": "cell",
            "cls": f"table{gid}" if (tid >= 0 and not nowrap) else None,
            "rect": crect, "base": c_fs, "family": family, "align": "left",
            "indent": 0, "lh": 1.25, "dir_css": d,
            "html": f"<p>{_html_escape(_clean_zh_text(dst))}</p>",
            "nowrap": nowrap, "table_id": tid,
        })
    return specs


def cell_spec_css(spec: dict, font_css: str) -> str:
    css = _para_css(font_css, spec["family"], spec["base"], spec["lh"],
                    "left", 0, spec["dir_css"])
    if spec.get("nowrap"):
        css = css[:-1] + "white-space:nowrap;}"
    return css


def _render_cells_htmlbox(page, cells: list[dict], cell_map: dict,
                          font_path: str, typography, lang: str,
                          warnings: list[str],
                          cell_specs: list[dict] | None = None,
                          factors: dict[str, dict] | None = None,
                          archive: "pymupdf.Archive | None" = None,
                          font_css: str = "") -> None:
    """表格单元格回灌的 HTML 框架路径（v0.5.1，替代 writer 残留路径）。

    v0.6.0：宽格按表类因子统一字号（同表同字号）；窄格维持 nowrap +
    引擎深缩放语义。RTL 目标语言注入 direction:rtl。
    """
    if not cells:
        return
    if archive is None or font_css is None:
        archive, font_css = _build_font_archive(
            font_path, typography.heading_path if typography else None)
    specs = cell_specs if cell_specs is not None else \
        collect_cell_specs(cells, cell_map, None, font_path, lang)
    for spec in specs:
        f = (factors or {}).get(spec.get("cls") or "") or \
            {"factor": 1.0, "lead": 1.0, "tracking": 1.0}
        css = _para_css(font_css, spec["family"], spec["base"], spec["lh"],
                        "left", 0, spec["dir_css"], factor=f["factor"],
                        lead=f.get("lead", 1.0), tracking=f.get("tracking", 1.0))
        if spec.get("nowrap"):
            css = css[:-1] + "white-space:nowrap;}"
        _insert_one_htmlbox(page, spec["rect"], spec["html"], css,
                            spec["tag"], warnings, archive=archive,
                            scale_low=0.3)


def _render_paras_htmlbox(page, paras: list[dict], tmap: dict,
                          font_path: str, typography, bilingual: bool,
                          formula_rects: list["pymupdf.Rect"],
                          lang: str, warnings: list[str],
                          para_specs: list[dict] | None = None,
                          factors: dict[str, dict] | None = None,
                          fit_cfg=None,
                          archive: "pymupdf.Archive | None" = None,
                          font_css: str = "",
                          layout: dict | None = None) -> None:
    """段落回灌的 HTML+CSS 排版引擎路径（v0.5.0 种子，v0.5.1 转默认）。

    与 writer 路径的差异：
    - 断行/避头尾/试排降字号全部交给 Story 引擎（justify + shaping + bidi）
    - v0.6.0：两遍式排版自适配——spec 由 collect_para_specs 收集
      （测量 pass 与本渲染 pass 共用），类因子 factors 统一同类字号
      /行距/字距；fit 关闭时逐段引擎 scale_low 缩放（v0.5.1 行为）
    - 双语原文层用 opacity 半透明呈现（writer 路径是灰色）
    - typography 样式映射保留：标题用标题字体加粗、caption/ref 小字号、
      CJK 正文首行缩进 2em；ref 条目不扩边不扩框
    - v0.5.1: RTL 目标语言注入 direction:rtl；双语原文层含 CJK 时用
      ptbody 家族（内置 serif 无 CJK 字形会豆腐块）
    """
    heading_path = typography.heading_path if typography else None
    if archive is None or font_css is None:
        archive, font_css = _build_font_archive(font_path, heading_path)

    specs = para_specs if para_specs is not None else \
        collect_para_specs(paras, tmap, typography, bilingual,
                           formula_rects, lang,
                           layout=layout or {}, fit_cfg=fit_cfg,
                           page_h=page.rect.height)
    for spec in specs:
        # fit 开启时用类因子；关闭时 factor=1（引擎 scale_low 兜底=旧行为）。
        # v0.6.1: 孤立紧段落带 per-spec override（测量期逐级降级的结果），
        # 不陪绑全类，也不依赖引擎盲缩
        if factors is not None:
            ov = spec.get("_fit_override")
            if ov is not None:
                factor = ov.get("factor", 1.0)
                lead = ov.get("lead", 1.0)
                track = ov.get("tracking", 1.0)
            else:
                f = factors.get(spec["cls"]) or \
                    {"factor": 1.0, "lead": 1.0, "tracking": 1.0}
                # 标题类不参与收缩：f 恒 1（扩框/行距已在测量 pass 用尽）
                factor = f["factor"] if spec["can_shrink"] else 1.0
                lead = f.get("lead", 1.0)
                track = f.get("tracking", 1.0)
            css = _para_css(font_css, spec["family"], spec["base"],
                            spec["lh"], spec["align"], spec["indent"],
                            spec["dir_css"], factor=factor,
                            lead=lead, tracking=track)
        else:
            css = _para_css(font_css, spec["family"], spec["base"],
                            spec["lh"], spec["align"], spec["indent"],
                            spec["dir_css"])
        # 双语：上部 60% 译文（spec.rect 已按 60% 切好）；底部原文层
        # 半透明（与公式区重叠则放弃）
        if bilingual and spec["raw_translated"] != spec["src_text"] \
                and not spec["is_heading"]:
            zh_rect = spec["rect"]
            full = spec.get("full_rect") or spec["rect"]
            en_rect = pymupdf.Rect(full.x0, zh_rect.y1 + 1.0, full.x1,
                                   full.y1)
            _insert_one_htmlbox(page, zh_rect, spec["html"], css,
                                f"{spec['tag']}/zh", warnings,
                                archive=archive)
            if en_rect.height >= MIN_FONT * 1.5 and en_rect.width >= 20 \
                    and not any(en_rect.intersects(fr) for fr in formula_rects):
                # 原文层字体：含 CJK（zh→en 反向）用 ptbody；西文用 serif
                en_family = "ptbody, serif" \
                    if (font_path and _has_cjk(spec["src_text"])) else "serif"
                base = spec["base"]
                en_css = (font_css +
                          f" p {{font-family:{en_family};"
                          f" font-size:{max(MIN_FONT, base * 0.75):.2f}pt;"
                          f" line-height:1.2; margin:0;}}")
                _insert_one_htmlbox(page, en_rect,
                                    f"<p>{_html_escape(spec['src_text'])}</p>",
                                    en_css, f"{spec['tag']}/en", warnings,
                                    opacity=0.6, archive=archive)
            continue
        _insert_one_htmlbox(page, spec["rect"], spec["html"], css,
                            spec["tag"], warnings, archive=archive)


def _insert_one_htmlbox(page, rect: "pymupdf.Rect", text: str, css: str,
                        tag: str, warnings: list[str],
                        opacity: float = 1.0,
                        archive: "pymupdf.Archive | None" = None,
                        scale_low: float = 0.5) -> None:
    """单段 insert_htmlbox + 溢出告警。

    v0.6.0 任务 A 丢段修复：spare < -0.5 表示引擎在 scale_low 下限仍
    装不下——insert_htmlbox 此时【不落任何墨】（v0.5.1 只告警，段落
    整段消失）。现改为：如实告警后以 scale_low=0（任意缩放）重试一次，
    极端情况下宁可以极小字号可见，也不整段消失。
    v0.6.0 任务 B 一致性守门：返回 scale < 1 记 warning（类因子已用尽
    仍在做元素级缩放——说明测量与渲染出现了偏差，值得暴露）。
    """
    if not text.strip():
        return
    spare = scale = None
    try:
        spare, scale = page.insert_htmlbox(
            rect, text, css=css, scale_low=scale_low,
            opacity=opacity, overlay=True, archive=archive)
    except Exception as e:
        warnings.append(f"{tag}: htmlbox failed ({e})")
        return
    if spare is not None and spare < -0.5:
        warnings.append(
            f"{tag}: paragraph dropped (scale {scale:.2f} < scale_low "
            f"{scale_low}) — retrying unrestricted")
        try:
            spare, scale = page.insert_htmlbox(
                rect, text, css=css, scale_low=0.0,
                opacity=opacity, overlay=True, archive=archive)
        except Exception as e:
            warnings.append(f"{tag}: paragraph dropped; retry failed ({e})")
            return
        if spare is not None and spare < -0.5:
            warnings.append(f"{tag}: paragraph dropped entirely")
            return
        warnings.append(f"{tag}: rendered at emergency scale {scale:.2f}")
        return
    if scale is not None and scale < 0.995:
        warnings.append(
            f"{tag}: element-level scale={scale:.2f} (style factor "
            f"exhausted) — size may be inconsistent")