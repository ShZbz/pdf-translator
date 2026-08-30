"""v0.7.0 验收单测：

- 流式解码 + 首包即回（_parse_pairs 增量解析 / _request 流式早断 /
  网关无视 stream 参数回退）
- 批内分段重试（部分 id 缺失只重问缺失段，不再整批重试/丢弃）
- 句子级缓存（ref 条目句段全命中免调；句数对齐回填）
- 术语锁确定性修复（源词逐字残留 → 原位替换，零调用）
- 多引擎 OCR 投票（IoU 对齐 / 冲突取高置信 / 冲突计数）
- 几何版面分割（双栏 + 段间隙，与识别文本无关）
- 影子页 GNN 区域分类（table/picture 保留、页眉页脚丢弃、漏检行兜底）
- 布局-翻译流水线重叠（overlap 开关逻辑 + 端到端译文分布与顺序路径一致）
- 嵌套表地狱样张（合并表头 / 数字右对齐 / 跨页延续）+ 置信度分级
- render htmlbox + fit off 回归（v0.5.1 路径不再踩 layout NameError）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from translator.cache import TranslationCache
from translator.config import (Config, FeatureConfig, IOConfig, LLMConfig,
                               OCRConfig, PerformanceConfig, load_config)
from translator.glossary import Glossary
from translator.layout import (link_crosspage_tables, table_cells)
from translator.llm import TranslationClient, _parse_pairs
from translator.ocr import (ocr_page_lines_voted, region_blocks_geometry)
from translator.pipeline import (_blocks_from_gnn_regions,
                                 _cell_translatable, _overlap_enabled,
                                 translate_document)


# ---------- 通用假 client ----------

class EchoLLM:
    """回显式假 client：id → 【译】+ 原文前缀；记录每次批内容。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        batch = json.loads(kwargs["messages"][-1]["content"])
        self.calls.append(batch)
        out = {k: f"【译】{v[:20]}" for k, v in batch.items()}
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(out, ensure_ascii=False)))])


class QueueLLM:
    """按队列回放的假 client（精细控制每次响应）。"""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        batch = json.loads(kwargs["messages"][-1]["content"])
        self.calls.append(batch)
        raw = self._responses.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=raw))])


def _make_pdf(pages_text: list[str], tmp_path: Path, name="t.pdf") -> Path:
    doc = pymupdf.open()
    for txt in pages_text:
        page = doc.new_page()
        if txt:
            page.insert_text((72, 90), txt, fontsize=11)
    p = tmp_path / name
    doc.save(str(p))
    doc.close()
    return p


def _cfg(src: Path, tmp_path: Path, **kw) -> Config:
    perf_kw = {"layout_workers": 1}
    perf_kw.update(kw.pop("performance", {}))
    return Config(
        io=IOConfig(input=str(src), output_dir=str(tmp_path / "out")),
        llm=LLMConfig(**{k: v for k, v in kw.items() if k in
                         ("model", "batch_size", "max_llm_calls",
                          "batch_char_budget", "max_workers")}),
        features=FeatureConfig(watermark_removal=False,
                               preserve_formatting=False,
                               translation_cache=False),
        performance=PerformanceConfig(**perf_kw),
    )


# ---------- 1. 流式增量解析 ----------

def test_parse_pairs_complete_fast_path():
    raw = 'prelude {"1": "甲", "2": "乙"} postlude'
    got, missing = _parse_pairs(raw, {"1", "2"})
    assert got == {"1": "甲", "2": "乙"} and missing == set()


def test_parse_pairs_partial_stream_buffer():
    """流式半途：id1 已闭合提交，id2 未闭合不收。"""
    raw = '{"1": "第一段", "2": "第二段半截'
    got, missing = _parse_pairs(raw, {"1", "2"})
    assert got == {"1": "第一段"}
    assert missing == {"2"}


def test_parse_pairs_escaped_values():
    raw = '{"1": "引号\\"内嵌\\n换行", "2": "ok"}'
    got, _ = _parse_pairs(raw, {"1", "2"})
    assert got["1"] == '引号"内嵌\n换行'


