"""布局分析 v2：双栏检测+跨栏重切 / 段落合并 / 表格 / display公式区 / 图内文字排除。

对应 SCHEME.md §4 与 D3/D6 决策。
"""
from __future__ import annotations

import re
from collections import Counter

import pymupdf

# 数学符号字体（真公式成分）：CMMI=斜体变量 CMSY=符号 CMEX=大算符
# ⚠️ 不含 CMR/CMBX/CMTT —— LaTeX 正文体也是 Computer Modern，加了会吞正文（实测教训）
# ⚠️ Times-Italic 是"弱信号"：Word/IEEE 排版用它排公式变量，但也用来排
#    参考文献期刊名（IEEE Sens. J. 整段斜体）——单独出现不构成种子，
#    必须与硬数学字体（MTSY/MTMI/CMMI 等）共现才计入（v0.2.2 实测 p4 整页
#    文献被链成假公式区的根因）
_MATH_FONT_RE = re.compile(
    r"(CMMI|CMSY|CMEX|MSBM|EUFM|CambriaMath|MTSY|MTMI|MTEX|RMTMI)", re.I)
_MATH_WEAK_FONT_RE = re.compile(r"(Times-Italic)", re.I)
# 图注/表注开头
_CAPTION_RE = re.compile(r"^(Fig(ure)?\.?\s|Table\s)", re.I)
# 页眉页脚判定阈值（D7: y < 8% 或 > 92% 且短行）
_HEADER_FOOTER_RATIO = 0.08   # 页眉阈值（顶部 8%）
# v0.2.3: 页脚阈值从 8% 收紧到 6%——0.08 时正文末行被误判页脚（不译不删，
# 输出残留英文）：实测 paper3 p6 '• Case 3: Two (or more)...' y0=730.3 vs
# 旧阈值 728.6，差 1.7pt 漏翻译。真页脚位置：paper3 ©IEEE y0=749.4（5.4%）、
# 2.pdf 版权行 y0=762.6（3.7%），6% 阈值均仍覆盖。
_FOOTER_RATIO = 0.06
_HF_MAX_CHARS = 80
# P2 结构包：段落合并硬上限（SCHEME §4: 300-500 字符）
_MERGE_CAP_CHARS = 500
# 小块（标题/作者/单位级）严格字号阈值：字号差 >0.25pt 即不并
_SMALL_BLOCK_CHARS = 120
_REFS_HEADING_RE = re.compile(r"REFERENCES[\s.:]*$", re.I)


def detect_columns(page, blocks: list[dict]) -> str:
    """双栏检测：body 区文字块 x 中位数分布，中央 [0.45,0.55] 页宽内块 <5% 判双栏。"""
    pw = page.rect.width
    body = [
        b for b in blocks
        if not is_header_footer(b["bbox"], page.rect) and b["text"].strip()
    ]
    if len(body) < 4:
        return "one"
    mid_lo, mid_hi = 0.45 * pw, 0.55 * pw
    in_gap = sum(
        1 for b in body
        if b["bbox"].x0 >= mid_lo and b["bbox"].x1 <= mid_hi
    )
    return "two" if in_gap / len(body) < 0.05 else "one"


def split_crossing_blocks(page, blocks: list[dict]) -> list[dict]:
    """把横跨栏中线的块按 word 坐标重切到所属栏（PyMuPDF block 会跨栏聚类）。

    仅双栏页调用；word 归属按其中心 x 判定。
    P2 收紧：只有当两侧各自重组出 ≥2 行时才切（真·双栏跨块）；
    单行通栏元素（标题盒子/Index Terms 通栏框/宽表格头）整体保留不切，
    避免把 Index Terms 之类的整宽内容腰斩成两半。
    """
    mid = page.rect.width / 2.0
    out: list[dict] = []
    for b in blocks:
        if b["bbox"].x0 < mid < b["bbox"].x1 and b["bbox"].width > 0.6 * page.rect.width:
            words = page.get_text("words", clip=b["bbox"])
            left, right = [], []
            for w in words:
                wx = (w[0] + w[2]) / 2
                (left if wx <= mid else right).append(w)

            def _nlines(grp: list) -> int:
                return len({round(g[1], 1) for g in grp})

            if not (left and right) or _nlines(left) < 2 or _nlines(right) < 2:
                out.append(b)
                continue
            for grp in (left, right):
                r = pymupdf.Rect(grp[0][:4])
                for g in grp[1:]:
                    r |= pymupdf.Rect(g[:4])
                # 按 y 分行重组文本
                lines: dict[float, list] = {}
                for g in grp:
                    lines.setdefault(round(g[1], 1), []).append(g)
                txt = "\n".join(
                    " ".join(t[4] for t in sorted(lines[k], key=lambda t: t[0]))
                    for k in sorted(lines)
                )
                spans = [{"text": txt, "size": 10.0, "flags": 0,
                          "font": "unknown", "bbox": r}]
                out.append({"bbox": r, "text": txt, "spans": spans})
        else:
            out.append(b)
    return out


def assign_columns(blocks: list[dict], page_width: float, mode: str) -> None:
    """给每个块标注栏归属 col: 0=左/单栏, 1=右。原地修改。"""
    mid = page_width / 2.0
    for b in blocks:
        cx = (b["bbox"].x0 + b["bbox"].x1) / 2.0
        b["col"] = (1 if cx > mid else 0) if mode == "two" else 0


def is_header_footer(bbox: pymupdf.Rect, page_rect: pymupdf.Rect) -> bool:
    h = page_rect.height
    return (bbox.y1 < _HEADER_FOOTER_RATIO * h
            or bbox.y0 > (1 - _FOOTER_RATIO) * h)


def is_line_number_block(b: dict, page_rect: pymupdf.Rect) -> bool:
    """IOP 录用稿左侧行号列：贴左缘(x1<45pt)、纯数字串。"""
    return (
        b["bbox"].x1 < 45.0
        and re.fullmatch(r"[\d\s]+", b["text"].strip()) is not None
    )


def dominant_size(block: dict) -> float:
    """块主字号 = span 字号按字符数加权众数。"""
    c: Counter = Counter()
    for s in block["spans"]:
        c[round(s["size"], 1)] += max(len(s["text"]), 1)
    return c.most_common(1)[0][0] if c else 10.0


