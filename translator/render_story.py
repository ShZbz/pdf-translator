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

from .fit import _HARD_FLOOR, measure_fit_factor

# 预检粗筛阈值：fit_scale 二分精度 ~0.001，恰好装下的段可能测得 0.999x——
# 预检只拦明显溢出（f 显著 < 1）；边界段的精确门在落墨前预演
# （_verify_flow 逐框对账，漫延必被拦截），预检漏网不致串位。
_STORY_FIT_EPS = 0.004


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


def build_page_story(specs: list[dict], factors: dict | None,
                     font_css: str) -> tuple[str, str, list[str]]:
    """specs → (整页 HTML, 整页 CSS, 每段测量用 CSS)。

    HTML：`<p id="pN" class="sN" style=page-break-after:always>…</p>`
    （末段不带断框）；每段内容沿用 collect_para_specs 产出的转义文本。
    CSS：font_css(@font-face) + body{margin:0} + 每段一条 class 规则
    （因子/行距/对齐/缩进/RTL 与逐段路径同源）。
    """
    rules: list[str] = ["body{margin:0;}"]
    measure_css: list[str] = []
    parts: list[str] = []
    n = len(specs)
    for i, spec in enumerate(specs):
        factor, lead, tracking = para_factors(spec, factors)
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
        brk = "" if i == n - 1 else " style=page-break-after:always"
        parts.append(f'<p id="p{i}" class="s{i}"{brk}>{inner}</p>')
    css = font_css + "".join(rules)
    html = "".join(parts)
    return html, css, measure_css


def _precheck(specs: list[dict], measure_css: list[str],
              archive) -> "tuple[bool, str]":
    """页级预检（任务 1.2）：每段以其因子在自身框内粗筛装得下（f ≥ 1-ε）。"""
    for i, spec in enumerate(specs):
        f, _ok = measure_fit_factor(
            spec["rect"], spec["html"], measure_css[i], archive,
            f_min=_HARD_FLOOR, f_max=1.0)
        if f < 1.0 - _STORY_FIT_EPS:
            return False, (f"{spec['tag']} overflow at fit factor "
                           f"{f:.3f} (box {spec['rect'].height:.0f}pt)")
    return True, ""


def _verify_flow(html: str, css: str, boxes: list["pymupdf.Rect"],
                 archive) -> "tuple[bool, str]":
    """落墨前预演：place → draw(None) 逐框推进 + element_positions 对账。

    place() 只测量不消耗——story 内部状态由 draw() 推进（write() 的
    dry 模式即 draw(None)：消耗已放置内容、不产生任何输出/墨迹）。
    第 k 框必须恰好包含第 k 段的 open(oc=1) 与 close(oc=2) 事件——
    多出的其他段 id 或缺失 close 都意味着漫延（预检漏网），整页回退。
    """
    story = pymupdf.Story(html=html, user_css=css, archive=archive)
    rect_num = 0
    filled = pymupdf.Rect(0, 0, 0, 0)
    n = len(boxes)
    while True:
        if rect_num >= n:
            return False, f"story overflowed all {n} box(es)"
        more, _filled = story.place(pymupdf.Rect(boxes[rect_num]))
        got: list = []
        story.element_positions(lambda pos: got.append(pos))
        story.draw(None, None)     # 消耗已放置内容（无输出设备=零墨迹）
        want = f"p{rect_num}"
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
                          archive, stats: "dict | None" = None) -> bool:
    """尝试整页 Story 接管。成功落墨返回 True；任何失败返回 False
    （不落墨，调用方回退逐段 insert_htmlbox 路径）。

    stats（可选）：{"story": n, "fallback": n, "reasons": [..]} 计数出口。
    """
    if not para_specs:
        return True          # 无文字段落：story 空转即完成
    tag = f"p.{page.number + 1}"

    def _fail(reason: str) -> bool:
        if stats is not None:
            stats["fallback"] = stats.get("fallback", 0) + 1
            rs = stats.setdefault("reasons", [])
            if len(rs) < 6:
                rs.append(f"{tag}: {reason}")
        return False

    try:
        html, css, measure_css = build_page_story(para_specs, factors,
                                                  font_css or "")
        boxes = [pymupdf.Rect(s["rect"]) for s in para_specs]
        ok, why = _precheck(para_specs, measure_css, archive)
        if not ok:
            return _fail(f"precheck failed ({why}); per-paragraph fallback")
        ok, why = _verify_flow(html, css, boxes, archive)
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
        return True
    except Exception as e:                      # noqa: BLE001
        return _fail(f"story render error ({e}); per-paragraph fallback")