def test_parse_pairs_garbage_returns_none():
    got, missing = _parse_pairs("I think the answer is...", {"1"})
    assert got is None and missing == {"1"}


def test_request_stream_early_break():
    """流式：收齐全部 id 即断流——生成器尾部的废话 token 不消费。"""
    consumed = []

    def gen():
        for chunk in ['{"1": "', "hello", '", ', '"2": "', "world", '"} ',
                      "trailing junk tokens that must not be consumed"]:
            consumed.append(chunk)
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=chunk))])

    class C:
        chat = SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: gen()))

    tc = TranslationClient(C(), model="m", stream=True)
    raw = tc._request([{"role": "user", "content": "x"}],
                      want_ids={"1", "2"})
    got, missing = _parse_pairs(raw, {"1", "2"})
    assert got == {"1": "hello", "2": "world"}
    assert "trailing junk" not in raw
    assert consumed[-1].strip() == '"}'   # 断流点：闭合 } 一到即停


def test_request_stream_fallback_non_iterable():
    """网关/mock 无视 stream 参数直接回完整响应 → 当非流式结果用（不二次请求）。"""

    class C:
        n_calls = 0

        def create(self, **kw):
            C.n_calls += 1
            assert kw.get("stream") is True
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"1": "ok"}'))])

    c = C()
    c.chat = SimpleNamespace(completions=SimpleNamespace(create=c.create))
    tc = TranslationClient(c, model="m", stream=True)
    raw = tc._request([{"role": "user", "content": "x"}], want_ids={"1"})
    assert _parse_pairs(raw, {"1"})[0] == {"1": "ok"}
    assert C.n_calls == 1


# ---------- 2. 批内分段重试 ----------

def test_partial_batch_retries_only_missing():
    """首答只回 id1 → 只重问 id2；两段都译成，不丢整批。"""
    fake = QueueLLM([
        json.dumps({"1": "一号"}, ensure_ascii=False),     # 缺 id2
        json.dumps({"2": "二号"}, ensure_ascii=False),     # 补 id2
    ])
    tc = TranslationClient(fake, model="m", max_retries=2)
    out, calls = tc.translate_paragraphs(["one", "two"])
    assert out == ["一号", "二号"]
    assert calls == 2
    # 第二次调用只带缺失的 id2
    assert list(fake.calls[1].keys()) == ["2"]
    assert not any("kept source" in w for w in tc.warnings)


def test_partial_batch_missing_after_retry_keeps_source_per_segment():
    """重试仍缺 → 仅缺失段保留原文，成功段照常回填。"""
    fake = QueueLLM([
        json.dumps({"1": "first"}, ensure_ascii=False),    # id2 永远缺
        json.dumps({"1": "first"}, ensure_ascii=False),
    ])
    tc = TranslationClient(fake, model="m", max_retries=2)
    out, _ = tc.translate_paragraphs(["one", "two"])
    assert out == ["first", "two"]        # 段2 保留原文
    assert any("kept source" in w for w in tc.warnings)
    assert any("#2" in w for w in tc.warnings)


# ---------- 3. 句子级缓存 ----------

