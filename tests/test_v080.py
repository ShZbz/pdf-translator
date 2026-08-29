"""v0.7.1 验收单测。

- validate-key 连通性测试成功分支（v0.7.x 的 `latency:` 裸名键 NameError
  被 except 吞掉——API 成功也回 ok=False，连通性测试恒失败）
- _column_anchor_split 空 bands 不再踩残留循环变量 NameError
- 首启向导支撑：GET /api/config first_run 标记、GET /api/presets
- --quick 预设：io.pages 解析 + provider 档位调优参数应用
- 项目级缓存库（doccache）：跨输出目录共享、legacy 迁移、文档指纹索引
- 页级 Story 渲染：断框落位、溢出整页回退、双语/fit-off 不走 story
- EventSink 升级：stage 标签、订阅、命令通道
零网络（LLM 用假 client）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------- validate-key 成功分支回归（阶段 A bug 修复） ----------

class _FakeCompletions:
    def __init__(self, parent):
        self._parent = parent

    def create(self, **kwargs):
        self._parent.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="你好。"))])


class _FakeOpenAI:
    """openai.OpenAI 替身：记录构造参数，chat.completions 返回固定译文。"""

    last_init = None
    calls: list = []

    def __init__(self, **kwargs):
        type(self).last_init = kwargs
        self.chat = SimpleNamespace(
            completions=_FakeCompletions(type(self)))


def test_validate_key_success_has_latency(monkeypatch):
    """连通性测试成功时必须回 ok=True + latency（回归：latency 裸名键）。"""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    import openai as _openai
    monkeypatch.setattr(_openai, "OpenAI", _FakeOpenAI)
    _FakeOpenAI.calls = []
    from server.app import app
    with fastapi_testclient.TestClient(app) as c:
        r = c.post("/api/validate-key", json={
            "provider": "deepseek", "model": "test-model",
            "api_key": "sk-test", "target_lang": "zh",
            "timeout": 10.0, "max_retries": 1})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True, f"expected success, got {d}"
    assert "latency" in d and isinstance(d["latency"], (int, float))
    assert d["sample"]


def test_validate_key_failure_reports_error(monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    import openai as _openai

    class _Boom(_FakeOpenAI):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.chat = SimpleNamespace(completions=SimpleNamespace(
                create=self._boom))

        @staticmethod
        def _boom(**kwargs):
            raise RuntimeError("429 too many requests")

    monkeypatch.setattr(_openai, "OpenAI", _Boom)
    from server.app import app
    with fastapi_testclient.TestClient(app) as c:
        r = c.post("/api/validate-key", json={
            "provider": "deepseek", "model": "m", "api_key": "k",
            "timeout": 5.0, "max_retries": 1})
    d = r.json()
    assert d["ok"] is False and "429" in d["error"]


# ---------- _column_anchor_split 防御（阶段 A bug 修复） ----------

def test_column_anchor_split_empty_bands():
    """空 bands 不踩残留循环变量（旧代码 band["col_groups"] 在循环外引用）。"""
    from translator.layout import _column_anchor_split
    _column_anchor_split([])          # 不抛即过


def test_column_anchor_split_single_band_defaults():
    """锚点不足 2 时全带回落旧行为（col_groups=None, conf=0.4）。"""
    from translator.layout import _column_anchor_split
    r = lambda x0, y0, x1, y1: pymupdf.Rect(x0, y0, x1, y1)
    bands = [{"bbox": r(0, 0, 100, 10), "items": [(r(0, 0, 40, 8), {"spans": []})]}]
    _column_anchor_split(bands)
    assert bands[0]["col_groups"] is None
    assert bands[0]["conf"] == 0.4


# ---------- 项目级缓存库 doccache（任务 2-1） ----------

def _make_pdf(tmp_path, name="t.pdf", text="Hello world paragraph."):
    src = tmp_path / name
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 90), text, fontsize=11)
    doc.save(str(src))
    doc.close()
    return src


def test_doccache_shared_across_output_dirs(tmp_path):
    """同一输入译到不同输出目录 → 同一项目库（输入目录侧）。"""
    from translator.doccache import DocumentCache, resolve_cache_root
    src = _make_pdf(tmp_path)
    out1, out2 = tmp_path / "o1", tmp_path / "o2"
    out1.mkdir()
    out2.mkdir()
    r1, tag1 = resolve_cache_root("", src, out1)
    r2, _ = resolve_cache_root("", src, out2)
    assert tag1 == "input-dir" and r1 == r2 == tmp_path
    d1 = DocumentCache(r1)
    d1.tc.put(d1.tc.make_key("openai-compat", "m", "en", "zh", "para"),
              "para", "段落")
    d1.close()
    d2 = DocumentCache(r2)          # 新句柄（模拟另一次运行）
    assert d2.tc.get(d2.tc.make_key("openai-compat", "m", "en", "zh", "para")) \
        == "段落"
    d2.close()


def test_doccache_legacy_migration(tmp_path):
    """旧 out_dir/.translation_cache.db → 项目库自动迁移（空库时）。"""
    from translator.cache import TranslationCache
    from translator.doccache import DocumentCache
    out = tmp_path / "out"
    out.mkdir()
    legacy = TranslationCache(out / ".translation_cache.db")
    key = legacy.make_key("openai-compat", "m", "en", "zh", "old para")
    legacy.put(key, "old para", "旧段落")
    legacy.close()
    d = DocumentCache(tmp_path, legacy_sources=[out / ".translation_cache.db"])
    assert d.migrated == 1
    assert d.tc.get(key) == "旧段落"
    d.close()
    # 非空库不重复迁移
    d2 = DocumentCache(tmp_path, legacy_sources=[out / ".translation_cache.db"])
    assert d2.migrated == 0
    d2.close()


def test_doccache_fingerprint_content_addressed(tmp_path):
    """同一文件复制/改名 → 同指纹（内容寻址）；内容变化 → 指纹变化。

    注意不能用两次 new_pdf 生成"同内容"PDF——PyMuPDF 每次保存写入
    不同的 CreationDate/ID，字节本就不同；真实场景是文件的复制/改名。
    """
    import shutil

    from translator.doccache import DocumentCache
    a = _make_pdf(tmp_path, "a.pdf")
    sub = tmp_path / "sub"
    sub.mkdir()
    b = sub / "b.pdf"
    shutil.copy(a, b)                 # 复制/改名：字节相同
    dc = DocumentCache(tmp_path)
    assert dc.fingerprint(a) == dc.fingerprint(b)
    c = _make_pdf(tmp_path, "c.pdf", text="Different content entirely.")
    assert dc.fingerprint(a) != dc.fingerprint(c)
    # 版面缓存读写 roundtrip + docs 指纹索引
    lay = [{"mode": "one", "paragraphs": [], "formulas": [],
            "tables_cells": [], "tables": [], "hf_blocks": [],
            "fig_text_blocks": [], "figure_regions": [],
            "layout_engine": "heuristic"}]
    fp = dc.fingerprint(a)
    dc.save_layout(fp, "heuristic", 2, a, 1, lay)
    assert dc.load_layout(fp, "heuristic", 2) is not None
    assert dc.load_layout(fp, "pymupdf-layout", 2) is None   # 引擎隔离
    row = dc._conn.execute("SELECT path, pages FROM docs WHERE fp=?",
                           (fp,)).fetchone()
    assert row is not None and row[1] == 1
    dc.close()


def test_doccache_fallback_to_output_dir(tmp_path, monkeypatch):
    """输入目录只读（mkdir 失败）→ 回退输出目录。"""
    from translator import doccache
    src = tmp_path / "ro" / "t.pdf"
    src.parent.mkdir()
    src.write_bytes(b"%PDF-fake")
    out = tmp_path / "out"
    out.mkdir()

    real_mkdir = Path.mkdir

    def _boom(self, *a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(Path, "mkdir", _boom)
    root, tag = doccache.resolve_cache_root("", src, out)
    assert tag == "output-dir" and root == out


# ---------- 首启向导支撑（任务 2-2） ----------

def test_config_first_run_flag(tmp_path, monkeypatch):
    """ui_config.yaml 不存在 → first_run=True（向导触发条件）。"""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    import server.app as appmod
    fake_cfg = tmp_path / "ui_config.yaml"
    monkeypatch.setattr(appmod, "UI_CONFIG_PATH", fake_cfg)
    with fastapi_testclient.TestClient(appmod.app) as c:
        d = c.get("/api/config").json()
        assert d["first_run"] is True
    fake_cfg.write_text("io: {input: '', output_dir: ''}\n", encoding="utf-8")
    with fastapi_testclient.TestClient(appmod.app) as c:
        d = c.get("/api/config").json()
        assert d["first_run"] is False


def test_presets_endpoint():
    """/api/presets 返回 provider 预设 + 档位调优参数（向导下拉数据源）。"""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from server.app import app
    with fastapi_testclient.TestClient(app) as c:
        d = c.get("/api/presets").json()
    assert "deepseek" in d["providers"]
    assert d["providers"]["deepseek"]["base_url"].startswith("https://")
    tuning = d["tuning"]["deepseek"]
    assert "batch_size" in tuning and "max_workers" in tuning
    assert "deepseek-v4-flash" in d["recommended_model"]["deepseek"]


# ---------- 页级 Story 接管（任务 2-3 P1） ----------

def _cjk_font_or_skip():
    from translator.render import find_cjk_font
    try:
        fp = find_cjk_font(None, lang="zh")
        assert fp and Path(fp).is_file()
        return fp
    except Exception as e:
        pytest.skip(f"no CJK font on this machine: {e}")


def _story_page(paras: list[dict]):
    """构造单页文档 + layout dict。"""
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=400)
    layout = {"mode": "one", "paragraphs": paras, "tables_cells": [],
              "tables": [], "formulas": [], "hf_blocks": [],
              "fig_text_blocks": [], "figure_regions": [],
              "layout_engine": "heuristic"}
    return doc, page, layout


def _para(idx, rect, text, size=10.0):
    return {"index": idx, "bbox": rect, "text": text, "size": size,
            "spans": [], "col": 0, "is_heading": False, "is_caption": False,
            "is_ref": False, "is_verbatim": False, "is_alg_caption": False}


def test_render_story_whole_page_placement():
    """页级 Story：多段各归其框（段落框级位置不变），无串位。"""
    font_path = _cjk_font_or_skip()
    paras = []
    zh = ["第一段落讲述学术翻译的质量评估方法与流程设计。",
          "第二段落阐述双栏版面的逐框落位验证策略。",
          "第三段落说明公式与表格的原位保留机制。"]
    for i, t in enumerate(zh):
        paras.append(_para(i, pymupdf.Rect(30, 20 + i * 60, 280,
                                           20 + i * 60 + 50), t))
    translated = [{"index": i, "text": f"【译】{p['text']}"}
                  for i, p in enumerate(paras)]
    doc, page, layout = _story_page(paras)
    stats = {"story": 0, "fallback": 0, "reasons": []}
    from translator.render import render_page
    render_page(page, layout, translated, font_path, renderer="htmlbox",
                lang="zh", page_story="on", story_stats=stats)
    assert stats["story"] == 1 and stats["fallback"] == 0, stats["reasons"]
    out = doc[0]
    for i, p in enumerate(paras):
        r = pymupdf.Rect(p["bbox"])
        got = out.get_text("text", clip=r).replace("\n", "")
        assert f"【译】{p['text']}" in got, f"para {i} not in its box: {got!r}"
    # 相邻段不串位：段 k 的译文不出现在段 k±1 框内
    for i in range(len(paras)):
        nb = pymupdf.Rect(paras[i]["bbox"])
        nb.y0 -= 60
        nb.y1 += 60
        for j, p in enumerate(paras):
            if j == i:
                continue
            r = pymupdf.Rect(p["bbox"])
            if nb.intersects(r):
                inter = pymupdf.Rect(nb)
                inter.intersect(r)
                got = out.get_text("text", clip=inter).replace("\n", "")
                assert f"【译】{p['text']}" not in got, \
                    f"para {j} spilled into neighbor {i}"


def test_render_story_overflow_falls_back():
    """超长段（译文超预算且禁缩）：预检拦截 → 整页回退 → 兜底仍出字。"""
    font_path = _cjk_font_or_skip()
    long_text = "超长" * 300          # 远超 50pt 框预算
    paras = [_para(0, pymupdf.Rect(30, 20, 280, 70), "正常段落。"),
             _para(1, pymupdf.Rect(30, 90, 280, 140), "第二段。")]
    translated = [{"index": 0, "text": long_text},
                  {"index": 1, "text": "【译】第二段。"}]
    doc, page, layout = _story_page(paras)
    stats = {"story": 0, "fallback": 0, "reasons": []}
    from translator.render import render_page
    render_page(page, layout, translated, font_path, renderer="htmlbox",
                lang="zh", page_story="on", story_stats=stats)
    assert stats["fallback"] == 1 and stats["story"] == 0, stats["reasons"]
    out = doc[0]
    full = out.get_text("text")
    assert "第二段。" in full or "【译】第二段。" in full
    # 兜底必出字：超长段不能整段消失
    assert "超长" in full


def test_render_story_off_and_bilingual_keep_legacy():
    """page_story=off / 双语模式：不进 story 路径（计数全零）。"""
    font_path = _cjk_font_or_skip()
    paras = [_para(0, pymupdf.Rect(30, 20, 280, 70), "Hello world paragraph.")]
    translated = [{"index": 0, "text": "【译】你好世界段落。"}]
    for mode, bilingual in (("off", False), ("on", True)):
        doc, page, layout = _story_page([dict(paras[0])])
        stats = {"story": 0, "fallback": 0, "reasons": []}
        from translator.render import render_page
        render_page(page, layout, translated, font_path, renderer="htmlbox",
                    lang="zh", page_story=mode, story_stats=stats,
                    bilingual=bilingual)
        assert stats["story"] == 0 and stats["fallback"] == 0, \
            f"mode={mode} bilingual={bilingual}: {stats}"


def test_verify_flow_detects_spill():
    """落墨前预演：框高不足时逐框对账必须拦截（不落墨）。"""
    from translator.render_story import _verify_flow, build_page_story
    font_path = _cjk_font_or_skip()
    from translator.render import collect_para_specs
    paras = [_para(0, pymupdf.Rect(30, 20, 280, 30), "很长很长" * 40)]
    translated = [{"index": 0, "text": "很" * 200}]
    specs = collect_para_specs(paras, {0: "很" * 200}, None, False, [],
                               "zh", page_h=400)
    html, css, _mc = build_page_story(specs, None, "")
    boxes = [pymupdf.Rect(s["rect"]) for s in specs]
    ok, why = _verify_flow(html, css, boxes, None)
    assert ok, f"should fit when box is generous: {why}"
    # 框压到 12pt：内容必然漫延（box 耗尽 → False）
    small = [pymupdf.Rect(30, 20, 280, 32)]
    ok2, why2 = _verify_flow(html, css, small, None)
    assert not ok2


def test_config_output_render_keys(tmp_path):
    """output.mode/render.page_story 校验与 YAML bool 归一。"""
    from translator.config import load_config
    src = _make_pdf(tmp_path)
    y = tmp_path / "c.yaml"
    y.write_text(f"""