def is_heading(block: dict, body_size: float | None = None) -> bool:
    """D8 标题判定：粗体字体主导 或 字号显著大于正文。

    粗体: bold 字体名(CMBX/Bold/-B)字符占比 >= 60%
    大字号: 主字号 > body_size * 1.15（body_size 缺省时仅看粗体）
    """
    spans = block.get("spans") or []
    if not spans:
        return False
    total = sum(max(len(s["text"]), 1) for s in spans)
    bold_c = sum(
        max(len(s["text"]), 1) for s in spans
        if re.search(r"(CMBX|Bold|-B$|Hei|SimHei|MT-B)", s.get("font", ""), re.I)
        or (s.get("flags", 0) & 16)   # bit 4: bold
    )
    if total and bold_c / total >= 0.6:
        return True
    size = dominant_size(block)
    return bool(body_size and size > body_size * 1.15)


def figure_regions(page) -> list[pymupdf.Rect]:
    """图区域 = 位图 bbox ∪ 矢量绘图密集簇。

    聚类法：所有绘图矩形膨胀 12pt 后迭代合并相交分量，
    保留 op 数 >=10 或含位图的簇（轴标签等图内文字落其中 → 不译不删）。
    """
    pw, ph = page.rect.width, page.rect.height
    items: list[tuple[pymupdf.Rect, int]] = []   # (rect, op_weight)
    for info in page.get_image_info():
        items.append((pymupdf.Rect(info["bbox"]), 10))   # 位图自带高权重
    for d in page.get_drawings():
        r = d["rect"]
        if r.width >= 0.98 * pw and r.height >= 0.98 * ph:  # 整页框线不算
            continue
        if r.is_empty and r.is_infinite:
            continue
        items.append((r, 1))

    PAD = 12.0
    changed = True
    while changed:
        changed = False
        merged: list[list] = []
        for r, w in items:
            er = pymupdf.Rect(r.x0 - PAD, r.y0 - PAD, r.x1 + PAD, r.y1 + PAD)
            hit = None
            for m in merged:
                if er.intersects(m[0]):
                    hit = m
                    break
            if hit is None:
                merged.append([pymupdf.Rect(r), w])
            else:
                hit[0] |= r
                hit[1] += w
                changed = True
        items = [(m[0], m[1]) for m in merged]

    regions = []
    page_area = pw * ph
    for r, w in items:
        if w >= 10 and r.width > 40 and r.height > 40:
            # v0.2.2 打磨: 巨型图区夹紧到页面内;若仍覆盖 >35% 页面则视为
            # 检测异常（透明边距子图粘连,实测 paper3 p6 三联图并集成
            # (-62,-100,666,381) 吞掉半页正文）——按面积排序保留最大的
            # 子图矩形而非并集
            r = pymupdf.Rect(max(r.x0, 0), max(r.y0, 0),
                             min(r.x1, pw), min(r.y1, ph))
            if r.get_area() > 0.35 * page_area:
                continue   # 异常巨区:宁漏勿吞(图内文字漏保护只是不译,不丢)
            regions.append(r)
    return regions


def _is_algorithm_block(text: str, spans: list[dict]) -> bool:
    """Algorithm 伪代码框识别（v0.2.2 打磨）。

    _is_algorithm_block: Algorithm 伪代码框整体保留原文不进翻译队列
    （LLM 拒译或输出格式不符，实测 paper3 #44-49 批失败）。
    _is_algorithm_remnant: 框内数学行/伪代码残留碎片，同样保留。
    _is_math_fragment: ≤8 字符的变量等式碎片（'l=i'），保留原文。

    v0.2.3 修正: Algorithm caption 行（'Algorithm 1: ...' 独立块）不再
    触发整块 verbatim——它只是标题行，正文伪代码块由 _is_algorithm_block
    独立判定。caption 单独成块会被翻译（中文期刊惯例：算法标题也译）。
    """
    head = text.lstrip()
    if not re.match(r"^Algorithm\s+\d+\s*:", head):
        return False
    # v0.2.3: 纯标题行（单行、无伪代码内容）→ 不是伪代码框主体
    if "\n" not in text.strip():
        return False
    for s in spans or []:
        f = (s.get("font") or "")
        # NimbusMon 是 Nimbus Mono 的缩写，Mon/Mono 都要匹配
        if re.search(r"(Mono|Mon\b|Courier|Consol)", f, re.I):
            return True
    # 无等宽字体但结构典型（Input : + for ... do + end for）
    low = text.lower()
    return "input" in low[:80] and ("for" in low and "do" in low)


def _is_algorithm_remnant(text: str, spans: list | None = None) -> bool:
    """Algorithm 框内数学行的残留碎片（v0.2.2 打磨）。

    框主体被 verbatim 保留后,框内数学行(4/5/6 编号 + CMMI 公式)是
    独立块漏网——'Break out of for loop\\n7' 这类被 LLM 译成
    '跳出 for 循环 7' 混进伪代码框（实测 paper3 p5）。
    特征：以 Break/End/for/if/while/return 开头的 ≤60 字符短行,
    或纯编号+数学符号。

    v0.2.3: 伪代码主体常被 PyMuPDF 拆成多块（paper3 p4 实测：框内
    行 1-6 是一块 y77-184，行 7-9 是独立块 y210-258，含 NimbusMonL
    等宽字体但不以 'Algorithm N:' 开头 → _is_algorithm_block 判 False
    被送译，中文回灌后框内中英混杂）。补判定：①块内含等宽字体 span；
    ②行号数字/% 注释开头。
    """
    t = text.strip()
    if len(t) > 200:
        return False
    if re.match(r"^(Break out of|End (for|while|if)|for\s|if\s|while\s|return\s|else\b|Sample\b|Initialise\b)", t):
        return True
    if _is_math_fragment(t):
        return True
    # v0.2.3: 等宽字体 span（伪代码主体拆块的强信号；IEEE 正文不用等宽字体）
    for s in spans or []:
        if re.search(r"(Mon|Courier|Consol)", s.get("font") or "", re.I):
            return True
    # 行号数字开头 / % 注释开头
    return bool(re.match(r"^\d+\s*[%\s]|^%\s", t))