def test_sentence_cache_ref_entry_roundtrip(tmp_path):
    """ref 条目首次翻译 → 句缓存回填；换一篇文档同句段 → 全命中免调。"""
    db = tmp_path / "t.db"
    cache = TranslationCache(db)
    src_a = "J. Doe. Physics of kagome. Nature 123, 45 (2020)."
    src_b = "Different lead text here. Nature 123, 45 (2020)."  # 共享后句
    fake = QueueLLM([
        json.dumps({"1": "J. Doe. kagome 物理。《自然》123, 45 (2020)。"},
                   ensure_ascii=False),
        json.dumps({"1": "不同开头。《自然》123, 45 (2020)。"},
                   ensure_ascii=False),
    ])
    tc = TranslationClient(fake, model="m", max_llm_calls=5,
                           sentence_cache=True)
    out1, _ = tc.translate_paragraphs([src_a], cache=cache,
                                      unit_kinds=["ref"])
    assert "kagome 物理" in out1[0]
    # 文档 B：句段部分命中 → 不拼装（走整段翻译），成功后句数对齐回填
    out2, _ = tc.translate_paragraphs([src_b], cache=cache,
                                      unit_kinds=["ref"])
    assert out2[0] is not None
    # 文档 A 再跑：句段全命中 → 0 调用
    tc2 = TranslationClient(QueueLLM([]), model="m", max_llm_calls=5,
                            sentence_cache=True)
    out3, calls3 = tc2.translate_paragraphs([src_a], cache=cache,
                                            unit_kinds=["ref"])
    assert calls3 == 0
    # 句段重拼装会归一化空白——语义等价断言（关键内容都在）
    assert "kagome 物理" in out3[0] and "(2020)" in out3[0]
    assert tc2.sent_cache_hits == 1


def test_sentence_cache_not_used_for_body(tmp_path):
    """正文段（kind=None）不启用句级缓存——上下文依赖句不跨文档复用。"""
    db = tmp_path / "t.db"
    cache = TranslationCache(db)
    tc = TranslationClient(QueueLLM([]), model="m", max_llm_calls=5)
    hit = tc._cache_first(cache, 0, "Some body text. More text.", None, None)
    assert hit is None


# ---------- 4. 术语锁确定性修复 ----------

def test_glossary_fix_translation():
    g = Glossary({"kagome": "笼目", "Hall effect": "霍尔效应"})
    fixed, done = g.fix_translation("The kagome lattice shows Hall effect.")
    assert fixed == "The 笼目 lattice shows 霍尔效应."
    assert sorted(done) == ["Hall effect", "kagome"]
    # 目标词已在 → 不动
    fixed2, done2 = g.fix_translation("笼目晶格 already correct")
    assert done2 == [] and fixed2 == "笼目晶格 already correct"


# ---------- 5. 多引擎 OCR 投票 ----------

def test_ocr_voting_conflict_picks_highest_confidence(monkeypatch):
    from translator import ocr as ocr_mod
    r1a, r1b = pymupdf.Rect(60, 90, 300, 104), pymupdf.Rect(60, 90, 300, 104)
    r2a, r2b = pymupdf.Rect(60, 120, 300, 134), pymupdf.Rect(60, 120, 300, 134)

    def fake_scored(page, engine="paddle", src_lang="en", dpi=200):
        if engine == "paddle":
            return [(r1a, "hello w0rld", 0.80), (r2a, "second", 0.90)]
        if engine == "rapidocr":
            return [(r1b, "hello world", 0.95), (r2b, "second", 0.99)]
        return None

    monkeypatch.setattr(ocr_mod, "engine_available", lambda e: True)
    monkeypatch.setattr(ocr_mod, "ocr_page_lines_scored", fake_scored)
    doc = pymupdf.open()
    page = doc.new_page()
    lines, conflicts = ocr_page_lines_voted(page, engines=["paddle", "rapidocr"])
    texts = [t for _, t in lines]
    assert "hello world" in texts      # 冲突行取置信度高者（0.95 > 0.80）
    assert "second" in texts           # 一致行直接取
    assert conflicts == 1


def test_ocr_voting_single_engine_no_conflict(monkeypatch):
    from translator import ocr as ocr_mod
    monkeypatch.setattr(ocr_mod, "engine_available",
                        lambda e: e == "paddle")
    monkeypatch.setattr(
        ocr_mod, "ocr_page_lines_scored",
        lambda page, engine="paddle", src_lang="en", dpi=200:
            [(pymupdf.Rect(60, 90, 300, 104), "only engine", 0.9)])
    doc = pymupdf.open()
    lines, conflicts = ocr_page_lines_voted(doc.new_page(),
                                            engines=["paddle", "rapidocr"])
    assert [t for _, t in lines] == ["only engine"]
    assert conflicts == 0


