"""v0.8.4 回归：plan 收尾三项工具化 + 全库审查修复的锁定测试。

- reflow×typography 缺失（preserve_formatting: false）不再崩溃
- reflow 零内容守卫（FAILPATHS R6 测试锚点补缺）
- build_template 退化栏（错标栏统计挤压）守卫
- OCR 临时 PNG 的 mkstemp fd 收口（Windows WinError 32 泄漏根因）
- JobManager.submit 起跑失败按失败返回（不留孤儿运行配置）
- /api/translate 空段节点（{"io": null}）容错
- ocr.engine / ocr.engines 白名单配置期校验
零网络（无 LLM 调用）。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_reflow import _texts_for, _two_col_layouts  # noqa: E402
from tests.test_v080 import _cjk_font_or_skip  # noqa: E402


# ---------- reflow × typo=None（preserve_formatting: false 路径） ----------

def test_reflow_typo_none_renders():
    """typo=None（Typography 未启用/初始化失败）时 reflow 兜底渲染，
    不再 AttributeError 崩溃（v0.8.4 修复）。"""
    from translator.render_reflow import render_reflow_document
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    doc.new_page(width=612, height=792)
    lays = _two_col_layouts()
    texts = _texts_for(lays)
    reflow_cfg = types.SimpleNamespace(columns="auto", body_size=0,
                                       segment_blocks=500)
    warns: list = []
    data = render_reflow_document(
        lays, doc, texts, {}, {}, set(), {}, None, font_path, "zh",
        reflow_cfg, warns, log=lambda m: None)
    assert len(data) > 0
    out = pymupdf.open("pdf", data)
    all_text = "".join(out[i].get_text("text")
                       for i in range(len(out))).replace(" ", "")
    assert "译文明细Body0text." in all_text.replace("\n", "")
    out.close()
    doc.close()


# ---------- reflow 零内容守卫（FAILPATHS R6 锚点） ----------

def test_reflow_zero_content_blank_page():
    """无可译块（空布局）→ 落一页空白页 + 告警，输出合法可打开。"""
    from translator.render_reflow import render_reflow_document
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    empty_lay = {"mode": "one", "paragraphs": [], "tables": [],
                 "tables_cells": [], "formulas": [], "hf_blocks": [],
                 "fig_text_blocks": [], "figure_regions": [],
                 "layout_engine": "heuristic"}
    reflow_cfg = types.SimpleNamespace(columns="auto", body_size=0,
                                       segment_blocks=500)
    warns: list = []
    typo = None       # 同时覆盖 typo=None 兜底路径
    data = render_reflow_document(
        [empty_lay], doc, {}, {}, {}, set(), {}, typo, font_path, "zh",
        reflow_cfg, warns, log=lambda m: None)
    assert any("no translatable content" in w for w in warns), warns
    out = pymupdf.open("pdf", data)     # 0 页 PDF 的 tobytes 会直接抛错
    assert len(out) == 1
    out.close()
    doc.close()


# ---------- build_template 退化栏守卫 ----------

def test_build_template_degenerate_cols_clamped():
    """栏归属错标的极端统计：栏框全部 ≥24pt 或退单栏，绝不产出空/极窄栏。"""
    from translator.render_reflow import build_template
    # col0 全部错标到页右缘、col1 更靠右——量化统计挤压出 ~10pt 栏
    lays = [{"mode": "two", "paragraphs": [
        {"bbox": pymupdf.Rect(400, 60, 500, 80), "col": 0, "text": "a" * 50},
        {"bbox": pymupdf.Rect(500, 60, 590, 80), "col": 1, "text": "b" * 50},
    ]}]
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    t = build_template(lays, doc)
    assert t.cols, "template must keep at least one column"
    for x0, x1 in t.cols:
        assert x1 - x0 >= 24.0, f"degenerate column ({x0:.0f},{x1:.0f})"
    doc.close()


# ---------- OCR 临时 PNG fd 收口 ----------

def test_render_png_closes_mkstemp_fd(monkeypatch):
    """_render_png 必须关闭 mkstemp 返回的 fd——旧版泄漏句柄在 Windows
    上令后续 unlink 报 WinError 32，%TEMP% 每页×每引擎永久积一个 PNG。"""
    import tempfile
    import translator.ocr as ocr_mod
    captured: list[int] = []
    real_mkstemp = tempfile.mkstemp

    def _spy(*a, **kw):
        fd, name = real_mkstemp(*a, **kw)
        captured.append(fd)
        return fd, name

    monkeypatch.setattr(ocr_mod.tempfile, "mkstemp", _spy)
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=100)
    page.insert_text((20, 50), "x")
    tmp = ocr_mod._render_png(page, dpi=36)
    assert tmp.is_file()
    # fd 已被关闭：os.fstat 抛 OSError（EBADF）即通过；Windows 上还能直接删
    for fd in captured:
        with pytest.raises(OSError):
            os_fstat(fd)
    tmp.unlink(missing_ok=True)     # Windows：句柄未关时这里会 PermissionError
    doc.close()


def os_fstat(fd: int):
    import os
    return os.fstat(fd)


# ---------- JobManager.submit 起跑失败 ----------

def test_submit_start_failure_returns_not_ok(monkeypatch, tmp_path):
    """直接起跑路径 start 失败：按 {ok: False} 返回（API 层据此清理临时
    运行配置），不再异常冒泡变 500 + 孤儿 .ui_run_config 文件。"""
    from server.jobs import JobManager
    mgr = JobManager(tmp_path)
    (tmp_path / "run.yaml").write_text("io: {input: x.pdf}\n",
                                       encoding="utf-8")

    def _boom(self, root):
        raise OSError("disk full")

    monkeypatch.setattr("server.jobs.Job.start", _boom)
    out = mgr.submit(str(tmp_path / "run.yaml"), input_path="x.pdf")
    assert out.get("ok") is False
    assert "起跑失败" in out.get("error", "")
    assert mgr.job is None            # 槽位不被半启动任务占住


# ---------- /api/translate 空段节点容错 ----------

def test_build_run_config_null_sections(monkeypatch, tmp_path):
    """API 调用方传 {"io": null, "llm": null}：旧版 setdefault 不替换
    None 键 → TypeError 500；现归一为 {} 正常合成。"""
    import importlib
    import tests.test_v083 as _t  # noqa: F401  （确保 sys.path 就绪）
    from server import app as app_mod
    stored = tmp_path / "ui.yaml"
    stored.write_text("llm:\n  api_key: sk-stored\n", encoding="utf-8")
    monkeypatch.setattr(app_mod, "UI_CONFIG_PATH", stored)
    req = app_mod.TranslateReq(input=str(tmp_path / "x.pdf"),
                               config={"io": None, "llm": None})
    out = app_mod._build_run_config(req, Path(tmp_path / "x.pdf"))
    assert out["io"]["input"].endswith("x.pdf")
    assert out["llm"]["api_key"] == "sk-stored"    # key 回填写回真实段


# ---------- ocr.engine / engines 白名单 ----------

def test_ocr_engine_whitelist(tmp_path):
    """拼错引擎名在配置期报错（旧版运行期才以 pip 提示警告误导排查）。"""
    from translator.config import load_config
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text(
        'io: {input: "x.pdf"}\nocr: {engine: paddel}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="ocr.engine"):
        load_config(cfg_file)
    cfg_file.write_text(
        'io: {input: "x.pdf"}\nocr: {engines: [paddle, tesseract]}\n',
        encoding="utf-8")
    load_config(cfg_file)                       # 合法集合照常通过
    cfg_file.write_text(
        'io: {input: "x.pdf"}\nocr: {engines: [rapidoc]}\n',
        encoding="utf-8")
    with pytest.raises(ValueError, match="ocr.engines"):
        load_config(cfg_file)


# ---------- 发布前终检（第二轮全库审查）----------

def test_narrow_cell_not_redacted():
    """窄小单元格（width<8 / height<5，不参与重排）不再被 redact——
    旧版删了原文又不回灌译文，单字符窄数字列静默缺格（实测复现）。"""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((50, 100), "N", fontsize=8)
    page.insert_text((100, 100), "0.35 cell", fontsize=8)
    cells = [
        {"bbox": pymupdf.Rect(48, 92, 54, 104), "text": "N",
         "spans": [], "conf": 1.0},
        {"bbox": pymupdf.Rect(98, 92, 200, 104), "text": "0.35 cell",
         "spans": [], "conf": 1.0},
    ]
    layout = {"mode": "one", "paragraphs": [],
              "tables": [{"bbox": pymupdf.Rect(40, 90, 210, 110),
                          "cells": cells}],
              "tables_cells": cells, "formulas": [], "hf_blocks": [],
              "fig_text_blocks": [], "figure_regions": []}
    from translator.render import render_page
    render_page(page, layout, [], "", formula_pixmaps={},
                cell_texts={0: "N译", 1: "0.35 格"}, renderer="htmlbox",
                lang="zh")
    txt = page.get_text()
    assert "N" in txt, "narrow cell text must survive (original pixels)"
    assert "0.35 格" in txt
    doc.close()


def test_job_snapshot_carries_cache_saved_calls():
    """pipeline done 事件的 cache_saved_calls 必须流经 Job → snapshot →
    DB——旧版 Job._handle_event 丢弃该字段，前端「省约 N 次调用」
    （完成行+历史面板）永远不显示。"""
    from server.jobs import Job
    j = Job("t1", "cfg.yaml")
    j._handle_event({"kind": "done", "pages": 3, "calls": 5,
                     "cache_hits": 4, "cache_saved_calls": 2,
                     "elapsed": 9.0})
    snap = j.snapshot()
    assert snap["cache_hits"] == 4
    assert snap["cache_saved_calls"] == 2


def test_store_cache_saved_calls_migration(tmp_path):
    """旧库（无 cache_saved_calls 列）打开即迁移，往返不丢字段。"""
    import sqlite3
    from server.store import JobStore
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL,"
        " config_path TEXT NOT NULL, output_path TEXT DEFAULT '',"
        " error TEXT DEFAULT '', stage TEXT DEFAULT '',"
        " progress TEXT DEFAULT '', pages INTEGER DEFAULT 0,"
        " paragraphs INTEGER DEFAULT 0, calls INTEGER DEFAULT 0,"
        " elapsed REAL DEFAULT 0, created REAL DEFAULT 0,"
        " updated REAL DEFAULT 0, seq INTEGER,"
        " input TEXT DEFAULT '', output_dir TEXT DEFAULT '',"
        " warnings TEXT DEFAULT '[]', cache_hits INTEGER DEFAULT 0)")
    conn.commit()
    conn.close()
    st = JobStore(db)
    st.upsert({"id": "j1", "status": "done", "config_path": "x",
               "cache_hits": 7, "cache_saved_calls": 2, "created": 1.0})
    snap = st.history(1)[0]
    assert snap["cache_saved_calls"] == 2
    st.close()


def test_put_config_null_llm(monkeypatch, tmp_path):
    """PUT /api/config 传 {"llm": null}：归一为 {}（key 保留语义照常），
    不再 AttributeError 500（与 /api/translate 同款容错）。"""
    import tests.test_v083 as _t  # noqa: F401  （fastapi TestClient 就绪）
    from fastapi.testclient import TestClient
    from server import app as app_mod
    stored = tmp_path / "ui.yaml"
    stored.write_text("llm:\n  api_key: sk-old\nio: {target_lang: zh}\n",
                      encoding="utf-8")
    monkeypatch.setattr(app_mod, "UI_CONFIG_PATH", stored)
    client = TestClient(app_mod.app)
    r = client.put("/api/config",
                   json={"config": {"io": None, "llm": None}})
    assert r.status_code == 200, r.text
    import yaml
    saved = yaml.safe_load(stored.read_text(encoding="utf-8"))
    assert saved["llm"]["api_key"] == "sk-old"     # 打码/空 key 保留旧值


def test_worker_warns_on_missing_base_url():
    """UI worker 在 base_url 解析失败时显式发 warning 事件（旧版静默按
    dry-run 跑完全程，输出全原文而用户以为翻译完成）。"""
    import ast
    from server.jobs import _WORKER
    src = _WORKER.format(root=r"C:/proj")
    ast.parse(src)                                  # 模板仍是合法 Python
    assert "无法确定 API 地址" in src
    assert "or 120.0" in src          # timeout=0 兜底与 CLI 对齐


def test_reflow_li_font_size_follows_body():
    """reflow 列表项字号随 body_size 插值（旧版硬编码 10.5pt，小字号
    文档下列表项比正文大）。"""
    from translator.render_reflow import build_reflow_css
    css = build_reflow_css(9.0, "", "")
    li = [seg for seg in css.split("}") if seg.startswith(" li{")]
    assert li, "li rule missing"
    assert "font-size:9.00pt" in li[0] and "10.5pt" not in li[0]


# ---------- detect_columns 单栏误判（P0：整宽段被竖中线腰斩） ----------

def _blocks_of(page):
    from translator.extract import get_page_blocks
    return get_page_blocks(page, page.get_text("dict"))


def test_detect_columns_single_col_full_width():
    """单栏整宽文档（≥4 个 body 块）必须判 one——旧判据统计「完全落在
    中央窄带内的块」，整宽块不在带内同样判 two → split_crossing_blocks
    把每个整宽段按竖中线腰斩成左右碎片送译（合成复现实锤）。"""
    from translator.layout import detect_columns, layout_page
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    y = 100
    for _p in range(4):
        for _l in range(3):
            page.insert_text((70, y), "Full width body text line with "
                                      "enough words to span the page", fontsize=10)
            y += 14
        y += 8
    blocks = _blocks_of(page)
    assert len(blocks) >= 4, "fixture must reach the heuristic (not early return)"
    assert detect_columns(page, blocks) == "one"
    lay = layout_page(page)
    assert lay["mode"] == "one"
    # 段落不被腰斩：首段文本是整行开头（右半碎片不再独立成段）
    texts = [p["text"] for p in lay["paragraphs"]]
    assert any(t.startswith("Full width") for t in texts)
    doc.close()


def test_detect_columns_two_col_with_fullwidth_title():
    """双栏 + 通栏标题仍判 two（crossing 占比 <35% + 左右各有 ≥2 栏块）。"""
    from translator.layout import detect_columns
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((131, 80), "A Wide Title Spanning Both Columns Here",
                     fontsize=14)
    for i in range(6):
        x = 54 if i < 3 else 316
        page.insert_text((x, 120 + (i % 3) * 60),
                         "Column text block with words", fontsize=10)
    assert detect_columns(page, _blocks_of(page)) == "two"
    doc.close()


def test_single_col_table_detected_full_width():
    """单栏页的整宽三线表必须被圈出（旧版三线表/算法框检测硬编码双栏
    分带，整宽表线起点落不进左栏带 → 检不出；detect_columns 修复前该
    路径不可见）。"""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    # 单栏正文（≥4 块，确保走栏型启发）
    y = 100
    for _p in range(4):
        for _l in range(2):
            page.insert_text((54, y), "Single column body line filling "
                                      "the full page width", fontsize=10)
            y += 14
        y += 8
    # 整宽三线表
    tx0, tx1 = 54, 500
    for i, ly in enumerate((y, y + 16, y + 32, y + 48)):
        page.draw_line(pymupdf.Point(tx0, ly), pymupdf.Point(tx1, ly),
                       width=0.8)
        page.insert_text((tx0 + 18, ly + 11.5), "Route label", fontsize=8)
        page.insert_text((tx1 - 40, ly + 11.5), "99.9", fontsize=8)
    from translator.layout import layout_page
    lay = layout_page(page)
    assert lay["mode"] == "one"
    assert lay["tables"], "full-width three-line table must be detected"
    cells = [c["text"] for c in lay["tables_cells"]]
    assert any("Route" in c for c in cells)
    assert any("99" in c for c in cells)
    doc.close()
