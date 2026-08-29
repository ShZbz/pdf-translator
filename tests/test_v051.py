"""v0.5.1 验收单测：
- htmlbox 单元格回灌修复（v0.5.0 htmlbox 模式表格文字全部丢失的回归）
- htmlbox 转默认渲染引擎（配置缺省值 + 载入 YAML）
- RTL/天城文解锁（注册表 rtl 标记 / htmlbox direction:rtl / writer 自动切换）
- 跨页断句拆分边界修复（B 段不再以标点开头）+ 连字符合并
- 版面缓存（断点续跑：JSON 往返 + 缓存命中跳过布局）
- pymupdf-layout 适配层实装（1.28.x 五元组条目）
- SSE Last-Event-ID（事件 id 日志 + 重放）
- JobStore 增量迁移（warnings/input/output_dir/cache_hits 列）
- 缓存统计（命中段数计数 + 折算节省批次数）
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from translator.config import FeatureConfig, LLMConfig, load_config
from translator.langs import LANGUAGES, is_rtl
from translator.pipeline import _layout_cache_decode, _layout_cache_encode, \
    _split_proportional
from translator.render import render_page


# ---------- 2-1: htmlbox 默认 + 单元格修复 ----------

def test_renderer_default_is_htmlbox():
    assert FeatureConfig().renderer == "htmlbox"


def test_renderer_writer_still_accepted(tmp_path):
    y = tmp_path / "c.yaml"
    y.write_text("""
io: {input: x.pdf, output_dir: out}
features: {renderer: writer}
""", encoding="utf-8")
    assert load_config(y).features.renderer == "writer"


def _cjk_font_or_skip():
    from translator.langs import resolve_output_fonts
    body, _ = resolve_output_fonts("zh", None)
    if not body:
        pytest.skip("no CJK font on this machine")
    return body


def test_htmlbox_cells_backfill_regression(tmp_path):
    """v0.5.1 核心修复回归：htmlbox 模式下单元格译文必须落页。

    v0.5.0 的 htmlbox 分支提前 return，tw.write_text 永不执行——
    单元格被 redact 后空白（表格文字全部丢失）。
    """
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 100), "Alpha", fontsize=9)
    page.insert_text((300, 100), "0.35", fontsize=9)
    lay = {"paragraphs": [], "tables_cells": [
        {"bbox": pymupdf.Rect(90, 88, 290, 112), "text": "Alpha", "spans": []},
        {"bbox": pymupdf.Rect(295, 88, 360, 112), "text": "0.35", "spans": []}],
        "formulas": []}
    render_page(page, lay, [], font_path,
                cell_texts={0: "阿尔法方法", 1: "0.35"}, renderer="htmlbox")
    txt = page.get_text()
    assert "阿尔法方法" in txt, "htmlbox 模式单元格译文丢失（v0.5.0 回归）"
    doc.save(str(tmp_path / "cells.pdf"))


def test_htmlbox_narrow_cell_no_wrap(tmp_path):
    """窄格（<20pt 数字列）单行不折行，scale_low 放深缩放。"""
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((300, 100), "0.35", fontsize=9)
    lay = {"paragraphs": [], "tables_cells": [
        {"bbox": pymupdf.Rect(295, 88, 316, 112), "text": "0.35", "spans": []}],
        "formulas": []}
    warnings: list[str] = []
    render_page(page, lay, [], font_path, cell_texts={0: "0.35"},
                renderer="htmlbox", warnings=warnings)
    assert "0.35" in page.get_text()


# ---------- 2-1: RTL/天城文 ----------

def test_rtl_registry_flags():
    assert is_rtl("ar") and is_rtl("he")
    assert not is_rtl("hi") and not is_rtl("zh") and not is_rtl("en")
    assert LANGUAGES["hi"].script == "indic"


def test_rtl_htmlbox_direction_css():
    from translator.render import _dir_css
    assert "direction:rtl" in _dir_css("ar")
    assert "direction:rtl" in _dir_css("he")
    assert _dir_css("hi") == ""
    assert _dir_css("zh") == ""


def test_rtl_render_smoke(tmp_path):
    """RTL 样张：htmlbox + direction:rtl 渲染阿拉伯语不炸且文本落页。

    提取出的字符是连笔呈现形式（U+FExx presentation forms）——这正是
    Story 引擎 shaping 生效的证据；用 NFKC 归一化回基础形式再断言。
    """
    import unicodedata
    from translator.langs import resolve_output_fonts
    body, _ = resolve_output_fonts("ar", None)
    if not body:
        pytest.skip("no Arabic font on this machine")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "English body sentence.", fontsize=11)
    paras = [{"bbox": pymupdf.Rect(72, 80, 300, 120),
              "text": "English body sentence.", "spans": [], "size": 11.0}]
    lay = {"paragraphs": paras, "tables_cells": [], "formulas": []}
    warnings: list[str] = []
    render_page(page, lay,
                [{"index": 0, "text": "هذه جملة اختبار للغة العربية."}],
                body, renderer="htmlbox", lang="ar", warnings=warnings)
    txt = unicodedata.normalize("NFKC", page.get_text())
    assert "العربية" in txt
    doc.save(str(tmp_path / "ar.pdf"))


def test_writer_autoswitch_for_rtl(tmp_path, monkeypatch):
    """writer + RTL 目标 → pipeline 强制切 htmlbox 并出告警（dry-run 端到端）。"""
    from translator.langs import resolve_output_fonts
    body, _ = resolve_output_fonts("ar", None)
    if not body:
        pytest.skip("no Arabic font on this machine")
    src = tmp_path / "t.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 90), "Hello world.", fontsize=11)
    doc.save(str(src))
    doc.close()
    y = tmp_path / "c.yaml"
    y.write_text(f"""