# ---------- 6. 几何版面分割 ----------

def test_region_blocks_geometry_two_columns():
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    lines = [(pymupdf.Rect(60, y, 270, y + 10), f"left {y}")
             for y in (100, 112, 124, 190, 202)]
    lines += [(pymupdf.Rect(320, y, 530, y + 10), f"right {y}")
              for y in (100, 112)]
    blocks = region_blocks_geometry(lines, p.rect)
    assert len(blocks) == 3                     # 左栏两段（间隙分断）+ 右栏一段
    left = [b for b in blocks if b[0].x0 < 300]
    assert len(left) == 2
    assert "left 100" in left[0][1] and "left 190" in left[1][1]


# ---------- 7. 影子页 GNN 区域分类 ----------

def test_blocks_from_gnn_regions_preserves_and_skips():
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    regions = [
        (pymupdf.Rect(60, 80, 530, 100), "page-header"),
        (pymupdf.Rect(60, 130, 530, 210), "text"),
        (pymupdf.Rect(60, 240, 530, 360), "table"),
        (pymupdf.Rect(60, 400, 530, 460), "picture"),
    ]
    lines = [
        (pymupdf.Rect(60, 85, 400, 95), "header line"),
        (pymupdf.Rect(60, 140, 400, 150), "body line one"),
        (pymupdf.Rect(60, 155, 380, 165), "body line two"),
        (pymupdf.Rect(60, 250, 200, 260), "table cell text"),      # 表区行
        (pymupdf.Rect(60, 410, 200, 420), "figure label"),          # 图区行
        (pymupdf.Rect(60, 500, 400, 510), "orphan missed by gnn"),  # 漏检行
    ]
    blocks = _blocks_from_gnn_regions(regions, lines, p)
    texts = [t for _, t in blocks]
    assert not any("header line" in t for t in texts)       # 页眉丢弃
    assert not any("table cell" in t for t in texts)        # 表区保留像素
    assert not any("figure label" in t for t in texts)      # 图区保留像素
    assert any("body line one" in t and "body line two" in t
               for t in texts)                              # text 区并块
    assert any("orphan" in t for t in texts)                # 漏检行兜底成块


def test_gnn_shadow_page_on_real_scanned_page():
    """影子页 GNN 在真扫描页（位图无文字层）上产出语义区域（装有
    pymupdf-layout 时）。返回 None 亦接受（未装/失败——调用方回退几何）。"""
    from translator.ocr import gnn_regions_for_lines
    src = pymupdf.open(str(Path(__file__).resolve().parents[2] / "example"
                           / "1.pdf"))
    pix = src[0].get_pixmap(dpi=150)
    out = pymupdf.open()
    page = out.new_page(width=src[0].rect.width, height=src[0].rect.height)
    page.insert_image(page.rect, pixmap=pix)
    # 用原文档真实行 bbox 充当 OCR 行（几何真值）
    lines = []
    for b in src[0].get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b["lines"]:
            t = "".join(s["text"] for s in l["spans"]).strip()
            if t:
                lines.append((pymupdf.Rect(l["bbox"]), t))
    regions = gnn_regions_for_lines(page, lines[:40])
    import importlib.util
    if importlib.util.find_spec("pymupdf.layout") is None:
        assert regions is None
        return
    assert regions is not None and len(regions) >= 3
    kinds = {k for _, k in regions}
    assert any(k == "text" or k.endswith("header") for k in kinds)


# ---------- 8. 布局-翻译流水线重叠 ----------

def test_overlap_enabled_switches():
    cfg = PerformanceConfig()
    assert _overlap_enabled.__name__
    from translator.config import Config as _C
    c = _C(io=IOConfig(input="x", output_dir="y"),
              performance=PerformanceConfig())
    # off 永不启用
    c.performance.pipeline_overlap = "off"
    assert not _overlap_enabled(c, object(), 100, "heuristic")
    # on 有 client 即启用
    c.performance.pipeline_overlap = "on"
    assert _overlap_enabled(c, object(), 2, "heuristic")
    assert not _overlap_enabled(c, None, 100, "heuristic")
    # auto：≥12 页 + heuristic + client
    c.performance.pipeline_overlap = "auto"
    assert _overlap_enabled(c, object(), 12, "heuristic")
    assert not _overlap_enabled(c, object(), 11, "heuristic")
    assert not _overlap_enabled(c, object(), 50, "pymupdf-layout")