io:
  input: {src.as_posix()}
  output_dir: {tmp_path.as_posix()}
output:
  mode: faithful
render:
  page_story: on
""", encoding="utf-8")
    cfg = load_config(y)
    assert cfg.output.mode == "faithful"
    assert cfg.render.page_story == "on"
    # 裸 on（YAML 1.1 → bool True）归一为 "on"
    y2 = tmp_path / "c2.yaml"
    y2.write_text(f"""
io:
  input: {src.as_posix()}
render:
  page_story: on
""", encoding="utf-8")
    assert load_config(y2).render.page_story == "on"
    # reflow 未实装：直接报错防误解
    y3 = tmp_path / "c3.yaml"
    y3.write_text(f"""
io:
  input: {src.as_posix()}
output:
  mode: reflow
""", encoding="utf-8")
    with pytest.raises(ValueError, match="reflow"):
        load_config(y3)
    # page_story 非法值报错
    y4 = tmp_path / "c4.yaml"
    y4.write_text(f"""
io:
  input: {src.as_posix()}
render:
  page_story: sometimes
""", encoding="utf-8")
    with pytest.raises(ValueError, match="page_story"):
        load_config(y4)


def test_pages_subset_quick_flow(tmp_path):
    """io.pages 子集：只译前 2 页 + 输出名带 -p1-2 标记（不覆盖全量名）。"""
    font_path = _cjk_font_or_skip()
    src = tmp_path / "t.pdf"
    doc = pymupdf.open()
    for i in range(4):
        doc.new_page().insert_text(
            (72, 90), f"Page {i + 1} content paragraph.", fontsize=11)
    doc.save(str(src))
    doc.close()
    y = tmp_path / "c.yaml"
    y.write_text(f"""