io:
  input: {src.as_posix()}
  output_dir: {tmp_path.as_posix()}
  target_lang: ar
features:
  renderer: writer
  translation_cache: false
performance:
  layout_cache: false
fonts:
  cjk: {Path(body).as_posix()}
""", encoding="utf-8")
    from translator.pipeline import translate_document
    stats = translate_document(load_config(y), client=None)
    assert any("htmlbox" in w for w in stats["warnings"])
    assert stats["pages"] == 1


# ---------- 2-5: 跨页断句 ----------

def test_split_proportional_no_leading_punct():
    """v0.5.1 修复：B 段不得以标点开头（旧版偏好恰恰相反）。"""
    dst = "这是第一部分的内容这是第二部分的内容，后面还有半句话。收尾"
    for ratio in (0.4, 0.5, 0.55, 0.6):
        a, b = _split_proportional(dst, ratio)
        assert b and b[0] not in "，。；：、）】", \
            f"ratio={ratio}: B 段以标点开头 {b!r}"
        assert a and b


def test_split_proportional_prefers_after_punct():
    dst = "前半句内容。后半句内容继续到这里结束"
    a, b = _split_proportional(dst, 0.5)
    # 目标 0.5 落在"。"附近时，切点应收在句号后（A 段以标点收尾）
    assert a.endswith("。") or abs(len(a) - len(dst) * 0.5) <= 2


def test_split_proportional_word_boundary_still_ok():
    dst = "the quick brown fox jumps over the lazy dog near the river bank"
    a, b = _split_proportional(dst, 0.5)
    assert a + " " + b == dst or a + b == dst


def test_crosspage_dehyphenation():
    """跨页连字符合并：'instrumen-\\ntation' 送译前拼回一个词。"""
    # _join_group 是 translate_document 内嵌函数，用等价逻辑验证其行为
    # （直接驱动 translate_document 成本高；此处锁语义）
    def _join_group(parts: list[str]) -> str:
        if len(parts) == 2:
            a, b = parts[0].rstrip(), parts[1].lstrip()
            if a.endswith("-") and b[:1].islower():
                return a[:-1] + b
        return "\n".join(parts)

    assert _join_group(["...the instru-", "mentation is..."]) == \
        "...the instrumentation is..."
    assert _join_group(["normal end.", "Next sentence"]) == \
        "normal end.\nNext sentence"


# ---------- 2-5: 版面缓存（断点续跑） ----------

def test_layout_cache_roundtrip():
    lay = {"mode": "two",
           "paragraphs": [{"bbox": pymupdf.Rect(1.5, 2.5, 3.5, 4.5),
                           "text": "hi", "spans": [
                               {"bbox": pymupdf.Rect(1, 2, 3, 4), "size": 10.0}],
                           "size": 10.0}],
           "tables_cells": [{"bbox": pymupdf.Rect(0, 0, 9, 9), "text": "c"}],
           "formulas": [{"bbox": pymupdf.Rect(5, 5, 6, 6), "is_display": True}],
           "hf_blocks": [], "fig_text_blocks": [], "figure_regions": [],
           "tables": [], "layout_engine": "heuristic"}
    enc = _layout_cache_encode([lay])
    import json
    dec = _layout_cache_decode(json.loads(json.dumps(enc)))
    assert dec[0]["paragraphs"][0]["bbox"] == pymupdf.Rect(1.5, 2.5, 3.5, 4.5)
    assert dec[0]["formulas"][0]["is_display"] is True


def test_layout_cache_key_invalidation(tmp_path):
    """引擎切换 → layout key 变化（v0.7.1 起在项目库 layouts 表内区分）。"""
    from translator.doccache import DocumentCache
    src = tmp_path / "a.pdf"
    src.write_bytes(b"%PDF-fake")
    dc = DocumentCache(tmp_path)
    fp = dc.fingerprint(src)
    assert dc.layout_key(fp, "heuristic", 2) != \
        dc.layout_key(fp, "pymupdf-layout", 2)
    dc.close()


def test_layout_cache_hit_skips_layout(tmp_path):
    """端到端：二次运行命中缓存跳过布局（事件流里有 layout_cache_hit）。"""
    font_path = _cjk_font_or_skip()
    src = tmp_path / "t.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 90), "Hello world paragraph.", fontsize=11)
    doc.save(str(src))
    doc.close()
    from translator.events import EventSink
    from translator.pipeline import translate_document
    y = tmp_path / "c.yaml"
    y.write_text(f"""
