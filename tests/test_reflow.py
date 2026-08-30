"""v0.8.0 P3 验收单测：reflow 整文档重排（任务 3.1-3.5）。

- 模板统计（栏数沿用/边距/帧几何）
- 文档模型（书签流序/列表语义化/图注绑定/跨页合并整段）
- 端到端流式写入（多页/页码/书签/位图守恒/全文本）
- config 校验（reflow 接受/双语互斥/非法值报错）+ 输出命名
零网络（无 LLM 调用）。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_v080 import _cjk_font_or_skip, _make_pdf  # noqa: E402


def _typo_zh():
    from translator.typography import Typography
    return Typography({"body": "", "cjk": ""}, "zh")


def _two_col_layouts():
    """合成两页双栏 layout：p0 标题+正文+列表项；p1 正文+公式+图+表。"""
    r = lambda x0, y0, x1, y1: pymupdf.Rect(x0, y0, x1, y1)
    p0 = [
        {"index": 0, "bbox": r(60, 40, 550, 70), "col": 0,
         "text": "Probabilistic Lane Graph Generation Study", "size": 16.0,
         "spans": [], "is_heading": True, "is_caption": False,
         "is_ref": False, "is_verbatim": False, "is_alg_caption": False,
         "is_list_item": False},
    ]
    body0 = [
        ("本文研究概率车道图的生成方法与边缘案例解释框架。", 60, 90),
        ("第二段阐述双栏文档的语义重排阅读序组装策略。", 60, 150),
        ("第三段说明模板系统如何沿用原文档的页面统计。", 320, 90),
    ]
    for j, (t, x, y) in enumerate(body0):
        p0.append({"index": len(p0), "bbox": r(x, y, x + 220, y + 44),
                   "col": 0 if x < 300 else 1, "text": f"Body {j} text.",
                   "size": 10.0, "spans": [], "is_heading": False,
                   "is_caption": False, "is_ref": False,
                   "is_verbatim": False, "is_alg_caption": False,
                   "is_list_item": False})
    for j, t in enumerate(["1. 第一项列表内容说明",
                           "2. 第二项列表内容说明"]):
        p0.append({"index": len(p0), "bbox": r(60, 250 + j * 16,
                                               280, 250 + j * 16 + 14),
                   "col": 0, "text": t, "size": 10.0, "spans": [],
                   "is_heading": False, "is_caption": False,
                   "is_ref": False, "is_verbatim": False,
                   "is_alg_caption": False, "is_list_item": True})
    # 大量正文保证跨页流式断页（模板 2 栏，每栏 ~600pt 高）
    for j in range(40):
        y = 290 + (j % 12) * 16
        x = 60 if j % 24 < 12 else 320
        p0.append({"index": len(p0),
                   "bbox": r(x, y, x + 220, y + 14),
                   "col": 0 if x < 300 else 1,
                   "text": f"Filler paragraph {j} for pagination.",
                   "size": 10.0, "spans": [], "is_heading": False,
                   "is_caption": False, "is_ref": False,
                   "is_verbatim": False, "is_alg_caption": False,
                   "is_list_item": False})
    fig_rect = r(320, 150, 540, 260)
    p1 = [
        {"index": 0, "bbox": r(60, 40, 280, 84), "col": 0,
         "text": "Fourth paragraph continues the discussion of reflow.",
         "size": 10.0, "spans": [], "is_heading": False,
         "is_caption": False, "is_ref": False, "is_verbatim": False,
         "is_alg_caption": False, "is_list_item": False},
        {"index": 1, "bbox": r(322, 264, 540, 282), "col": 1,
         "text": "Fig. 1: A corner case generated using PLG.", "size": 8.5,
         "spans": [], "is_heading": False, "is_caption": True,
         "is_ref": False, "is_verbatim": False, "is_alg_caption": False,
         "is_list_item": False},
        {"index": 2, "bbox": r(60, 120, 280, 164), "col": 0,
         "text": "Fifth paragraph about the table rendering path.",
         "size": 10.0, "spans": [], "is_heading": False,
         "is_caption": False, "is_ref": False, "is_verbatim": False,
         "is_alg_caption": False, "is_list_item": False},
    ]
    cells = [
        {"bbox": r(60, 300, 160, 316), "text": "Method", "conf": 0.9,
         "spans": []},
        {"bbox": r(160, 300, 280, 316), "text": "Score", "conf": 0.9,
         "spans": []},
        {"bbox": r(60, 316, 160, 332), "text": "PLG", "conf": 0.9,
         "spans": []},
        {"bbox": r(160, 316, 280, 332), "text": "0.91", "conf": 0.9,
         "spans": []},
    ]
    lay0 = {"mode": "two", "paragraphs": p0, "tables": [], "tables_cells": [],
            "formulas": [], "hf_blocks": [], "fig_text_blocks": [],
            "figure_regions": [], "layout_engine": "heuristic"}
    lay1 = {"mode": "two", "paragraphs": p1,
            "tables": [{"bbox": r(60, 300, 280, 332), "cells": cells}],
            "tables_cells": cells,
            "formulas": [{"bbox": r(320, 320, 540, 350),
                          "para_hint_y": 320, "is_display": True}],
            "hf_blocks": [], "fig_text_blocks": [],
            "figure_regions": [fig_rect], "layout_engine": "heuristic"}
    return [lay0, lay1]


def _texts_for(lays, prefix="译文明细"):
    # 列表项译文保留原文形态（编号前缀不被翻译打断——真实译文如此）
    return {pno: {i: (p["text"] if p.get("is_list_item")
                      else f"{prefix}{p['text']}")
                  for i, p in enumerate(lay["paragraphs"])}
            for pno, lay in enumerate(lays)}


def test_reflow_template_and_model():
    """模板统计 + 文档模型：栏数/书签流序/列表分组/图注绑定。"""
    from translator.render_reflow import build_document_model, build_template
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    doc.new_page(width=612, height=792)
    lays = _two_col_layouts()
    texts = _texts_for(lays, "译")
    tpl = build_template(lays, doc, columns="auto")
    assert len(tpl.cols) == 2, f"two-col template expected: {tpl.cols}"
    assert 20 <= tpl.margin_top <= 100 and tpl.page_w == 612
    # 帧几何：col0 携带真值 mediabox（自动断页），col1 falsy
    mb, rect = tpl.frame(0)
    assert mb is not None and rect.width > 100
    mb1, rect1 = tpl.frame(1)
    assert mb1 is None and abs(rect1.y0 - rect.y0) < 0.01
    typo = _typo_zh()
    blocks, images, bookmarks, _ls = build_document_model(
        lays, doc, texts, {(1, 0): "0.91", (1, 1): "0.91"}, {}, set(),
        {}, typo)
    kinds = [b.kind for b in blocks]
    assert "list" in kinds and "figure" in kinds and "formula" in kinds \
        and "table" in kinds, kinds
    # 书签流序：标题第一（右栏标题不能排到节标题后——流序推导）
    assert bookmarks and bookmarks[0]["level"] == 1 \
        and "Probab" in bookmarks[0]["text"], bookmarks
    # 列表语义化：编号剥除、合成 <ol>
    lb = next(b for b in blocks if b.kind == "list")
    assert lb.html_extra.startswith("<ol>") and "1." not in lb.html_extra
    # 图注绑定：图注译文进 figure.caption 且不重复出现为段落
    fig = next(b for b in blocks if b.kind == "figure")
    assert fig.caption and "PLG" in fig.caption, fig.caption
    assert not any(b.kind == "para" and "Fig. 1" in b.text for b in blocks)
    # 表格高置信 → HTML 重排（格译文经 cell_texts）
    tab = next(b for b in blocks if b.kind == "table")
    assert tab.html_extra and "0.91" in tab.html_extra
    doc.close()


def test_reflow_render_e2e():
    """端到端：文档模型 → 流式写入 → 多页输出/页码/书签/位图/全文本。"""
    from translator.render import find_cjk_font
    from translator.render_reflow import render_reflow_document
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    doc.new_page(width=612, height=792)
    lays = _two_col_layouts()
    texts = _texts_for(lays)
    typo = _typo_zh()
    # 公式位图（与 faithful 同源的预裁复用路径）
    fdoc = pymupdf.open()
    fp = fdoc.new_page(width=100, height=30)
    fp.draw_rect(pymupdf.Rect(0, 0, 100, 30), color=(0, 0, 0),
                 fill=(0.9, 0.9, 0.9))
    fpng = fp.get_pixmap(dpi=72).tobytes("png")
    fdoc.close()
    reflow_cfg = types.SimpleNamespace(columns="auto", body_size=0,
                                       segment_blocks=500)
    warns: list = []
    data = render_reflow_document(
        lays, doc, texts, {(1, 0): "0.91", (1, 1): "0.91"}, {}, set(),
        {1: {0: fpng}}, typo, font_path, "zh", reflow_cfg, warns,
        log=lambda m: None)
    assert warns == [], warns
    out = pymupdf.open("pdf", data)
    assert len(out) >= 2, f"expect multi-page output, got {len(out)}"
    all_text = "".join(out[i].get_text("text") for i in range(len(out)))
    compact = all_text.replace(" ", "").replace("\n", "")
    for probe in ("译文明细Body0text.", "译文明细Fifthparagraph",
                  "第一项列表内容", "0.91", "cornercase",
                  "Fillerparagraph39"):
        assert probe in compact, f"missing {probe!r}"
    # 全部 filler 段落落位（跨页断页不丢块）
    for j in (0, 17, 39):
        assert f"Fillerparagraph{j}" in compact, f"filler {j} lost"
    toc = out.get_toc()
    assert toc and toc[0][0] == 1, toc
    # 位图守恒：公式+图全部随文流
    n_img = sum(len(out[i].get_images(full=True)) for i in range(len(out)))
    assert n_img >= 2, f"bitmaps lost: {n_img}"
    out.close()
    doc.close()


def test_reflow_cross_page_merge_whole():
    """跨页合并单元：reflow 整段入流（A 位置全量译文，B 段不重复）。"""
    from translator.render_reflow import build_document_model
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    doc.new_page(width=612, height=792)
    r = lambda x0, y0, x1, y1: pymupdf.Rect(x0, y0, x1, y1)

    def para(i, x, y, t):
        return {"index": i, "bbox": r(x, y, x + 220, y + 44),
                "col": 0 if x < 300 else 1, "text": t, "size": 10.0,
                "spans": [], "is_heading": False, "is_caption": False,
                "is_ref": False, "is_verbatim": False,
                "is_alg_caption": False, "is_list_item": False}

    lays = [
        {"mode": "one",
         "paragraphs": [para(0, 60, 40, "Tail sentence on page one.")],
         "tables": [], "tables_cells": [], "formulas": [], "hf_blocks": [],
         "fig_text_blocks": [], "figure_regions": [],
         "layout_engine": "heuristic"},
        {"mode": "one",
         "paragraphs": [para(0, 60, 40, "Head sentence on page two.")],
         "tables": [], "tables_cells": [], "formulas": [], "hf_blocks": [],
         "fig_text_blocks": [], "figure_regions": [],
         "layout_engine": "heuristic"},
    ]
    texts = {0: {0: "页一末句译文的_B_部分。"}, 1: {0: "页一首句的B部分。"}}
    cross_full = {(0, 0): "页一末句与页一首句合并后的完整译文段落。"}
    cross_skip = {(1, 0)}
    blocks, _images, _bm, _ls = build_document_model(
        lays, doc, texts, {}, cross_full, cross_skip, {}, _typo_zh())
    paras = [b.text for b in blocks if b.kind == "para"]
    assert paras == ["页一末句与页一首句合并后的完整译文段落。"], paras
    doc.close()


def test_config_reflow_mode(tmp_path):
    """output.mode: reflow 校验——接受/双语互斥/非法值报错。"""
    from translator.config import load_config
    src = _make_pdf(tmp_path)
    y = tmp_path / "c.yaml"
    y.write_text(f"""
