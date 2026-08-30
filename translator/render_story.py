"""v0.7.1 P1：faithful 模式整页 Story 接管（页级流式排版，任务 2-3）。

动机：v0.5.1 起的逐段 insert_htmlbox 每段各建一个 Story 独立排版——
相邻段的基线不共享连续网格，段间 baseline 抖动由此而生（v0.7.0 基线：
段间空隙 median 29.8pt 但逐段波动）。页级 Story 把整页段落拼进单个
Story 流式排版，行位置由同一引擎一次性连续排出，段间网格天然对齐。

机制（PyMuPDF 1.28.2 实测，见 tools/story_css_probe.py 能力矩阵）：
- 每段 <p> 携带 page-break-after:always（末段除外）→ 引擎在每个段后
  强制断框，rectfn 按阅读序逐段返回该段原始 bbox（页几何冻结）；
- 全框共用同一 mediabox → write 产出的临时文档只有一页，段位置即页
  绝对坐标；show_pdf_page 1:1 贴回目标页（无缩放）；
- body{margin:0}：margin 只作用于首框（实测），置 0 让每段精确对齐
  框顶（旧逐段路径是 body{margin:1px}，逐框 +1px）。

正确性生命线（任务 1.2）——流式引擎死穴是「第 N 段在框内装不下时，
内容漫延到第 N+1 段的框」（译文串位）。两级防线：
1. 页级预检：开排前用 fit.py 测量基座逐段验证该段以其因子在框内
   fit_scale ≥ 1.0（~1ms/段）；任一失败 → 整页回退逐段路径；
2. 落墨前预演：place-only 逐框推进，用 element_positions 校验
   「第 k 框恰好包含且仅包含第 k 段的 open+close 事件」；溢出漫延
   （预检漏网的引擎分歧）在此拦截——预演不落墨，失败整页回退。
绝不段级混排（同页两种基线节奏比抖动更难看）；回退计数进 stats。
"""
from __future__ import annotations

import io

import pymupdf

from .fit import _HARD_FLOOR, _MEASURE_MARGIN, measure_fit_factor

# 预检粗筛阈值：fit_scale 二分精度 ~0.001，恰好装下的段可能测得 0.999x——
# 预检只拦明显溢出（f 显著 < 1）；边界段的精确门在落墨前预演
# （_verify_flow 逐框对账，漫延必被拦截），预检漏网不致串位。
_STORY_FIT_EPS = 0.004

# v0.8.2 预检局部收缩下限：紧段（f ∈ [floor, 1-ε)）在页级 Story 内
# 以确定性局部字号因子收纳（与 fit.pass per-spec override 同一语义——
# 旧版任何 f<1-ε 的段都把整页打回逐段路径，每页多付 N 次
# insert_htmlbox 的字体重解析 ~320ms/次）。段落下限对齐 fit.min_scale
# （0.78，低于此交回退路径由引擎 scale_low 深缩+告警）；单元格下限对齐
# _render_cells_htmlbox 的 scale_low=0.3（数字窄格深缩是既有语义）。
_PARA_SHRINK_FLOOR = 0.78
_CELL_SHRINK_FLOOR = 0.30


def _para_rule(family: str, base: float, lh: float, align: str,
               indent_em: float, dir_css: str, factor: float = 1.0,
               lead: float = 1.0, tracking: float = 1.0) -> str:
    """单条段落声明串（花括号体，不含选择器与 font_css 前缀）。

    与 render._para_css 同源——页级 Story 用 `p.sN {声明}` 拼装，
    逐段路径用 `p {声明}`，两路样式语义严格一致。
    """
    css = (f"{{font-family:{family}; font-size:{base * factor:.2f}pt;"
           f" line-height:{lh * lead:.3f}; margin:0; text-align:{align};")
    if indent_em:
        css += f"text-indent:{indent_em}em;"
    if tracking < 1.0:
        css += f"letter-spacing:{tracking - 1.0:.4f};"
    css += dir_css + "}"
    return css


def para_factors(spec: dict, factors: dict | None) -> tuple[float, float, float]:
    """spec → (factor, lead, tracking)：类因子 + per-spec override 统一解析。

    页级 Story 与逐段路径共用——两路 CSS 语义必须一致，测量/渲染才对齐。
    """
    if factors is None:
        return 1.0, 1.0, 1.0
    ov = spec.get("_fit_override")
    if ov is not None:
        return (ov.get("factor", 1.0), ov.get("lead", 1.0),
                ov.get("tracking", 1.0))
    f = factors.get(spec["cls"]) or {"factor": 1.0, "lead": 1.0,
                                     "tracking": 1.0}
    factor = f["factor"] if spec["can_shrink"] else 1.0
    return factor, f.get("lead", 1.0), f.get("tracking", 1.0)


