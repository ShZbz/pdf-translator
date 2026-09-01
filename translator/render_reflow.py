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

# 单 Story 块数软上限默认值（可被 reflow.segment_blocks 配置覆盖：
# 超过则在章节边界分段写入，防超长文档内存膨胀）
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
        # v0.8.4 退化守卫：栏归属错标/极端布局的量化统计可能产出
        # 极窄（统计挤压）或空（Rect 归一化后 x1≤x0）栏框——空框喂
        # story.write 会一路空转到 _MAX_FRAMES 保险丝整个任务报错。
        # 窄于 24pt 的栏不可读也无意义，剔除；两栏全退化退单栏。
        cols = [c for c in cols if c[1] - c[0] >= 24.0]
        if not cols:
            cols = [(ml, page_w - mr)]
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
    # ---- v0.8.3 ②：链接重映射（reflow 下源页链接全部丢失的修复）----
    # links: 源页归属到本块的链接记录（build_document_model 几何归属，
    # render 阶段完成译文内定位与 href 构造；字段见 _collect_page_links）
    links: list = field(default_factory=list)
    anchor_id: str = ""           # 内部跳转目标锚 id（ltN；标题块复用 hd 锚）


_ORDERED_MARKER_RE = re.compile(
    r"^\s*[\(\[（【]?(?:\d{1,2}|[ivxIVX]{1,4}|[a-hA-H])[\)\]）】]?[.、：\s]")

# ---- v0.8.3 ②：链接重映射（reflow 与 faithful 的最大单项体验差距收口）----
# 实测探针（本文件写入路径 + pymupdf 1.28.2）：
# - Story 手动 place/draw 循环不物化 <a> 链接（0 注释）；
#   story.write(writer, rectfn, positionfn) + 位置后处理才产链接；
# - <a href="URI"> → LINK_URI；<a href="#锚"> → LINK_GOTO 指向 id=锚
#   元素 rect 左上角（Story.add_pdf_links 语义，探针实测命中）；
# - insert_pdf 携带链接注释跨段合并（GoTo 页码重映射正确）；
#   save(garbage=4, deflate) 存活。
# 上游 add_pdf_links 对「#锚 无对应 id」直接 raise——本文件自实现
# 可控版本（缺失目标跳过计数，绝不阻塞出片）。

def _collect_page_links(page, n_layout_pages: int) -> list[dict]:
    """源页链接 → 归一化记录（几何归属在调用方做）。

    记录字段：rect（源矩形）/ href_kind（"uri"|"goto"）/ uri / dest
    （goto 的 (目标页号, Point)）/ src（矩形下源文本，去空白归一化）/
    owner（段落索引，调用方回填）/ field（"text"|"caption"，回填）。
    仅收录 URI 与已解析的 GOTO/NAMED；LAUNCH/GOTOR/空 URI/越界目标
    不进（调用方按 unsupported 计数）。
    """
    out: list[dict] = []
    try:
        raw = page.get_links()
    except Exception:
        return out
    for l in raw:
        # v0.8.3: 单条防御——奇形 PDF 的链接字段缺 to/from 或类型意外时
        # 跳过该条，绝不让一条坏链接炸掉整个 reflow 渲染
        try:
            rec = _normalize_link(l, n_layout_pages)
        except Exception:
            continue
        if rec is None:
            continue
        try:
            rec["src"] = "".join(
                ch for ch in (page.get_text("text", clip=rec["rect"]) or "")
                if not ch.isspace())
        except Exception:
            rec["src"] = ""
        rec["owner"] = None
        rec["field"] = "text"
        out.append(rec)
    return out


def _normalize_link(l: dict, n_layout_pages: int) -> "dict | None":
    """get_links 单条 → 归一化记录（URI / 已解析 GOTO·NAMED；其余 None）。

    记录自带 rect（源矩形）。
    """
    r = l.get("from")
    if r is None or getattr(r, "is_empty", True):
        return None
    k = l.get("kind")
    if k == pymupdf.LINK_URI:
        uri = (l.get("uri") or "").strip()
        if uri:
            return {"href_kind": "uri", "uri": uri,
                    "rect": pymupdf.Rect(r)}
        return None
    if k in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED):
        tp, to = l.get("page"), l.get("to")
        if tp is not None and 0 <= int(tp) < n_layout_pages \
                and to is not None:
            return {"href_kind": "goto",
                    "dest": (int(tp), pymupdf.Point(to.x, to.y)),
                    "rect": pymupdf.Rect(r)}
    return None