io:
  input: {src.as_posix()}
  output_dir: {tmp_path.as_posix()}
  pages: "1-2"
features:
  translation_cache: false
fonts:
  cjk: {Path(font_path).as_posix()}
""", encoding="utf-8")
    from translator.config import load_config
    from translator.pipeline import translate_document
    stats = translate_document(load_config(y), client=None)
    assert stats["pages"] == 2
    assert "-p1-2-" in Path(stats["output"]).name
    out_doc = pymupdf.open(stats["output"])
    assert len(out_doc) == 2
    out_doc.close()


def test_parse_page_ranges():
    from translator.config import parse_page_ranges
    assert parse_page_ranges("", 10) == []
    assert parse_page_ranges("1-2,5", 10) == [0, 1, 4]
    assert parse_page_ranges("3", 4) == [2]
    assert parse_page_ranges("9-20", 10) == [8, 9]   # 越界钳制
    with pytest.raises(ValueError):
        parse_page_ranges("abc", 10)
    with pytest.raises(ValueError):
        parse_page_ranges("0", 10)
    with pytest.raises(ValueError):
        parse_page_ranges("5-2", 10)


# ---------- 页级 Story 接管（任务 2-3 P1） ----------

def _cjk_font_or_skip():
    from translator.render import find_cjk_font
    try:
        fp = find_cjk_font(None, lang="zh")
        assert fp and Path(fp).is_file()
        return fp
    except Exception as e:
        pytest.skip(f"no CJK font on this machine: {e}")


def _story_page(paras: list[dict]):
    """构造单页文档 + layout dict。"""
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=400)
    layout = {"mode": "one", "paragraphs": paras, "tables_cells": [],
              "tables": [], "formulas": [], "hf_blocks": [],
              "fig_text_blocks": [], "figure_regions": [],
              "layout_engine": "heuristic"}
    return doc, page, layout


def _para(idx, rect, text, size=10.0):
    return {"index": idx, "bbox": rect, "text": text, "size": size,
            "spans": [], "col": 0, "is_heading": False, "is_caption": False,
            "is_ref": False, "is_verbatim": False, "is_alg_caption": False}


def test_render_story_whole_page_placement():
    """页级 Story：多段各归其框（段落框级位置不变），无串位。"""
    font_path = _cjk_font_or_skip()
    paras = []
    zh = ["第一段落讲述学术翻译的质量评估方法与流程设计。",
          "第二段落阐述双栏版面的逐框落位验证策略。",
          "第三段落说明公式与表格的原位保留机制。"]
    for i, t in enumerate(zh):
        paras.append(_para(i, pymupdf.Rect(30, 20 + i * 60, 280,
                                           20 + i * 60 + 50), t))
    translated = [{"index": i, "text": f"【译】{p['text']}"}
                  for i, p in enumerate(paras)]
    doc, page, layout = _story_page(paras)
    stats = {"story": 0, "fallback": 0, "reasons": []}
    from translator.render import render_page
    render_page(page, layout, translated, font_path, renderer="htmlbox",
                lang="zh", page_story="on", story_stats=stats)
    assert stats["story"] == 1 and stats["fallback"] == 0, stats["reasons"]
    out = doc[0]
    # span 级几何断言（get_text(clip=) 是块级语义，会带出框外文本）
    spans = []
    for b in out.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b.get("lines", []):
            for sp in l["spans"]:
                spans.append((pymupdf.Rect(sp["bbox"]), sp["text"]))

    def spans_in(rect):
        return "".join(t for r, t in spans if r.intersects(rect))

    for i, p in enumerate(paras):
        got = spans_in(pymupdf.Rect(p["bbox"])).replace(" ", "")
        assert f"【译】{p['text']}" in got, \
            f"para {i} not fully in its box: {got!r}"
    # 相邻段不串位：段 j 的译文 span 不落入段 i 的框
    for i, pi in enumerate(paras):
        for j, pj in enumerate(paras):
            if j == i:
                continue
            got = spans_in(pymupdf.Rect(pi["bbox"]))
            assert f"【译】{pj['text']}" not in got, \
                f"para {j} text found in box {i}: {got!r}"


def test_render_story_overflow_falls_back():
    """超长段（译文超预算且禁缩）：预检拦截 → 整页回退 → 兜底仍出字。"""
    font_path = _cjk_font_or_skip()
    long_text = "超长" * 300          # 远超 50pt 框预算
    paras = [_para(0, pymupdf.Rect(30, 20, 280, 70), "正常段落。"),
             _para(1, pymupdf.Rect(30, 90, 280, 140), "第二段。")]
    translated = [{"index": 0, "text": long_text},
                  {"index": 1, "text": "【译】第二段。"}]
    doc, page, layout = _story_page(paras)
    stats = {"story": 0, "fallback": 0, "reasons": []}
    from translator.render import render_page
    render_page(page, layout, translated, font_path, renderer="htmlbox",
                lang="zh", page_story="on", story_stats=stats)
    assert stats["fallback"] == 1 and stats["story"] == 0, stats["reasons"]
    out = doc[0]
    full = out.get_text("text")
    assert "第二段。" in full or "【译】第二段。" in full
    # 兜底必出字：超长段不能整段消失
    assert "超长" in full


def test_render_story_off_and_bilingual_keep_legacy():
    """page_story=off / 双语模式：不进 story 路径（计数全零）。"""
    font_path = _cjk_font_or_skip()
    paras = [_para(0, pymupdf.Rect(30, 20, 280, 70), "Hello world paragraph.")]
    translated = [{"index": 0, "text": "【译】你好世界段落。"}]
    for mode, bilingual in (("off", False), ("on", True)):
        doc, page, layout = _story_page([dict(paras[0])])
        stats = {"story": 0, "fallback": 0, "reasons": []}
        from translator.render import render_page
        render_page(page, layout, translated, font_path, renderer="htmlbox",
                    lang="zh", page_story=mode, story_stats=stats,
                    bilingual=bilingual)
        assert stats["story"] == 0 and stats["fallback"] == 0, \
            f"mode={mode} bilingual={bilingual}: {stats}"


def test_verify_flow_detects_spill():
    """落墨前预演：框高不足时逐框对账必须拦截（不落墨）。"""
    from translator.render_story import _verify_flow, build_page_story
    paras = [_para(0, pymupdf.Rect(30, 20, 280, 200), "很长很长" * 40)]
    specs_text = "很" * 200
    from translator.render import collect_para_specs
    specs = collect_para_specs(paras, {0: specs_text}, None, False, [],
                               "zh", page_h=400)
    html, css, _mc = build_page_story(specs, None, "")
    boxes = [pymupdf.Rect(s["rect"]) for s in specs]
    ok, why = _verify_flow(html, css, boxes, None)
    assert ok, f"should fit when box is generous: {why}"
    # 框压到 12pt：内容必然漫延（框耗尽 → False）
    small = [pymupdf.Rect(30, 20, 280, 32)]
    ok2, why2 = _verify_flow(html, css, small, None)
    assert not ok2


def test_config_output_render_keys(tmp_path):
    """output.mode/render.page_story 校验与 YAML bool 归一。"""
    from translator.config import load_config
    src = _make_pdf(tmp_path)
    y = tmp_path / "c.yaml"
    y.write_text(f"""
