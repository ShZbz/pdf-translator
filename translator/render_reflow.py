"""v0.8.0 P3：reflow 整文档重排模式（output.mode: reflow，任务 3.1-3.5）。

模式 B：语义文档模型 → 新模板 → Story 整文档流式写入（自动断页）。
与 faithful 的本质区别：页面对应关系不存在，框跟内容走——无 fit 因子、
无扩框/缩字/降级阶梯（任务 3.4），排版质量由「内容流 + 预设样式表」
自然产生（任务 3.4.1：正文 10.5pt/1.4、H1 16、H2 13、图注 8.5/1.3、
文献 9/1.25；正文字号默认沿用原文档 body 字号保持视觉延续）。

文档模型（任务 3.1）：复用 layout 层全部输出，跨页组装阅读序——
段落（typography 单一来源分类）/ 公式位图（复用 pipeline 预裁 PNG）/
图区+图注绑定 / 表格（高置信 HTML 重排，低置信整表位图）/ Algorithm
框（verbatim 整框位图保真——reflow 下原像素不存在，缩进与数学排版
无法用 HTML 子集重建）/ 列表语义化（is_list_item 连续项 → <ol>/<ul>，
编号/符号由引擎生成）；跨页断句合并单元整段入流（不再拆回两页）。

模板（任务 3.2）：页尺寸/边距/栏结构取原文档统计（栏数与栏宽沿用，
可配 single）；rectfn 按模板逐框供栏、col 0 真值 mediabox 自动开新页；
页码与 PDF 书签（doc.set_toc）写入后统一落。

引擎能力依据（tools/reflow_probe.py 6/6 实测）：多页 write 循环、
element_positions 页相对坐标（书签依据）、<img> width 样式纵横比保持、
page-break-inside:avoid（图+注 keep-together）、表格行跨框整行迁移
不拆断、ol/ul 文流内编号。
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pymupdf

from .render import _build_font_archive, _clean_zh_text, _dir_css, _html_escape

# 单 Story 块数软上限：超过则在章节边界分段写入（防超长文档内存膨胀）
SEGMENT_BLOCKS = 500
# 图注绑定：图下注/表上注的最大吸附距离（pt）
_CAPTION_GAP = 34.0


# ---- 任务 3.2：模板系统 ----

@dataclass
class Template:
    """reflow 页面模板：每页栏框序列由原文档统计得出。"""
    page_w: float
    page_h: float
    margin_top: float
    margin_bottom: float
    cols: list = field(default_factory=list)   # [(x0, x1)] 阅读序

    def frame(self, index: int):
        """第 index 个栏框 → (mediabox, rect)。col 0 携带真值 mediabox
        （自动开新页语义，实测 falsy 续框）；框无限供给=自动断页。"""
        _page, col = divmod(index, len(self.cols))
        x0, x1 = self.cols[col]
        rect = pymupdf.Rect(x0, self.margin_top, x1,
                            self.page_h - self.margin_bottom)
        return (pymupdf.Rect(0, 0, self.page_w, self.page_h)
                if col == 0 else None), rect


def _quantile(vals: list[float], q: float, default: float) -> float:
    if not vals:
        return default
    s = sorted(vals)
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] * (1.0 - pos + lo) + s[hi] * (pos - lo)


def build_template(page_layouts: list[dict], doc, columns: str = "auto") \
        -> Template:
    """原文档统计 → 页面模板（页尺寸/边距/栏结构，任务 3.2）。"""
    page_w, page_h = doc[0].rect.width, doc[0].rect.height
    x0s, x1s, y0s, y1s = [], [], [], []
    col0_x0, col0_x1, col1_x0, col1_x1 = [], [], [], []
    modes: dict[str, int] = {}
    for lay in page_layouts:
        m = lay.get("mode", "one")
        modes[m] = modes.get(m, 0) + 1
        for p in lay["paragraphs"]:
            r = pymupdf.Rect(p["bbox"])
            x0s.append(r.x0)
            x1s.append(r.x1)
            y0s.append(r.y0)
            y1s.append(r.y1)
            if p.get("col", 0) == 0:
                col0_x0.append(r.x0)
                col0_x1.append(r.x1)
            else:
                col1_x0.append(r.x0)
                col1_x1.append(r.x1)
    ml = max(24.0, min(_quantile(x0s, 0.06, 57.0), page_w * 0.25))
    mr = max(24.0, min(page_w - _quantile(x1s, 0.94, page_w - 57.0),
                       page_w * 0.25))
    # 上下边距同样夹持（图在页顶/页底的文档，正文 y 统计会被带偏——
    # 实测合成样张 mt=280/mb=388 把栏框压到 124pt 高，布局散架）
    mt = max(30.0, min(_quantile(y0s, 0.06, 57.0), page_h * 0.18))
    mb = max(30.0, min(page_h - _quantile(y1s, 0.94, page_h - 57.0),
                       page_h * 0.18))
    mode = max(modes, key=modes.get) if modes else "one"
    cols: list = []
    if columns == "single" or mode != "two":
        cols = [(ml, page_w - mr)]
    else:
        # 栏宽沿用原文档：左右栏各自取 col 块 x 范围统计
        l0 = _quantile(col0_x0, 0.06, ml)
        r0 = min(_quantile(col0_x1, 0.94, page_w / 2 - 10), page_w / 2)
        l1 = max(_quantile(col1_x0, 0.06, page_w / 2 + 10),
                 page_w / 2 - 5)
        r1 = max(_quantile(col1_x1, 0.94, page_w / 2 + 30), l1 + 40)
        r1 = min(r1, page_w - mr)
        cols = [(min(l0, page_w / 2 - 10), r0), (l1, r1)]
    return Template(page_w, page_h, mt, mb, cols)


# ---- 任务 3.1：文档模型 ----

@dataclass
class Block:
    """文档模型统一块。kind ∈ para/list/figure/table/formula/verbatim。"""
    kind: str
    text: str = ""                # para 译文（或保留原文）
    kind_cls: str = "body"        # typography kind / list_item
    html_extra: str = ""          # list 的 <ol>/<ul>、table 的 <table>
    src_text: str = ""            # 原文（书签回退/调试）
    caption: str = ""             # 绑定图注译文（figure/table）
    width_pt: float = 0.0         # 位图原宽（pt）
    img_name: str = ""            # archive 内名（位图块）
    html_id: str = ""             # 书签锚 id（heading 段）


_ORDERED_MARKER_RE = re.compile(
    r"^\s*[\(\[（【]?(?:\d{1,2}|[ivxIVX]{1,4}|[a-hA-H])[\)\]）】]?[.、：\s]")

# 伪代码碎片形态（Algorithm 框判定漏网块——faithful 原位无感，reflow
# 拆位图后成散落文本行，实测 paper3 p4 'action = {ak, Lk}' 等）
_PSEUDO_RE = re.compile(
    r"[{}⟨⟩]|←|∼|~|⌊|⌋|\b(?:for|while|if|else|end|return|break|"
    r"sample|Input|Output|Require|Ensure|Initialize)\b")


def _pseudo_clusters(paras: list[dict]) -> "list[tuple[set, pymupdf.Rect]]":
    """Algorithm 框聚类：每个 verbatim 种子迭代吸收「相交且形似伪代码」
    的碎片段（faithful 原位无感，reflow 拆位图后散落文本行——实测
    paper3 p4 'action = {ak, Lk}' 等）。返回 [(索引集, 联合框)]。"""
    clusters: list[tuple[set, pymupdf.Rect]] = []
    used: set = set()
    for s, p in enumerate(paras):
        if not p.get("is_verbatim") or s in used:
            continue
        idx = {s}
        u = pymupdf.Rect(p["bbox"])
        changed = True
        while changed:
            changed = False
            for i, q in enumerate(paras):
                if i in idx or q.get("is_caption") or q.get("is_ref"):
                    continue
                r = pymupdf.Rect(q["bbox"])
                if not u.intersects(r):
                    continue
                if not _PSEUDO_RE.search(q.get("text") or ""):
                    continue
                idx.add(i)
                u |= r
                changed = True
        used |= idx
        clusters.append((idx, u))
    return clusters


# 剥标记专用（layout._LIST_MARKER_RE 是匹配器——其 \d[.)]\s+\S 分支
# 把首字内容算进匹配，直接 sub 会吃掉正文首字，实测「第一项」→「一项」）
_STRIP_MARKER_RE = re.compile(
    r"^\s*(?:"
    r"[(\[（【]?(?:\d{1,2}|[ivxIVX]{1,4}|[a-hA-H])[)\]）】]?(?:[.、：]|\s)"
    r"|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫]"
    r"|[•◦‣▪●▸‧·]\s?"
    r")\s*")


def _strip_marker(text: str) -> str:
    """剥列表项前缀标记（编号/符号交由 <ol>/<ul> 引擎生成）。"""
    return _STRIP_MARKER_RE.sub("", text, count=1).strip()


# 无歧义项目符号（不含 CJK 姓名间隔号 U+00B7——「冯·诺依曼」不可拆）
_BULLET_SPLIT_RE = re.compile(r"[•◦‣▪●▸]")


def _group_lists(blocks: list[Block]) -> list[Block]:
    """连续 list-item 段合成语义列表块（任务 1.5 在 reflow 落地）。

    并段残留的句中项目符号（源 PDF 贡献列表多符号并入一块）拆成多个
    <li>——悬挂缩进与符号由引擎生成。
    """
    out: list[Block] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.kind == "para" and b.kind_cls == "list_item":
            ordered = bool(_ORDERED_MARKER_RE.match(b.text))
            texts: list[str] = []
            j = i
            while j < len(blocks) and blocks[j].kind == "para" \
                    and blocks[j].kind_cls == "list_item" \
                    and bool(_ORDERED_MARKER_RE.match(
                        blocks[j].text)) == ordered:
                bj = blocks[j]
                if ordered:
                    texts.append(_strip_marker(bj.text))
                else:
                    parts = [s.strip() for s in
                             _BULLET_SPLIT_RE.split(bj.text) if s.strip()]
                    texts.extend(parts or [bj.text])
                j += 1
            lis = "".join(f"<li>{_html_escape(t)}</li>" for t in texts)
            tag = "ol" if ordered else "ul"
            out.append(Block(kind="list", kind_cls=tag,
                             html_extra=f"<{tag}>{lis}</{tag}>",
                             text=" ".join(texts)))
            i = j
        else:
            out.append(b)
            i += 1
    return out


def build_document_model(page_layouts: list[dict], doc,
                         texts_by_page: dict, cell_texts: dict,
                         cross_full: dict, cross_skip: set,
                         formula_pixmaps: dict, typo,
                         warnings: "list | None" = None,
                         dcache=None, doc_fp: "str | None" = None) \
        -> tuple[list[Block], dict[str, bytes], list[dict]]:
    """layout 全输出 → 跨页阅读序统一块流。

    返回 (blocks, images{name: png_bytes}, bookmarks[{level, text}])；
    书签页码由写入循环回填。公式位图复用 pipeline 预裁 PNG（与 faithful
    同源，reflow 不重复裁剪）；图区/表格/verbatim 位图此处从原 doc 裁剪
    （reflow 不 redact，原像素完整）。
    v0.8.1 S4: dcache/doc_fp 提供时全部 300dpi 裁图入项目位图缓存
    （(指纹,页,区域,dpi) 内容寻址——重跑/调开关重渲染场景免重裁）。
    """
    warn = warnings if warnings is not None else []
    images: dict[str, bytes] = {}
    bm_by_block: dict[int, dict] = {}

    def _crop(page, rect: "pymupdf.Rect", name: str, dpi: int = 300) -> str:
        png = None
        if dcache is not None and doc_fp is not None:
            from .doccache import DocumentCache
            key = DocumentCache.pixmap_key(doc_fp, page.number, rect, dpi)
            png = dcache.load_pixmap(key)
            if png is None:
                png = page.get_pixmap(clip=rect, dpi=dpi).tobytes("png")
                dcache.save_pixmap(key, doc_fp, page.number, png)
        else:
            png = page.get_pixmap(clip=rect, dpi=dpi).tobytes("png")
        images[name] = png
        return name

    blocks: list[Block] = []
    for pno, lay in enumerate(page_layouts):
        page = doc[pno]
        paras = lay["paragraphs"]
        page_texts = texts_by_page.get(pno) or [None] * len(paras)
        figs = [pymupdf.Rect(f) for f in
                (lay.get("figure_regions") or [])]
        tabs = lay.get("tables") or []
        formulas = lay.get("formulas") or []

        # --- 图注绑定：图下注（Fig.）吸附图区；表上注吸附表区 ---
        # 注可位于区 y 范围之外（下方贴邻）或区内下半部（源排版把注
        # 排进图区 bbox——paper3 p1 实测），两种几何都绑定
        fig_caps: dict[int, int] = {}
        tab_caps: dict[int, int] = {}
        for i, p in enumerate(paras):
            if not p.get("is_caption"):
                continue
            r = pymupdf.Rect(p["bbox"])
            for fi, fr in enumerate(figs):
                below = r.y0 >= fr.y1 - 6 and r.y0 - fr.y1 <= _CAPTION_GAP
                inside_low = (fr.y0 + 0.55 * fr.height <= r.y0
                              and r.y1 <= fr.y1 + 10)
                if _x_overlap(r, fr) >= 0.5 and (below or inside_low):
                    fig_caps.setdefault(fi, i)
                    break
            else:
                for ti, t in enumerate(tabs):
                    tr = pymupdf.Rect(t["bbox"])
                    above = r.y1 <= tr.y0 + 6 and tr.y0 - r.y1 <= _CAPTION_GAP
                    inside_top = (tr.y0 - 10 <= r.y0
                                  and r.y1 <= tr.y0 + 0.45 * tr.height)
                    if _x_overlap(r, tr) >= 0.5 and (above or inside_top):
                        tab_caps.setdefault(ti, i)
                        break
        bound = set(fig_caps.values()) | set(tab_caps.values())
        baked_rects: list = []           # 烘焙进位图的区域（正文带回归判定用）
        # Algorithm 框聚类：代表块（簇内最小索引）出联合框位图，碎片随框
        v_rep: dict[int, pymupdf.Rect] = {}
        v_absorbed: set = set()
        for idx, u in _pseudo_clusters(paras):
            rep = min(idx)
            v_rep[rep] = u
            v_absorbed |= (idx - {rep})

        def ptext(i: int) -> str:
            if (pno, i) in cross_skip:
                return ""
            if (pno, i) in cross_full:
                return cross_full[(pno, i)]
            return page_texts[i] or paras[i]["text"]

        # (y, x, col_hint, Block, geometry_rect)；col_hint -1 = 全宽分带
        entries: list[tuple] = []

        for i, p in enumerate(paras):
            if i in bound or (pno, i) in cross_skip or i in v_absorbed:
                continue
            txt = _clean_zh_text(ptext(i))
            if not txt.strip():
                continue
            r = pymupdf.Rect(p["bbox"])
            if i in v_rep:
                # Algorithm 伪代码框：整框位图保真（含判定漏网碎片——
                # reflow 下原像素不存在，缩进/数学排版无法用 HTML 重建）
                grow = pymupdf.Rect(v_rep[i].x0 - 2, v_rep[i].y0 - 2,
                                    v_rep[i].x1 + 2, v_rep[i].y1 + 2)
                name = _crop(page, grow, f"v{pno}_{i}")
                b = Block(kind="verbatim", img_name=name,
                          width_pt=grow.width)
                baked_rects.append(pymupdf.Rect(grow))
                entries.append((grow.y0, grow.x0, p.get("col", 0), b, None))
                continue
            style = typo.resolve(p, _doc_body_size(page_layouts))
            kind_cls = style.kind
            if not p.get("is_heading") and not p.get("is_caption") \
                    and not p.get("is_ref") \
                    and bool(p.get("is_list_item")):
                kind_cls = "list_item"
            b = Block(kind="para", text=txt, kind_cls=kind_cls,
                      src_text=p["text"])
            if kind_cls in ("title", "sec_title", "subsec_title"):
                # 书签元数据挂块上——最终书签序由文档流序推导（entries
                # 构建序是栏序，直接 append 会把右栏标题排到节标题后）
                bm_by_block[id(b)] = {
                    "level": {"title": 1, "sec_title": 2,
                              "subsec_title": 3}[kind_cls], "text": txt}
            entries.append((r.y0, r.x0, p.get("col", 0), b, r))

        # display 公式位图（复用 pipeline 预裁；全宽分带元素）。
        # v0.8.1 修复：公式 rect 必须进 baked_rects——layout 的 in_protected
        # 把与公式区 ≥30% 相交的碎片块（'dt .'/'(6)'）放 fig_text_blocks，
        # 漏登记会令这些碎片以原文段落重复回归文流（位图已含其像素）
        page_pms = (formula_pixmaps or {}).get(pno) or {}
        for fi, f in enumerate(formulas):
            fr = pymupdf.Rect(f["bbox"])
            png = page_pms.get(fi)
            name = f"x{pno}_{fi}"
            if png:
                images[name] = png
            else:
                _crop(page, fr, name)
            b = Block(kind="formula", img_name=name, width_pt=fr.width)
            entries.append((fr.y0, fr.x0, -1, b, fr))
            baked_rects.append(fr)

        # 图区（+绑定图注；裁剪修整见 _trim_region——区域检测 bbox 可能
        # 吞进邻接作者块/正文边缘，faithful 原位无感而 reflow 搬运后
        # 残影可见；被裁文字带以文本段落回归文流，内容零丢失）
        fig_texts = lay.get("fig_text_blocks") or []
        for fi, fr0 in enumerate(figs):
            ci = fig_caps.get(fi)
            cap_r = pymupdf.Rect(paras[ci]["bbox"]) if ci is not None else None
            cap_below = (cap_r.y0 >= fr0.y0 + 0.5 * fr0.height
                         if cap_r is not None else True)
            fr = _trim_region(fr0, fig_texts, paras, bound, cap_r, cap_below)
            if fr.is_empty or fr.width < 10 or fr.height < 8:
                fr = pymupdf.Rect(fr0)          # 修整退化：回原区
            name = _crop(page, fr, f"g{pno}_{fi}")
            b = Block(kind="figure", img_name=name, width_pt=fr.width)
            if ci is not None:
                b.caption = _clean_zh_text(ptext(ci))
            entries.append((fr.y0, fr.x0, -1, b, fr))
            baked_rects.append(pymupdf.Rect(fr))

        # 表格（高置信 HTML 重排 / 低置信整表位图，任务 3.3）
        cell_off = 0
        for ti, t in enumerate(tabs):
            tr0 = pymupdf.Rect(t["bbox"])
            tr = _tighten_region(tr0, paras, bound)
            ci = tab_caps.get(ti)
            if ci is not None:
                cap_r = pymupdf.Rect(paras[ci]["bbox"])
                if cap_r.y1 <= tr.y0 + 0.5 * tr.height:      # 表上注
                    tr.y0 = max(tr.y0, cap_r.y1 + 1)
                else:                                        # 表下注
                    tr.y1 = min(tr.y1, cap_r.y0 - 1)
            if tr.is_empty or tr.width < 10 or tr.height < 8:
                tr = pymupdf.Rect(tr0)
            cells = t.get("cells") or []
            confs = [c.get("conf", 1.0) for c in cells]
            html_tab = None
            if cells and min(confs) >= 0.7 \
                    and tr.height <= page.rect.height * 0.85:
                html_tab = _table_html(cells, cell_texts, pno, cell_off)
            if html_tab is not None:
                b = Block(kind="table", html_extra=html_tab)
                # v0.8.1 修复：HTML 重排表同样要进 baked_rects——表内文字块
                # 被 layout 归入 fig_text_blocks（中心落表判定），漏登记会
                # 令表内容以原文段落重复回归文流（译文已在 HTML 表里）。
                # 用原始 tr0（收紧只考虑 paras 侵入，格可布满原区）
                baked_rects.append(pymupdf.Rect(tr0))
            else:
                name = _crop(page, tr, f"t{pno}_{ti}")
                b = Block(kind="table", img_name=name, width_pt=tr.width)
                baked_rects.append(pymupdf.Rect(tr))
                if tr.height > page.rect.height:
                    # 超页高整表：按高度比例缩宽（位图纵横比自带）
                    b.width_pt = tr.width * (page.rect.height * 0.9
                                             / tr.height)
                    warn.append(
                        f"reflow p{pno + 1}: table {ti} taller than page "
                        f"({tr.height:.0f}pt) — scaled to "
                        f"{b.width_pt:.0f}pt wide")
            ci = tab_caps.get(ti)
            if ci is not None:
                b.caption = _clean_zh_text(ptext(ci))
            entries.append((tr.y0, tr.x0, -1, b, tr))
            cell_off += len(cells)

        # 被吞文字带回归文流：未烘焙进任何位图区域的 fig_text_blocks。
        # 自然语言（作者块/机构行）→ 原文文本块回流；伪代码形态碎片
        # （Algorithm 框漏网行，实测 'action = {ak, Lk}'/'Break out of
        # for loop'）→ 独立位图随文流（避免英文散落中文行间）。
        # v0.8.1: 自然语言带先做纵向并块——作者块/机构行在源文档是一个
        # 视觉单元，被图区边界切成多个碎块，逐块独立成段在 reflow 下
        # 成为间距不均的散落行（实测 paper3 p1 每位作者 2 块三种版式）
        two = _mode_cols(page_layouts) == "two"
        mid = page.rect.width / 2
        nat_bands: list[dict] = []
        frag_bands: list[tuple] = []
        for tb in fig_texts:
            try:
                r = pymupdf.Rect(tb["bbox"])
            except Exception:
                continue
            if any(r.intersects(br) for br in baked_rects):
                continue
            t = (tb.get("text") or "").strip()
            if not t:
                continue
            if _PSEUDO_RE.search(t) and len(t) < 90:
                frag_bands.append((
                    r, t,
                    (0 if (r.x0 + r.x1) / 2 < mid else 1) if two else 0))
            else:
                nat_bands.append({
                    "r": r, "t": t,
                    "hint": ((0 if (r.x0 + r.x1) / 2 < mid else 1)
                             if two else 0)})
        for m in _merge_adjacent_bands(nat_bands):
            b = Block(kind="para", text=_clean_zh_text(m["t"]),
                      kind_cls="body", src_text=m["t"])
            # geo=None：自然语言带按列项参与栏序（hint 已按 x 中点定栏）。
            # 传 rect 会让 _is_fullwidth 把跨几何中线的带升级成全宽分带
            # ——作者区横跨中线的带会把同 y 左栏第一作者段落挤到后面
            # （实测 paper3 p1 作者序 3rd/2nd/1st 错乱根因）
            entries.append((m["r"].y0, m["r"].x0, m["hint"], b, None))
        for r, t, hint in frag_bands:
            grow = pymupdf.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2)
            name = _crop(page, grow, f"ft{pno}_{len(baked_rects)}")
            b = Block(kind="verbatim", img_name=name,
                      width_pt=grow.width)
            baked_rects.append(pymupdf.Rect(grow))
            entries.append((r.y0, r.x0, hint, b, r))

        blocks.extend(_order_page(entries, page, page_layouts))

    # 书签按最终文档流序推导（跨页 + 页内分带序）
    bookmarks = [bm_by_block[id(b)] for b in blocks if id(b) in bm_by_block]
    blocks = _group_lists(blocks)
    return blocks, images, bookmarks


def _x_overlap(a: "pymupdf.Rect", b: "pymupdf.Rect") -> float:
    inter = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    return inter / max(min(a.width, b.width), 1.0)


def _merge_adjacent_bands(bands: list[dict]) -> list[dict]:
    """自然语言带回流的纵向并块（v0.8.1，reflow 特有）。

    作者块/机构行在源文档是一个视觉单元，被图区检测边界切成多个碎块
    （实测 paper3 p1 每位作者「姓名/机构」+「大学/邮箱」两块）；逐块
    独立成段在重排后成为间距不均的散落行。同一纵向堆叠（横向重叠
    ≥ 0.25 且纵向紧邻 gap ≤ 1.2×行高）的带并成一段——按 (栏, x, y)
    排序聚堆（按 (y, x) 排会把左右并列的堆叠交错，实测并块失效）。
    """
    if len(bands) < 2:
        return bands
    bands = sorted(bands, key=lambda b: (b["hint"], b["r"].x0, b["r"].y0))
    out: list[dict] = []
    for b in bands:
        if out:
            p = out[-1]
            gap = b["r"].y0 - p["r"].y1
            h = max(p["r"].height, b["r"].height, 1.0)
            if b["hint"] == p["hint"] and -0.3 * h <= gap <= 1.2 * h \
                    and _x_overlap(p["r"], b["r"]) >= 0.25:
                p["t"] = p["t"] + " " + b["t"]
                p["r"] |= b["r"]
                continue
        out.append(dict(b))
    return out


def _trim_region(fr0: "pymupdf.Rect", fig_texts: list[dict],
                 paras: list[dict], exclude: set,
                 cap_r: "pymupdf.Rect | None", cap_below: bool) \
        -> "pymupdf.Rect":
    """图区裁剪修整（区域检测 bbox 误差不进位图，reflow 特有）：
    - 上下缘贴边的**多行文字带**（被区域吞掉的正文，实测 paper3 p1
      图区 y0=155 吞掉 2/3 作者块）→ 裁除，之后作为文本段落回归文流；
      短标签（坐标轴刻度/图内标注）保留烘焙
    - 绑定图注带裁除（防「烘焙原文注 + 译文注」双图注）
    - x 向按 mostly-outside 正文段收紧（左栏正文右缘残影）
    """
    r = pymupdf.Rect(fr0)
    for tb in fig_texts:
        try:
            b = pymupdf.Rect(tb["bbox"])
        except Exception:
            continue
        if not b.intersects(r):
            continue
        t = (tb.get("text") or "").strip()
        if len(t) < 30 and "\n" not in t:
            continue                       # 短标签保留烘焙
        if b.y0 <= r.y0 + 4 and b.height < 0.6 * r.height:
            r.y0 = max(r.y0, b.y1 + 1)     # 顶部贴边文字带
        elif b.y1 >= r.y1 - 4 and b.height < 0.6 * r.height:
            r.y1 = min(r.y1, b.y0 - 1)     # 底部贴边文字带
    if cap_r is not None:
        if cap_below:
            r.y1 = min(r.y1, cap_r.y0 - 1)
        else:
            r.y0 = max(r.y0, cap_r.y1 + 1)
    r2 = _tighten_region(r, paras, exclude)
    if r2.is_empty or r2.width < 10 or r2.height < 8:
        return pymupdf.Rect(fr0)
    return r2


def _tighten_region(rect: "pymupdf.Rect", paras: list[dict],
                    exclude: set) -> "pymupdf.Rect":
    """裁剪区收紧（reflow 特有问题：faithful 原位贴回无感，reflow 搬运
    后残影可见）：
    - x 收紧：与区边缘搭界的正文段（侵入深度浅、主体在区外）把区边
      推回到段边缘——源图区 x0 侵入左栏时烘焙进位图的半列英文残影
      （实测 paper3 p1 图区 x0=263 < 左栏 x1=300）
    - 不动 y（上下搭界通常是页眉/图注带，另由绑定图注裁除处理）
    """
    r = pymupdf.Rect(rect)
    for i, p in enumerate(paras):
        if i in exclude:
            continue
        pb = pymupdf.Rect(p["bbox"])
        if not pb.intersects(r):
            continue
        ir = pymupdf.Rect(pb)
        ir.intersect(r)
        if ir.get_area() > 0.4 * max(pb.get_area(), 1e-6):
            continue          # 主体在区内（图内标注块），不动
        # 左侵入：段右缘在区内左半部 → 区左缘推到段右缘
        if pb.x1 <= r.x0 + 0.5 * r.width and pb.x1 > r.x0:
            r.x0 = max(r.x0, pb.x1 + 1)
        # 右侵入
        if pb.x0 >= r.x1 - 0.5 * r.width and pb.x0 < r.x1:
            r.x1 = min(r.x1, pb.x0 - 1)
    return r if not r.is_empty and r.width > 10 else pymupdf.Rect(rect)


def _doc_body_size(page_layouts: list[dict]) -> float:
    """文档级正文字号（众数；预设样式表的视觉延续基准）。"""
    from collections import Counter
    _sz: Counter = Counter()
    for lay in page_layouts:
        for p in lay["paragraphs"]:
            _sz[round(p.get("size") or 10.0, 1)] += max(len(p["text"]), 1)
    return _sz.most_common(1)[0][0] if _sz else 10.0


def _mode_cols(page_layouts: list[dict]) -> str:
    """文档主导栏结构（阅读序分带用；精确栏框由模板统计）。

    返回主导模式字符串 "two"/"one"（历史教训：曾返回 ["two"] 单元素
    列表，调用方 len(...) >= 2 恒 False——全部文档静默走单栏排序，
    跨栏阅读序交错，实测 paper3 双栏文流左右栏段落穿插）。
    """
    modes: dict[str, int] = {}
    for lay in page_layouts:
        m = lay.get("mode", "one")
        modes[m] = modes.get(m, 0) + 1
    if not modes:
        return "one"
    return max(modes, key=modes.get)


def _order_page(entries: list[tuple], page, page_layouts: list[dict]) \
        -> list[Block]:
    """页内阅读序：全宽元素（图/表/公式）分带，带内按栏序（col0→col1）。

    学术语档全宽元素通常在页顶/页底，分带模型覆盖；栏内元素按 y。
    单栏文档全体按 y。
    """
    two_col = _mode_cols(page_layouts) == "two"
    if not two_col:
        return [e[3] for e in sorted(entries, key=lambda e: (e[0], e[1]))]
    page_w = page.rect.width
    mid = page_w / 2
    col_items: dict[int, list] = {0: [], 1: []}
    dividers: list[tuple] = []
    for y, x, hint, b, geo in entries:
        if hint == -1 or (geo is not None and _is_fullwidth(geo, page_w, mid)):
            dividers.append((y, x, b))
        else:
            if hint in (0, 1):
                c = hint
            elif geo is not None:
                c = 0 if (geo.x0 + geo.x1) / 2 < mid else 1
            else:
                c = 0 if x < mid else 1
            col_items[c].append((y, x, b))
    ordered: list[Block] = []
    # 排序键 y 做 2pt 桶量化：源 PDF 同行元素的 y0 常带亚像素抖动
    # （实测 paper3 p1 两位作者 y0 差 0.001pt），不量化时 x 决胜永不
    # 生效——同行右元素以微小 y 优势排到左元素前（3rd 排到 2nd 前）
    qy = lambda v: round(v / 2.0)
    for c in (0, 1):
        col_items[c].sort(key=lambda e: (qy(e[0]), e[1]))
    dividers.sort(key=lambda e: (qy(e[0]), e[1]))
    c0 = c1 = 0
    for dy, _dx, db in dividers:
        while c0 < len(col_items[0]) and col_items[0][c0][0] < dy:
            ordered.append(col_items[0][c0][2])
            c0 += 1
        while c1 < len(col_items[1]) and col_items[1][c1][0] < dy:
            ordered.append(col_items[1][c1][2])
            c1 += 1
        ordered.append(db)
    while c0 < len(col_items[0]):
        ordered.append(col_items[0][c0][2])
        c0 += 1
    while c1 < len(col_items[1]):
        ordered.append(col_items[1][c1][2])
        c1 += 1
    return ordered


def _is_fullwidth(r: "pymupdf.Rect", page_w: float, mid: float) -> bool:
    return r.width > 0.5 * page_w or (r.x0 < mid - 5 and r.x1 > mid + 5)


def _table_html(cells: list[dict], cell_texts: dict, pno: int,
                cell_off: int) -> "str | None":
    """高置信表 → HTML（y 聚类成行、x0 锚点聚类成列、跨锚格 colspan）。

    任一环节异常退 None（调用方落整表位图路径）。格译文按
    cell_texts[(pno, cell_off+local_i)]，缺失保留原文。
    """
    try:
        rects = [(pymupdf.Rect(c["bbox"]), c, k)
                 for k, c in enumerate(cells)]
        xs = sorted(r.x0 for r, _c, _k in rects)
        anchors: list[float] = []
        for x in xs:
            if not anchors or x - anchors[-1] > 4:
                anchors.append(x)
        if len(anchors) < 2:
            return None

        def col_idx(x: float) -> int:
            return min(range(len(anchors)), key=lambda k: abs(anchors[k] - x))

        hs = sorted(r.height for r, _c, _k in rects)
        tol = 0.6 * hs[len(hs) // 2]
        rows: list[list] = []
        for r, c, k in sorted(rects, key=lambda rc: (rc[0].y0, rc[0].x0)):
            if rows and abs(r.y0 - min(rr.y0 for rr, _cc, _kk in rows[-1])) \
                    <= tol:
                rows[-1].append((r, c, k))
            else:
                rows.append([(r, c, k)])
        rows.sort(key=lambda row: min(r.y0 for r, _c, _k in row))
        n_cols = len(anchors)
        out = ["<table>"]
        for row in rows:
            tds = []
            covered: set[int] = set()
            for r, c, k in sorted(row, key=lambda rc: rc[0].x0):
                c0 = col_idx(r.x0)
                c1 = col_idx(r.x1)
                span = max(1, min(n_cols - c0, c1 - c0))
                if any(cc in covered for cc in range(c0, c0 + span)):
                    continue
                covered.update(range(c0, c0 + span))
                dst = cell_texts.get((pno, cell_off + k))
                if dst is None:
                    dst = c.get("text") or ""
                txt = _html_escape(_clean_zh_text(dst)) or "&#160;"
                colspan = f" colspan={span}" if span > 1 else ""
                tds.append(f"<td{colspan}>{txt}</td>")
            if tds:
                out.append("<tr>" + "".join(tds) + "</tr>")
        out.append("</table>")
        return "".join(out)
    except Exception:
        return None


# ---- 任务 3.4.1：预设样式表 ----

def build_reflow_css(body_size: float, dir_css: str, font_css: str) -> str:
    """预设样式表（reflow 不算 fit 因子；初值按任务 3.4.1）。"""
    body = min(max(body_size if body_size > 0 else 10.5, 9.0), 12.0)
    return (font_css
            + f" body{{margin:0;{dir_css}}}"
            + f" p{{font-family:ptbody,serif;font-size:{body:.2f}pt;"
              f"line-height:1.4;margin:0 0 {body * 0.35:.2f}pt 0;"
              f"text-align:justify;}}"
            + " p.ti{text-indent:2em;}"
            + " h1{font-family:pthead,ptbody,serif;font-size:16pt;"
              "line-height:1.3;margin:6pt 0 8pt 0;text-align:center;"
              "font-weight:bold;}"
            + " h2{font-family:pthead,ptbody,serif;font-size:13pt;"
              "line-height:1.3;margin:10pt 0 5pt 0;font-weight:bold;}"
            + " h3{font-family:pthead,ptbody,serif;font-size:11.5pt;"
              "line-height:1.3;margin:8pt 0 4pt 0;font-weight:bold;}"
            + " p.hp{font-family:ptbody,serif;line-height:1.4;"
              "margin:6pt 0 3pt 0;font-weight:bold;}"
            + " p.abs{line-height:1.35;}"
            + " p.caption{font-size:8.5pt;line-height:1.3;"
              "text-align:center;margin:3pt 0 9pt 0;}"
            + " p.ref{font-size:9pt;line-height:1.25;margin:0 0 3pt 0;}"
            + " p.fx{text-align:center;margin:4pt 0 6pt 0;}"
            + " div.fig{page-break-inside:avoid;margin:6pt 0;}"
            + " div.fig p.fc{text-align:center;margin:0;}"
            + " table{width:100%;margin:4pt 0 8pt 0;"
              "border-collapse:collapse;}"
            + " td{font-family:ptbody,serif;font-size:8.5pt;line-height:1.3;"
              "margin:0;padding:2pt 3pt;border:0.5px solid #999;"
              "text-align:left;}"
            + " ol,ul{margin:0 0 6pt 0;padding-left:22pt;}"
            + " li{font-family:ptbody,serif;font-size:10.5pt;line-height:1.4;"
              "margin:0 0 2pt 0;text-align:justify;}")


_KIND_TAG = {"title": "h1", "sec_title": "h2", "subsec_title": "h3"}


def blocks_to_html(blocks: list[Block], col_width: float, cjk: bool) -> str:
    """块流 → 单 Story HTML（顶级块元素序列；样式全在预设样式表）。"""
    parts: list[str] = []
    for b in blocks:
        if b.kind == "list":
            parts.append(b.html_extra)
            continue
        if b.kind == "formula":
            w = min(b.width_pt, col_width)
            parts.append(f'<p class=fx><img src="{b.img_name}" '
                         f'style="width:{w:.1f}pt"></p>')
            continue
        if b.kind in ("figure", "verbatim", "table"):
            cap = (f'<p class=caption>{_html_escape(b.caption)}</p>'
                   if b.caption else "")
            if b.kind == "table" and b.html_extra:
                parts.append(f'<div class=fig>{cap}{b.html_extra}</div>')
            else:
                w = min(b.width_pt, col_width)
                img = (f'<p class=fc><img src="{b.img_name}" '
                       f'style="width:{w:.1f}pt"></p>')
                # 表注在表上方（学术惯例），图注在图下方
                inner = f"{cap}{img}" if b.kind == "table" else \
                    f"{img}{cap}"
                parts.append(f'<div class=fig>{inner}</div>')
            continue
        # 段落
        tag = _KIND_TAG.get(b.kind_cls, "p")
        extra = ""
        if tag == "p":
            if b.kind_cls == "head_plain":
                extra = " class=hp"
            elif b.kind_cls == "abstract":
                extra = " class=abs"
            elif b.kind_cls == "caption":
                extra = " class=caption"
            elif b.kind_cls == "ref_entry":
                extra = " class=ref"
            elif cjk and b.kind_cls == "body":
                extra = ' class="ti"'
        hid = f" {b.html_id}" if b.html_id else ""
        parts.append(f"<{tag}{extra}{hid}>{_html_escape(b.text)}</{tag}>")
    return "".join(parts)


# ---- 任务 3.3：整文档流式写入 ----

def _write_segment(html: str, css: str, template: Template, archive,
                   heading_ids: set) -> tuple[bytes, list[dict]]:
    """单段 Story → PDF bytes + 书签落位 [{id, page, y}]。"""
    story = pymupdf.Story(html=html, user_css=css, archive=archive)
    stream = io.BytesIO()
    writer = pymupdf.DocumentWriter(stream)
    dev = None
    rect_num = 0
    page_no = -1
    filled = pymupdf.Rect(0, 0, 0, 0)
    marks: list[dict] = []
    seen: set = set()
    while 1:
        mediabox, rect = template.frame(rect_num)
        rect_num += 1
        if mediabox:
            if dev is not None:
                writer.end_page()
            page_no += 1
            dev = writer.begin_page(mediabox)
        more, filled = story.place(rect)
        got: list = []
        story.element_positions(lambda p: got.append(p))
        for p in got:
            hid = getattr(p, "id", None)
            if hid in heading_ids and hid not in seen \
                    and getattr(p, "open_close", 0) == 1:
                seen.add(hid)
                r = getattr(p, "rect", None)
                if r is None:
                    y = 0.0
                elif hasattr(r, "y0"):
                    y = r.y0
                else:
                    y = r[1]
                marks.append({"id": hid, "page": page_no, "y": y})
        story.draw(dev, None)
        if not more:
            if dev is not None:
                writer.end_page()
            break
    writer.close()
    stream.seek(0)
    return stream.read(), marks


def render_reflow_document(page_layouts: list[dict], doc,
                           texts_by_page: dict, cell_texts: dict,
                           cross_full: dict, cross_skip: set,
                           formula_pixmaps: dict, typo, font_path: str,
                           lang: str, reflow_cfg, warnings: list[str],
                           log, dcache=None, doc_fp: "str | None" = None) \
        -> bytes:
    """翻译完成的文档模型 → reflow PDF bytes（不触碰原 doc 页面）。
    v0.8.1 S4: dcache/doc_fp 提供时图/表/verbatim 位图裁剪入项目缓存。"""
    template = build_template(page_layouts, doc,
                              columns=getattr(reflow_cfg, "columns",
                                              "auto") or "auto")
    body_size = float(getattr(reflow_cfg, "body_size", 0.0) or 0.0) \
        or _doc_body_size(page_layouts)
    blocks, images, bookmarks = build_document_model(
        page_layouts, doc, texts_by_page, cell_texts, cross_full,
        cross_skip, formula_pixmaps, typo, warnings=warnings,
        dcache=dcache, doc_fp=doc_fp)
    log(f"reflow: template {template.page_w:.0f}x{template.page_h:.0f}pt "
        f"x {len(template.cols)} col(s); {len(blocks)} block(s), "
        f"{len(images)} bitmap(s)")

    archive, font_css = _build_font_archive(
        font_path, typo.heading_path if typo else None)
    for name, png in images.items():
        archive.add(png, name)
    from .langs import is_rtl, lang_info
    css = build_reflow_css(body_size,
                           "direction:rtl;" if is_rtl(lang) else "",
                           font_css)
    col_w = min(x1 - x0 for x0, x1 in template.cols)
    cjk = lang_info(lang).script == "cjk"

    # 章节边界分段（超长文档防内存）：满 SEGMENT_BLOCKS 且块尾是标题时切
    segments: list[list[Block]] = [[]]
    for b in blocks:
        segments[-1].append(b)
        if len(segments[-1]) >= SEGMENT_BLOCKS and b.kind_cls in \
                ("title", "sec_title"):
            segments.append([])
    segments[:] = [s for s in segments if s]

    # 书签锚 id 对齐：blocks 顺序 = bookmarks 顺序
    hi = 0
    for seg in segments:
        for b in seg:
            if b.kind == "para" and b.kind_cls in ("title", "sec_title",
                                                   "subsec_title"):
                b.html_id = f"id=hd{hi}"
                hi += 1
    heading_ids = {f"hd{i}" for i in range(len(bookmarks))}

    all_pdf = pymupdf.open()
    page_base = 0
    toc: list[list] = []
    for seg in segments:
        html = blocks_to_html(seg, col_w, cjk)
        data, marks = _write_segment(html, css, template, archive,
                                     heading_ids)
        seg_doc = pymupdf.open("pdf", data)
        all_pdf.insert_pdf(seg_doc)
        for m in marks:
            idx = int(m["id"][2:])
            if 0 <= idx < len(bookmarks):
                meta = bookmarks[idx]
                toc.append([meta["level"], meta["text"],
                            page_base + m["page"] + 1])
        page_base += len(seg_doc)
        seg_doc.close()

    # 零内容守卫（实测：无可译块的纯扫描页文档 0 块 → 0 页 PDF，
    # tobytes 直接 ValueError "cannot save with zero pages"）——落一页
    # 空白页保输出合法，并告警提示内容缺失
    if len(all_pdf) == 0:
        all_pdf.new_page(width=template.page_w, height=template.page_h)
        warnings.append(
            "reflow: no translatable content (all pages scanned/empty?) "
            "- output is a blank page")

    # 页码（页脚居中，首页封面惯例跳过）+ PDF 书签
    n_out = len(all_pdf)
    for i in range(1, n_out):
        pg = all_pdf[i]
        pg.insert_text(
            pymupdf.Point(pg.rect.width / 2 - 4, pg.rect.height - 24),
            str(i + 1), fontsize=9, fontname="helv",
            color=(0.35, 0.35, 0.35))
    if toc:
        # set_toc 层级规约：首项必须 level 1、逐项不得跳级（文档无
        # 大标题块时首个节标题顶到 level 1）
        norm: list[list] = []
        prev_lvl = 0
        for lvl, txt, pg in toc:
            lvl = 1 if not norm else min(lvl, prev_lvl + 1)
            norm.append([lvl, txt, pg])
            prev_lvl = lvl
        all_pdf.set_toc(norm)
    log(f"reflow: wrote {n_out} page(s), {len(toc)} bookmark(s)")
    data = all_pdf.tobytes(deflate=True, garbage=3)
    all_pdf.close()
    return data