def _is_math_fragment(text: str) -> bool:
    """数学碎片单行识别：'l=i'、'x>=2' 这类 ≤8 字符的变量等式。

    来自公式/算法环境的碎块，LLM 无法成句翻译，保留原文。
    """
    t = text.strip()
    if len(t) > 8:
        return False
    if "=" not in t and "<" not in t and ">" not in t:
        return False
    return not re.search(r"[a-zA-Z]{4,}", t)   # 无长单词=纯符号/单字母


def _algorithm_box_regions(page) -> list[pymupdf.Rect]:
    """v0.2.3: Algorithm 伪代码框区域检测（顶线+题注+底线三线结构）。

    伪代码主体常被 PyMuPDF 拆成多个文本块（paper3 p4 实测：行 1-6 一块
    y77-184，行 7-9 独立块 y210-258），span 启发式逐块判定必漏。改用
    几何判定：同栏内全宽横线对，且首条线下方 25pt 内有 'Algorithm'
    题注词 → 两线之间整块是算法框。框内段落全部 verbatim（含标题行，
    整框语言统一不混杂）。
    """
    pw = page.rect.width
    mid = pw / 2.0
    hlines: list[pymupdf.Rect] = []
    try:
        for d in page.get_drawings():
            r = d["rect"]
            if r.height <= 2.5 and r.width >= 40:
                hlines.append(pymupdf.Rect(r))
    except Exception:
        return []
    try:
        alg_words = [w for w in page.get_text("words") if w[4] == "Algorithm"]
    except Exception:
        return []
    if not alg_words:
        return []
    boxes: list[pymupdf.Rect] = []
    for x_lo, cap_x in ((0.0, mid), (mid, pw)):
        col_w = cap_x - x_lo
        wide = sorted(
            (r for r in hlines
             if x_lo - 10 <= r.x0 <= x_lo + 0.15 * col_w
             and (r.x1 - r.x0) >= 0.70 * col_w),
            key=lambda r: r.y0)
        if len(wide) < 2:
            continue
        for i, top in enumerate(wide[:-1]):
            # 题注 'Algorithm' 词在 top 线下方 25pt 内、x 在本栏
            caps = [w for w in alg_words
                    if top.y1 - 2 <= w[1] <= top.y1 + 25
                    and x_lo - 5 <= w[0] <= cap_x]
            if not caps:
                continue
            # 底线：top 之后第一条低于题注的线（框高上限 300pt）
            for nxt in wide[i + 1:]:
                if nxt.y0 > max(w[3] for w in caps) + 3 \
                        and nxt.y0 - top.y0 <= 300:
                    boxes.append(pymupdf.Rect(
                        x_lo, top.y0 - 2, cap_x, nxt.y1 + 2))
                    break
    return boxes


def _strip_caption_eqnum_lines(block: dict) -> None:
    """v0.2.3: 从块内剥离独立成行的公式编号 '(n)' 行。

    paper3 p6 实测：'(7)' 独立成块首行、'(8)' 独立成块末行，且与公式
    主体不同块 → 编号吸收窗口（基于簇 bbox）够不着。剥离后：
    - 块内不再残留编号行（正文重灌不会出现 '（7）这将通过…' 夹裸编号）
    - 编号行 bbox 交给公式区吸收（collect_display_formulas 编号吸收步骤
      对独立块同样生效——吸收判定基于 span_recs 全集而非宿主块）
    """
    lines = block.get("lines") or []
    if len(lines) < 2:
        return
    eq_idx = [i for i, ln in enumerate(lines)
              if re.fullmatch(r"\(\d+\)", ln["text"].strip())]
    if not eq_idx:
        return
    keep_lines = [ln for i, ln in enumerate(lines) if i not in eq_idx]
    if not keep_lines:      # 整块都是编号（罕见）：不剥离，保持原样
        return
    eq_line_rects = [lines[i]["bbox"] for i in eq_idx]

    def _is_eq_span(s: dict) -> bool:
        if not re.fullmatch(r"\(\d+\)", s["text"].strip()):
            return False
        return any(lr.contains(s["bbox"]) or lr.intersects(s["bbox"])
                   for lr in eq_line_rects)

    # v0.2.3: 编号 span 必须保留在 spans 里——collect_display_formulas 的
    # 编号吸收读的就是块 spans，删掉 span = 编号从公式区消失（实测回归抓出）。
    # 只从 text/lines/bbox 剥离：翻译队列不再含裸编号，编号像素由公式位图保留。
    block["lines"] = keep_lines
    block["text"] = "\n".join(ln["text"] for ln in keep_lines)
    nb = pymupdf.Rect(keep_lines[0]["bbox"])
    for ln in keep_lines[1:]:
        nb |= ln["bbox"]
    block["bbox"] = nb