io:
  input: {src.as_posix()}
  output_dir: {tmp_path.as_posix()}
output:
  mode: faithful
render:
  page_story: on
""", encoding="utf-8")
    cfg = load_config(y)
    assert cfg.output.mode == "faithful"
    assert cfg.render.page_story == "on"
    # reflow 未实装：直接报错防误解
    y3 = tmp_path / "c3.yaml"
    y3.write_text(f"""
io:
  input: {src.as_posix()}
output:
  mode: reflow
""", encoding="utf-8")
    with pytest.raises(ValueError, match="reflow"):
        load_config(y3)
    # page_story 非法值报错
    y4 = tmp_path / "c4.yaml"
    y4.write_text(f"""
io:
  input: {src.as_posix()}
render:
  page_story: sometimes
""", encoding="utf-8")
    with pytest.raises(ValueError, match="page_story"):
        load_config(y4)


def test_pages_subset_quick_flow(tmp_path):
    """io.pages 子集：只译前 2 页 + 输出名带 -p1-2 标记（不覆盖全量名）。"""
    font_path = _cjk_font_or_skip()
    src = tmp_path / "t.pdf"
    doc = pymupdf.open()
    for i in range(4):
        doc.new_page().insert_text(
            (72, 90), f"Page {i + 1} content paragraph.", fontsize=11)
    doc.save(str(src))
    doc.close()
    y = tmp_path / "c.yaml"
    y.write_text(f"""
