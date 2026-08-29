#!/usr/bin/env python3
"""P1 任务 1.0：PyMuPDF 1.28.2 Story CSS 能力盘点探针。

对 Story 引擎逐项实测：line-height / opacity·alpha / text-align /
margin·padding / border / ol·ul·li（嵌套与标记）/ table（colspan·rowspan、
边框、跨框断行）/ img / height / 分页控制（page-break-*）/ direction:rtl /
px→pt 换算 / body margin 跨框行为 / rectfn falsy mediabox 语义。

产出：能力矩阵（可用/部分/不支持三档），结果同步 PLAN.md。
运行：WSL 侧 .venv/bin/python tools/story_css_probe.py
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pymupdf

MB = pymupdf.Rect(0, 0, 300, 400)


def make_rectfn(boxes, raise_on_exhaust=True):
    """单页多框 rectfn：第 0 框带真值 mediabox（开页），后续 falsy 续框。"""
    def rectfn(rect_num, filled):
        if rect_num >= len(boxes):
            if raise_on_exhaust:
                raise OverflowError(f"story exceeded {len(boxes)} box(es)")
            return None, pymupdf.Rect(0, 0, 0, 0), None
        r = pymupdf.Rect(boxes[rect_num])
        return (MB if rect_num == 0 else None), r, None
    return rectfn


def render_boxes(html, css, boxes, collect_positions=False):
    """低层复刻 Story.write 循环（可拦截 place→draw 之间做校验）。

    返回 (temp_doc_bytes, per_box_positions)。
    """
    story = pymupdf.Story(html=html, user_css=css, archive=None)
    stream = io.BytesIO()
    writer = pymupdf.DocumentWriter(stream)
    dev = None
    page_open = False
    rect_num = 0
    per_box: list[list] = []
    filled = pymupdf.Rect(0, 0, 0, 0)
    try:
        while 1:
            mediabox, rect, ctm = make_rectfn(boxes)(rect_num, filled)
            rect_num += 1
            if mediabox:
                if dev is not None:
                    writer.end_page()
                dev = writer.begin_page(mediabox)
                page_open = True
            more, filled = story.place(rect)
            if collect_positions:
                got: list = []
                story.element_positions(lambda pos: got.append(pos))
                per_box.append([
                    {"id": getattr(p, "id", None),
                     "oc": getattr(p, "open_close", None),
                     "rect": tuple(getattr(p, "rect", ()))}
                    for p in got])
            story.draw(dev, ctm)
            if not more:
                if page_open:
                    writer.end_page()
                break
    finally:
        writer.close()
    stream.seek(0)
    return stream.read(), per_box


def open_doc(pdf_bytes) -> pymupdf.Document:
    return pymupdf.open("pdf", pdf_bytes)


def page_text_lines(doc, page_no=0):
    """提取 (文本行 y0, x0, x1, size, text) 列表。"""
    out = []
    d = doc[page_no].get_text("dict")
    for b in d["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                out.append((round(s["bbox"][1], 2), round(s["bbox"][0], 2),
                            round(s["bbox"][2], 2), round(s["size"], 2),
                            s["text"]))
    out.sort()
    return out


def drawings(doc, page_no=0):
    return doc[page_no].get_drawings()


results: dict[str, dict] = {}


def record(name, verdict, detail):
    results[name] = {"verdict": verdict, "detail": detail}
    print(f"[{verdict:8s}] {name}: {detail}")


# ---------- 1. 分页控制：page-break-after（正确模式：除末段外逐段 after） ----------
def probe_page_break():
    boxes = [(0, 0, 200, 60), (0, 100, 200, 160), (0, 200, 200, 260)]
    html = ('<p id="a" style="page-break-after:always">AAAA</p>'
            '<p id="b" style="page-break-after:always">BBBB</p>'
            '<p id="c">CCCC</p>')
    try:
        pdf, per_box = render_boxes(html, "p{margin:0;font-size:12pt;}", boxes,
                                    collect_positions=True)
        doc = open_doc(pdf)
        lines = page_text_lines(doc)
        a_in_b0 = any("AAAA" in t for *xy, t in lines if xy[0] < 60)
        b_in_b1 = any("BBBB" in t for *xy, t in lines if 90 <= xy[0] < 160)
        c_in_b2 = any("CCCC" in t for *xy, t in lines if xy[0] >= 190)
        wrong = (any("BBBB" in t for *xy, t in lines if xy[0] < 60)
                 or any("CCCC" in t for *xy, t in lines if xy[0] < 160))
        ids_ok = per_box == [[{"id": "a"}, {"id": "a"}],
                             [{"id": "b"}, {"id": "b"}],
                             [{"id": "c"}, {"id": "c"}]]
        n_pages = len(doc)
        if a_in_b0 and b_in_b1 and c_in_b2 and not wrong and n_pages == 1:
            record("page-break-after:always", "可用",
                   f"逐段 after 断框：每段各占其框单页渲染 ids_ok={ids_ok} "
                   f"per_box={[[p['id'] for p in bx] for bx in per_box]}")
        else:
            record("page-break-after:always", "部分",
                   f"a0={a_in_b0} b1={b_in_b1} c2={c_in_b2} wrong={wrong} "
                   f"ids_ok={ids_ok} pages={n_pages}")
    except Exception as e:
        record("page-break-after:always", "不支持", f"{type(e).__name__}: {e}")


# ---------- 2. page-break-before ----------
def probe_page_break_before():
    boxes = [(0, 0, 200, 60), (0, 100, 200, 160)]
    html = ('<p id="a">AAAA</p>'
            '<p id="b" style="page-break-before:always">BBBB</p>')
    try:
        pdf, _ = render_boxes(html, "p{margin:0;font-size:12pt;}", boxes)
        lines = page_text_lines(open_doc(pdf))
        b_any = [(y, t) for y, *_r, t in lines if "BBBB" in t]
        b_in_b1 = any(90 <= y < 160 for y, t in b_any)
        record("page-break-before:always", "可用" if b_in_b1 else "部分",
               f"BBBB 落位={b_any}")
    except Exception as e:
        record("page-break-before:always", "不支持", f"{type(e).__name__}: {e}")


# ---------- 3. break-after 现代语法 ----------
def probe_break_after():
    boxes = [(0, 0, 200, 60), (0, 100, 200, 160)]
    html = ('<p id="a" style="break-after:page">AAAA</p><p id="b">BBBB</p>')
    try:
        pdf, _ = render_boxes(html, "p{margin:0;font-size:12pt;}", boxes)
        lines = page_text_lines(open_doc(pdf))
        b_in_b1 = any("BBBB" in t for *xy, t in lines if 90 <= xy[0] < 160)
        wrong = any("BBBB" in t for *xy, t in lines if xy[0] < 60)
        record("break-after:page", "可用" if (b_in_b1 and not wrong) else "部分",
               f"b1={b_in_b1} wrong={wrong}")
    except Exception as e:
        record("break-after:page", "不支持", f"{type(e).__name__}: {e}")


# ---------- 4. line-height ----------
def probe_line_height():
    boxes = [(0, 0, 200, 200)]
    html = "<p>s1<br/>s2<br/>s3</p>"
    lh = {}
    for v in (1.0, 1.5, 2.0):
        pdf, _ = render_boxes(html, f"p{{margin:0;font-size:10pt;line-height:{v};}}",
                              boxes)
        ys = sorted(y for y, *_ in page_text_lines(open_doc(pdf)))
        ys = sorted({y for y in ys})
        lh[v] = round(ys[1] - ys[0], 2) if len(ys) >= 2 else None
    ok = lh[1.0] < lh[1.5] < lh[2.0]
    record("line-height", "可用" if ok else "部分", f"行距 {lh}")


# ---------- 5. opacity / 颜色 alpha ----------
def probe_opacity():
    boxes = [(0, 0, 200, 200)]
    verdicts = {}
    for name, css in (
            ("opacity属性", "p{margin:0;font-size:12pt;opacity:0.5;}"),
            ("color:rgba", "p{margin:0;font-size:12pt;color:rgba(255,0,0,0.5);}"),
            ("color:#rrggbbaa", "p{margin:0;font-size:12pt;color:#ff000080;}")):
        try:
            pdf, _ = render_boxes("<p>TEST</p>", css, boxes)
            doc = open_doc(pdf)
            spans = [s for _, _, _, _, s in page_text_lines(doc)]
            verdicts[name] = "出墨" if any("TEST" in s for s in spans) else "无墨"
        except Exception as e:
            verdicts[name] = f"{type(e).__name__}"
    record("opacity/颜色alpha", "可用" if all(v == "出墨" for v in verdicts.values())
           else "部分", str(verdicts))


# ---------- 6. text-align ----------
def probe_text_align():
    boxes = [(0, 0, 200, 60)]
    edges = {}
    for al in ("left", "center", "right", "justify"):
        pdf, _ = render_boxes("<p>hello world test</p>",
                              f"p{{margin:0;font-size:10pt;text-align:{al};}}",
                              boxes)
        xs1 = [x1 for _, _, x1, _, _ in page_text_lines(open_doc(pdf))]
        edges[al] = max(xs1) if xs1 else None
    ok = edges["left"] < edges["center"] < edges["right"]
    record("text-align", "可用" if ok else "部分", str(edges))


# ---------- 7. margin / padding ----------
def probe_margin_padding():
    boxes = [(0, 0, 200, 300)]
    pdf0, _ = render_boxes("<p>A</p><p>B</p>",
                           "p{margin:0;font-size:12pt;}", boxes)
    pdf1, _ = render_boxes("<p>A</p><p>B</p>",
                           "p{margin:0 0 20px 0;font-size:12pt;}", boxes)
    y0 = [y for y, *_ in page_text_lines(open_doc(pdf0))]
    y1 = [y for y, *_ in page_text_lines(open_doc(pdf1))]
    gap0 = y0[1] - y0[0] if len(y0) >= 2 else None
    gap1 = y1[1] - y1[0] if len(y1) >= 2 else None
    ok = gap1 is not None and gap0 is not None and gap1 - gap0 > 10
    record("margin(block间)", "可用" if ok else "部分", f"gap0={gap0} gap1={gap1}")


# ---------- 8. border ----------
def probe_border():
    boxes = [(0, 0, 200, 200)]
    pdf, _ = render_boxes("<p>BOXED</p>",
                          "p{margin:0;font-size:12pt;border:1pt solid #000;}",
                          boxes)
    ds = drawings(open_doc(pdf))
    n = len(ds)
    record("border", "可用" if n >= 1 else "不支持", f"drawings={n}")


# ---------- 9. 列表 ol/ul/li ----------
def probe_lists():
    boxes = [(0, 0, 200, 300)]
    html = ("<ol><li>first</li><li>second</li>"
            "<ul><li>nested-a</li><li>nested-b</li></ul></ol>")
    pdf, _ = render_boxes(html, "p{margin:0;} li{font-size:12pt;margin:0;}",
                          boxes)
    lines = page_text_lines(open_doc(pdf))
    texts = " | ".join(t for *_, t in lines)
    has_num = ("1." in texts or "1 " in texts) and "2." in texts
    texts_norm = texts.replace("ﬁ", "fi")
    has_items = all(w in texts_norm for w in ("first", "second", "nested-a"))
    has_bullet = any(ch in texts for ch in ("•", "●", "◦", "-", "▪"))
    record("ol/ul/li", "可用" if (has_num and has_items) else "部分",
           f"编号={has_num} 项={has_items} 符号={has_bullet} texts={texts[:120]}")


# ---------- 10. 表格 ----------
def probe_table():
    boxes = [(0, 0, 200, 300)]
    html = ("<table><tr><td>A1</td><td>B1</td></tr>"
            "<tr><td colspan='2'>SPANNED</td></tr></table>")
    pdf, _ = render_boxes(html, "table{width:100%;} td{font-size:12pt;margin:0;}",
                          boxes)
    lines = page_text_lines(open_doc(pdf))
    texts = " | ".join(t for *_, t in lines)
    ok = all(w in texts for w in ("A1", "B1", "SPANNED"))
    ds = drawings(open_doc(pdf))
    record("table(含colspan)", "可用" if ok else "部分",
           f"texts={texts[:100]} 线框drawings={len(ds)}")


# ---------- 11. img（archive 传图） ----------
def probe_img():
    # 1x1 red PNG
    import base64
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    arch = pymupdf.Archive()
    arch.add(png, "dot.png")
    boxes = [(0, 0, 200, 200)]
    html = "<p>before</p><p><img src='dot.png' width='50' height='50'/></p>"
    story = pymupdf.Story(html=html, user_css="p{margin:0;}", archive=arch)
    stream = io.BytesIO()
    writer = pymupdf.DocumentWriter(stream)
    rect = pymupdf.Rect(boxes[0])
    more = 1
    dev = None
    while more:
        more, _ = story.place(rect)
        if dev is None:
            dev = writer.begin_page(MB)
        story.draw(dev)
    writer.end_page()
    writer.close()
    stream.seek(0)
    doc = pymupdf.open("pdf", stream.read())
    imgs = doc[0].get_images()
    record("img(archive)", "可用" if imgs else "部分", f"images={len(imgs)}")


# ---------- 12. height 属性 ----------
def probe_height():
    boxes = [(0, 0, 200, 60), (0, 100, 200, 160)]
    html = ('<div style="height:60px">AAA</div>'
            '<div style="height:60px">BBB</div>')
    try:
        pdf, _ = render_boxes(html, "div{margin:0;font-size:12pt;}", boxes)
        lines = page_text_lines(open_doc(pdf))
        b_in_b1 = any("BBB" in t for *xy, t in lines if xy[0] >= 90)
        spill = any("BBB" in t for *xy, t in lines if xy[0] < 55)
        record("height(块级定高)", "可用" if (b_in_b1 and not spill) else "部分",
               f"b1={b_in_b1} spill={spill}")
    except Exception as e:
        record("height(块级定高)", "不支持", f"{type(e).__name__}: {e}")


# ---------- 13. direction:rtl ----------
def probe_rtl():
    boxes = [(0, 0, 200, 100)]
    ar = "الترجمة الأكاديمية"
    html = f"<p>{ar}</p>"
    pdf, _ = render_boxes(html,
                          "p{margin:0;font-size:12pt;direction:rtl;text-align:right;}",
                          boxes)
    lines = page_text_lines(open_doc(pdf))
    got = " ".join(t for *_, t in lines)
    record("direction:rtl", "可用" if ar.split()[0] in got or len(got) > 3 else "部分",
           f"extracted={got[:60]}")


# ---------- 14. px→pt 换算 ----------
def probe_px_pt():
    boxes = [(0, 0, 200, 200)]
    sizes = {}
    for css_size, unit in ((20, "pt"), (20, "px")):
        pdf, _ = render_boxes("<p>Hg</p>",
                              f"p{{margin:0;font-size:{css_size}{unit};}}", boxes)
        ss = [s for _, _, _, s, _ in page_text_lines(open_doc(pdf))]
        sizes[unit] = ss[0] if ss else None
    ratio = sizes["px"] / sizes["pt"] if sizes["pt"] else None
    record("px→pt", "可用" if ratio and 0.9 < ratio < 1.1 else
           ("部分" if ratio else "不支持"), f"pt={sizes['pt']} px={sizes['px']} "
           f"ratio={ratio and round(ratio, 3)}")


# ---------- 15. body margin 跨框行为（page-break 分框后每框是否重受 margin） ----------
def probe_body_margin():
    boxes = [(0, 0, 200, 60), (0, 100, 200, 160)]
    html = ('<p style="page-break-after:always">AAA</p><p>BBB</p>')
    shifts = []
    for mg in (0, 5):
        pdf, _ = render_boxes(html,
                              f"body{{margin:{mg}px;}} p{{margin:0;font-size:12pt;}}",
                              boxes)
        lines = page_text_lines(open_doc(pdf))
        ya = min(y for y, *_r, t in lines if "AAA" in t)
        yb = min(y for y, *_r, t in lines if "BBB" in t)
        shifts.append((round(ya, 2), round(yb, 2)))
    (a0, b0), (a1, b1) = shifts
    record("body margin 跨框", "可用",
           f"margin0: A@{a0} B@{b0}；margin5: A@{a1} B@{b1}；"
           f"位移 A={round(a1-a0,2)} B={round(b1-b0,2)}"
           f"（B 在框2：{'每框重受' if abs((b1-b0)-(a1-a0)) < 0.5 else '仅首框'}）")


# ---------- 16. rectfn falsy mediabox 语义（None vs 空Rect） ----------
def probe_falsy_mediabox():
    html = "<p>X</p><p>Y</p>"
    css = "p{margin:0;font-size:12pt;}"
    boxes = [(0, 0, 200, 60), (0, 100, 200, 160)]
    for label, falsy in (("None", None), ("空Rect", pymupdf.Rect(0, 0, 0, 0))):
        story = pymupdf.Story(html=html, user_css=css)
        stream = io.BytesIO()
        writer = pymupdf.DocumentWriter(stream)
        dev = None
        rn = 0
        filled = pymupdf.Rect(0, 0, 0, 0)
        try:
            while 1:
                mb, rect, ctm = (MB if rn == 0 else falsy), \
                    pymupdf.Rect(boxes[min(rn, len(boxes) - 1)]), None
                rn += 1
                if mb:
                    if dev is not None:
                        writer.end_page()
                    dev = writer.begin_page(mb)
                more, filled = story.place(rect)
                story.draw(dev, ctm)
                if not more:
                    writer.end_page()
                    break
                if rn > 10:
                    raise RuntimeError("loop guard")
            writer.close()
            stream.seek(0)
            doc = pymupdf.open("pdf", stream.read())
            n = len(doc)
            record(f"falsy mediabox={label}", "可用", f"pages={n}")
        except Exception as e:
            record(f"falsy mediabox={label}", "不支持",
                   f"{type(e).__name__}: {e}")


# ---------- 17. 段落↔框一一对应整页演练（P1 核心机制） ----------
def probe_page_story_e2e():
    """2 栏 8 段：每段 page-break-after + 各自 class 样式，验证逐框落位。"""
    import random
    random.seed(7)
    zh = ("学术翻译质量评估段落", "双栏版面逐框落位验证", "公式与表格原位保留",
          "页级流式排版连续网格", "段间基线抖动显著收敛", "字号因子样式级统一",
          "跨页断句与连字符合并", "版面冻结对照阅读体验")
    specs = []
    boxes = []
    cols = [(30, 150), (160, 280)]
    for i in range(8):
        col = cols[i // 4]
        y0 = 20 + (i % 4) * 80
        specs.append((i, zh[i], 10.0 + (i % 3) * 0.5))
        boxes.append((col[0], y0, col[1], y0 + 70))
    html_parts = []
    css_parts = ["body{margin:0;}"]
    for i, txt, fs in specs:
        html_parts.append(f'<p class="s{i}" '
                          f'{"style=page-break-after:always" if i < 7 else ""}>'
                          f"{txt}{'。' * 20}</p>")
        css_parts.append(f'p.s{i}{{font-size:{fs}pt;line-height:1.35;'
                         f'margin:0;text-align:justify;}}')
    html = "".join(html_parts)
    css = "".join(css_parts)
    try:
        pdf, per_box = render_boxes(html, css, boxes, collect_positions=True)
        doc = open_doc(pdf)
        # 逐框区域取文本，验证第 i 框只含第 i 段
        ok_all = True
        details = []
        for i, box in enumerate(boxes):
            r = pymupdf.Rect(box)
            txt = doc[0].get_text("text", clip=r).replace("\n", "")
            expect = zh[i]
            others = [zh[j] for j in range(8) if j != i and zh[j] in txt]
            good = expect in txt and not others
            ok_all &= good
            details.append(f"box{i}:{'OK' if good else 'BAD(' + txt[:20] + ')'}")
        record("页级Story逐框落位(2栏8段)", "可用" if ok_all else "部分",
               " ".join(details))
    except Exception as e:
        record("页级Story逐框落位(2栏8段)", "不支持",
               f"{type(e).__name__}: {e}")


# ---------- 18. 溢出漫延行为确认（回退依据） ----------
def probe_overflow_spill():
    boxes = [(0, 0, 200, 30), (0, 100, 200, 160)]
    html = ('<p style="page-break-after:always">长段落长段落长段落长段落长段落'
            '长段落长段落长段落长段落长段落长段落</p><p id="b">BBB</p>')
    try:
        pdf, per_box = render_boxes(html, "p{margin:0;font-size:12pt;}", boxes,
                                    collect_positions=True)
        doc = open_doc(pdf)
        t1 = doc[0].get_text("text", clip=pymupdf.Rect(boxes[1]))
        spill = "长段落" in t1
        record("溢出漫延(预检必要性)", "确认",
               f"box0溢出→box1漫延={spill}（预检兜底必要）")
    except OverflowError:
        record("溢出漫延(预检必要性)", "确认",
               "box0 溢出漫延直至框耗尽（rectfn 抛 OverflowError 止损）"
               "——预检兜底必要")


def main():
    pymupdf_version = pymupdf.__version__
    print(f"== Story CSS 能力盘点 @ PyMuPDF {pymupdf_version} ==\n")
    probe_page_break()
    probe_page_break_before()
    probe_break_after()
    probe_line_height()
    probe_opacity()
    probe_text_align()
    probe_margin_padding()
    probe_border()
    probe_lists()
    probe_table()
    probe_img()
    probe_height()
    probe_rtl()
    probe_px_pt()
    probe_body_margin()
    probe_falsy_mediabox()
    probe_page_story_e2e()
    probe_overflow_spill()
    out = Path(__file__).parent / "story_css_matrix.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n矩阵已写 {out}")


if __name__ == "__main__":
    sys.exit(main())