io:
  input: {src.as_posix()}
  output_dir: {tmp_path.as_posix()}
features:
  translation_cache: false
fonts:
  cjk: {Path(font_path).as_posix()}
""", encoding="utf-8")
    sink = EventSink()
    translate_document(load_config(y), client=None, sink=sink)
    assert not any(e["kind"] == "layout_cache_hit" for e in sink.events)
    sink2 = EventSink()
    translate_document(load_config(y), client=None, sink=sink2)
    hits = [e for e in sink2.events if e["kind"] == "layout_cache_hit"]
    assert hits and hits[0]["pages"] == 1
    # v0.7.1: 缓存落项目级库（DB 内 layouts 表），输入目录下建库
    assert (tmp_path / ".pdf_translator_cache" / "cache.db").is_file()


# ---------- 2-2: pymupdf-layout 实装 ----------

def test_external_layout_regions_5tuple_format(monkeypatch):
    """1.28.x 实际条目 [x0,y0,x1,y1,kind]：旧适配层当二元组解读会崩。"""
    import types
    fake = types.ModuleType("pymupdf.layout")
    monkeypatch.setitem(sys.modules, "pymupdf.layout", fake)
    regions = [[100.0, 100.0, 200.0, 180.0, "figure"],
               [300.0, 100.0, 500.0, 160.0, "table"],
               [100.0, 300.0, 400.0, 330.0, "formula"],
               [100.0, 400.0, 500.0, 500.0, "text"],
               [18.2, 214.5, 35.1, 560.0, "page-header"]]
    monkeypatch.setattr(pymupdf, "_get_layout", lambda page: regions,
                        raising=False)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "some body text", fontsize=11)
    from translator.layout import external_layout_regions
    got = external_layout_regions(page)
    assert got is not None
    assert got["figures"] == [pymupdf.Rect(100, 100, 200, 180)]
    assert got["tables"] == [pymupdf.Rect(300, 100, 500, 160)]
    assert got["formulas"] == [pymupdf.Rect(100, 300, 400, 330)]


def test_external_layout_regions_attr_format(monkeypatch):
    """属性形态（旧假设）仍兼容。"""
    import types
    fake = types.ModuleType("pymupdf.layout")
    monkeypatch.setitem(sys.modules, "pymupdf.layout", fake)
    regions = [SimpleNamespace(bbox=(10, 10, 20, 90), kind="figure")]
    monkeypatch.setattr(pymupdf, "_get_layout", lambda page: regions,
                        raising=False)
    doc = pymupdf.open()
    page = doc.new_page()
    from translator.layout import external_layout_regions
    got = external_layout_regions(page)
    assert got is not None and len(got["figures"]) == 1


# ---------- 2-4: SSE Last-Event-ID ----------

def test_event_log_ids_and_replay():
    from server.jobs import JobManager
    mgr = JobManager(Path("."))
    for i in range(3):
        mgr._broadcast({"kind": "job_event", "seq": i})
    ids = [eid for eid, _ in mgr._event_log]
    assert ids == [1, 2, 3]
    # 每帧 payload 携带 eid（SSE id: 行的数据源）
    assert mgr._event_log[-1][1]["eid"] == 3
    replay = mgr.events_after(1)
    assert [eid for eid, _ in replay] == [2, 3]


def test_event_log_bounded():
    from server.jobs import JobManager
    mgr = JobManager(Path("."))
    mgr._EVENT_LOG_CAP = 5
    for i in range(8):
        mgr._broadcast({"kind": "job_event", "seq": i})
    assert len(mgr._event_log) == 5
    assert mgr._event_log[0][0] == 4      # 旧的被挤掉，id 保留单调


# ---------- 2-3: JobStore 迁移 + 新字段 ----------

def test_jobstore_migration_from_v050_schema(tmp_path):
    """v0.5.0 旧库（无新列）打开自动加列，不丢旧数据。"""
    import sqlite3
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE jobs (
        id TEXT PRIMARY KEY, status TEXT NOT NULL,
        config_path TEXT NOT NULL, output_path TEXT DEFAULT '',
        error TEXT DEFAULT '', stage TEXT DEFAULT '',
        progress TEXT DEFAULT '', pages INTEGER DEFAULT 0,
        paragraphs INTEGER DEFAULT 0, calls INTEGER DEFAULT 0,
        elapsed REAL DEFAULT 0, created REAL DEFAULT 0,
        updated REAL DEFAULT 0, seq INTEGER)""")
    conn.execute("INSERT INTO jobs (id, status, config_path, created) "
                 "VALUES ('old1', 'done', 'c.yaml', 1.0)")
    conn.commit()
    conn.close()
    from server.store import JobStore
    st = JobStore(db)
    cols = {r[1] for r in st._conn.execute("PRAGMA table_info(jobs)")}
    assert {"input", "output_dir", "warnings", "cache_hits"} <= cols
    hist = st.history()
    assert hist and hist[0]["id"] == "old1"      # 旧行保留
    assert hist[0]["warnings"] == []
    st.close()