def _norm_find(text: str, pat: str, start: int = 0) -> "tuple[int, int] | None":
    """去空白归一化子串定位 → 原文字符区间（译文内链接文本定位）。

    链接文本（"[22]" 引文标记 / URL / DOI）在译文里保形，但重排后
    空白/换行位置漂移——按去空白序列匹配；引文/公式编号的括号常被
    译者写成全角（"(3)"→"（3）"），折叠成半角再比（实测真译 56/64 →
    恢复率受此影响）。start 为原文侧搜索起点（多链接单调游标，同段
    重复引文序匹配）。
    """
    if not pat or not text:
        return None
    fold = str.maketrans("（）［］｛｝", "()[]{}")
    idx: list[int] = []
    buf: list[str] = []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        buf.append(ch.translate(fold))
        idx.append(i)
    t_norm = "".join(buf)
    p_norm = "".join(ch for ch in pat if not ch.isspace()).translate(fold)
    m = t_norm.find(p_norm, sum(1 for c in text[:start] if not c.isspace()))
    if m < 0 and start > 0:                 # 游标后未见：回退全文找首现
        m = t_norm.find(p_norm)
    if m < 0 or m + len(p_norm) > len(idx):
        return None
    return idx[m], idx[m + len(p_norm) - 1] + 1


def _attr_escape(s: str) -> str:
    """href 属性值转义（_html_escape 不转引号，属性上下文必须处理）。"""
    return (s.replace("&", "&amp;").replace('"', "&quot;")
             .replace("<", "&lt;").replace(">", "&gt;"))


def _link_wrap(text: str, links: list, field: str) -> str:
    """按已定位 span 把链接文本包 <a href>（blocks_to_html 段落/图注出口）。

    span 未定位（None）的链接不包（静默丢该链接，计数在渲染汇总）；
    区间按起点排序 + 重叠守卫（理论不重叠，防御性跳过）。
    """
    ls = [l for l in links
          if l.get("field", "text") == field and l.get("span")]
    if not ls:
        return _html_escape(text)
    out, pos = "", 0
    for l in sorted(ls, key=lambda x: x["span"][0]):
        s, e = l["span"]
        if s < pos:
            continue
        out += (_html_escape(text[pos:s])
                + f'<a href="{_attr_escape(l["href"])}">'
                + _html_escape(text[s:e]) + "</a>")
        pos = e
    return out + _html_escape(text[pos:])


# 伪代码碎片形态（Algorithm 框判定漏网块——faithful 原位无感，reflow
# 拆位图后成散落文本行，实测 paper3 p4 'action = {ak, Lk}' 等）
_PSEUDO_RE = re.compile(
    r"[{}⟨⟩]|←|∼|~|⌊|⌋|\b(?:for|while|if|else|end|return|break|"
    r"sample|Input|Output|Require|Ensure|Initialize)\b")


def _pseudo_clusters(paras: list[dict],
                     formulas: "list[dict] | None" = None
                     ) -> "list[tuple[set, set, pymupdf.Rect]]":
    """Algorithm 框聚类：每个 verbatim 种子迭代吸收「相交且形似伪代码」
    的碎片段（faithful 原位无感，reflow 拆位图后散落文本行——实测
    paper3 p4 'action = {ak, Lk}' 等）。返回 [(段落索引集, 公式索引集,
    联合框)]。
    v0.8.5 终检：框内数学行（'sample nk+1 ∼p(...)' 这类）被
    collect_display_formulas 抢先判成独立显示公式——伪代码框被拆成
    「头块位图+公式条+尾块位图」散落文流（实测 paper3 p5 Algorithm 2
    行 4-6）。补几何吸收：与联合框相交的公式条并入框位图，吸收后
    联合框长高再回吸原不相交的尾块（'Break out of for loop'）。"""
    clusters: list[tuple[set, set, pymupdf.Rect]] = []
    used: set = set()
    for s, p in enumerate(paras):
        if not p.get("is_verbatim") or s in used:
            continue
        idx = {s}
        fidx: set = set()
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
            for fi, f in enumerate(formulas or []):
                if fi in fidx:
                    continue
                r = pymupdf.Rect(f["bbox"])
                if u.intersects(r):
                    fidx.add(fi)
                    u |= r
                    changed = True
        used |= idx
        clusters.append((idx, fidx, u))
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


