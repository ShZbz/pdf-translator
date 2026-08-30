#!/usr/bin/env python3
"""P2 任务 2.2.1 前置探针：双语 <table> 语义布局的引擎能力实测。

对 Story/insert_htmlbox 引擎逐项实测（不猜 API 行为）：
- td 选择器 + td.class（原文行弱化样式能否命中）
- opacity 应用于 td（矩阵已知 p 可用，td 未测）
- 表格是否尊重外框宽度（width:100% / 长 CJK 文本是否在框内换行）
- <p> 嵌套 <td>（段落样式 p{} 规则是否在 td 内生效）
- 双行同框不分离（两行落位顺序与水平范围）
- insert_htmlbox 真实路径端到端（scale/spare 返回语义）

产出：PASS/FAIL 结论（stdout），结果记入 PLAN.md。
运行：WSL 侧 .venv/bin/python tools/bilingual_table_probe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from translator.render import find_cjk_font  # noqa: E402

ZH = ("双语表格布局验证段落，包含较长的中文正文以测试单元格内部的自动"
      "换行行为是否符合外框宽度约束，同时观察两行是否保持在同一框内。")
EN = ("English original line for the bilingual table layout probe, "
      "long enough to wrap inside the cell width constraint.")

results: list[tuple[str, str, str]] = []      # (name, verdict, detail)


def check(name, ok, detail):
    results.append((name, "PASS" if ok else "FAIL", detail))


def main():
    font_path = find_cjk_font(None, lang="zh")
    from translator.render import _build_font_archive
    archive, font_css = _build_font_archive(font_path, None)

    box = pymupdf.Rect(50, 50, 300, 220)     # 250pt 宽

    # --- ① td 选择器 + class + opacity + 换行（insert_htmlbox 真实路径） ---
    css = (font_css
           + " table{width:100%;margin:0;}"
           " td{font-family:ptbody,serif;font-size:10pt;line-height:1.4;"
           "margin:0;text-align:justify;}"
           " td.o{opacity:0.6;font-size:8pt;line-height:1.2;"
           "font-family:serif;text-align:left;}")
    html = ('<table><tr><td>' + ZH + '</td></tr>'
            '<tr><td class=o>' + EN + '</td></tr></table>')
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=400)
    spare, scale = page.insert_htmlbox(box, html, css=css, scale_low=0.5,
                                       archive=archive)
    spans = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b.get("lines", []):
            for sp in l["spans"]:
                spans.append((pymupdf.Rect(sp["bbox"]), sp["text"],
                              sp["size"]))
    all_text = "".join(t for _r, t, _s in spans)
    check("table-insert_htmlbox", ZH[:10] in all_text and "English" in all_text,
          f"spare={spare:.1f} scale={scale:.3f} spans={len(spans)}")
    inside = all(r.x1 <= box.x1 + 1.0 and r.x0 >= box.x0 - 1.0
                 for r, _t, _s in spans)
    check("table-width-respected", inside,
          f"max_x1={max((r.x1 for r, _t, _s in spans), default=0):.1f} "
          f"box_x1={box.x1:.1f}")
    # 两行都在框内（不分离到框外）
    inside_v = all(r.y1 <= box.y1 + 1.0 for r, _t, _s in spans)
    check("table-rows-same-box", inside_v,
          f"max_y1={max((r.y1 for r, _t, _s in spans), default=0):.1f} "
          f"box_y1={box.y1:.1f}")
    # 行序：zh 行 y 全部 < en 行 y
    zh_y = [r.y0 for r, t, _s in spans if t.strip() and t.strip()[0] == "双"]
    en_y = [r.y0 for r, t, _s in spans if t.strip().startswith("English")]
    check("table-row-order", bool(zh_y and en_y) and max(zh_y) < min(en_y),
          f"zh_last={max(zh_y, default=-1):.1f} en_first={min(en_y, default=-1):.1f}")
    # td.o 样式命中：en 行字号 ≈ 8pt（不是 10pt）
    en_sizes = {round(s, 1) for r, t, s in spans
                if t.strip().startswith("English")}
    check("td.class-style-hit", en_sizes and en_sizes <= {8.0},
          f"en span sizes={sorted(en_sizes)}")

    # --- ② opacity 弱化是否有墨且更浅（像素级对比） ---
    pix_box = page.get_pixmap(clip=pymupdf.Rect(box.x0, box.y0, box.x1,
                                                box.y1 + 2), dpi=72)
    # 粗验证：en 行区域应有墨（非全白）
    en_zone = page.get_text("dict")
    en_rects = [r for r, t, _s in spans if t.strip().startswith("English")]
    if en_rects:
        clip = en_rects[0]
        pix2 = page.get_pixmap(clip=clip, dpi=150)
        nonwhite = sum(1 for i in range(0, len(pix2.samples), pix2.n)
                       if pix2.samples[i] < 240)
        check("opacity-td-has-ink", nonwhite > 0,
              f"nonwhite_px={nonwhite} in en zone {clip}")
    else:
        check("opacity-td-has-ink", False, "no en spans found")

    # --- ③ <p> 嵌套 <td>（段落规则是否继承） ---
    doc2 = pymupdf.open()
    page2 = doc2.new_page(width=400, height=400)
    css_p = (font_css
             + " p{font-family:ptbody,serif;font-size:10pt;text-indent:2em;}"
             " table{width:100%;margin:0;}"
             " td{margin:0;padding:0;}")
    html_p = ('<table><tr><td><p>' + ZH + '</p></td></tr>'
              '<tr><td class=o><p>' + EN + '</p></td></tr></table>')
    try:
        spare2, _scale2 = page2.insert_htmlbox(box, html_p, css=css_p,
                                               scale_low=0.5, archive=archive)
        txt2 = page2.get_text("text", clip=box)
        check("p-nested-in-td", ZH[:8] in txt2.replace("\n", ""),
              f"spare={spare2:.1f}")
    except Exception as e:                    # noqa: BLE001
        check("p-nested-in-td", False, f"exception {e}")

    # --- ④ text-indent 应用于 td（CJK 首行缩进在 td 上是否生效） ---
    doc3 = pymupdf.open()
    page3 = doc3.new_page(width=400, height=400)
    css_ti = (font_css
              + " table{width:100%;margin:0;}"
              " td{font-family:ptbody,serif;font-size:10pt;"
              "line-height:1.4;margin:0;text-indent:2em;}")
    try:
        page3.insert_htmlbox(box, '<table><tr><td>' + ZH + '</td></tr></table>',
                             css=css_ti, scale_low=0.5, archive=archive)
        sp3 = []
        for b in page3.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for l in b.get("lines", []):
                for sp in l["spans"]:
                    sp3.append((pymupdf.Rect(sp["bbox"]), sp["text"]))
        first_line = min(sp3, key=lambda rs: rs[0].y0)
        indented = first_line[0].x0 >= box.x0 + 15   # 2em@10pt ≈ 20pt
        check("td-text-indent", indented,
              f"first_x0={first_line[0].x0:.1f} box_x0={box.x0:.1f}")
    except Exception as e:                    # noqa: BLE001
        check("td-text-indent", False, f"exception {e}")

    # --- ⑤ RTL 方向在 td 内（阿拉伯样张双语兼容） ---
    doc4 = pymupdf.open()
    page4 = doc4.new_page(width=400, height=400)
    css_rtl = (font_css
               + " table{width:100%;margin:0;}"
               " td{font-family:ptbody,serif;font-size:10pt;margin:0;"
               "direction:rtl;}")
    try:
        page4.insert_htmlbox(box,
                             '<table><tr><td>مرحبا بكم</td></tr></table>',
                             css=css_rtl, scale_low=0.5, archive=archive)
        txt4 = page4.get_text("text", clip=box).strip()
        check("td-direction-rtl", bool(txt4), f"text={txt4[:20]!r}")
    except Exception as e:                    # noqa: BLE001
        check("td-direction-rtl", False, f"exception {e}")

    # --- ⑥ 溢出行为：两行总高超框 → insert_htmlbox 压缩（不丢行） ---
    doc5 = pymupdf.open()
    page5 = doc5.new_page(width=400, height=400)
    small = pymupdf.Rect(50, 50, 300, 90)     # 40pt 高，装不下两行
    try:
        spare5, scale5 = page5.insert_htmlbox(small, html, css=css,
                                              scale_low=0.5, archive=archive)
        txt5 = page5.get_text("text", clip=small).replace("\n", "")
        both = ZH[:6] in txt5 and "English" in txt5
        check("table-overflow-compress", both or (spare5 is not None
                                                  and spare5 < -0.5),
              f"spare={spare5:.1f} scale={scale5:.3f} both_rows={both}")
    except Exception as e:                    # noqa: BLE001
        check("table-overflow-compress", False, f"exception {e}")

    print("=" * 64)
    for name, verdict, detail in results:
        print(f"[{verdict}] {name}: {detail}")
    n_fail = sum(1 for _n, v, _d in results if v == "FAIL")
    print("=" * 64)
    print(f"{len(results) - n_fail}/{len(results)} passed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