def merge_paragraphs(blocks: list[dict]) -> list[dict]:
    """同栏内垂直间距 < 1.5x 行高的相邻块合并；图注/表注独立成段。

    P2 收紧三约束（修"标题作者糊一段"/"参考文献巨型段"）：
    1. 字号一致性：任一侧块 ≤120 字符（标题/作者/单位级小块）时，
       主字号差 >0.25pt 即不并——标题(14pt)不再吸走作者(10pt)单位(9pt)
    2. 硬上限：合并后 >500 字符(SCHEME §4 上限)不并
    3. 参考文献条目([N] 开头)独立成段，绝不与前段合并

    v0.2.2 新增第4约束的前置吸收：bbox 被前段高度包含的小块
    （PyMuPDF 把 'Abstract—' 引导词拆成独立块且与正文块 y 完全重叠）
    先按包含关系吸收为前缀，否则两块分别重灌必然叠印。
    """
    paras: list[dict] = []
    for b in sorted(blocks, key=lambda x: (x.get("col", 0), x["bbox"].y0,
                                           x["bbox"].x0)):
        txt_head = b["text"].strip()
        cap = bool(_CAPTION_RE.match(txt_head))
        is_ref_entry = bool(re.match(r"^\[\d+\]", txt_head)) or \
                       bool(_REFS_HEADING_RE.search(txt_head))
        merged = False
        if paras:
            prev = paras[-1]
            same_col = prev.get("col", 0) == b.get("col", 0)
            line_h = dominant_size(prev) or 10.0
            gap = b["bbox"].y0 - prev["bbox"].y1
            ov = _h_overlap(prev["bbox"], b["bbox"])
            merged_len = len(prev["text"]) + len(b["text"]) + 1
            # v0.2.2: 包含吸收——小块 bbox 几乎完全落在前段内（重叠面积
            # ≥85% 小块自身面积）且字号相近 → 直接拼为前段前缀。
            # 'Abstract—' 类引导块与正文块 y 完全重叠，gap 判定接不住。
            bb_area = b["bbox"].get_area() or 1.0
            inter_area = 0.0
            if prev["bbox"].intersects(b["bbox"]):
                ir = pymupdf.Rect(prev["bbox"])
                ir.intersect(b["bbox"])
                inter_area = ir.get_area()
            contained = (same_col and not cap and not prev["is_caption"]
                         and not is_ref_entry and not prev.get("is_ref")
                         and len(b["text"]) <= _SMALL_BLOCK_CHARS
                         and inter_area / bb_area >= 0.85
                         and abs(dominant_size(prev) - dominant_size(b)) <= 1.5)
            if contained:
                prev["text"] = b["text"] + " " + prev["text"] \
                    if b["bbox"].x0 <= prev["bbox"].x0 + 2 \
                    else prev["text"] + " " + b["text"]
                prev["bbox"] |= b["bbox"]
                prev["spans"].extend(b["spans"])
                prev["size"] = dominant_size(prev)
                merged = True
            # v0.2.2 反向包含：前段是小引导块（'Abstract—'），来块是大正文块
            # 且前段被来块包含 → 前段文本拼到来块头部（引导词在先）
            prev_area = prev["bbox"].get_area() or 1.0
            contained_rev = False
            if not merged and prev["bbox"].intersects(b["bbox"]):
                ir2 = pymupdf.Rect(prev["bbox"])
                ir2.intersect(b["bbox"])
                contained_rev = (
                    same_col and not cap and not prev["is_caption"]
                    and not is_ref_entry and not prev.get("is_ref")
                    and len(prev["text"]) <= _SMALL_BLOCK_CHARS
                    and len(b["text"]) > _SMALL_BLOCK_CHARS
                    and ir2.get_area() / prev_area >= 0.85
                    and abs(dominant_size(prev) - dominant_size(b)) <= 1.5)
            if contained_rev:
                prev["text"] = prev["text"] + " " + b["text"]
                prev["bbox"] |= b["bbox"]
                prev["spans"].extend(b["spans"])
                prev["size"] = dominant_size(prev)
                merged = True
            # 小块字号一致性：两侧任一是小块且字号不同 → 不并
            small_either = (len(prev["text"]) <= _SMALL_BLOCK_CHARS
                            or len(b["text"]) <= _SMALL_BLOCK_CHARS)
            size_mismatch = abs(dominant_size(prev) - dominant_size(b)) > 0.25
            # v0.2.2: 参考文献续行合并——prev 是 [N] 条目、来块无 [N] 前缀、
            # 同栏小 gap → 续行并入当前条目（旧逻辑禁止 ref 段合并导致
            # 一条文献被拆成多个碎片段,渲染后中英混糊+溢出,实测 p4 叠印）。
            # 800 字符软顶防误吸正文;来块是 [N] 新条目/图注时不并。
            ref_cont = (same_col and prev.get("is_ref") and not is_ref_entry
                        and not cap and 0 <= gap < 2.0 * line_h
                        and ov > 0.5 and merged_len <= 800)
            if ref_cont:
                prev["text"] += "\n" + b["text"]
                prev["bbox"] |= b["bbox"]
                prev["spans"].extend(b["spans"])
                prev["size"] = dominant_size(prev)
                merged = True
            ok = (not merged and same_col and not cap and not prev["is_caption"]
                  and not is_ref_entry and not prev.get("is_ref")
                  and 0 <= gap < 1.5 * line_h and ov > 0.6
                  and merged_len <= _MERGE_CAP_CHARS
                  and not (small_either and size_mismatch))
            if ok:
                prev["text"] += "\n" + b["text"]
                prev["bbox"] |= b["bbox"]
                prev["spans"].extend(b["spans"])
                prev["size"] = dominant_size(prev)
                merged = True
        if not merged:
            p = dict(b)
            p["is_caption"] = cap
            p["is_ref"] = is_ref_entry
            # v0.2.3: Algorithm caption 行（'Algorithm 1: ...' 纯标题块）
            # 标记为 is_alg_caption——翻译它（中文期刊惯例），渲染层按
            # caption 风格处理；伪代码主体仍由 _is_algorithm_block verbatim
            p["is_alg_caption"] = bool(
                re.match(r"^Algorithm\s+\d+\s*:", txt_head)
                and "\n" not in txt_head)
            # v0.2.2: Algorithm 伪代码框/数学碎片 → 保留原文标记
            p["is_verbatim"] = (not cap and not is_ref_entry
                                and (_is_algorithm_block(b["text"], b.get("spans") or [])
                                     or _is_math_fragment(b["text"])))
            p["size"] = dominant_size(b)
            paras.append(p)
    for p in paras:
        p.setdefault("size", dominant_size(p))
        # v0.2.2: 合并完成后统一重判 verbatim（首块可能只是标题行
        # 'Algorithm 1: ...'，伪代码主体在后续合并块里）
        if not p.get("is_caption") and not p.get("is_ref"):
            p["is_verbatim"] = (_is_algorithm_block(p["text"], p.get("spans") or [])
                                or _is_math_fragment(p["text"])
                                or _is_algorithm_remnant(p["text"], p.get("spans") or []))
    return paras