def _group_lists(blocks: list[Block]) -> "tuple[list[Block], int]":
    """连续 list-item 段合成语义列表块（任务 1.5 在 reflow 落地）。

    并段残留的句中项目符号（源 PDF 贡献列表多符号并入一块）拆成多个
    <li>——悬挂缩进与符号由引擎生成。
    v0.8.3 ②: 返回值追加被吸收段落携带的链接数（<li> 文本与段落文本
    剥标记后不一致，span 定位不迁移——按已知限制计数丢弃）。
    """
    out: list[Block] = []
    n_links = 0
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
                n_links += len(bj.links)
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
    return out, n_links


def build_document_model(page_layouts: list[dict], doc,
                         texts_by_page: dict, cell_texts: dict,
                         cross_full: dict, cross_skip: set,
                         formula_pixmaps: dict, typo,
                         warnings: "list | None" = None,
                         dcache=None, doc_fp: "str | None" = None) \
        -> tuple[list[Block], dict[str, bytes], list[dict], dict]:
    """layout 全输出 → 跨页阅读序统一块流。

    返回 (blocks, images{name: png_bytes}, bookmarks[{level, text}],
    link_stats)；书签页码由写入循环回填。公式位图复用 pipeline 预裁 PNG
    （与 faithful 同源，reflow 不重复裁剪）；图区/表格/verbatim 位图此处
    从原 doc 裁剪（reflow 不 redact，原像素完整）。
    v0.8.1 S4: dcache/doc_fp 提供时全部 300dpi 裁图入项目位图缓存
    （(指纹,页,区域,dpi) 内容寻址——重跑/调开关重渲染场景免重裁）。
    v0.8.3 ②: 源页链接按几何归属挂块（Block.links）——链接矩形与段落
    bbox 最大相交者为归属；跨页合并 B 半句的链接改挂 A 段块（整段译文
    在 A）；内部跳转目标（引文→文献条目/图注）按 dest 点位在区域登记
    表解析。link_stats: {total, unowned, no_target, grouped}。
    """
    warn = warnings if warnings is not None else []
    images: dict[str, bytes] = {}
    bm_by_block: dict[int, dict] = {}
    link_stats = {"total": 0, "unowned": 0, "no_target": 0, "grouped": 0}
    para_blocks: dict[tuple[int, int], Block] = {}   # (pno, para_i) → 块
    regions: list[tuple[int, pymupdf.Rect, Block]] = []   # 跳转目标解析用

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

        # --- v0.8.3 ②：源页链接收集 + 段落几何归属（阅读序） ---
        plinks = _collect_page_links(page, len(page_layouts))
        link_stats["total"] += len(plinks)
        owned: dict[int, list] = {}
        for lk in plinks:
            best_i, best_a = None, 0.0
            for i, p in enumerate(paras):
                pb = pymupdf.Rect(p["bbox"])
                ir = pymupdf.Rect(lk["rect"])
                ir.intersect(pb)
                a = ir.get_area()
                if a > best_a:
                    best_a, best_i = a, i
            if best_i is not None and (pno, best_i) in cross_skip:
                # 跨页合并 B 半句：整段译文在 A 段块（cross_full 的 key）
                cand = [pi for (pa, pi) in cross_full if pa == pno - 1]
                ab = para_blocks.get((pno - 1, max(cand))) if cand else None
                if ab is not None:
                    ab.links.append(lk)      # A 块已在早前页建好
                    lk["owner"] = -1        # 已归属标记（计数豁免）
                    best_i = None
            if best_i is not None:
                lk["owner"] = best_i
                owned.setdefault(best_i, []).append(lk)
        plinks.sort(key=lambda l: (l["rect"].y0, l["rect"].x0))
        for ls in owned.values():
            ls.sort(key=lambda l: (l["rect"].y0, l["rect"].x0))

        def _take_links(i: int, field: str = "text") -> list:
            """取出归属段落 i 的链接（标记 field：text/caption）。"""
            ls = owned.pop(i, [])
            for lk in ls:
                lk["field"] = field
            return ls

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
        v_absorbed_f: set = set()          # 已烘进框位图的公式条（不再独立出块）
        for idx, fidx, u in _pseudo_clusters(paras, formulas):
            rep = min(idx)
            v_rep[rep] = u
            v_absorbed |= (idx - {rep})
            v_absorbed_f |= fidx

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
                regions.append((pno, pymupdf.Rect(grow), b))
                continue
            style = typo.resolve(p, _doc_body_size(page_layouts))
            kind_cls = style.kind
            if not p.get("is_heading") and not p.get("is_caption") \
                    and not p.get("is_ref") \
                    and bool(p.get("is_list_item")):
                kind_cls = "list_item"
            b = Block(kind="para", text=txt, kind_cls=kind_cls,
                      src_text=p["text"])
            b.links = _take_links(i)
            para_blocks[(pno, i)] = b
            regions.append((pno, r, b))
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
            if fi in v_absorbed_f:
                continue    # 已随伪代码框位图烘焙（union 裁剪含其像素）
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
            regions.append((pno, fr, b))

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
                b.links = _take_links(ci, "caption")
                regions.append((pno, cap_r, b))   # 图注点位 → 图块锚
            entries.append((fr.y0, fr.x0, -1, b, fr))
            baked_rects.append(pymupdf.Rect(fr))
            regions.append((pno, fr, b))

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
                b.links = _take_links(ci, "caption")
                regions.append((pno, pymupdf.Rect(paras[ci]["bbox"]), b))
            entries.append((tr.y0, tr.x0, -1, b, tr))
            cell_off += len(cells)
            regions.append((pno, pymupdf.Rect(tr0), b))

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
            # v0.8.3 ②：无段落归属的链接（作者 ORCID/邮箱等图区带内链接）
            # 按与并块带的相交归属兜底——带文本即原文，src 直接可匹配
            rest = [lk for lk in plinks
                    if lk["owner"] is None and lk["field"] == "text"
                    and pymupdf.Rect(lk["rect"]).intersects(m["r"])]
            b.links = rest
            for lk in rest:
                lk["owner"] = -1           # 标记已归属（band 兜底）
            # geo=None：自然语言带按列项参与栏序（hint 已按 x 中点定栏）。
            # 传 rect 会让 _is_fullwidth 把跨几何中线的带升级成全宽分带
            # ——作者区横跨中线的带会把同 y 左栏第一作者段落挤到后面
            # （实测 paper3 p1 作者序 3rd/2nd/1st 错乱根因）
            entries.append((m["r"].y0, m["r"].x0, m["hint"], b, None))
            regions.append((pno, m["r"], b))
        for r, t, hint in frag_bands:
            grow = pymupdf.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2)
            name = _crop(page, grow, f"ft{pno}_{len(baked_rects)}")
            b = Block(kind="verbatim", img_name=name,
                      width_pt=grow.width)
            baked_rects.append(pymupdf.Rect(grow))
            entries.append((r.y0, r.x0, hint, b, r))
            regions.append((pno, pymupdf.Rect(grow), b))

        blocks.extend(_order_page(entries, page, page_layouts))
        # 无文本块可挂（位图区/verbatim 段）的链接计数——band 兜底已
        # 打过 owner=-1 标记，此处剩的是真丢弃
        link_stats["unowned"] += sum(len(v) for v in owned.values()) \
                                 + sum(1 for lk in plinks
                                       if lk["owner"] is None)

    # v0.8.3 ②：内部跳转目标解析（dest 点位 → 目标块引用）。
    # 优先包含 dest 点的最小区域（段/图/表/公式/图注登记表）；无包含时
    # 取 dest 下方最近区域。包含判定带 2pt 容差——实测 named dest 的
    # to 点常落在段落 bbox 边界外 1e-5pt（x=54.0 vs bbox.x0=54.000011），
    # 纯浮点边界差不容差会误判无目标。
    def _resolve_target(dest: tuple) -> "Block | None":
        tp, to = dest
        cands = [(rr, b) for (pp, rr, b) in regions if pp == tp]
        best = None
        for rr, b in cands:
            grown = pymupdf.Rect(rr.x0 - 2, rr.y0 - 2, rr.x1 + 2, rr.y1 + 2)
            if grown.contains(to) and (best is None
                                       or rr.get_area() < best[0].get_area()):
                best = (rr, b)
        if best is not None:
            return best[1]
        below = [(rr.y0 - to.y, abs((rr.x0 + rr.x1) / 2 - to.x), rr, b)
                 for rr, b in cands if rr.y0 >= to.y - 2]
        if below:
            below.sort(key=lambda t: (t[0], t[1]))
            return below[0][3]
        return None

    for b in blocks:
        for lk in b.links:
            if lk.get("href_kind") == "goto":
                lk["target"] = _resolve_target(lk["dest"])
                if lk["target"] is None:
                    link_stats["no_target"] += 1

    # 书签按最终文档流序推导（跨页 + 页内分带序）
    bookmarks = [bm_by_block[id(b)] for b in blocks if id(b) in bm_by_block]
    blocks, n_grouped_links = _group_lists(blocks)
    link_stats["grouped"] += n_grouped_links
    return blocks, images, bookmarks, link_stats


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
            # v0.8.2: 文献条目悬挂缩进（探针实测 padding-left+负 text-indent
            # 在 Story 流式写入精确生效 14pt）——续行右移对齐编号后正文，
            # 多行条目不再呈方块段（v0.8.0 已知限制「文献无悬挂缩进」收尾）
            + " p.ref{font-size:9pt;line-height:1.25;margin:0 0 3pt 0;"
              "padding-left:14pt;text-indent:-14pt;}"
            + " p.fx{text-align:center;margin:4pt 0 6pt 0;}"
            + " div.fig{page-break-inside:avoid;margin:6pt 0;}"
            + " div.fig p.fc{text-align:center;margin:0;}"
            + " table{width:100%;margin:4pt 0 8pt 0;"
              "border-collapse:collapse;}"
            + " td{font-family:ptbody,serif;font-size:8.5pt;line-height:1.3;"
              "margin:0;padding:2pt 3pt;border:0.5px solid #999;"
              "text-align:left;}"
            + " ol,ul{margin:0 0 6pt 0;padding-left:22pt;}"
            # v0.8.4: li 字号随 body_size（沿用原文档正文字号）——旧版硬编码
            # 10.5pt，小字号文档（body 9pt）下列表项比正文大 1.5pt
            + f" li{{font-family:ptbody,serif;font-size:{body:.2f}pt;"
              "line-height:1.4;"
              "margin:0 0 2pt 0;text-align:justify;}")