io:
  input: {src.as_posix()}
output:
  mode: reflow
reflow:
  columns: single
  body_size: 10.5
""", encoding="utf-8")
    cfg = load_config(y)
    assert cfg.output.mode == "reflow"
    assert cfg.reflow.columns == "single"
    assert abs(cfg.reflow.body_size - 10.5) < 1e-6
    # 双语互斥
    y2 = tmp_path / "c2.yaml"
    y2.write_text(f"""
io:
  input: {src.as_posix()}
output:
  mode: reflow
features:
  bilingual: true
""", encoding="utf-8")
    with pytest.raises(ValueError, match="双语"):
        load_config(y2)
    # 非法栏结构
    y3 = tmp_path / "c3.yaml"
    y3.write_text(f"""
io:
  input: {src.as_posix()}
reflow:
  columns: wide
""", encoding="utf-8")
    with pytest.raises(ValueError, match="columns"):
        load_config(y3)
    # 非法 mode
    y4 = tmp_path / "c4.yaml"
    y4.write_text(f"""
io:
  input: {src.as_posix()}
output:
  mode: linear
""", encoding="utf-8")
    with pytest.raises(ValueError, match="output.mode"):
        load_config(y4)


def test_reflow_output_name():
    from translator.pipeline import output_pdf_name
    assert output_pdf_name("paper", "zh", False, extra="-reflow") == \
        "paper-reflow-Zh.pdf"


def test_reflow_tall_table_scaled():
    """超页高整表（低置信→位图路径）：按高度比例缩宽，不炸不越界。"""
    from translator.render_reflow import render_reflow_document
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    r = lambda x0, y0, x1, y1: pymupdf.Rect(x0, y0, x1, y1)
    tall = r(60, 40, 550, 1400)          # 高 1360pt > 页高 792
    cells = [{"bbox": r(70, 50 + k * 90, 540, 120 + k * 90),
              "text": f"cell {k}", "conf": 0.3,   # 低置信 → 整表位图
              "spans": []} for k in range(15)]
    lay = {"mode": "one",
           "paragraphs": [
               {"index": 0, "bbox": r(60, 40, 550, 60), "col": 0,
                "text": "Intro para before the tall table.",
                "size": 10.0, "spans": [], "is_heading": False,
                "is_caption": False, "is_ref": False,
                "is_verbatim": False, "is_alg_caption": False,
                "is_list_item": False}],
           "tables": [{"bbox": tall, "cells": cells}],
           "tables_cells": cells, "formulas": [], "hf_blocks": [],
           "fig_text_blocks": [], "figure_regions": [],
           "layout_engine": "heuristic"}
    typo = _typo_zh()
    reflow_cfg = types.SimpleNamespace(columns="single", body_size=0,
                                       segment_blocks=500)
    warns: list = []
    data = render_reflow_document(
        [lay], doc, {0: {0: "表格前导段落译文。"}}, {}, {}, set(), {},
        typo, font_path, "zh", reflow_cfg, warns, log=lambda m: None)
    out = pymupdf.open("pdf", data)
    # 位图存在且每页图都在页面框内
    ok_img = False
    for pg in out:
        for im in pg.get_images(full=True):
            for rect in pg.get_image_rects(im[0]):
                assert pg.rect.contains(rect), \
                    f"table bitmap out of page: {rect}"
                ok_img = True
    assert ok_img, "tall table bitmap missing"
    assert any("scaled to" in w for w in warns), warns
    assert "表格前导段落译文" in out[0].get_text("text")
    out.close()
    doc.close()


def test_reflow_rtl_direction():
    """RTL 目标语言（阿拉伯语）：direction:rtl 进预设样式表，整形出墨。"""
    from translator.render_reflow import render_reflow_document
    from translator.langs import resolve_output_fonts
    try:
        body_path, _ = resolve_output_fonts("ar", {})
        assert body_path, "no arabic font on this machine"
    except Exception as e:
        pytest.skip(f"no arabic font: {e}")
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    r = lambda x0, y0, x1, y1: pymupdf.Rect(x0, y0, x1, y1)
    lay = {"mode": "one",
           "paragraphs": [
               {"index": 0, "bbox": r(60, 40, 550, 90), "col": 0,
                "text": "Corner case generation for autonomous driving.",
                "size": 10.0, "spans": [], "is_heading": False,
                "is_caption": False, "is_ref": False,
                "is_verbatim": False, "is_alg_caption": False,
                "is_list_item": False}],
           "tables": [], "tables_cells": [], "formulas": [],
           "hf_blocks": [], "fig_text_blocks": [], "figure_regions": [],
           "layout_engine": "heuristic"}
    typo = _typo_zh().__class__ and None
    from translator.typography import Typography
    typo = Typography({"body": "", "cjk": ""}, "ar")
    reflow_cfg = types.SimpleNamespace(columns="single", body_size=0,
                                       segment_blocks=500)
    ar_text = "توليد حالات الحافة للقيادة الذاتية"
    data = render_reflow_document(
        [lay], doc, {0: {0: ar_text}}, {}, {}, set(), {},
        typo, body_path, "ar", reflow_cfg, [], log=lambda m: None)
    out = pymupdf.open("pdf", data)
    txt = out[0].get_text("text").strip()
    assert txt, "rtl text lost"
    # 非空白出墨（整形后的阿拉伯字形）
    pix = out[0].get_pixmap(dpi=72)
    ink = sum(1 for k in range(0, len(pix.samples), pix.n)
              if pix.samples[k] < 200)
    assert ink > 50, f"no ink for rtl text: {ink}"
    out.close()
    doc.close()