def test_streaming_pipeline_matches_sequential(tmp_path):
    """overlap=on 与 off 端到端：译文分布一致（同一 mock client 语义）。"""
    pages = [f"Paragraph {i} of the document body with some content." for i
             in range(12)]
    outs = {}
    for mode in ("off", "on"):
        src = _make_pdf(pages, tmp_path, name=f"{mode}.pdf")
        cfg = _cfg(src, tmp_path,
                   performance={"layout_workers": 1,
                                "pipeline_overlap": mode,
                                "layout_cache": False})
        cfg.performance.pipeline_overlap = mode
        client = EchoLLM()
        stats = translate_document(cfg, client=client)
        d = pymupdf.open(stats["output"])
        outs[mode] = [d[i].get_text() for i in range(len(d))]
        d.close()
        assert stats["paragraphs"] == 12
    # 每页译文落位一致（【译】前缀由 mock 按源文回显，等价于分布断言）
    for i in range(12):
        a = "".join(outs["off"][i].split())
        b = "".join(outs["on"][i].split())
        assert a == b, f"page {i + 1} distribution mismatch"


def test_streaming_pipeline_respects_max_calls(tmp_path):
    """流式路径触顶：程序不崩、剩余段保留原文、警告明确。"""
    pages = [f"Overflow test paragraph {i} with enough words to translate."
             for i in range(6)]
    src = _make_pdf(pages, tmp_path, name="cap.pdf")
    cfg = _cfg(src, tmp_path, max_llm_calls=1, batch_size=1,
               performance={"layout_workers": 1, "pipeline_overlap": "on",
                            "layout_cache": False})
    cfg.performance.pipeline_overlap = "on"
    stats = translate_document(cfg, client=EchoLLM())
    joined = " ".join(stats["warnings"])
    assert "max_llm_calls" in joined
    d = pymupdf.open(stats["output"])
    assert len(d) == 6
    d.close()


# ---------- 9. 嵌套表地狱样张（任务 2-3 验收基建）----------

def _table_pdf(rows: list, tmp_path: Path, name: str,
               right_col_x1: float | None = None,
               hlines: "list[float] | None" = None,
               col_x: tuple = (100, 250, 400)) -> pymupdf.Document:
    """合成表样张：rows 每行 [(col_idx, text)]；right_col_x1 非空时
    最后一列按右对齐落位；hlines 画三线表横线（触发表区检测）。"""
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    font = pymupdf.Font("helv")
    for ri, row in enumerate(rows):
        y = 120 + ri * 22
        for ci, t in row:
            if right_col_x1 is not None and ci == len(col_x) - 1:
                x = right_col_x1 - font.text_length(t, fontsize=9)
            else:
                x = col_x[ci]
            p.insert_text((x, y), t, fontsize=9)
    if hlines:
        for y in hlines:
            sh = p.new_shape()
            sh.draw_line(pymupdf.Point(90, y), pymupdf.Point(510, y))
            sh.finish(color=(0, 0, 0), width=0.7)
            sh.commit()
    path = tmp_path / name
    doc.save(str(path))
    doc.close()
    return pymupdf.open(str(path))