_KIND_TAG = {"title": "h1", "sec_title": "h2", "subsec_title": "h3"}


def blocks_to_html(blocks: list[Block], col_width: float, cjk: bool) -> str:
    """块流 → 单 Story HTML（顶级块元素序列；样式全在预设样式表）。

    v0.8.3 ②: 带链接块的文本按已定位 span 包 <a href>（URI 直通 /
    内部跳转 #锚）；跳转目标块元素携带 id（ltN，标题块复用书签 hd 锚）
    ——story.write 的位置回调会记录锚点，出墨后据此重插链接注释。
    """
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
            cap = (f'<p class=caption>'
                   f'{_link_wrap(b.caption, b.links, "caption")}</p>'
                   if b.caption else "")
            if b.kind == "table" and b.html_extra:
                parts.append(f'<div class=fig{_id_attr(b)}>{cap}'
                             f'{b.html_extra}</div>')
            else:
                w = min(b.width_pt, col_width)
                img = (f'<p class=fc><img src="{b.img_name}" '
                       f'style="width:{w:.1f}pt"></p>')
                # 表注在表上方（学术惯例），图注在图下方
                inner = f"{cap}{img}" if b.kind == "table" else \
                    f"{img}{cap}"
                parts.append(f'<div class=fig{_id_attr(b)}>{inner}</div>')
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
        aid = "" if hid else _id_attr(b)
        parts.append(f"<{tag}{extra}{hid}{aid}>"
                     f"{_link_wrap(b.text, b.links, 'text')}</{tag}>")
    return "".join(parts)