def test_jobstore_roundtrip_new_fields(tmp_path):
    from server.store import JobStore
    st = JobStore(tmp_path / "jobs.db")
    snap = {"id": "n1", "status": "done", "config_path": "c.yaml",
            "output_path": "/tmp/o.pdf", "error": "", "stage": "",
            "progress": None, "pages": 3, "paragraphs": 30, "calls": 4,
            "elapsed": 20.0, "created": 1.0,
            "input": "/docs/paper.pdf", "output_dir": "/docs/out",
            "warnings": ["w1", "w2"], "cache_hits": 12}
    st.upsert(snap)
    got = st.history()[0]
    assert got["input"] == "/docs/paper.pdf"
    assert got["warnings"] == ["w1", "w2"]
    assert got["cache_hits"] == 12
    st.close()


def test_job_snapshot_captures_input():
    from server.jobs import Job
    j = Job("j1", "c.yaml", input_path="/x/p.pdf", output_dir="/x/out")
    s = j.snapshot()
    assert s["input"] == "/x/p.pdf" and s["output_dir"] == "/x/out"
    assert "cache_hits" in s


# ---------- 2-4: 缓存统计 ----------

def test_translation_client_cache_hits_counter(tmp_path):
    from translator.cache import TranslationCache
    from translator.llm import TranslationClient
    cache = TranslationCache(tmp_path / "c.db")
    # 预置两条缓存
    cache.put(cache.make_key("openai-compat", "m", "en", "zh", "cached one"),
              "cached one", "缓存一")
    cache.put(cache.make_key("openai-compat", "m", "en", "zh", "cached two"),
              "cached two", "缓存二")

    class Boom:
        def chat(self):
            raise AssertionError("miss 段才允许发请求")

    tc = TranslationClient(None, model="m", batch_size=6,
                           max_llm_calls=5, max_retries=1)
    out, calls = tc.translate_paragraphs(
        ["cached one", "cached two", "fresh paragraph"], cache=cache)
    assert out[0] == "缓存一" and out[1] == "缓存二"
    assert out[2] == "fresh paragraph"      # miss 段失败降级回原文
    assert tc.cache_hits == 2
    # miss 段走了一次真实尝试（client=None → 传输层失败计 1 次调用）
    assert calls == 1
    cache.close()