def _cell_rule(spec: dict, factors: dict | None, k: float = 1.0) -> str:
    """单元格声明串（类因子 × 局部收缩 k；nowrap 窄格单行语义保留）。

    与 _render_cells_htmlbox 的 CSS 构造同源（同因子/行距/nowrap），
    页级 Story 与逐格路径的表格文字样式才一致。
    """
    f = (factors or {}).get(spec.get("cls") or "") \
        or {"factor": 1.0, "lead": 1.0, "tracking": 1.0}
    rule = _para_rule(spec["family"], spec["base"], spec["lh"],
                      "left", 0, spec["dir_css"],
                      factor=f["factor"] * k,
                      lead=f.get("lead", 1.0),
                      tracking=f.get("tracking", 1.0))
    if spec.get("nowrap"):
        rule = rule[:-1] + "white-space:nowrap;}"
    return rule


def build_page_story(specs: list[dict], factors: dict | None,
                     font_css: str, cell_specs: "list[dict] | None" = None,
                     cell_factors: "dict | None" = None,
                     local: "dict[int, float] | None" = None,
                     cell_local: "dict[int, float] | None" = None
                     ) -> tuple[str, str, list[str]]:
    """specs（+可选 cell_specs）→ (整页 HTML, 整页 CSS, 每项测量用 CSS)。

    HTML：`<p id=pN class=sN style=page-break-after:always>…</p>`
    （末项不带断框）；单元格 `<p id=cM class=tM>`。每项内容沿用
    collect_*_specs 产出的转义文本。
    CSS：font_css(@font-face) + body{margin:0} + 每项一条 class 规则
    （因子/行距/对齐/缩进/RTL 与逐段路径同源）。
    v0.8.2: local/cell_local——预检产出的局部收缩因子（乘在类因子上）。
    """
    cell_specs = cell_specs or []
    n = len(specs) + len(cell_specs)
    rules: list[str] = ["body{margin:0;}"]
    measure_css: list[str] = []
    parts: list[str] = []
    for i, spec in enumerate(specs):
        factor, lead, tracking = para_factors(spec, factors)
        k = (local or {}).get(i)
        if k is not None and k < 1.0:
            factor *= k
        rule = _para_rule(spec["family"], spec["base"], spec["lh"],
                          spec["align"], spec["indent"], spec["dir_css"],
                          factor=factor, lead=lead, tracking=tracking)
        rules.append(f"p.s{i} {rule}")
        # 预检用：同一声明换成裸 p 选择器；必须带 font_css（@font-face），
        # 否则 CJK 字宽按兜底字体测量全错。measure_fit_factor 会前置
        # body{margin:1px}——比实际 margin:0 更严，保守方向正确
        measure_css.append(font_css + f" p {rule}")
        inner = spec["html"]
        if inner.startswith("<p>") and inner.endswith("</p>"):
            inner = inner[3:-4]
        brk = "" if len(parts) == n - 1 else " style=page-break-after:always"
        parts.append(f'<p id="p{i}" class="s{i}"{brk}>{inner}</p>')
    for j, cspec in enumerate(cell_specs):
        k = (cell_local or {}).get(j) or 1.0
        rule = _cell_rule(cspec, cell_factors, k=k)
        rules.append(f"p.t{j} {rule}")
        measure_css.append(font_css + f" p {rule}")
        inner = cspec["html"]
        if inner.startswith("<p>") and inner.endswith("</p>"):
            inner = inner[3:-4]
        brk = "" if len(parts) == n - 1 else " style=page-break-after:always"
        parts.append(f'<p id="c{j}" class="t{j}"{brk}>{inner}</p>')
    css = font_css + "".join(rules)
    html = "".join(parts)
    return html, css, measure_css


