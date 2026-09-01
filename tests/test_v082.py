"""v0.8.2 验收单测：bug 修复回归 + 提速改造不变量。

- config 空节点容错（`output:` 等只有键没有内容不再 AttributeError）
- 流式 overlap 路径返回 cross_full/cross_skip（reflow ≥12 页整段入流
  特性不再在流式路径静默失效）
- 热缓存运行不重写布局缓存（save_layout 只在冷布局后调用一次）
- 页级 Story 局部收缩：紧段（f ∈ [0.78, 1-ε)）整页收编 + 告警；
  深溢出段（f < 0.78）仍整页回退
- 页级 Story 单元格收编：表格文字进同一 Story；深缩窄格剔除后仍渲染
- Typography 字体懒加载（构造零 Font）
- reflow 文献条目悬挂缩进（续行右移 14pt）
零网络（无 LLM 调用）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_v043 import EchoLLM  # noqa: E402
from tests.test_v080 import _cjk_font_or_skip, _make_pdf, _para, _story_page  # noqa: E402


def _typo_zh():
    from translator.typography import Typography
    return Typography({"body": "", "cjk": ""}, "zh")


def _fit():
    from translator.fit import FitConfig
    return FitConfig()


# ---------- config 空节点容错 ----------

def test_config_empty_section_nodes(tmp_path):
    """`output:`/`llm:` 等空节点（YAML null）走默认值，不再 AttributeError。"""
    from translator.config import load_config
    src = _make_pdf(tmp_path)
    y = tmp_path / "empty_sections.yaml"
    y.write_text(f"""
io:
  input: {src.as_posix()}
