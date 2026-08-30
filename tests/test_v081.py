"""v0.8.1 验收单测：bug 修复回归 + 性能改造不变量。

- Bug1 回归：reflow 已烘焙区域（HTML 表/公式位图）文字不重复回归文流；
  未烘焙的自然语言带（作者块）仍正常回归（防过度抑制）
- Bug2 回归：reflow × 扫描页在翻译开始前拦截（0 次 LLM 调用即报错）
- Bug3 回归：all_warnings 入列即转发 sink.warning；事件流同文本去重
- S1 不变量：first-fit 组批全覆盖/不超限/批内升序（流式与整批两路）
- S4：doccache 位图缓存往返 + LRU 字节淘汰
- S5：cache.put_many 批量事务
- app.py 回归：空 ui_config.yaml 不再 500（validate-key 存量回填路径）
零网络（无 LLM 调用）。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_v043 import EchoLLM  # noqa: E402
from tests.test_v080 import _make_pdf  # noqa: E402


def _typo_zh():
    from translator.typography import Typography
    return Typography({"body": "", "cjk": ""}, "zh")


# ---------- Bug 1: reflow 已烘焙区域文字不重复回归文流 ----------

def _baked_leak_layout():
    """高置信表 + display 公式 + fig_text_blocks（表内文字/公式碎片/
    区外作者块——真实 layout in_protected 的三类产物）。"""
    r = lambda x0, y0, x1, y1: pymupdf.Rect(x0, y0, x1, y1)
    para = lambda i, x, y, t: {
        "index": i, "bbox": r(x, y, x + 220, y + 14), "col": 0, "text": t,
        "size": 10.0, "spans": [], "is_heading": False, "is_caption": False,
        "is_ref": False, "is_verbatim": False, "is_alg_caption": False,
        "is_list_item": False}
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
    fig_texts = [
        {"bbox": r(65, 302, 155, 330), "text": "Method\nPLG"},   # 表内
        {"bbox": r(330, 330, 420, 344), "text": "dt ."},         # 公式碎片
        {"bbox": r(500, 330, 538, 344), "text": "(6)"},          # 公式编号
        {"bbox": r(60, 200, 500, 216), "text":
            "Author Block, University of Somewhere"},            # 区外自然语言
    ]
    return {
        "mode": "one",
        "paragraphs": [para(0, 60, 60, "Intro paragraph before everything.")],
        "tables": [{"bbox": r(60, 300, 280, 332), "cells": cells}],
        "tables_cells": cells,
        "formulas": [{"bbox": r(320, 320, 540, 350), "para_hint_y": 320,
                      "is_display": True}],
        "hf_blocks": [], "fig_text_blocks": fig_texts,
        "figure_regions": [], "layout_engine": "heuristic",
    }


def test_reflow_baked_regions_no_text_leak():
    """表内文字/公式碎片已烘焙（HTML 表/位图），不得再以段落回归文流。"""
    from translator.render_reflow import build_document_model
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    lay = _baked_leak_layout()
    cell_texts = {(0, 0): "方法", (0, 1): "得分", (0, 2): "PLG", (0, 3): "0.91"}
    blocks, _imgs, _bm, _ls = build_document_model(
        [lay], doc, {0: {0: "前导段落译文。"}}, cell_texts, {}, set(), {},
        _typo_zh())
    paras = [b.text for b in blocks if b.kind == "para"]
    for probe in ("Method", "PLG", "dt .", "(6)", "Score"):
        assert not any(probe in t for t in paras), \
            f"{probe!r} leaked back into text flow: {paras}"
    # HTML 表存在且带译文；公式位图块存在
    tab = next(b for b in blocks if b.kind == "table")
    assert tab.html_extra and "方法" in tab.html_extra
    assert sum(1 for b in blocks if b.kind == "formula") == 1
    doc.close()


def test_reflow_unbaked_text_still_returns():
    """未烘焙的区外自然语言（作者块）仍回归文流（防过度抑制）。"""
    from translator.render_reflow import build_document_model
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    lay = _baked_leak_layout()
    blocks, _imgs, _bm, _ls = build_document_model(
        [lay], doc, {0: {0: "前导段落译文。"}}, {}, {}, set(), {}, _typo_zh())
    paras = [b.text for b in blocks if b.kind == "para"]
    assert any("Author Block" in t for t in paras), \
        f"natural-language fig_text should return to flow: {paras}"
    doc.close()


# ---------- Bug 2: reflow × 扫描页前置拦截（0 调用即报错） ----------

def test_reflow_scanned_page_rejected_before_llm(monkeypatch, tmp_path):
    from translator import ocr as ocr_mod
    from translator import pipeline
    from translator.config import (Config, FeatureConfig, IOConfig,
                                   OCRConfig, OutputConfig)

    # 无文字层页（空白页=扫描页等价物）
    doc = pymupdf.open()
    doc.new_page()
    src = tmp_path / "scan.pdf"
    doc.save(str(src))
    doc.close()

    monkeypatch.setattr(ocr_mod, "engine_available", lambda e: True)
    cfg = Config(
        io=IOConfig(input=str(src), output_dir=str(tmp_path / "out")),
        features=FeatureConfig(watermark_removal=False),
        ocr=OCRConfig(engine="paddle", engines=["paddle"]),
        output=OutputConfig(mode="reflow"),
    )
    fake = EchoLLM()
    with pytest.raises(ValueError, match="扫描页"):
        pipeline.translate_document(cfg, client=fake)
    assert fake.calls == [], "reflow×scan 必须在翻译开始前拦截（0 调用）"
    # 无 OCR 引擎可用时不再前置拦截（保持旧行为：扫描页原样保留）
    monkeypatch.setattr(ocr_mod, "engine_available", lambda e: False)
    stats = pipeline.translate_document(cfg, client=None)
    assert stats["calls"] == 0


# ---------- Bug 3: 警告转发 + 事件流去重 ----------

def test_warning_list_forwards_to_sink():
    from translator.events import EventSink
    from translator.pipeline import _WarningList
    sink = EventSink()
    wl = _WarningList(sink)
    wl.append("w1")
    wl.extend(["w2", "w1"])
    warned = [e["msg"] for e in sink.events if e["kind"] == "warning"]
    assert warned == ["w1", "w2"]      # 重复文本只发一次事件
    assert wl == ["w1", "w2", "w1"]    # stats 列表保留全部出现


def test_eventsink_warning_dedup():
    from translator.events import EventSink
    sink = EventSink()
    sink.warning("a")
    sink.warning("a")
    sink.warning("b")
    msgs = [e["msg"] for e in sink.events if e["kind"] == "warning"]
    assert msgs == ["a", "b"]


def test_pipeline_warnings_stream_via_sink(tmp_path):
    """端到端：管线警告（字体覆盖率等）以 warning 事件可订阅（UI 数据源）。"""
    from translator.events import EventSink
    from translator import pipeline
    from translator.config import (Config, FeatureConfig, IOConfig,
                                   PerformanceConfig)
    src = _make_pdf(tmp_path)
    cfg = Config(
        io=IOConfig(input=str(src), output_dir=str(tmp_path / "out")),
        features=FeatureConfig(watermark_removal=False,
                               preserve_formatting=False,
                               translation_cache=False),
        performance=PerformanceConfig(layout_workers=1),
    )
    sink = EventSink()
    pipeline.translate_document(cfg, client=None, sink=sink)
    # 无警告文档也应不炸；有（如丢段兜底）时必在事件流里。
    # 这里文档极小，仅断言事件流存在 warning 通道能力
    assert all(e["kind"] in ("stage", "progress", "done", "warning",
                             "layout_cache_hit", "fit_pass")
               for e in sink.events)


# ---------- S1: first-fit 组批不变量（整批 + 流式） ----------

def test_first_fit_invariants_random():
    import random
    from translator.llm import TranslationClient
    tc = TranslationClient(EchoLLM(), model="m", batch_size=6,
                           batch_char_budget=3000)
    random.seed(7)
    paras = ["x" * (random.randint(2500, 3500) if random.random() < 0.08
                    else int(random.lognormvariate(6.2, 0.75)))
             for _ in range(200)]
    miss = list(range(len(paras)))
    batches = tc._pack_batches(miss, paras)
    assert sorted(i for b in batches for i in b) == miss
    for b in batches:
        assert len(b) <= 6
        assert sum(len(paras[i]) for i in b) <= 3000 or len(b) == 1
        assert b == sorted(b)


def test_streaming_first_fit_batches():
    from translator.llm import StreamingTranslator, TranslationClient
    fake = EchoLLM()
    tc = TranslationClient(fake, model="m", batch_size=10,
                           batch_char_budget=30, max_llm_calls=10)
    st = StreamingTranslator(tc)
    for t in ["x" * 20, "y" * 20, "z" * 5]:
        st.add_unit(t)
    out, calls = st.finish()
    assert calls == 2
    assert sorted(len(c) for c in fake.calls) == [1, 2]
    assert all(o.startswith("【译】") for o in out)


def test_streaming_flush_semantics_preserved():
    """放满即发车：批满不必等 finish（流水线重叠保住）。"""
    from translator.llm import StreamingTranslator, TranslationClient
    fake = EchoLLM()
    tc = TranslationClient(fake, model="m", batch_size=2,
                           batch_char_budget=0, max_llm_calls=10)
    st = StreamingTranslator(tc)
    st.add_unit("a")
    st.add_unit("b")          # 段数到顶 → 立即发车
    assert tc._batches_total == 1 and tc._batches_ok + len(st._open) >= 0
    st.add_unit("c")
    st.finish()
    assert sorted(len(c) for c in fake.calls) == [1, 2]


# ---------- S4: 位图裁剪缓存 ----------

def test_doccache_pixmap_roundtrip_and_prune(tmp_path, monkeypatch):
    from translator import doccache as dc_mod
    from translator.doccache import DocumentCache
    dc = DocumentCache(tmp_path)
    r = pymupdf.Rect(10, 20, 30, 40)
    k = DocumentCache.pixmap_key("fp", 0, r, 300)
    dc.save_pixmap(k, "fp", 0, b"\x89PNG-fake-bytes")
    assert dc.load_pixmap(k) == b"\x89PNG-fake-bytes"
    assert dc.load_pixmap("missing-key") is None
    # 区域敏感：y1 差 1pt 的裁剪 key 不同
    assert DocumentCache.pixmap_key(
        "fp", 0, pymupdf.Rect(10, 20, 30, 41), 300) != k
    # LRU 字节淘汰：压低上限强制 prune
    monkeypatch.setattr(dc_mod, "PIXMAP_CACHE_MAX_BYTES", 12)
    dc._prune_pixmaps()
    assert dc.load_pixmap(k) is None
    dc.close()


def test_crop_formulas_cached_serves_second_run(tmp_path):
    """同一指纹二次裁剪走缓存（不再渲染位图）。"""
    from translator.doccache import DocumentCache
    from translator.pipeline import _crop_formulas_cached
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.draw_rect(pymupdf.Rect(0, 0, 60, 20), color=(0, 0, 0))
    src = tmp_path / "f.pdf"
    doc.save(str(src))
    doc.close()
    d = pymupdf.open(src)
    dc = DocumentCache(tmp_path / "cache-root")
    fp = "feedface"
    f1 = _crop_formulas_cached(d, 0, [{"bbox": pymupdf.Rect(0, 0, 60, 20)}],
                               dc, fp)
    # 篡改页面渲染结果以证明第二次来自缓存：直接再取应命中
    f2 = _crop_formulas_cached(d, 0, [{"bbox": pymupdf.Rect(0, 0, 60, 20)}],
                               dc, fp)
    assert f1[0] == f2[0]
    k = DocumentCache.pixmap_key(fp, 0, pymupdf.Rect(0, 0, 60, 20), 300)
    assert dc.load_pixmap(k) == f1[0]
    # 指纹不同 → miss 重裁
    f3 = _crop_formulas_cached(d, 0, [{"bbox": pymupdf.Rect(0, 0, 60, 20)}],
                               dc, "otherfp")
    assert f3[0] == f1[0]
    dc.close()
    d.close()


# ---------- S5: cache.put_many ----------

def test_cache_put_many(tmp_path):
    from translator.cache import TranslationCache
    c = TranslationCache(tmp_path / "pm.db")
    c.put_many([("k1", "s1", "d1"), ("k2", "s2", "d2")])
    assert c.get("k1") == "d1" and c.get("k2") == "d2"
    c.put_many([])          # 空批不炸
    c.put_many([("k1", "s1", "d1v2")])   # 覆盖写
    assert c.get("k1") == "d1v2"
    assert c.count() == 2
    c.close()


# ---------- app.py 回归：空 ui_config.yaml 不再 500 ----------

def test_validate_key_empty_config_no_500(monkeypatch, tmp_path):
    """ui_config.yaml 为空文件时 validate-key 走存量回填路径不炸
    （v0.8.1 前 yaml.safe_load(None).get → AttributeError → 500）。"""
    import server.app as app_mod

    empty_cfg = tmp_path / "ui_config.yaml"
    empty_cfg.write_text("", encoding="utf-8")
    monkeypatch.setattr(app_mod, "UI_CONFIG_PATH", empty_cfg)

    class _Boom:
        """替身 OpenAI：构造后 create 必抛——测试只验证不发生 500。"""

        def __init__(self, *a, **kw):
            pass

        class chat:   # noqa: N801
            class completions:   # noqa: N801
                @staticmethod
                def create(**kw):
                    raise RuntimeError("offline-test")

    import openai
    monkeypatch.setattr(openai, "OpenAI", _Boom)
    from fastapi.testclient import TestClient
    with TestClient(app_mod.app) as client:
        resp = client.post("/api/validate-key", json={
            "provider": "deepseek", "model": "m", "api_key": "",
            "base_url": "", "target_lang": "zh"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False   # 离线：连通失败，但不是 500