def _precheck_local(specs: list[dict], measure_css: list[str], archive,
                    floor: float) -> "tuple[dict[int, float] | None, str]":
    """页级预检（v0.8.2 局部收缩版）：每段以其类因子在自身框内粗筛。

    f ≥ 1-ε 通过；f ∈ [floor, 1-ε) 记入局部收缩（×0.998 余量钉死边界，
    与 compute_style_factors 的 _MEASURE_MARGIN 同纪律）；f < floor 返回
    None（整页回退——深缩场景交逐段路径的引擎 scale_low + 告警）。
    """
    local: dict[int, float] = {}
    for i, spec in enumerate(specs):
        f, _ok = measure_fit_factor(
            spec["rect"], spec["html"], measure_css[i], archive,
            f_min=_HARD_FLOOR, f_max=1.0)
        if f >= 1.0 - _STORY_FIT_EPS:
            continue
        if f < floor:
            return None, (f"{spec['tag']} overflow at fit factor "
                          f"{f:.3f} (box {spec['rect'].height:.0f}pt)")
        local[i] = max(f * (1.0 - _MEASURE_MARGIN), floor)
    return local, ""


def _cell_precheck_local(cell_specs: list[dict], font_css: str,
                         archive, cell_factors: "dict | None"
                         ) -> "tuple[dict[int, float], list[dict], list[float], list[dict]]":
    """单元格预检：返回 (局部收缩{新序: k}, 保留 specs, 对应 k 列表, 剔除 specs)。

    单元格是网格锚定的独立排版上下文（基线不与段落共享），deep-shrink
    语义本就 per-cell——f < 0.3 的格剔除交回 insert_htmlbox 深缩路径
    （不拖垮整页），其余按确定性局部因子入 Story。
    """
    local: dict[int, float] = {}
    keep: list[dict] = []
    ks: list[float] = []
    dropped: list[dict] = []
    for cs in cell_specs:
        rule = _cell_rule(cs, cell_factors, k=1.0)
        f, ok = measure_fit_factor(
            cs["rect"], cs["html"], font_css + f" p {rule}", archive,
            f_min=_CELL_SHRINK_FLOOR, f_max=1.0)
        if not ok and f <= _CELL_SHRINK_FLOOR + 1e-9:
            dropped.append(cs)   # 深缩格：剔除（回 insert_htmlbox scale_low=0.3）
            continue
        k = 1.0
        if f < 1.0 - _STORY_FIT_EPS:
            k = max(f * (1.0 - _MEASURE_MARGIN), _CELL_SHRINK_FLOOR)
        local[len(keep)] = k
        keep.append(cs)
        ks.append(k)
    return local, keep, ks, dropped


def _verify_flow(html: str, css: str, boxes: list["pymupdf.Rect"],
                 archive, ids: "list[str] | None" = None) \
        -> "tuple[bool, str]":
    """落墨前预演：place → draw(None) 逐框推进 + element_positions 对账。

    place() 只测量不消耗——story 内部状态由 draw() 推进（write() 的
    dry 模式即 draw(None)：消耗已放置内容、不产生任何输出/墨迹）。
    第 k 框必须恰好包含第 k 项的 open(oc=1) 与 close(oc=2) 事件——
    多出的其他项 id 或缺失 close 都意味着漫延（预检漏网），整页回退。
    """
    story = pymupdf.Story(html=html, user_css=css, archive=archive)
    rect_num = 0
    filled = pymupdf.Rect(0, 0, 0, 0)
    n = len(boxes)
    if ids is None:
        ids = [f"p{i}" for i in range(n)]
    while True:
        if rect_num >= n:
            return False, f"story overflowed all {n} box(es)"
        more, _filled = story.place(pymupdf.Rect(boxes[rect_num]))
        got: list = []
        story.element_positions(lambda pos: got.append(pos))
        story.draw(None, None)     # 消耗已放置内容（无输出设备=零墨迹）
        want = ids[rect_num]
        opens = sum(1 for p in got if p.id == want and getattr(p, "open_close", 0) == 1)
        closes = sum(1 for p in got if p.id == want and getattr(p, "open_close", 0) == 2)
        others = sorted({p.id for p in got
                         if p.id and p.id != want})
        if opens != 1 or closes != 1 or others:
            return False, (f"box {rect_num}: expected exactly {want} "
                           f"(open+close), got opens={opens} closes={closes} "
                           f"others={others[:3]}")
        rect_num += 1
        if not more:
            break
    if rect_num != n:
        return False, f"story ended after {rect_num}/{n} box(es)"
    return True, ""