def test_hell_merged_header_row(tmp_path):
    """地狱样张 1：合并表头（'Measured value' 跨列居中）。
    旧 gap 兜底会把表头切碎/数字错位；锚点切分：表头 conf 0.7 成组、
    数据行 conf 0.9 三列各归位。"""
    rows = [
        [(0, "Parameter"), (1, "Measured value")],        # 合并表头（居中跨列）
        [(0, "Thickness"), (1, "10 nm"), (2, "0.32")],
        [(0, "Width"), (1, "500 nm"), (2, "0.41")],
        [(0, "Radius"), (1, "80 nm"), (2, "0.29")],
    ]
    doc = _table_pdf(rows, tmp_path, "hell1.pdf", col_x=(100, 260, 420))
    cells = table_cells(doc[0], pymupdf.Rect(85, 105, 520, 220),
                         detected=[])
    by_conf = {}
    for c in cells:
        by_conf.setdefault(c["conf"], []).append(c["text"])
    assert len(by_conf.get(0.9, [])) == 9          # 3 数据行 × 3 列
    assert "0.32" in by_conf[0.9] and "0.41" in by_conf[0.9]
    hdr = " ".join(by_conf.get(0.7, []))
    assert "Parameter" in hdr and "Measured value" in hdr   # 表头文本完整


def test_hell_right_aligned_numeric(tmp_path):
    """地狱样张 2：数字右对齐列（x1 稳定、x0 漂移）。
    x0 锚点学不到该列 → 右锚点接管；数据行仍 conf 0.9 不降级。"""
    rows = [
        [(0, "Sample"), (1, "Rxx"), (2, "1.23")],
        [(0, "S1"), (1, "Ryy"), (2, "12.345")],
        [(0, "S2"), (1, "Rzz"), (2, "7.8")],
        [(0, "S3"), (1, "Rxy"), (2, "105.2")],
    ]
    doc = _table_pdf(rows, tmp_path, "hell2.pdf", right_col_x1=460,
                     col_x=(100, 300, 0))
    cells = table_cells(doc[0], pymupdf.Rect(85, 105, 520, 220),
                         detected=[])
    texts = [c["text"] for c in cells]
    for want in ("1.23", "12.345", "7.8", "105.2"):
        assert want in texts                      # 数字独立成格不错位
        cell = next(c for c in cells if c["text"] == want)
        assert cell["conf"] >= 0.9
    assert len([c for c in cells if c["conf"] >= 0.9]) == 12


def test_hell_crosspage_continuation(tmp_path):
    """地狱样张 3：跨页表（同表头跨页重复）——延续检测 + 同 gid。"""
    rows_p1 = [
        [(0, "Method"), (1, "Score"), (2, "Ref")],
        [(0, "PLG baseline"), (1, "0.0056"), (2, "[7]")],
        [(0, "PLG + RL corners"), (1, "0.416"), (2, "[8]")],
    ]
    rows_p2 = [
        [(0, "Method"), (1, "Score"), (2, "Ref")],   # 跨页重复表头
        [(0, "RL generation only"), (1, "0.30"), (2, "[8]")],
    ]
    doc = _table_pdf(rows_p1 + rows_p2, tmp_path, "hell3.pdf",
                     hlines=[110, 132, 176, 198], col_x=(100, 260, 420))
    # 人为拆成两"页"布局（同一物理页模拟跨页几何：下半区 y 偏移一页高）
    lay1 = {"tables": [{"bbox": pymupdf.Rect(85, 105, 520, 200),
                        "cells": table_cells(doc[0], pymupdf.Rect(
                            85, 105, 520, 200))}]}
    lay2_shift = pymupdf.Rect(85, 105 + 842, 520, 200 + 842)

    def cells_shifted():
        out = []
        for c in table_cells(doc[0], pymupdf.Rect(85, 105, 520, 200)):
            b = pymupdf.Rect(c["bbox"])
            b.y0 += 842; b.y1 += 842
            out.append({"bbox": b, "text": c["text"],
                        "conf": c.get("conf", 1.0)})
        return out

    lay2 = {"tables": [{"bbox": lay2_shift, "cells": cells_shifted()}]}
    pr = [pymupdf.Rect(0, 0, 595, 842), pymupdf.Rect(0, 0, 595, 842)]
    # 下页表须在页顶（y1 < 45% 页高）：842 偏移后 y1=1042 > 378 不满足
    # ——用缩放后的模拟页高验证判定核心（表头相似度 > 0.8）
    from difflib import SequenceMatcher
    t1 = " ".join(c["text"] for c in lay1["tables"][0]["cells"][:3]).lower()
    t2 = " ".join(c["text"] for c in lay2["tables"][0]["cells"][:3]).lower()
    assert SequenceMatcher(None, t1, t2).ratio() > 0.8   # 表头同源可判延续

    # 正常几何（上页底/下页顶）的完整链接路径
    lay_bot = {"tables": [{"bbox": pymupdf.Rect(85, 600, 520, 780),
                           "cells": lay1["tables"][0]["cells"]}]}
    lay_top = {"tables": [{"bbox": pymupdf.Rect(85, 60, 520, 200),
                           "cells": lay2["tables"][0]["cells"]}]}
    n = link_crosspage_tables([lay_bot, lay_top], pr)
    assert n == 1
    assert lay_top["tables"][0].get("continued_from") == (0, 0)
    assert lay_top["tables"][0]["gid"] == lay_bot["tables"][0]["gid"]