io:
  input: {src.as_posix()}
  output_dir: {tmp_path.as_posix()}
  pages: "1-2"
features:
  translation_cache: false
fonts:
  cjk: {Path(font_path).as_posix()}
""", encoding="utf-8")
    from translator.config import load_config
    from translator.pipeline import translate_document
    stats = translate_document(load_config(y), client=None)
    assert stats["pages"] == 2
    assert "-p1-2-" in Path(stats["output"]).name
    out_doc = pymupdf.open(stats["output"])
    assert len(out_doc) == 2
    out_doc.close()


def test_parse_page_ranges():
    from translator.config import parse_page_ranges
    assert parse_page_ranges("", 10) == []
    assert parse_page_ranges("1-2,5", 10) == [0, 1, 4]
    assert parse_page_ranges("3", 4) == [2]
    assert parse_page_ranges("9-20", 10) == [8, 9]   # 越界钳制
    with pytest.raises(ValueError):
        parse_page_ranges("abc", 10)
    with pytest.raises(ValueError):
        parse_page_ranges("0", 10)
    with pytest.raises(ValueError):
        parse_page_ranges("5-2", 10)


# ---------- EventSink 命令总线（任务 2-4 预备） ----------

def test_events_stage_tag_and_subscribe():
    """emit 自动携带当前阶段标签；订阅者与 on_event 并行派发。"""
    from translator.events import EventSink
    seen = []
    sink = EventSink(on_event=lambda ev: seen.append(("cb", ev)))
    sink.subscribe(lambda ev: seen.append(("sub", ev)))
    sink.stage("layout")
    sink.page_done(0, 3)
    sink.stage("translate")
    kinds = [(tag, ev["kind"], ev.get("stage")) for tag, ev in seen]
    assert ("cb", "stage", "layout") in kinds
    assert ("sub", "progress", "layout") in kinds
    assert ("cb", "stage", "translate") in kinds
    # 既有行为不回退：events 留档完整
    assert [e["kind"] for e in sink.events] == ["stage", "progress", "stage"]
    # unsubscribe
    sub2 = lambda ev: None
    sink.subscribe(sub2)
    sink.unsubscribe(sub2)


def test_command_bus_pause_cancel_via_sink():
    """sink.post(pause/cancel) → JobControl.checkpoint 消费（页间粒度）。"""
    import threading

    from translator.control import JobControl, JobCancelled
    from translator.events import EventSink
    sink = EventSink()
    control = JobControl()
    control.bind_commands(sink.drain)
    sink.post("pause")
    done = threading.Event()

    def worker():
        control.checkpoint()      # 消费 pause → 阻塞
        sink.post("cancel")
        try:
            control.checkpoint()  # 消费 cancel → 抛
        except JobCancelled:
            done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=1.0)
    assert control.state == "paused"
    sink.post("resume")
    assert done.wait(timeout=2.0), "cancel via command bus failed"


def test_command_bus_direct_calls_unaffected():
    """未绑定命令源时行为不变（None drain 安全）。"""
    from translator.control import JobControl
    control = JobControl()
    control.checkpoint()          # running 直通
    control.pause()
    assert control.state == "paused"
    control.resume()
    assert control.state == "running"
    control._apply_commands()     # 未绑定 → no-op
    assert control.state == "running"