def _h_overlap(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    inter = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    denom = min(a.width, b.width) or 1.0
    return inter / denom


def find_tables(page) -> list[pymupdf.Rect]:
    """D3: 表格区域圈定。

    v0.2.3: find_tables() 对无竖线三线表（IEEE/学术排版主流）检出 0 张
    （实测 paper3 p6 表 I）→ 表区不保护、单元格被当正文重灌成乱码。
    补充三线表启发式：同栏内 ≥3 条全宽横线（顶线/表头线/底线，x 覆盖
    >70% 栏宽、间距 8-80pt）围出的区域判为表区。
    """
    rects: list[pymupdf.Rect] = []
    try:
        tf = page.find_tables()
        if tf and tf.tables:
            rects = [pymupdf.Rect(t.bbox) for t in tf.tables]
    except Exception:
        rects = []
    # ---- 三线表启发式（find_tables 检不出时兜底）----
    pw, ph = page.rect.width, page.rect.height
    mid = pw / 2.0
    # 收集横线（细高比极端的水平线段）
    hlines: list[pymupdf.Rect] = []
    try:
        for d in page.get_drawings():
            r = d["rect"]
            if r.height <= 2.5 and r.width >= 40:
                hlines.append(pymupdf.Rect(r))
    except Exception:
        hlines = []
    for x_lo, cap in ((0.0, mid), (mid, pw)):
        # 该栏的全宽横线：x 覆盖 >70% 栏宽（paper3 表 I 实测 79.7%，
        # 旧 0.98 页宽判定漏检；IEEE 三线表通常与最宽内容行等宽）
        col_w = cap - x_lo
        wide = sorted(
            (r for r in hlines
             # 线起点须落在该栏内（下界 x_lo-10）——否则左栏的线会满足
             # 右栏的上界条件（实测 2.pdf p2/p4 左栏线拼出右栏假表区）
             if x_lo - 10 <= r.x0 <= x_lo + 0.15 * col_w
             and (r.x1 - r.x0) >= 0.70 * col_w),
            key=lambda r: r.y0)
        if len(wide) < 3:
            continue
        # v0.2.3: 等宽约束——真三线表的顶线/中线/底线 x 范围一致（±4pt）；
        # 公式区横线（分数线/长等号线）与页眉线宽度各异，混进来会拼出
        # 假表区（实测 2.pdf p2/p4 页眉线+分数线误检）。以出现最多的
        # (x0,x1) 组合为基准筛同宽线。
        from collections import Counter as _C2
        combos = _C2((round(r.x0), round(r.x1)) for r in wide)
        ref_x0, ref_x1 = combos.most_common(1)[0][0]
        col_lines = [r for r in wide
                     if abs(r.x0 - ref_x0) < 4 and abs(r.x1 - ref_x1) < 4]
        if len(col_lines) < 3:
            continue
        # 相邻横线间距 8-80pt 视为同一表的三线结构；取首尾线围区域
        groups: list[list[pymupdf.Rect]] = [[col_lines[0]]]
        for r in col_lines[1:]:
            g = groups[-1]
            if 8 <= r.y0 - g[-1].y0 <= 80:
                g.append(r)
            else:
                groups.append([r])
        for g in groups:
            if len(g) >= 3:
                # 区域 x 范围用实际线段范围（±6pt 裹住单元格文字），
                # 不用栏边界——栏宽矩形会吞进表旁的正文块
                lx0 = min(r.x0 for r in g) - 6
                lx1 = max(r.x1 for r in g) + 6
                rects.append(pymupdf.Rect(
                    max(lx0, x_lo), g[0].y0 - 2,
                    min(lx1, cap), g[-1].y1 + 2))
    # 与 find_tables 结果去重（相交即视为同表）
    dedup: list[pymupdf.Rect] = []
    for r in rects:
        if not any(r.intersects(d) for d in dedup):
            dedup.append(r)
    return dedup


def table_cells(page, table_bbox: pymupdf.Rect) -> list[dict]:
    """D3 单元格原子块：返回 {bbox, text} 列表。

    v0.2.3: find_tables 检不出三线表时（启发式兜底圈出的表区），
    按行带切分单元格——用表区内的文字块 y 带分组，每行带内按 x0 排序
    逐块成格。单元格粒度粗于 find_tables 但足以让每格独立翻译不糊。
    """
    cells = []
    try:
        tf = page.find_tables()
        found = False
        for t in (tf.tables if tf else []):
            tb = pymupdf.Rect(t.bbox)
            if not tb.intersects(table_bbox):
                continue
            found = True
            for c in t.header.cells + t.cells:
                if c is None:
                    continue
                r = pymupdf.Rect(c)
                txt = page.get_text("text", clip=r).strip()
                if txt:
                    cells.append({"bbox": r, "text": txt})
        if found:
            return cells
    except Exception:
        pass
    # ---- 三线表行带切分兜底 ----
    tb = pymupdf.Rect(table_bbox)
    blocks = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        r = pymupdf.Rect(b["bbox"])
        if not tb.contains(r):
            continue
        blocks.append((r, b))
    if not blocks:
        return cells
    # 行带分组按 LINE 粒度（block 粒度会把多行塞一带——实测 paper3 表 I
    # 一个 block 含 4 行，整块成"格"导致两行数据挤一格译文粘连）
    items: list[tuple[pymupdf.Rect, dict]] = []
    for r, b in blocks:
        for l in b.get("lines", []):
            lr = pymupdf.Rect(l["bbox"])
            if tb.contains(lr):
                items.append((lr, l))
    if not items:
        return cells
    # 行带分组：y 重叠即同带
    items.sort(key=lambda x: x[0].y0)
    bands: list[dict] = []
    for r, l in items:
        placed = False
        for band in bands:
            if min(r.y1, band["bbox"].y1) - max(r.y0, band["bbox"].y0) > 0:
                band["items"].append((r, l))
                band["bbox"] |= r
                placed = True
                break
        if not placed:
            bands.append({"bbox": pymupdf.Rect(r), "items": [(r, l)]})
    for band in bands:
        items2 = sorted(band["items"], key=lambda x: x[0].x0)
        # 同一带内 x 间隙 > 12pt 的拆成独立格（同一行不同列）。
        # v0.2.4: 数字列右对齐表（paper3 表 I）间隙 467→475 仅 12pt 恰好
        # 不触发——改为"间隙>12pt 或 右侧是纯数字/短数字串"都拆格，
        # 防止数字粘进方法名列导致译文回灌后数字错位
        col_groups: list[list] = [[items2[0]]]
        for it in items2[1:]:
            prev = col_groups[-1][-1]
            gap_split = it[0].x0 - prev[0].x1 > 12
            if not gap_split:
                it_txt = "".join(s["text"] for s in it[1].get("spans", [])).strip()
                # 右侧格是独立数字且与左侧有明显 x 错位 → 拆
                if (it_txt and re.fullmatch(r"[\d.,%]+", it_txt)
                        and it[0].x0 >= prev[0].x1 - 2):
                    gap_split = True
            if gap_split:
                col_groups.append([it])
            else:
                col_groups[-1].append(it)
        for g in col_groups:
            gr = pymupdf.Rect(g[0][0])
            txts = []
            for _, l in g:
                gr |= pymupdf.Rect(l["bbox"])
                txts.append("".join(s["text"] for s in l.get("spans", [])))
            txt = " ".join(t for t in txts if t.strip())
            if txt.strip():
                cells.append({"bbox": gr, "text": txt})
    return cells


def collect_display_formulas(page, blocks: list[dict]) -> list[dict]:
    """D6/P3-v3: 种子生长式公式区检测，按栏隔离合并。

    算法：
    1. 全部 span 中 math-font 字符为种子，携带所属块的栏归属
       （Word 系 PDF 公式被拆成大量碎块，块级判定漏检率过高——实测教训）
    2. 同栏种子膨胀(横向14pt/纵向6pt)后迭代合并 → 完整公式区
       （v2 教训：跨栏合并会把左右两栏文字烤进同一位图，回贴后盖住译文，
        产生"半行中文半行英文"叠印）
    3. 区内吸收同栏非 math span（cos/数字），右缘吸收同栏公式编号 "(n)"
    4. 过滤：高度 <13pt 视为行内小公式不保护；宽度 >0.62 页宽的异常簇丢弃
    """
    pw = page.rect.width
    mid = pw / 2.0
    # (rect, text, col, font) 四元组；col/font 由宿主块决定
    span_recs = []
    for b in blocks:
        col = b.get("col", 0)
        for s in (b.get("spans") or []):
            if s.get("text", "").strip():
                span_recs.append((pymupdf.Rect(s["bbox"]), s["text"], col,
                                  s.get("font", "")))
    # v0.2.2: 种子=硬数学字体 span；Times-Italic 是弱信号——只有同页/同簇
    # 存在硬数学字体时才升级为种子（文献期刊名整段斜体的误种子根因）。
    hard_spans = [(pymupdf.Rect(sr), cc) for sr, t, cc, f in span_recs
                  if _MATH_FONT_RE.search(f) and t.strip()]
    weak_spans = [(pymupdf.Rect(sr), cc) for sr, t, cc, f in span_recs
                  if _MATH_WEAK_FONT_RE.search(f) and t.strip()]
    if not hard_spans:
        # 全页无硬数学字体 → Times-Italic 全是期刊名/强调词,不设公式区
        return []
    seeds = list(hard_spans)

    PAD_X, PAD_Y = 14.0, 6.0

    def _chain_col(spans_col: list, col: int) -> list[pymupdf.Rect]:
        """同栏 span 三级聚类：y带分组 → 带内x链合 → 跨带纵向合并。

        教训链：膨胀相交会被跨栏巨簇粘连；纯x0排序会让不同行span交错断链。
        正解：①按y重叠分带（同一视觉行）②带内按x0排序、gap≤24pt链合
        （非math的ln/数字/括号参与桥接）③x重叠且垂直间距小的簇合并
        （覆盖 (x−a)²/k² ± (y−b)²/h² 这类上下两行的display公式）。
        """
        items = [(pymupdf.Rect(sr[0]), sr[2] if len(sr) > 3 else "")
                 for sr in spans_col]
        items.sort(key=lambda it: it[0].y0)
        # ① y带分组（任意重叠即同带；正文行距~3pt不重叠不受影响）
        bands: list[dict] = []
        for r, f in items:
            placed = False
            for band in bands:
                bb = band["bbox"]
                if min(r.y1, bb.y1) - max(r.y0, bb.y0) > 0:
                    band["rects"].append((r, f))
                    bb |= r
                    placed = True
                    break
            if not placed:
                bands.append({"rects": [(r, f)], "bbox": pymupdf.Rect(r)})
        # ② 带内按 x 链合
        row_clusters: list[pymupdf.Rect] = []
        for band in bands:
            rs = sorted(band["rects"], key=lambda it: it[0].x0)
            cur: pymupdf.Rect | None = None
            for r, f in rs:
                if cur is None:
                    cur = pymupdf.Rect(r)
                    continue
                if r.x0 - cur.x1 <= 24.0:
                    cur |= r
                else:
                    row_clusters.append(cur)
                    cur = pymupdf.Rect(r)
            if cur is not None:
                row_clusters.append(cur)
        # ③ 跨带纵向合并（多行公式/重叠带），逐对 math 占比门控防吞正文
        row_clusters.sort(key=lambda r: (r.y0, r.x0))

        def _ratio(rr: pymupdf.Rect) -> tuple[int, float]:
            mn = mc = tc = 0
            for sr, t, c, f in span_recs:
                if c != col:
                    continue
                srr = pymupdf.Rect(sr)
                if srr.intersects(rr):
                    tc += len(t)
                    if _MATH_FONT_RE.search(f):
                        mn += 1
                        mc += len(t)
            return mn, (mc / tc if tc else 0.0)

        merged: list[pymupdf.Rect] = []
        for r in row_clusters:
            if merged:
                last = merged[-1]
                x_ov = min(r.x1, last.x1) - max(r.x0, last.x0)
                v_gap = r.y0 - last.y1
                if x_ov > 0.35 * min(r.width, last.width) and -12 <= v_gap <= 8:
                    trial = pymupdf.Rect(last)
                    trial |= r
                    _, pr = _ratio(trial)
                    if pr >= 0.5:
                        merged[-1] = trial
                        continue
            merged.append(pymupdf.Rect(r))
        return merged

    regions = []
    # v0.2.2: 弱信号 span(Times-Italic 变量)参与链合桥接(与 ln/数字同角色),
    # 但不计入 math_n/ratio 分子——公式(6) 'V=−RC dV/dt' 的 italic 变量把
    # MTSY '=−' 与 RMTMI '.' 桥起来,簇才完整;文献期刊名因无硬字体同带而不受影响
    bridge_spans = [(pymupdf.Rect(sr), cc) for sr, t, cc, f in span_recs
                    if _MATH_WEAK_FONT_RE.search(f) and t.strip()]
    for col in (0, 1):
        col_spans = [sr for sr in span_recs if sr[2] == col]
        if not col_spans:
            continue
        col_bridges = [b for b in bridge_spans if b[1] == col]
        for r in _chain_col(col_spans + col_bridges, col):
            math_n = math_chars = total_chars = 0
            for sr, t, c, f in span_recs:
                if c != col:
                    continue
                srr = pymupdf.Rect(sr)
                if not srr.intersects(r):
                    continue
                total_chars += len(t)
                if _MATH_FONT_RE.search(f):
                    math_n += 1
                    math_chars += len(t)
            ratio = math_chars / total_chars if total_chars else 0.0
            # v0.2.2: 矮簇(<13pt)保护门槛加严——参考文献区每行都有
            # Times-Italic 期刊名(IEEE Sens. J. 等),种子密度高但占比低,
            # 旧 math_n>=2 就放行使整页文献被误保护(实测 p4 十个假公式区)。
            # display 公式特征: math span 密集且占比高。矮簇要求 ratio>=0.5,
            # 正常高度簇维持 ratio>=0.3（真公式实测 0.65-0.79,文献误判 0.10-0.43）
            # 注: 弱桥接簇(变量密集型如 V=−RC·dV/dt, italic 变量不计分子)
            # 实测 ratio≈0.29,故普通簇门槛 0.3 下调至 0.22;文献假簇因同带
            # 无硬种子根本不会成簇,不受影响。
            if r.height < 13:
                if math_n < 3 or ratio < 0.5:
                    continue   # 行内小符号/文献斜体：不设保护区
                is_display = False   # 矮真公式:保护区保留但不裁图
            else:
                if math_n < 2 or ratio < 0.22:
                    continue   # 正文行/仅含个别符号的行：不设保护区
                is_display = True
            r.x0 -= 2; r.y0 -= 2; r.x1 += 2; r.y1 += 2
            # 吸收右缘公式编号 "(n)"（v0.2.2 重写，v0.2.3 再修：
            # ①窗口放宽到 x1+110pt（paper3 p6 实测编号距公式右缘 98pt，
            #   超 95pt 旧窗口 3pt 漏吸；2.pdf 90pt、paper3 98pt，取 110pt 裕量）
            # ②y 带按编号行高独立判定（编号基线可比公式主体低 12pt 以上）
            # ③吸收全部命中编号（多行公式组各带编号），不再 break 于第一个
            # ④x_cap 用 max(r.x1, cap) 防倒置窗口——公式簇 x1 已超栏右缘时
            #   (r.x1+1, min(x_cap,...)) 是 x0>x1 的空矩形，永不相交=编号
            #   永远吸不进来（paper3 p6 (7) 漏吸的第二根因）
            x_cap = (mid - 6) if col == 0 else (pw - 20)
            zone_x1 = max(r.x1 + 1, min(x_cap, r.x1 + 110))
            # v0.2.3: 吸收域从"右侧带"扩为"右下方域"——PyMuPDF 常把编号
            # 塞进下一个文本块首行（paper3 p6 (7) 实测：x 288-300 在簇 x
            # 范围内、y 低于簇底 0.4pt），右侧带永远够不着。
            # span 全文 fullmatch "(n)" 的前置条件不变：正文句中引用
            # "from (7) we get" 是整行 span 不会误吸。
            eq_zone = pymupdf.Rect(r.x0, r.y0 - 8, zone_x1, r.y1 + 16)
            for sr, t, c, f in span_recs:
                if c != col or not re.fullmatch(r"\(\d+\)", t.strip()):
                    continue
                srr = pymupdf.Rect(sr)
                if eq_zone.intersects(srr):
                    r |= srr
            regions.append({"bbox": r, "para_hint_y": r.y0,
                            "is_display": is_display})
    # v0.2.2: 跨带纵向合并的 x 重叠门控（>35%）会漏掉斜向排布的多行公式，
    # 产出互相重叠的区域对（实测 page3 公式(7)拆成 F1/F2 两簇）→ 位图双重回贴。
    # 返回前统一去重合并。
    merged_regions: list[dict] = []
    for reg in sorted(regions, key=lambda g: pymupdf.Rect(g["bbox"]).y0):
        rr = pymupdf.Rect(reg["bbox"])
        hit = None
        for m in merged_regions:
            mr = pymupdf.Rect(m["bbox"])
            if mr.intersects(rr):
                inter = (min(mr.x1, rr.x1) - max(mr.x0, rr.x0)) * \
                        (min(mr.y1, rr.y1) - max(mr.y0, rr.y0))
                smaller = min(mr.get_area(), rr.get_area()) or 1.0
                if inter / smaller > 0.3:
                    hit = m
                    break
        if hit:
            hr = pymupdf.Rect(hit["bbox"])
            hit["bbox"] = tuple(hr | rr)
            hit["is_display"] = hit["is_display"] or reg["is_display"]
        else:
            merged_regions.append(dict(reg))
    return merged_regions


def layout_page(page) -> dict:
    """单页总编排：blocks → 栏检测/重切 → 排除 → 合并 → 表格/公式。"""
    from . import extract as ex

    raw = ex.get_page_blocks(page)
    mode = detect_columns(page, raw)

    # v0.2.3: 块内独立成行的公式编号 '(n)' 行剥离——编号与公式主体不同块时
    # 吸收窗口够不着（paper3 p6 (7)(8) 实测），剥离后编号 span 独立参与
    # 公式区吸收，正文重灌不再夹裸编号。
    for b in raw:
        _strip_caption_eqnum_lines(b)

    # v0.2.2: 参考文献条目重切——PyMuPDF 块内嵌多条件目（⏎[N] 行首），
    # 块级 is_ref 判定只看块头 → 一条文献被拆成碎片段、渲染后中英混糊。
    # 按条目边界拆块后每条 [N] 独立成段（D8/merge 的 is_ref 规则才能生效）。
    from .refsplit import split_ref_blocks
    raw = split_ref_blocks(page, raw)

    body, hf = [], []
    for b in raw:
        if is_header_footer(b["bbox"], page.rect):
            hf.append(b)
        elif is_line_number_block(b, page.rect):
            hf.append(b)          # 行号列当非译文内容丢弃
        else:
            body.append(b)

    if mode == "two":
        body = split_crossing_blocks(page, body)
    assign_columns(body, page.rect.width, mode)

    figs = figure_regions(page)
    tables = [pymupdf.Rect(t) for t in find_tables(page)]
    # v0.2.3: Algorithm 伪代码框几何检测——框内段落全部 verbatim
    # （主体被拆成多块时 span 启发式必漏，实测 paper3 p4 行 7-9 独立成块）
    alg_boxes = _algorithm_box_regions(page)
    # P3: display 公式区先于段落收集（基于原始 body 块），文字块受保护：
    # 公式区内的 span/碎片不进翻译队列、原文不抹除，仅裁图回贴（D6 完整闭环）。
    formulas = collect_display_formulas(page, body)
    formula_rects = [pymupdf.Rect(f["bbox"]) for f in formulas]
    # v0.2.2: 保护判定从"块中心"改为"块与公式区相交"——混合块
    # （如 'dt .\n(6)'，一半公式碎片一半编号）中心在区外会被漏保护，
    # 导致 redact 抹掉编号+碎片重灌成乱码（实测 page3 (6)(7) 被吞根因）。
    protected = figs + tables + formula_rects

    def in_protected(bb) -> bool:
        # bb 可为块 dict 或 Rect（v0.2.4: 传 dict 以便读取 text 判图注豁免）
        if isinstance(bb, dict):
            btxt_full = bb.get("text") or ""
            bb = pymupdf.Rect(bb["bbox"])
        else:
            btxt_full = ""
        # 图/表：块中心落入区域（原行为）；公式区：相交即保护（v0.2.2，
        # 混合碎片块的中心在区外会被漏保护——实测 page3 (6)(7) 被吞根因）。
        # v0.2.2 打磨: 相交判定加"主体性"约束——块与公式区的相交面积须占
        # 块高的 ≥30%，否则视为擦边正文不保护（paper3 p6 实测: 公式(7)下方
        # 94pt 高的正文块顶部 2pt 擦到扩边公式区,整段被误吞）。
        # v0.2.4: 图注豁免——'Fig./Table N:' 开头的 caption 块即使中心
        # 落在图区位图矩形内也归正文送译（paper3 p1 实测：作者把图注文字
        # 层叠在图片对象的白色下半区，位图 bbox 吞掉 caption → 整段漏译）。
        c = pymupdf.Point((bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2)
        if any(p.contains(c) for p in tables):
            return True
        for fp in figs:
            if not fp.contains(c):
                continue
            if not re.match(r"^(Fig\.?|Figure|Table)\s*\d*", btxt_full.strip(),
                            re.I):
                return True
            # 图注在图区下半部（y 中心 > 区高 55%）→ 豁免，归正文送译；
            # 叠在上半部的算图内标注照旧保护
            cap_mid_y = (bb.y0 + bb.y1) / 2
            if cap_mid_y >= fp.y0 + 0.55 * fp.height:
                continue   # 下半部 → 不保护，归正文
            return True
        for fr in formula_rects:
            if not fr.intersects(bb):
                continue
            ir = pymupdf.Rect(fr)
            ir.intersect(bb)
            if ir.get_area() >= 0.30 * max(bb.get_area(), 1e-6):
                return True
        return False

    body_txt = [b for b in body if not in_protected(b)]
    fig_text = [b for b in body if in_protected(b)]   # 图/表/公式内杂文字：不译不删
    # v0.2.3: Algorithm 框内块从正文队列摘出、整体 verbatim（含标题行，
    # 整框语言统一）。渲染层 verbatim 段不 redact 不重灌，原像素保留。
    alg_final: list[dict] = []
    body_txt2: list[dict] = []
    for b in body_txt:
        r = pymupdf.Rect(b["bbox"])
        if any(ab.contains(r) or (ab.intersects(r)
                and ab.intersect(pymupdf.Rect(r)).get_area()
                >= 0.6 * max(r.get_area(), 1e-6))
               for ab in alg_boxes):
            b2 = dict(b)
            b2["is_alg_box"] = True
            alg_final.append(b2)
        else:
            body_txt2.append(b)
    body_txt = body_txt2
    paras = merge_paragraphs(body_txt)

    # D8: 正文众数字号（标题判定的基准）
    from collections import Counter as _C
    _sz = _C()
    for p in paras:
        _sz[round(p["size"], 1)] += max(len(p["text"]), 1)
    body_size = _sz.most_common(1)[0][0] if _sz else None
    for p in paras:
        p["is_heading"] = is_heading(p, body_size)
    # 行内数学符号不 token 化（v1 决策）：Word 系 PDF 希腊字母逐个成 span，
    # token 化毁阅读顺序；行内符号保留原文交 LLM 语义转写（SCHEME 实测事实#4 路线）。
    # D6 裁图回贴仅限 display 公式块与图内公式区。

    # v0.2.3: 全部表区的单元格平铺（pipeline 翻译队列用）
    tables_cells: list[dict] = []
    for t in tables:
        tables_cells.extend(table_cells(page, t))

    # v0.2.3: Algorithm 框内块并入 paragraphs 尾部（按 y 排序恢复阅读序），
    # 标记 verbatim——不进翻译队列、不 redact
    paras.extend(alg_final)
    for p in alg_final:
        p["is_verbatim"] = True
        p.setdefault("size", dominant_size(p))
    paras.sort(key=lambda p: (p.get("col", 0), p["bbox"].y0, p["bbox"].x0))

    return {
        "mode": mode,
        "paragraphs": paras,
        "tables": [{"bbox": t, "cells": table_cells(page, t)} for t in tables],
        "tables_cells": tables_cells,
        "formulas": formulas,
        "hf_blocks": hf,
        "fig_text_blocks": fig_text,
        "figure_regions": figs,
    }