def _id_attr(b: Block) -> str:
    """跳转目标锚属性（空串=无）。标题块的书签 html_id 优先（复用锚）。"""
    if b.html_id:
        return f" {b.html_id}"
    return f" id={b.anchor_id}" if b.anchor_id else ""


# ---- 任务 3.3：整文档流式写入 ----

# 写入循环保险丝：正常文档远达不到（模板框无限供给自动断页），
# 防御性兜底（旧版手动循环同款无界风险）
_MAX_FRAMES = 20000


def _write_segment(html: str, css: str, template: Template, archive,
                   heading_ids: set) -> "tuple[bytes, list]":
    """单段 Story → PDF bytes + element_positions（书签/链接定位源）。

    v0.8.3 ②: 手动 place/draw 循环换 Story.write——与旧循环同构
    （mediabox 真值开新页、frame 无限供给、not more 终止），但
    positionfn 收集的 ElementPosition 带 href 字段（<a> 元素才有），
    出墨后 _add_story_links 据此物化链接注释（旧循环 0 链接，探针实测）。
    书签 marks（hd 锚 → 页号+y）由调用方从 positions 提取（语义与
    旧循环一致：首 open 事件）。
    """
    story = pymupdf.Story(html=html, user_css=css, archive=archive)
    stream = io.BytesIO()
    writer = pymupdf.DocumentWriter(stream)
    positions: list = []

    def _rectfn(rect_num: int, filled):
        if rect_num > _MAX_FRAMES:
            raise RuntimeError("reflow story exceeded frame fuse")
        mediabox, rect = template.frame(rect_num)
        return mediabox, rect, None

    story.write(writer, _rectfn, positionfn=positions.append)
    writer.close()
    stream.seek(0)
    return stream.read(), positions