def try_render_page_story(page, para_specs: list[dict],
                          factors: dict | None, font_css: str,
                          archive, stats: "dict | None" = None,
                          cell_specs: "list[dict] | None" = None,
                          cell_factors: "dict | None" = None,
                          warnings: "list[str] | None" = None
                          ) -> "tuple[bool, list | None]":
    """尝试整页 Story 接管（v0.8.2：段落+单元格一并收编）。

    成功落墨返回 (True, 剩余单元格 specs——深缩剔除的格，调用方走
    insert_htmlbox)；任何失败返回 (False, None)（不落墨，调用方整页
    回退逐段/逐格路径）。

    stats（可选）：{"story": n, "fallback": n, "reasons": [..]} 计数出口。
    warnings（可选）：局部收缩告警出口（与逐段路径的 element-level
    scale 告警对等——装不下必告警不静默）。
    """
    cell_specs = list(cell_specs or [])
    if not para_specs and not cell_specs:
        return True, []           # 无可排项：story 空转即完成
    tag = f"p.{page.number + 1}"
    warn = warnings if warnings is not None else []
    if cell_factors is None:
        cell_factors = factors

    def _fail(reason: str):
        if stats is not None:
            stats["fallback"] = stats.get("fallback", 0) + 1
            rs = stats.setdefault("reasons", [])
            if len(rs) < 6:
                rs.append(f"{tag}: {reason}")
        return False, None

    saved_ov: dict[int, dict | None] = {}
    try:
        # ---- 预检（v0.8.2 局部收缩版）----
        _html, _css, measure_css = build_page_story(
            para_specs, factors, font_css or "", cell_specs=[],
            cell_factors=cell_factors)
        p_local, why = _precheck_local(para_specs, measure_css, archive,
                                       _PARA_SHRINK_FLOOR)
        if p_local is None:
            return _fail(f"precheck failed ({why}); per-paragraph fallback")
        c_local, keep_cells, cell_ks, dropped = _cell_precheck_local(
            cell_specs, font_css or "", archive, cell_factors)
        # 段落局部收缩进 per-spec override（与 fit pass 同机制，回退时还原）
        for i, k in p_local.items():
            spec = para_specs[i]
            saved_ov[i] = spec.get("_fit_override")
            base_f, base_lead, base_track = para_factors(spec, factors)
            spec["_fit_override"] = {"factor": base_f * k,
                                     "lead": base_lead,
                                     "tracking": base_track}
            if k < 0.995:
                warn.append(f"{spec['tag']}: story local scale x{k:.2f} "
                            f"(tight box, unified page story)")
        for cs, k in zip(keep_cells, cell_ks):
            if k < 0.995:
                warn.append(f"{cs['tag']}: story local scale x{k:.2f} "
                            f"(tight cell, unified page story)")

        html, css, _mc2 = build_page_story(
            para_specs, factors, font_css or "", cell_specs=keep_cells,
            cell_factors=cell_factors, local=p_local, cell_local=c_local)
        boxes = [pymupdf.Rect(s["rect"]) for s in para_specs] \
            + [pymupdf.Rect(s["rect"]) for s in keep_cells]
        ids = [f"p{i}" for i in range(len(para_specs))] \
            + [f"c{j}" for j in range(len(keep_cells))]
        ok, why = _verify_flow(html, css, boxes, archive, ids=ids)
        if not ok:
            return _fail(f"flow verify failed ({why}); per-paragraph fallback")
        # 真实落墨：全新 Story（预演实例不复用），临时单页 1:1 贴回
        mediabox = pymupdf.Rect(page.rect)
        story = pymupdf.Story(html=html, user_css=css, archive=archive)

        def rectfn(rect_num: int, filled):
            if rect_num >= len(boxes):
                raise OverflowError("story overflowed boxes at write time")
            r = boxes[rect_num]
            return (mediabox if rect_num == 0 else None), r, None

        doc = story.write_with_links(rectfn)
        try:
            page.show_pdf_page(mediabox, doc, 0, overlay=True)
            # 生成内容无链接；write_with_links 的链接机制保持可用以防万一
            for link in doc[0].get_links():
                page.insert_link(link)
        finally:
            doc.close()
        if stats is not None:
            stats["story"] = stats.get("story", 0) + 1
        return True, dropped
    except Exception as e:                      # noqa: BLE001
        return _fail(f"story render error ({e}); per-paragraph fallback")
    finally:
        # 回退时还原 per-spec override（避免影响逐段路径的因子解析）
        for i, old in saved_ov.items():
            if i < len(para_specs):
                if old is None:
                    para_specs[i].pop("_fit_override", None)
                else:
                    para_specs[i]["_fit_override"] = old