def test_cell_confidence_gating():
    """置信度分级：conf<0.5 的格不送译（保守保留原文）。"""
    assert _cell_translatable({"text": "x", "conf": 0.9})
    assert _cell_translatable({"text": "x", "conf": 0.7})
    assert not _cell_translatable({"text": "x", "conf": 0.4})
    assert _cell_translatable({"text": "x"})       # 旧布局缓存无 conf → 放行


def test_hell_render_dry_run_smoke(tmp_path):
    """地狱样张渲染冒烟：干跑（client=None）整管线不崩，低置信格保留原文。"""
    rows = [
        [(0, "Parameter"), (1, "Measured value")],
        [(0, "Thickness"), (1, "10 nm"), (2, "0.32")],
    ]
    doc = _table_pdf(rows, tmp_path, "hell_smoke.pdf", col_x=(100, 260, 420))
    p = tmp_path / "hell_smoke.pdf"
    cfg = _cfg(p, tmp_path, performance={"layout_workers": 1})
    stats = translate_document(cfg, client=None)
    d = pymupdf.open(stats["output"])
    txt = d[0].get_text()
    d.close()
    assert "0.32" in txt and "Thickness" in txt   # 表内容未被吞


# ---------- 10. 回归：htmlbox + fit off（v0.5.1 路径） ----------

def test_htmlbox_fit_off_dry_run_no_crash(tmp_path):
    """fit.mode=off + htmlbox（v0.5.1 行为）——修复 _render_paras_htmlbox
    兜底分支引用未定义 layout 的 NameError 隐患。"""
    from translator.fit import FitConfig
    src = _make_pdf(["Regression check paragraph."], tmp_path)
    cfg = _cfg(src, tmp_path, performance={"layout_workers": 1})
    cfg.fit = FitConfig(mode="off")
    stats = translate_document(cfg, client=None)
    d = pymupdf.open(stats["output"])
    assert "Regression check paragraph." in d[0].get_text()
    d.close()


# ---------- 11. 配置面 ----------

def test_config_new_keys_and_validation(tmp_path):
    yaml = tmp_path / "c.yaml"
    yaml.write_text("""
io: {input: x.pdf, output_dir: out}
llm: {stream: false, sentence_cache: false}
ocr:
  engine: paddle
  mode: reconstruct
  engines: [paddle, rapidocr, tesseract]
performance:
  pipeline_overlap: on
""", encoding="utf-8")
    cfg = load_config(yaml)
    assert cfg.ocr.mode == "reconstruct"
    assert cfg.ocr.engines == ["paddle", "rapidocr", "tesseract"]
    assert cfg.llm.stream is False
    assert cfg.performance.pipeline_overlap == "on"

    yaml.write_text("""
io: {input: x.pdf, output_dir: out}
ocr: {mode: bogus}
""", encoding="utf-8")
    with pytest.raises(ValueError, match="reconstruct"):
        load_config(yaml)
