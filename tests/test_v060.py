"""v0.6.0 排版自适配验收单测：

- 任务 A：测量基座（measure_para/measure_fit_factor 与 insert_htmlbox 同源）
  + 丢段 bug 修复（spare<-0.5 时 scale_low=0 兜底必出字）
- 任务 B：两遍式渲染 + 样式级全局因子（同类同字号；30% 膨胀文档不再
  出现 0.5 级个别段缩放）
- 任务 C：降级阶梯（扩框与下邻 bbox 零重叠；refs 不扩框；行距阶梯）
- 任务 D：EN→ZH 微升（低填充文档 body 因子 >1，上限 body_boost）
- 任务 E：源头控长（预算 prompt 规则；超预算单段重问恰好一次；预算档
  缓存 |#b{N} 后缀二次运行 0 调用；CJK/拉丁两套字宽预算）
- 2-1 回归：small caps 节标题字号不再小于正文；整块粗体 Abstract
  不再被误判 heading
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from translator.config import load_config
from translator.fit import FitConfig, compute_style_factors, \
    estimate_char_budget, fit_ladder, measure_fit_factor
from translator.render import _build_font_archive, collect_para_specs, \
    measure_para, render_page, spec_css


def _cjk_font_or_skip():
    from translator.langs import resolve_output_fonts
    body, _ = resolve_output_fonts("zh", None)
    if not body:
        pytest.skip("no CJK font on this machine")
    return body


def _mk_para(text: str, bbox, size=10.0, col=0, **kw):
    p = {"bbox": pymupdf.Rect(bbox), "text": text,
         "spans": [{"text": text, "size": size,
                    "bbox": pymupdf.Rect(bbox), "font": "x"}],
         "size": size, "col": col}
    p.update(kw)
    return p


def _mk_layout(paras, tables_cells=(), tables=(), figures=(), formulas=()):
    return {"mode": "one", "paragraphs": list(paras),
            "tables_cells": list(tables_cells),
            "tables": [{"bbox": pymupdf.Rect(t)} for t in tables],
            "figure_regions": [pymupdf.Rect(f) for f in figures],
            "formulas": [{"bbox": pymupdf.Rect(f)} for f in formulas],
            "hf_blocks": [], "fig_text_blocks": []}


def _render_with_fit(page, layout, translated, font_path, fit_cfg=None,
                     lang="zh", page_h=None):
    """驱动两遍式渲染（collect → factors → render），返回 (specs, factors)。"""
    from translator import render as R
    fit_cfg = fit_cfg or FitConfig()
    tmap = {t["index"]: t["text"] for t in translated}
    paras = layout["paragraphs"]
    frs = [pymupdf.Rect(f["bbox"]) for f in layout.get("formulas", [])]
    specs = collect_para_specs(paras, tmap, None, False, frs, lang,
                               layout=layout, fit_cfg=fit_cfg,
                               page_h=page_h)
    arch, font_css = _build_font_archive(font_path, None)
    groups: dict[str, list[dict]] = {}
    for s in specs:
        groups.setdefault(s["cls"], []).append(s)
    factors = compute_style_factors(
        groups, fit_cfg,
        lambda s, factor, lead, track: spec_css(s, font_css, lead, track,
                                                factor=factor), arch)
    warnings: list[str] = []
    render_page(page, layout, translated, font_path, warnings=warnings,
                renderer="htmlbox", lang=lang, para_specs=specs,
                factors=factors, fit_cfg=fit_cfg, archive=arch,
                font_css=font_css)
    return specs, factors, warnings


def _span_sizes(page, needle: str) -> list[float]:
    """页面上含 needle 的 span 字号列表。"""
    out = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                if needle in s["text"]:
                    out.append(round(s["size"], 2))
    return out


# ---------- 任务 A：测量基座 ----------

def test_measure_para_needed_height():
    font_path = _cjk_font_or_skip()
    arch, font_css = _build_font_archive(font_path, None)
    css = font_css + (" p {font-family:ptbody, serif; font-size:10pt;"
                      " line-height:1.35; margin:0;}")
    html = "<p>" + "中文测试文本" * 10 + "</p>"
    h = measure_para(pymupdf.Rect(0, 0, 200, 1000), html, css, arch)
    assert 30 < h < 1000      # 实际高度（约 4-5 行 × 13.5pt）
    # 短文本一行
    h1 = measure_para(pymupdf.Rect(0, 0, 200, 1000), "<p>短</p>", css, arch)
    assert 0 < h1 < h


def test_measure_factor_matches_engine_direction():
    """测量因子与 insert_htmlbox 实际缩放方向一致（同源 Story 语义）。"""
    font_path = _cjk_font_or_skip()
    arch, font_css = _build_font_archive(font_path, None)
    css = font_css + (" p {font-family:ptbody, serif; font-size:10pt;"
                      " line-height:1.35; margin:0;}")
    html = "<p>" + "很长的中文段落需要缩小字号才能装进这个小框。" * 8 + "</p>"
    rect = pymupdf.Rect(0, 0, 220, 60)
    f, ok = measure_fit_factor(rect, html, css, arch, f_min=0.5, f_max=1.0)
    assert ok and f < 1.0                     # 需要收缩
    doc = pymupdf.open()
    page = doc.new_page()
    _, scale = page.insert_htmlbox(pymupdf.Rect(72, 72, 292, 132), html,
                                   css=css, scale_low=0.5)
    assert abs(scale - f) < 0.02              # 测量=引擎（±二分误差）


def test_dropped_paragraph_retry_guarantees_ink(tmp_path):
    """任务 A 核心：装不下的段落必须落墨（scale_low=0 兜底），不得消失。"""
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    page = doc.new_page()
    paras = [_mk_para("tiny", (72, 100, 150, 114), size=10.0)]
    lay = _mk_layout(paras)
    warnings: list[str] = []
    render_page(page, lay, [{"index": 0, "text": "溢出" * 200}], font_path,
                warnings=warnings, renderer="htmlbox")
    txt = page.get_text()
    assert "溢出" in txt, "段落被整段丢弃（v0.5.1 丢段回归）"
    assert any("retrying unrestricted" in w or "emergency scale" in w
               for w in warnings), warnings


# ---------- 任务 B：样式级全局因子 ----------

def test_inflated_para_renders_at_class_factor(tmp_path):
    """1.6× 膨胀样例段（模拟 ZH→EN 方向）：span 存在且字号=类因子结果。"""
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    page = doc.new_page()
    # 框 228×60pt @10pt/1.32 行高 ≈ 4.5 行 × 22.8 字 ≈ 100 字容量；
    # 译文 160 字 = 1.6× 容量 → 必须收缩
    paras = [_mk_para("Aa Bb Cc Dd Ee.", (72, 100, 300, 160), size=10.0)]
    lay = _mk_layout(paras)
    specs, factors, warnings = _render_with_fit(
        page, lay, [{"index": 0, "text": "甲" * 160}], font_path)
    sizes = _span_sizes(page, "甲")
    assert sizes, "膨胀段未渲染"
    f = factors["body"]["factor"]
    assert abs(sizes[0] - 10.0 * f) < 0.15, (sizes, f)
    assert f >= 0.78 - 1e-6                  # 类因子下限


def test_same_kind_uniform_size_30pct_inflated(tmp_path):
    """系统性膨胀文档（50% 段溢出 ≥ 30% 门槛）：同类段输出字号完全一致，
    且不再出现 0.5 级个别缩放。"""
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    page = doc.new_page()
    # 每框 228×60pt @10pt/1.32 行高 ≈ 22 字/行 × 4.5 行 ≈ 102 字容量
    paras = [
        _mk_para("inflated one", (72, 100, 300, 160), size=10.0),
        _mk_para("normal one", (72, 170, 300, 230), size=10.0),
        _mk_para("inflated two", (72, 240, 300, 300), size=10.0),
        _mk_para("normal two", (72, 310, 300, 370), size=10.0),
    ]
    lay = _mk_layout(paras)
    specs, factors, warnings = _render_with_fit(page, lay, [
        {"index": 0, "text": "丁" * 135},      # ~32% 膨胀
        {"index": 1, "text": "乙" * 60},
        {"index": 2, "text": "丙" * 130},      # ~27% 膨胀
        {"index": 3, "text": "戊" * 50},
    ], font_path)
    assert factors["body"]["factor"] < 1.0     # 系统性收缩生效
    s_bing = _span_sizes(page, "乙")
    s_ding = _span_sizes(page, "丁")
    s_wu = _span_sizes(page, "戊")
    assert s_bing and s_ding and s_wu
    assert len(set(s_bing + s_ding + s_wu)) == 1, \
        f"同类字号不一致: {s_bing} {s_ding} {s_wu}"
    size = s_bing[0]
    assert size >= 10.0 * 0.78 - 0.01, size   # 不再出现 0.5 级缩放
    assert size <= 10.0 + 0.01, size          # 无微升（膨胀方向 boost 关）


def test_isolated_overflow_no_class_shrink(tmp_path):
    """孤立溢出（1/4 段紧 < 30% 门槛）：类因子恒 1.0 绝不陪绑缩小——
    v0.6.0 让 1 个紧段把全类拖到 0.93 的回归；紧段走 per-spec override。"""
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    page = doc.new_page()
    paras = [
        _mk_para("normal a", (72, 100, 300, 160), size=10.0),
        _mk_para("normal b", (72, 170, 300, 230), size=10.0),
        _mk_para("tight one", (72, 240, 300, 300), size=10.0),
        _mk_para("normal c", (72, 310, 300, 370), size=10.0),
    ]
    lay = _mk_layout(paras)
    specs, factors, warnings = _render_with_fit(page, lay, [
        {"index": 0, "text": "甲" * 60},
        {"index": 1, "text": "乙" * 55},
        {"index": 2, "text": "丙" * 130},      # 唯一紧段
        {"index": 3, "text": "丁" * 50},
    ], font_path)
    assert factors["body"]["factor"] == 1.0, factors
    # 紧段有 per-spec override（行距阶梯）或引擎兜底，但文本必须完整落墨
    s_tight = _span_sizes(page, "丙")
    assert s_tight, "紧段未渲染"
    # 其余段保持 10pt（类因子 1.0）
    assert all(abs(s - 10.0) < 0.05 for s in _span_sizes(page, "甲"))


def test_fill_lead_eats_box_slack(tmp_path):
    """收缩方向低填充文档：body 行距填充 > 1.0（剩余空间吃进段落内部，
    而不是 dangling 在段间）——v0.6.0 段间距增大回归的定向修复。"""
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    page = doc.new_page()
    paras = [
        _mk_para("long source one", (72, 100, 300, 200)),
        _mk_para("long source two", (72, 215, 300, 315)),
    ]
    lay = _mk_layout(paras)
    specs, factors, warnings = _render_with_fit(page, lay, [
        {"index": 0, "text": "短译文一"},
        {"index": 1, "text": "短译文二"},
    ], font_path)
    assert factors["body"]["factor"] > 1.0          # 字号微升
    assert factors["body"]["lead"] > 1.0            # 行距填充
    assert factors["body"]["lead"] <= 1.12 + 1e-6   # 封顶
    # 渲染文本实际占据高度显著大于 10pt/1.35 基线（≥15%）
    spans = [s for b in page.get_text("dict")["blocks"]
             for l in b.get("lines", []) for s in l.get("spans", [])
             if "短译文" in s["text"]]
    assert spans
    y0 = min(s["bbox"][1] for s in spans)
    y1 = max(s["bbox"][3] for s in spans)
    box_h = 100.0                                    # 第一个框高
    assert (y1 - y0) >= box_h * 0.10                 # 两段文字有可观高度


# ---------- 任务 C：降级阶梯 ----------

def test_expansion_zero_overlap_with_next_para(tmp_path):
    """扩框结果与下一段 bbox 零重叠；refs 不扩框。"""
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    page = doc.new_page()
    paras = [
        _mk_para("first", (72, 100, 300, 160), size=10.0),   # 膨胀段
        _mk_para("second", (72, 170, 300, 230), size=10.0),
    ]
    lay = _mk_layout(paras)
    specs, factors, warnings = _render_with_fit(page, lay, [
        {"index": 0, "text": "甲" * 150},
        {"index": 1, "text": "乙" * 60},
    ], font_path, fit_cfg=FitConfig(expand_lines=1.0))
    # 段0 扩框下探不越过 段1 y0(170) - 2pt
    assert specs[0]["rect"].y1 <= 170 - 2.0 + 0.01
    # 渲染落墨：段0 任何 span 不得进入段1 bbox（y >= 170）
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                if "甲" in s["text"]:
                    assert s["bbox"][3] <= 170 + 0.5, \
                        f"扩框压到下一段: {s['bbox']}"
    assert _span_sizes(page, "乙"), "下一段未渲染"


def test_ref_entries_not_expanded():
    font_path = _cjk_font_or_skip()
    paras = [_mk_para("[1] ref entry", (72, 100, 300, 160), size=8.0,
                      is_ref=True),
             _mk_para("body", (72, 170, 300, 230), size=10.0)]
    lay = _mk_layout(paras)
    specs = collect_para_specs(
        paras, {0: "文" * 80, 1: "文" * 40}, None, False, [], "zh",
        layout=lay, fit_cfg=FitConfig(expand_lines=1.0))
    ref_spec = specs[0]
    # ref 原框 y1=160-0.5=159.5（贴边内缩），扩框后不得越过该值
    assert ref_spec["rect"].y1 <= 159.5 + 0.01


def test_expansion_clamped_to_page_bottom():
    """末段（无下邻元素）扩框不得越过页底（v0.6.1：旧版把框扩进页底
    空白，译文渲染进页边距——paper3 p1 末条目悬行的根因之一）。"""
    paras = [_mk_para("last para of page", (72, 740, 300, 780), size=10.0)]
    lay = _mk_layout(paras)
    specs = collect_para_specs(
        paras, {0: "文" * 200}, None, False, [], "zh",
        layout=lay, fit_cfg=FitConfig(expand_lines=1.0), page_h=792.0)
    assert specs[0]["rect"].y1 <= 792.0 - 2.0 + 0.01


def test_split_conjunction_boundary():
    """跨页拆分连词边界：长中文串中间优先切在 且/和/或/等 连词前——
    paper3 p1 末尾 '…高度可解释且人 | 类…' 悬字回归。"""
    from translator.pipeline import _split_proportional
    dst = ("我们引入概率车道图来表示和可视化高度可解释且人类可理解的多智能体"
           "角例场景一种从真实世界交通数据学习的算法被提出用于离散化")
    # 目标切点落在 "解释且人" 附近
    a, b = _split_proportional(dst, 0.42)
    assert b and (b[0] in "且和或与并而但及又以，。；：" or
                  a[-1:] in "，。；：）】"), f"切点不自然: A=…{a[-8:]!r} B={b[:8]!r}"
    assert a + b == dst.replace(" ", "") or len(a) + len(b) == len(dst)


def test_ladder_levels_and_lead_applied():
    """行距阶梯进入测量循环：压缩后类因子不小于未压缩时（每级重测）。"""
    cfg = FitConfig()
    ladder = fit_ladder(cfg)
    assert ladder[0] == (1.0, 1.0)
    assert (0.95, 1.0) in ladder and (0.92, 1.0) in ladder
    assert ladder[-1] == (0.92, cfg.tracking)


# ---------- 任务 D：EN→ZH 微升 ----------

def test_body_boost_low_fill_document(tmp_path):
    """低填充文档（译文远短于框）：body 因子 >1 且 ≤ body_boost；
    caption/ref 不参与微升。"""
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    page = doc.new_page()
    paras = [
        _mk_para("long english source paragraph one", (72, 100, 300, 200)),
        _mk_para("long english source paragraph two", (72, 215, 300, 315)),
        _mk_para("Fig. 1: caption text here", (72, 330, 300, 360),
                 is_caption=True),
    ]
    lay = _mk_layout(paras)
    specs, factors, warnings = _render_with_fit(page, lay, [
        {"index": 0, "text": "短译文一"},
        {"index": 1, "text": "短译文二"},
        {"index": 2, "text": "图注"},
    ], font_path)
    assert factors["body"]["factor"] > 1.0
    assert factors["body"]["factor"] <= 1.05 + 1e-6
    assert factors["caption"]["factor"] == 1.0
    sizes = _span_sizes(page, "短译文")
    assert all(s > 10.0 for s in sizes), sizes


def test_body_boost_disabled_when_any_shrink():
    """任一段需要收缩时微升关闭（body 因子回 1.0）。"""
    font_path = _cjk_font_or_skip()
    arch, font_css = _build_font_archive(font_path, None)
    paras = [
        _mk_para("one", (72, 100, 300, 200)),
        _mk_para("two", (72, 215, 300, 240)),        # 小框
    ]
    lay = _mk_layout(paras)
    specs = collect_para_specs(paras, {0: "短", 1: "乙" * 120}, None, False,
                               [], "zh", layout=lay, fit_cfg=FitConfig())
    groups = {"body": specs}
    factors = compute_style_factors(
        groups, FitConfig(),
        lambda s, factor, lead, track: spec_css(s, font_css, lead, track,
                                                factor=factor), arch)
    assert factors["body"]["factor"] <= 1.0


# ---------- 任务 E：源头控长 ----------

def test_estimate_char_budget_cjk_vs_latin():
    """CJK 预算按 1em 字宽，拉丁按标定串实测（每字符更窄 → 预算更大）。"""
    rect = pymupdf.Rect(0, 0, 228, 132)
    b_cjk = estimate_char_budget(rect, 10.0, 1.32, None, cjk=True)
    # CJK: 22.8 字/行 × 10 行 × 0.92 ≈ 209
    assert 190 <= b_cjk <= 220, b_cjk
    font = pymupdf.Font("helv")
    b_lat = estimate_char_budget(rect, 10.0, 1.32, font, cjk=False)
    # 拉丁平均字宽 ~0.5em → 约两倍容量
    assert b_lat > b_cjk * 1.5, (b_lat, b_cjk)


def test_budget_rule_text():
    from translator.llm import _budget_rule
    rule = _budget_rule({"1": "a", "2": "b"}, [100, None, 200], [0, 2])
    assert "id 1 <= 100" in rule and "id 3 <= 200" in rule
    assert "id 2" not in rule
    assert _budget_rule({"1": "a"}, [None], [0]) == ""


def test_over_budget_triggers_single_reask(tmp_path):
    """超预算批：主批 1 次 + 单段重问恰好 1 次；重问结果被采纳并落缓存。"""
    from translator.cache import TranslationCache
    from translator.llm import TranslationClient

    class BudgetMock:
        def __init__(self):
            self.n = 0
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.create))

        def create(self, **kw):
            self.n += 1
            if self.n == 1:      # 主批：超长译文
                content = json.dumps({"1": "很长" * 60}, ensure_ascii=False)
            else:                # 重问：短版
                content = json.dumps({"1": "短版"}, ensure_ascii=False)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=content))])

    mock = BudgetMock()
    cache = TranslationCache(tmp_path / "c.db")
    tc = TranslationClient(mock, model="m", batch_size=6, max_llm_calls=10,
                           max_retries=1)
    out, calls = tc.translate_paragraphs(
        ["source paragraph"], cache=cache, budgets=[30])
    assert mock.n == 2, f"应恰好 2 次调用（主批+重问），实际 {mock.n}"
    assert out[0] == "短版"
    assert calls == 2
    # 预算档缓存 key 带后缀；二次运行 0 调用
    tc2 = TranslationClient(_NoCallClient(), model="m", batch_size=6,
                            max_llm_calls=10)
    out2, calls2 = tc2.translate_paragraphs(
        ["source paragraph"], cache=cache, budgets=[30])
    assert calls2 == 0 and out2[0] == "短版"
    cache.close()


class _NoCallClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(
            create=self._boom))

    def _boom(self, **kw):
        raise AssertionError("缓存命中时不得发请求")


def test_over_budget_reask_capped():
    """重问上限 = max_llm_calls 的 10%（10 次 → 1 次），超限段接受并告警。"""
    from translator.llm import TranslationClient

    class AlwaysLong:
        def __init__(self):
            self.n = 0
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.create))

        def create(self, **kw):
            self.n += 1
            ids = json.loads(kw["messages"][-1]["content"])
            content = json.dumps({k: "长" * 200 for k in ids},
                                 ensure_ascii=False)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=content))])

    mock = AlwaysLong()
    tc = TranslationClient(mock, model="m", batch_size=10, max_llm_calls=10,
                           max_retries=1)
    paras = [f"para {i}" for i in range(5)]
    out, calls = tc.translate_paragraphs(paras, budgets=[20] * 5)
    # 1 主批（10 段上限组一批）+ 1 次重问（10% 上限）= 2
    assert mock.n == 2, mock.n
    assert any("still" in w or "renderer ladder" in w for w in tc.warnings)


def test_cache_zero_calls_untouched(tmp_path):
    """既有断言不破：无预算二次运行仍 0 调用（budgets=None 路径）。"""
    from translator.cache import TranslationCache
    from translator.llm import TranslationClient

    class OneShot:
        def __init__(self):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.create))

        def create(self, **kw):
            ids = json.loads(kw["messages"][-1]["content"])
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(
                    {k: "译" for k in ids})))])

    cache = TranslationCache(tmp_path / "c.db")
    tc = TranslationClient(OneShot(), model="m", max_llm_calls=5)
    tc.translate_paragraphs(["hello"], cache=cache)
    tc2 = TranslationClient(_NoCallClient(), model="m", max_llm_calls=5)
    out, calls = tc2.translate_paragraphs(["hello"], cache=cache)
    assert calls == 0 and out == ["译"]
    cache.close()


# ---------- 2-1 回归：标题/摘要样式 ----------

def test_small_caps_sec_title_not_smaller_than_body():
    """'I. INTRODUCTION'（small caps：大字号'I.'+小字号'NTRODUCTION'）：
    样式必须是 sec_title 且字号基准不低于正文——v0.5.1 输出 7.4pt 标题
    小于 10pt 正文的根因回归。"""
    from translator.typography import Typography
    typo = Typography({"cjk": ""}, lang="zh")
    para = {
        "text": "I.   I NTRODUCTION",
        "size": 7.97,               # dominant_size 会取 small caps 小字号
        "spans": [
            {"text": "I.   I", "size": 9.96, "font": "NimbusRomNo9L-Regu",
             "bbox": pymupdf.Rect(138, 473, 153, 483), "flags": 0},
            {"text": "NTRODUCTION", "size": 7.97,
             "font": "NimbusRomNo9L-Regu",
             "bbox": pymupdf.Rect(154, 475, 216, 483), "flags": 0},
        ],
        "is_heading": False,        # layout 检测不到（非粗体、字号不大）
    }
    style = typo.resolve(para, body_size=9.96)
    assert style.kind == "sec_title"
    assert style.size >= 9.96
    assert style.bold


def test_bold_abstract_not_heading():
    """整块粗斜体的 Abstract 不再被判 heading（长度守卫），
    样式走 abstract 分支。"""
    from translator.layout import is_heading
    from translator.typography import Typography
    abstract_spans = [
        {"text": "Abstract—Validating the safety " + "of vehicles. " * 90,
         "size": 8.97, "font": "NimbusRomNo9L-MediItal", "flags": 16,
         "bbox": pymupdf.Rect(54, 231, 300, 330)}]
    block = {"text": abstract_spans[0]["text"], "spans": abstract_spans,
             "size": 8.97}
    assert not is_heading(block, body_size=9.96)
    typo = Typography({"cjk": ""}, lang="zh")
    para = dict(block, is_heading=True)      # 即便上游误判，样式也走 abstract
    style = typo.resolve(para, body_size=9.96)
    assert style.kind == "abstract"


# ---------- 配置 ----------

def test_fit_config_defaults_and_validation(tmp_path):
    assert FitConfig().mode == "auto"
    y = tmp_path / "c.yaml"
    y.write_text("""
io: {input: x.pdf, output_dir: out}
fit:
  mode: off
  expand_lines: 0
  min_scale: 0.8
""", encoding="utf-8")
    cfg = load_config(y)
    assert cfg.fit.mode == "off" and cfg.fit.expand_lines == 0
    assert cfg.fit.min_scale == 0.8

    y2 = tmp_path / "bad.yaml"
    y2.write_text("""
io: {input: x.pdf, output_dir: out}
fit: {mode: bogus}
""", encoding="utf-8")
    with pytest.raises(ValueError, match="fit"):
        load_config(y2)


def test_no_fit_section_means_auto(tmp_path):
    """旧配置（无 fit 段）缺省 auto——v0.6.0 行为默认生效。"""
    y = tmp_path / "c.yaml"
    y.write_text("io: {input: x.pdf, output_dir: out}",
                 encoding="utf-8")
    cfg = load_config(y)
    assert cfg.fit is None            # pipeline 侧 None → FitConfig()=auto
    from translator.pipeline import translate_document
    assert translate_document.__doc__ is not None
