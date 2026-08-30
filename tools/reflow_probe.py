#!/usr/bin/env python3
"""P3 任务 3.x 前置探针：Story 整文档流式写入的引擎能力实测（不猜 API）。

逐项实测：
- 多页 write 循环：rectfn 首框真值 mediabox 开新页，falsy 续框；
  element_positions 坐标是否页相对（书签/页码记录依据）
- <img> 宽度样式与纵横比（Archive 传内存 PNG bytes）
- page-break-inside:avoid（图+图注 keep-together 依据）
- <table> 行跨框行为（行拆断 vs 整行移框；高置信表 HTML 重排依据）
- ol/ul 文流内编号/符号（列表语义化依据）

产出：PASS/FAIL 结论（stdout），结果记入 PLAN.md 能力矩阵。
运行：WSL 侧 .venv/bin/python tools/reflow_probe.py
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MB = pymupdf.Rect(0, 0, 300, 400)      # 单栏模板页
results = []


def check(name, ok, detail):
    results.append((name, "PASS" if ok else "FAIL", detail))


def flow_write(html, css, rectfn, archive=None, collect_ids=False):
    """自研 write 循环（与 Story.write 同构，可拦截 place 做记录）。

    返回 (doc_bytes, positions)；positions = [(page_no, id, oc, rect)]。
    """
    story = pymupdf.Story(html=html, user_css=css, archive=archive)
    stream = io.BytesIO()
    writer = pymupdf.DocumentWriter(stream)
    dev = None
    page_no = -1
    rect_num = 0
    filled = pymupdf.Rect(0, 0, 0, 0)
    positions = []
    while 1:
        mediabox, rect, ctm = rectfn(rect_num, filled)
        rect_num += 1
        if mediabox:
            if dev is not None:
                writer.end_page()
            page_no += 1
            dev = writer.begin_page(mediabox)
        more, filled = story.place(rect)
        if collect_ids:
            got = []
            story.element_positions(lambda p: got.append(p))
            for p in got:
                positions.append((page_no, getattr(p, "id", None),
                                  getattr(p, "open_close", None),
                                  tuple(getattr(p, "rect", ()))))
        story.draw(dev, ctm)
        if not more:
            if dev is not None:
                writer.end_page()
            break
    writer.close()
    stream.seek(0)
    return stream.read(), positions


def make_png(w_px=200, h_px=100, color=(0.8, 0.1, 0.1)):
    doc = pymupdf.open()
    pg = doc.new_page(width=w_px, height=h_px)
    pg.draw_rect(pymupdf.Rect(0, 0, w_px, h_px), color=color, fill=color)
    pix = pg.get_pixmap(dpi=72)
    doc.close()
    return pix.tobytes("png")


def main():
    font_path = None
    try:
        from translator.render import find_cjk_font
        font_path = find_cjk_font(None, lang="zh")
    except Exception:
        pass
    font_css = ""
    archive = None
    if font_path:
        from translator.render import _build_font_archive
        archive, font_css = _build_font_archive(font_path, None)

    # --- ① 多页 write 循环 + element_positions 页相对坐标 ---
    cols = 1
    per_page = 400 // 40 - 1                      # 9 框/页（每框 40pt）
    def rectfn_mp(rect_num, filled):
        page = rect_num // 8
        col = rect_num % 8
        r = pymupdf.Rect(30, 20 + col * 45, 280, 20 + col * 45 + 42)
        return (pymupdf.Rect(MB) if col == 0 else None), r, None
    paras = [f'<p id="p{i}">第{i}段落内容，用于验证多页流式写入的段落落位。</p>'
             for i in range(20)]
    html_mp = font_css + "".join(paras)
    css_mp = font_css + " p{font-family:ptbody,serif;font-size:10pt;line-height:1.3;margin:0;}"
    data, positions = flow_write(html_mp, css_mp, rectfn_mp,
                                 archive=archive, collect_ids=True)
    doc = pymupdf.open("pdf", data)
    n_pages = len(doc)
    all_text = "".join(doc[i].get_text("text") for i in range(n_pages))
    complete = all(f"第{i}段落" in all_text for i in range(20))
    check("multipage-write", n_pages >= 2 and complete,
          f"pages={n_pages} all_paras_present={complete}")
    # 页相对坐标：同 id 的 open 事件 rect 应落在各页框内
    opens = [(pn, rid, rc) for pn, rid, oc, rc in positions
             if rid and oc == 1]
    in_page = all(0 <= rc[0] < 300 and 0 <= rc[1] < 400 for _pn, _rid, rc in opens)
    ids_page1 = {rid for pn, rid, _rc in opens if pn == 1}
    check("element-positions-page-relative", in_page and bool(ids_page1),
          f"opens={len(opens)} in_page_bounds={in_page} "
          f"page1_ids={sorted(ids_page1)[:4]}")
    doc.close()

    # --- ② <img> 宽度样式 + 纵横比（Archive 传 bytes） ---
    png = make_png(200, 100)                       # 2:1 纵横比
    arch2 = pymupdf.Archive()
    arch2.add(png, "fig1.png")
    def rectfn_1p(rect_num, filled):
        return pymupdf.Rect(MB), pymupdf.Rect(20, 20, 280, 380), None
    html_img = ('<p class=f><img src="fig1.png" style="width:120pt"></p>')
    css_img = " p.f{text-align:center;margin:0;}"
    data2, _ = flow_write(html_img, css_img, rectfn_1p, archive=arch2)
    doc2 = pymupdf.open("pdf", data2)
    imgs = doc2[0].get_images(full=True)
    rects = doc2[0].get_image_rects(imgs[0][0]) if imgs else []
    if rects:
        r = rects[0]
        ratio = r.width / max(r.height, 0.1)
        check("img-width-style", abs(r.width - 120) < 6 and abs(ratio - 2.0) < 0.2,
              f"rect={r.width:.1f}x{r.height:.1f} ratio={ratio:.2f} (want 120pt, 2:1)")
    else:
        check("img-width-style", False, f"no image rendered; imgs={len(imgs)}")
    doc2.close()

    # --- ③ page-break-inside:avoid（图+注 keep-together） ---
    # 两框页：框A 余量不足以放整个 图+注 块 → 应整体移到框B
    def rectfn_2f(rect_num, filled):
        r = (pymupdf.Rect(20, 20, 280, 120) if rect_num % 2 == 0
             else pymupdf.Rect(20, 140, 280, 380))
        return (pymupdf.Rect(MB) if rect_num % 2 == 0 else None), r, None
    html_fig = ('<div class="fig"><p class=f><img src="fig1.png" '
                'style="width:200pt"></p>'
                '<p class=cap>图1：这是绑定图注文字。</p></div>'
                '<p>前置引导段落。</p>')
    css_fig = (" p{text-align:justify;margin:0;font-size:10pt;}"
               " p.cap{font-size:8pt;text-align:center;}"
               " div.fig{page-break-inside:avoid;}")
    data3, pos3 = flow_write(html_fig, css_fig, rectfn_2f, archive=arch2,
                             collect_ids=True)
    doc3 = pymupdf.open("pdf", data3)
    t0 = doc3[0].get_text("text")
    t1 = doc3[1].get_text("text") if len(doc3) > 1 else ""
    # 引导段在前框；图注不应被拆（图与注同框）
    fig_first = "图1" in t0
    cap_with_fig = ("图1" in t0 and "绑定图注文字" in t0) or \
                   ("图1" in t1 and "绑定图注文字" in t1)
    check("page-break-inside-avoid", cap_with_fig,
          f"p0_has_fig={fig_first} cap_same_page_as_fig={cap_with_fig}")
    doc3.close()

    # --- ④ <table> 行跨框行为 ---
    rows = "".join(f"<tr><td>行{i}左格内容</td><td>行{i}右格</td></tr>"
                   for i in range(10))
    html_tab = f"<table>{rows}</table>"
    css_tab = font_css + (" table{width:100%;margin:0;}"
                          " td{font-family:ptbody,serif;font-size:10pt;"
                          "margin:0;}")
    data4, _ = flow_write(html_tab, css_tab, rectfn_2f, archive=archive)
    doc4 = pymupdf.open("pdf", data4)
    spans = []
    for pgno in range(len(doc4)):
        for b in doc4[pgno].get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for l in b.get("lines", []):
                for sp in l["spans"]:
                    spans.append((pgno, pymupdf.Rect(sp["bbox"]),
                                  sp["text"].strip()))
    torn = 0
    for i in range(10):
        left = [s for s in spans if s[2] == f"行{i}左格内容"]
        right = [s for s in spans if s[2] == f"行{i}右格"]
        if not left or not right or left[0][0] != right[0][0] \
                or abs(left[0][1].y0 - right[0][1].y0) > 2:
            torn += 1
    check("table-rows-intact-across-frames", torn == 0,
          f"rows_torn={torn}/10 pages={len(doc4)}")
    doc4.close()

    # --- ⑤ ol/ul 文流内 ---
    html_ol = ('<ol><li>第一项列表内容</li><li>第二项列表内容</li></ol>'
               '<ul><li>圆点项甲</li><li>圆点项乙</li></ul>')
    css_ol = font_css + " li{font-family:ptbody,serif;font-size:10pt;margin:0;}"
    data5, _ = flow_write(html_ol, css_ol, rectfn_1p, archive=archive)
    doc5 = pymupdf.open("pdf", data5)
    t5 = doc5[0].get_text("text")
    ok_ol = ("1." in t5 or "1、" in t5) and "第二项列表内容" in t5 \
        and "圆点项甲" in t5
    check("ol-ul-in-flow", ok_ol, f"text_sample={t5[:60]!r}")
    doc5.close()

    print("=" * 64)
    for name, verdict, detail in results:
        print(f"[{verdict}] {name}: {detail}")
    n_fail = sum(1 for _n, v, _d in results if v == "FAIL")
    print("=" * 64)
    print(f"{len(results) - n_fail}/{len(results)} passed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