def _extract_bookmark_marks(positions: list, heading_ids: set) -> list[dict]:
    """positions → 书签落位 [{id, page, y}]（首 open 事件，同旧循环）。"""
    marks: list[dict] = []
    seen: set = set()
    for p in positions:
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
            marks.append({"id": hid, "page": p.page_num - 1, "y": y})
    return marks


def _add_story_links(pdf_doc, positions: list) -> "set[str]":
    """ElementPosition → 输出 PDF 链接注释（v0.8.3 ②：reflow 链接重插）。

    Story.add_pdf_links 的可控版：语义相同（<a href="URI"> → LINK_URI；
    <a href="#锚"> → LINK_GOTO 指向 id=锚 元素 rect 左上角），差异：
    - 内部锚缺失（目标块被分段/列表吸收/文本未定位）→ 跳过不 raise
      ——上游直接 RuntimeError，任一坏锚会废掉全部链接；
    - 逐条 try/except：单条插入失败只丢该条，绝不阻塞出片；
    - 同一 <a> 的行内碎片合并为单条注释——碎片 rect 可为零宽（探针
      实测首碎片 x0==x1），逐条插会产生不可点空链接。合并仅限「同页
      同 href 同行且水平间隙 ≤12pt」：普通文本不产 positions，同段两处
      独立引文在流里相邻，无条件并会把两引文之间的正文全圈进链接；
      跨行换行的 URL 碎片各成一条注释（PDF 惯例，语义不变）。
    返回 (成功落锚的 href 集, 实插注释数)——渲染层据此核算恢复率。
    """
    id_to_pos: dict = {}
    for p in positions:
        if (getattr(p, "open_close", 0) & 1) and getattr(p, "id", None) \
                and p.id not in id_to_pos:
            id_to_pos[p.id] = p
    runs: list = []       # [page_num, href, rect]
    for pf in positions:
        href = getattr(pf, "href", None)
        if not href or not (getattr(pf, "open_close", 0) & 1):
            continue
        pg = getattr(pf, "page_num", 0)
        r = pymupdf.Rect(pf.rect)
        if runs:
            pg0, h0, r0 = runs[-1]
            if pg0 == pg and h0 == href:
                same_line = (min(r0.y1, r.y1) - max(r0.y0, r.y0)
                             > 0.5 * min(r0.height, r.height, 1.0))
                if same_line and -2 <= r.x0 - r0.x1 <= 12:
                    runs[-1][2] = r0 | r
                    continue
        runs.append([pg, href, r])
    placed: set = set()
    n_ann = 0
    for pg, href, rect in runs:
        if rect.is_empty or rect.get_area() <= 1.0:
            continue                       # 独立零宽碎片：不可点，丢弃
        try:
            link = {"from": rect}
            if href.startswith("#"):
                tgt = id_to_pos.get(href[1:])
                if tgt is None:
                    continue               # 坏锚：跳过（上游会 raise）
                x0, y0 = tgt.rect[0], tgt.rect[1]
                link["kind"] = pymupdf.LINK_GOTO
                link["to"] = pymupdf.Point(x0, y0)
                link["page"] = tgt.page_num - 1
            else:
                link["kind"] = pymupdf.LINK_URI
                link["uri"] = href
            pdf_doc[pg - 1].insert_link(link)
            placed.add(href[1:] if href.startswith("#") else href)
            n_ann += 1
        except Exception:
            continue
    return placed, n_ann