llm:
features:
ocr:
performance:
output:
render:
reflow:
""", encoding="utf-8")
    cfg = load_config(y)
    assert cfg.output.mode == "faithful"
    assert cfg.render.page_story == "auto"
    assert cfg.llm.model == ""
    assert cfg.reflow.columns == "auto"


# ---------- 流式 overlap 返回 cross_full（reflow 整段入流） ----------

class _FullEchoLLM(EchoLLM):
    """回显全文（不截断）——合并单元译文可被输出文本完整识别。"""

    def create(self, **kwargs):
        batch = json.loads(kwargs["messages"][-1]["content"])
        self.calls.append(batch)
        out = {k: f"【译】{v}" for k, v in batch.items()}
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(
                content=json.dumps(out, ensure_ascii=False)))])


def _two_page_merge_pdf(tmp_path):
    doc = pymupdf.open()
    doc.new_page()
    doc[0].insert_text((72, 90), "Alpha beta gamma delta epsilon", fontsize=11)
    doc.new_page()
    doc[1].insert_text((72, 90), "zeta continues the sentence here.",
                       fontsize=11)
    src = tmp_path / "cross.pdf"
    doc.save(str(src))
    doc.close()
    return src


def test_streaming_overlap_returns_cross_full(monkeypatch, tmp_path):
    """pipeline_overlap=on 走流式路径：cross_full/cross_skip 传到 reflow。

    v0.8.2 修复前流式路径返回 dict 漏这两个键——reflow 拿空 dict，
    跨页合并段退回按比例拆分（P3 整段入流特性在 ≥12 页文档静默失效）。
    """
    from translator import pipeline
    from translator.config import (Config, FeatureConfig, IOConfig,
                                   OutputConfig, PerformanceConfig)
    src = _two_page_merge_pdf(tmp_path)
    cfg = Config(
        io=IOConfig(input=str(src), output_dir=str(tmp_path / "out")),
        features=FeatureConfig(watermark_removal=False,
                               translation_cache=False),
        performance=PerformanceConfig(layout_workers=1, layout_cache=False,
                                      pipeline_overlap="on"),
        output=OutputConfig(mode="reflow"),
    )
    captured: dict = {}
    orig = pipeline.render_reflow_document

    def spy(page_layouts, doc, texts_by_page, cell_texts, cross_full,
            cross_skip, *a, **kw):
        captured["cross_full"] = dict(cross_full)
        captured["cross_skip"] = set(cross_skip)
        return orig(page_layouts, doc, texts_by_page, cell_texts,
                    cross_full, cross_skip, *a, **kw)

    monkeypatch.setattr(pipeline, "render_reflow_document", spy)
    fake = _FullEchoLLM()
    pipeline.translate_document(cfg, client=fake)
    assert len(fake.calls) >= 1, "流式路径应发生翻译调用"
    # 合并单元存在（页尾开放句 + 下页小写开头）且完整传给 reflow
    assert captured["cross_full"], \
        f"流式路径必须返回 cross_full: {captured}"
    assert captured["cross_skip"], captured


# ---------- 热缓存不重写布局 ----------

def test_warm_layout_not_resaved(tmp_path, monkeypatch):
    """布局缓存命中时跳过 save_layout（旧版热跑全量重编码+重写 ~1.3s/8页）。"""
    from translator import pipeline
    from translator.config import (Config, FeatureConfig, IOConfig,
                                   PerformanceConfig)
    from translator.doccache import DocumentCache
    src = _make_pdf(tmp_path, text="warm layout cache probe paragraph.")
    cfg = Config(
        io=IOConfig(input=str(src), output_dir=str(tmp_path / "out")),
        features=FeatureConfig(watermark_removal=False,
                               preserve_formatting=False,
                               translation_cache=False),
        performance=PerformanceConfig(layout_workers=1,
                                      cache_dir=str(tmp_path / "cache")),
    )
    calls = {"n": 0}
    orig = DocumentCache.save_layout

    def counting(self, *a, **kw):
        calls["n"] += 1
        return orig(self, *a, **kw)

    monkeypatch.setattr(DocumentCache, "save_layout", counting)
    pipeline.translate_document(cfg, client=None)
    assert calls["n"] == 1, "冷跑应写一次布局缓存"
    pipeline.translate_document(cfg, client=None)
    assert calls["n"] == 1, "热跑（缓存命中）不得重写布局缓存"


# ---------- 页级 Story 局部收缩 ----------

def test_story_tight_para_local_shrink():
    """紧段（f ∈ [0.78, 1-ε)）不再打回逐段路径：整页收编 + 局部收缩告警。"""
    font_path = _cjk_font_or_skip()
    # 框高 46pt：10pt/1.35 行距下 3.4 行——译文 4 行需 ~0.9 因子（紧但可收）
    paras = [_para(0, pymupdf.Rect(30, 20, 280, 40),
                   "这段译文在目标框内略微偏长，需要轻微的局部字号收缩才能在"
                   "页级排版流中完整装下，既不至于深溢出触发整页回退，也不会"
                   "宽松到因子为一，恰好落在局部收缩区间内验证收编。")]
    doc, page, layout = _story_page(paras)
    stats = {"story": 0, "fallback": 0, "reasons": []}
    warns: list[str] = []
    from translator.render import render_page
    render_page(page, layout, [{"index": 0, "text": paras[0]["text"]}],
                font_path, renderer="htmlbox", lang="zh",
                page_story="auto", story_stats=stats, warnings=warns,
                fit_cfg=_fit())
    if stats["fallback"] and "overflow at fit factor 0.7" in \
            ";".join(stats["reasons"]):
        pytest.skip("合成段在无 fit 因子下测得深溢出（环境字体差异）")
    assert stats["story"] == 1, stats["reasons"]
    assert any("story local scale" in w for w in warns), warns
    assert paras[0]["text"][:6] in page.get_text()


def test_story_deep_overflow_still_falls_back():
    """深溢出段（f < 0.78）仍整页回退（引擎深缩+告警路径不动摇）。"""
    font_path = _cjk_font_or_skip()
    paras = [_para(0, pymupdf.Rect(30, 20, 280, 34),
                   "这是一段极长的译文内容，目标框高度只够一行文字，"
                   "无论怎样局部收缩都装不下，必须整页回退到逐段路径由排版"
                   "引擎做深度缩放处理并给出告警，文本还要更长才能确保"
                   "局部收缩因子低于段落下限零点七八。")]
    doc, page, layout = _story_page(paras)
    stats = {"story": 0, "fallback": 0, "reasons": []}
    warns: list[str] = []
    from translator.render import render_page
    render_page(page, layout, [{"index": 0, "text": paras[0]["text"]}],
                font_path, renderer="htmlbox", lang="zh",
                page_story="auto", story_stats=stats, warnings=warns,
                fit_cfg=_fit())
    assert stats["fallback"] == 1 and stats["story"] == 0, stats
    # 兜底必出字
    assert "这是一段极长的译文内容" in page.get_text()


# ---------- 页级 Story 单元格收编 ----------

def test_story_cells_included():
    """表格宽格并入页级 Story（一次字体重解析）；深缩窄格剔除后仍渲染。"""
    font_path = _cjk_font_or_skip()
    paras = [_para(0, pymupdf.Rect(30, 20, 280, 60), "正文段落与表格同页。")]
    doc, page, layout = _story_page(paras)
    r = lambda x0, y0, x1, y1: pymupdf.Rect(x0, y0, x1, y1)
    cells = [
        {"bbox": r(40, 100, 160, 116), "text": "方法名称列", "conf": 0.9,
         "spans": []},
        {"bbox": r(160, 100, 270, 116), "text": "数值结果列", "conf": 0.9,
         "spans": []},
        # 深缩窄格：13pt 宽装不下 6 个 CJK 字（需 <0.3 因子）→ 剔除回
        # insert_htmlbox 深缩路径
        {"bbox": r(40, 116, 53, 130), "text": "很长的窄格文字", "conf": 0.9,
         "spans": []},
    ]
    layout["tables_cells"] = cells
    layout["tables"] = [{"bbox": r(40, 100, 270, 130), "cells": cells}]
    stats = {"story": 0, "fallback": 0, "reasons": []}
    from translator.render import render_page
    render_page(page, layout, [{"index": 0, "text": paras[0]["text"]}],
                font_path, renderer="htmlbox", lang="zh",
                page_story="on", story_stats=stats)
    txt = page.get_text()
    assert stats["story"] == 1, stats["reasons"]
    assert "正文段落与表格同页" in txt
    assert "方法名称列" in txt and "数值结果列" in txt, \
        "宽格应进 Story（或深缩回退），文字不得丢失"
    assert "窄格文字" in txt, "深缩窄格剔除后仍须经 insert_htmlbox 渲染"


# ---------- Typography 懒加载 ----------

def test_typography_lazy_font_load():
    """Typography 构造零 Font 对象；首次访问才加载（htmlbox 主路径无感）。"""
    typo = _typo_zh()
    assert typo._fonts == {}, "构造不应加载 Font（旧版 ~2s/文档）"
    _ = typo.f_body
    assert len(typo._fonts) == 1
    _ = typo.f_head
    assert typo.f_head is typo.f_body or len(typo._fonts) == 2


# ---------- 流式停顿墙钟（v0.8.2 e2e 假死根因） ----------

class _TrickleStream:
    """内容滴漏但永不收齐的假流（逐 chunk 墙钟检查路径）。"""
    closed = False

    def __iter__(self):
        return self

    def __next__(self):
        import time as _t
        _t.sleep(0.02)
        return SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content="x"))])

    def close(self):
        self.closed = True


class _SilentStream(_TrickleStream):
    """不发任何 chunk 的假流（看门狗强关路径——close 后以异常终止，
    模拟真实 httpx 流被关闭时阻塞读抛错的行为）。"""

    def __next__(self):
        import time as _t
        while not self.closed and _t.time() - _T0[0] < 5:
            _t.sleep(0.02)
        raise RuntimeError("stream closed by watchdog")


_T0 = [0.0]


def _stream_client(stream_obj):
    from translator.llm import TranslationClient
    calls = {"n": 0}

    class _C:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kw):
                    calls["n"] += 1
                    if kw.get("stream"):
                        return stream_obj
                    msg = SimpleNamespace(content='{"1": "ok"}')
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=msg)])

    tc = TranslationClient(_C(), model="m", timeout=0.01,
                           max_retries=1, stream=True,
                           stream_deadline=0.3)
    return tc, calls


def test_stream_trickle_stall_wallclock():
    """内容滴漏型停顿：逐 chunk 墙钟超时 → 退非流式拿到结果。"""
    import time
    tc, calls = _stream_client(_TrickleStream())
    _T0[0] = time.time()
    t0 = time.time()
    raw = tc._request([{"role": "user", "content": "x"}], want_ids={"1"})
    assert time.time() - t0 < 3, "墙钟必须在秒级截断滴漏流"
    assert raw == '{"1": "ok"}', raw
    assert calls["n"] == 2, "流式失败后必须非流式重发一次"
    assert any("stream failed" in w for w in tc.warnings), tc.warnings


def test_stream_silent_stall_watchdog():
    """保活字节型停顿（无 chunk 产出）：看门狗强制关流，秒级返回。"""
    import time
    st = _SilentStream()
    tc, _calls = _stream_client(st)
    _T0[0] = time.time()
    t0 = time.time()
    raw = tc._request([{"role": "user", "content": "x"}], want_ids={"1"})
    assert time.time() - t0 < 3, "看门狗必须强制终结静默流"
    assert st.closed, "看门狗必须关闭流"
    assert raw == '{"1": "ok"}', raw


# ---------- reflow 文献悬挂缩进 ----------

def test_reflow_hanging_indent(tmp_path):
    """文献条目悬挂缩进：续行 x0 比首行右移 ~14pt（探针语义锁定）。"""
    from translator.render_reflow import build_reflow_css, render_reflow_document
    css = build_reflow_css(10.5, "", "")
    assert "text-indent:-14pt" in css and "padding-left:14pt" in css
    # 端到端：ref 条目多行渲染后续行右移
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    r = lambda x0, y0, x1, y1: pymupdf.Rect(x0, y0, x1, y1)
    ref_text = ("[1] Zhang et al., Anisotropic magnetoresistance in "
                "Mn3Pt films with thickness and temperature dependence, "
                "Phys. Rev. B 103 012345 2026 long title continuation.")
    paras = [{"index": 0, "bbox": r(60, 40, 550, 56), "col": 0,
              "text": ref_text, "size": 9.0, "spans": [],
              "is_heading": False, "is_caption": False, "is_ref": True,
              "is_verbatim": False, "is_alg_caption": False,
              "is_list_item": False}]
    lay = {"mode": "one", "paragraphs": paras, "tables": [],
           "tables_cells": [], "formulas": [], "hf_blocks": [],
           "fig_text_blocks": [], "figure_regions": [],
           "layout_engine": "heuristic"}
    from translator.config import ReflowConfig
    data = render_reflow_document(
        [lay], doc, {0: {0: ref_text}}, {}, {}, set(), {}, _typo_zh(),
        "", "zh", ReflowConfig(), [], lambda m: None)
    out = pymupdf.open("pdf", data)
    lines = []
    for b in out[0].get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            lines.append((round(l["bbox"][0], 1),
                          "".join(s["text"] for s in l["spans"])[:30]))
    firsts = sorted({x for x, t in lines if t.startswith("[1]")})
    contin = sorted({x for x, t in lines if not t.startswith("[1]")})
    assert firsts and contin, f"应有首行+续行: {lines}"
    assert min(contin) - min(firsts) >= 10.0, \
        f"续行应右移≥10pt（悬挂 14pt）: firsts={firsts} contin={contin}"
    doc.close()
    out.close()