def _split_segments(blocks: list[Block], limit: int) -> list["list[Block]"]:
    """章节边界分段（超长文档防内存）：满 limit 且块尾是标题时切。

    limit 来自 reflow.segment_blocks 配置（v0.8.3 修复：旧版配置键
    存在但渲染层读的是模块常量 SEGMENT_BLOCKS，配置改了不生效）。
    """
    segments: list[list[Block]] = [[]]
    for b in blocks:
        segments[-1].append(b)
        if len(segments[-1]) >= limit and b.kind_cls in \
                ("title", "sec_title"):
            segments.append([])
    return [s for s in segments if s]


def render_reflow_document(page_layouts: list[dict], doc,
                           texts_by_page: dict, cell_texts: dict,
                           cross_full: dict, cross_skip: set,
                           formula_pixmaps: dict, typo, font_path: str,
                           lang: str, reflow_cfg, warnings: list[str],
                           log, dcache=None, doc_fp: "str | None" = None) \
        -> bytes:
    """翻译完成的文档模型 → reflow PDF bytes（不触碰原 doc 页面）。
    v0.8.1 S4: dcache/doc_fp 提供时图/表/verbatim 位图裁剪入项目缓存。
    v0.8.4 修复：typo=None（features.preserve_formatting: false 或
    Typography 初始化失败）时 reflow 崩溃——build_document_model 的
    段落样式分类离不开 Typography，这里兜底构造默认实例（Font 懒加载，
    构造只解析字体路径串，零额外成本）。"""
    if typo is None:
        from .typography import Typography
        typo = Typography({}, lang=lang)
    template = build_template(page_layouts, doc,
                              columns=getattr(reflow_cfg, "columns",
                                              "auto") or "auto")
    body_size = float(getattr(reflow_cfg, "body_size", 0.0) or 0.0) \
        or _doc_body_size(page_layouts)
    blocks, images, bookmarks, link_stats = build_document_model(
        page_layouts, doc, texts_by_page, cell_texts, cross_full,
        cross_skip, formula_pixmaps, typo, warnings=warnings,
        dcache=dcache, doc_fp=doc_fp)
    log(f"reflow: template {template.page_w:.0f}x{template.page_h:.0f}pt "
        f"x {len(template.cols)} col(s); {len(blocks)} block(s), "
        f"{len(images)} bitmap(s), {link_stats['total']} source link(s)")

    archive, font_css = _build_font_archive(
        font_path, typo.heading_path if typo else None)
    # v0.8.5 审查修复：无字体环境（拉丁目标且候选链全空）时
    # _build_font_archive 返回 (None, "")——有位图要进 Archive 的文档
    # 在 archive.add 直接 AttributeError（实证无字体容器 reflow×图必崩）。
    # 建空 Archive 承载位图，CSS 落 serif 兜底（字体由引擎内置衬线顶上）
    if archive is None and images:
        archive = pymupdf.Archive()
    for name, png in images.items():
        archive.add(png, name)
    from .langs import is_rtl, lang_info
    css = build_reflow_css(body_size,
                           "direction:rtl;" if is_rtl(lang) else "",
                           font_css)
    col_w = min(x1 - x0 for x0, x1 in template.cols)
    cjk = lang_info(lang).script == "cjk"

    # 章节边界分段（超长文档防内存）：满 segment_blocks 且块尾是标题时切
    seg_limit = max(50, int(getattr(reflow_cfg, "segment_blocks", 500)
                            or 500))
    segments = _split_segments(blocks, seg_limit)

    # 书签锚 id 对齐：blocks 顺序 = bookmarks 顺序
    hi = 0
    for seg in segments:
        for b in seg:
            if b.kind == "para" and b.kind_cls in ("title", "sec_title",
                                                   "subsec_title"):
                b.html_id = f"id=hd{hi}"
                hi += 1
    heading_ids = {f"hd{i}" for i in range(len(bookmarks))}

    # ---- v0.8.3 ②：链接 href 构造 + 译文内定位（失败退化不阻塞出片）----
    # 跳转目标锚分配（标题块复用 hd 锚）→ href 定型 → src 文本 span 定位
    n_link_ok = 0
    try:
        targets = set()
        for b in blocks:
            for lk in b.links:
                t = lk.get("target")
                if lk.get("href_kind") == "goto" and t is not None:
                    targets.add(id(t))
        n_anchor = 0
        for b in blocks:
            if id(b) in targets and not b.html_id:
                b.anchor_id = f"lt{n_anchor}"
                n_anchor += 1
        for b in blocks:
            cursors = {"text": 0, "caption": 0}
            for lk in b.links:
                field = lk.get("field", "text")
                text = b.caption if field == "caption" else b.text
                span = _norm_find(text, lk.get("src") or "",
                                  cursors.get(field, 0))
                if lk.get("href_kind") == "uri":
                    lk["href"] = lk.get("uri") or ""
                    # src 是 clip 提取（rect 边界偏一点就抓残串）；URL 在
                    # 译文里保形——src 定位失败时直接用 URI 文本兜底
                    if not span and lk["href"]:
                        span = _norm_find(text, lk["href"],
                                          cursors.get(field, 0))
                elif lk.get("href_kind") == "goto":
                    t = lk.get("target")
                    aid = (t.html_id or t.anchor_id) if t is not None else ""
                    lk["href"] = f"#{aid}" if aid else ""
                else:
                    lk["href"] = ""
                if span and lk["href"]:
                    lk["span"] = span
                    cursors[field] = span[1]
                    n_link_ok += 1
                else:
                    lk["span"] = None
    except Exception as e:
        n_link_ok = 0
        for b in blocks:
            b.links = []
        warnings.append(f"reflow links: prepare failed ({e}); "
                        f"links disabled for this output")

    all_pdf = pymupdf.open()
    page_base = 0
    toc: list[list] = []
    all_positions: list = []
    for seg in segments:
        html = blocks_to_html(seg, col_w, cjk)
        data, positions = _write_segment(html, css, template, archive,
                                         heading_ids)
        seg_doc = pymupdf.open("pdf", data)
        all_pdf.insert_pdf(seg_doc)
        marks = _extract_bookmark_marks(positions, heading_ids)
        for m in marks:
            idx = int(m["id"][2:])
            if 0 <= idx < len(bookmarks):
                meta = bookmarks[idx]
                toc.append([meta["level"], meta["text"],
                            page_base + m["page"] + 1])
        # 链接定位的页号对齐到全文档坐标（段内 page_num 是 1-based 段内值）
        for p in positions:
            p.page_num += page_base
        all_positions.extend(positions)
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
    # ---- v0.8.3 ②：链接注释物化（页码/书签之后，坐标系一致）----
    n_restored = 0
    try:
        placed, n_ann = _add_story_links(all_pdf, all_positions)
        # 恢复核算：href 目标锚实际落位（URI 恒可插，goto 需锚在 placed）
        n_restored = sum(
            1 for b in blocks for lk in b.links
            if lk.get("span")
            and (not lk["href"].startswith("#") or lk["href"][1:] in placed))
        n_src = link_stats["total"]
        n_drop = n_src - n_restored
        log(f"reflow links: {n_restored}/{n_src} restored"
            f" ({n_ann} annotation(s))"
            + (f", {n_drop} dropped"
               f" (bitmap/list/no-text-match: {link_stats['unowned']}"
               f" no-target: {link_stats['no_target']}"
               f" grouped: {link_stats['grouped']})" if n_drop else ""))
        if n_drop and n_src:
            warnings.append(
                f"reflow links: {n_restored}/{n_src} restored, {n_drop} "
                f"dropped (in-bitmap/list-item/no-text-match/no-target)")
    except Exception as e:
        warnings.append(f"reflow links: insertion failed ({e}); "
                        f"output kept without links")
    log(f"reflow: wrote {n_out} page(s), {len(toc)} bookmark(s)")
    data = all_pdf.tobytes(deflate=True, garbage=3)
    all_pdf.close()
    return data
