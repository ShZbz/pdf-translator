"""v0.8.5 验收单测。

- FAILPATHS I8 收口：CLI 坏 PDF/文件不存在/坏 YAML 一行 ERROR + 提示
  （退出码 2，不甩裸 traceback）
- 整页渲染结果缓存（候选池 S4 推广）：热跑第二次 render_page 0 次调用、
  输出逐字节一致；译文变化/渲染配置变化/坏 BLOB 各自正确 miss
- 管线 actor 化（任务 2-4 第一步）：LayoutActor 异常传回消费者；
  页级重译命令（retranslate_pages 参数 + EventSink 命令总线两入口）
  只重付点名页的调用，其余页缓存命中，缓存被新译文覆盖
- config fonts 标量归一（fonts: simsun.ttc 不再 AttributeError）
零网络（LLM 用假 client）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_v043 import EchoLLM  # noqa: E402
from tests.test_v080 import _make_pdf  # noqa: E402


def _cfg(src: Path, tmp_path: Path, **perf_kw):
    from translator.config import (Config, FeatureConfig, IOConfig,
                                   PerformanceConfig)
    perf = {"layout_workers": 1, "cache_dir": str(tmp_path / "cache")}
    perf.update(perf_kw)
    return Config(
        io=IOConfig(input=str(src), output_dir=str(tmp_path / "out")),
        features=FeatureConfig(watermark_removal=False,
                               preserve_formatting=False),
        performance=PerformanceConfig(**perf),
    )


# ---------- FAILPATHS I8：CLI 友好报错 ----------

def _run_cli(monkeypatch, cfg_path: Path, capsys):
    from translator import cli
    monkeypatch.setattr(sys, "argv", ["translator", "-c", str(cfg_path)])
    rc = cli.main()
    err = capsys.readouterr().err
    return rc, err


def test_cli_bad_pdf_friendly_error(monkeypatch, tmp_path, capsys):
    """坏 PDF（非 PDF 字节）→ 一行 ERROR + 提示 + 退出码 2，无 traceback。"""
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"this is definitely not a pdf")
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(f"io:\n  input: {bad}\n", encoding="utf-8")
    rc, err = _run_cli(monkeypatch, cfgp, capsys)
    assert rc == 2
    assert "ERROR:" in err and "提示" in err
    assert "Traceback" not in err


def test_cli_missing_input_friendly_error(monkeypatch, tmp_path, capsys):
    missing = tmp_path / "nope.pdf"
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(f"io:\n  input: {missing}\n", encoding="utf-8")
    rc, err = _run_cli(monkeypatch, cfgp, capsys)
    assert rc == 2
    assert "不存在" in err and "Traceback" not in err


def test_cli_bad_yaml_friendly_error(monkeypatch, tmp_path, capsys):
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text("io: [unclosed\n", encoding="utf-8")
    rc, err = _run_cli(monkeypatch, cfgp, capsys)
    assert rc == 2
    assert "ERROR:" in err and "Traceback" not in err


def test_cli_unexpected_error_keeps_traceback(monkeypatch, tmp_path, capsys):
    """未预期异常不吞堆栈（bug 报告需要）——注入 KeyError 级别的意外。"""
    from translator import cli
    src = _make_pdf(tmp_path)
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(f"io:\n  input: {src}\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["translator", "-c", str(cfgp)])

    import translator.pipeline as pl
    monkeypatch.setattr(pl, "translate_document",
                        lambda *a, **k: (_ for _ in ()).throw(KeyError("x")))
    with pytest.raises(KeyError):
        cli.main()


# ---------- 整页渲染结果缓存 ----------

def test_render_cache_hot_run_skips_render(monkeypatch, tmp_path):
    """干跑两次：第二次 render_page 0 次调用、输出逐字节一致、可直接打开。"""
    from translator import pipeline
    src = _make_pdf(tmp_path, text="render cache probe paragraph one.")
    cfg = _cfg(src, tmp_path)
    calls = {"n": 0}
    orig = pipeline.render_page

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)

    monkeypatch.setattr(pipeline, "render_page", counting)
    s1 = pipeline.translate_document(cfg, client=None)
    assert calls["n"] >= 1 and s1["render_cache_hit"] is False
    first = Path(s1["output"]).read_bytes()

    calls["n"] = 0
    s2 = pipeline.translate_document(cfg, client=None)
    assert calls["n"] == 0, "热跑必须整页回放，不再进 render_page"
    assert s2["render_cache_hit"] is True
    assert Path(s2["output"]).read_bytes() == first
    doc = pymupdf.open(s2["output"])
    assert len(doc) == 1
    doc.close()


def test_render_cache_translation_change_misses(tmp_path):
    """译文变化（换 client 输出）→ 负载哈希不同 → 重渲染不回放旧版式。"""
    from translator import pipeline
    from translator.config import FeatureConfig
    src = _make_pdf(tmp_path, text="translation drift probe paragraph.")
    cfg = _cfg(src, tmp_path)
    cfg.features = FeatureConfig(watermark_removal=False,
                                 preserve_formatting=False,
                                 translation_cache=False)

    class V2(EchoLLM):
        def create(self, **kw):
            batch = json.loads(kw["messages"][-1]["content"])
            self.calls.append(batch)
            out = {k: f"【新译】{v[:20]}" for k, v in batch.items()}
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(out, ensure_ascii=False)))])

    s1 = pipeline.translate_document(cfg, client=EchoLLM())
    assert s1["render_cache_hit"] is False
    s2 = pipeline.translate_document(cfg, client=V2())
    assert s2["render_cache_hit"] is False, "译文变化不得回放旧渲染"
    text2 = pymupdf.open(s2["output"])[0].get_text()
    assert "新译" in text2


def test_render_cache_config_change_misses(monkeypatch, tmp_path):
    """渲染配置变化（双语开关）→ key 不同 → 重渲染。"""
    from translator import pipeline
    from translator.config import FeatureConfig
    src = _make_pdf(tmp_path, text="bilingual flip render cache probe.")
    cfg = _cfg(src, tmp_path)
    calls = {"n": 0}
    orig = pipeline.render_page

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)

    monkeypatch.setattr(pipeline, "render_page", counting)
    pipeline.translate_document(cfg, client=None)
    assert calls["n"] >= 1
    cfg.features = FeatureConfig(watermark_removal=False,
                                 preserve_formatting=False, bilingual=True)
    calls["n"] = 0
    s2 = pipeline.translate_document(cfg, client=None)
    assert calls["n"] >= 1, "渲染配置变化必须重渲染"
    assert s2["render_cache_hit"] is False


def test_render_cache_corrupt_blob_is_miss(monkeypatch, tmp_path):
    """缓存 BLOB 损坏（非 PDF 字节）按 miss 处理：重渲染成功出片。"""
    from translator import pipeline
    src = _make_pdf(tmp_path, text="corrupt blob probe paragraph.")
    cfg = _cfg(src, tmp_path)
    pipeline.translate_document(cfg, client=None)
    db = tmp_path / "cache" / ".pdf_translator_cache" / "cache.db"
    assert db.is_file()
    conn = sqlite3.connect(str(db))
    n = conn.execute("UPDATE renders SET data = x'67617262616765'"
                     " WHERE key LIKE 'rnd:%'").rowcount
    conn.commit()
    conn.close()
    assert n >= 1, "首轮干跑应写入渲染缓存"
    calls = {"n": 0}
    orig = pipeline.render_page

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)

    monkeypatch.setattr(pipeline, "render_page", counting)
    s2 = pipeline.translate_document(cfg, client=None)
    assert calls["n"] >= 1 and s2["render_cache_hit"] is False
    doc = pymupdf.open(s2["output"])
    assert len(doc) == 1
    doc.close()


def test_render_cache_disabled_by_config(monkeypatch, tmp_path):
    """performance.render_cache: false → 两次都全量渲染（行为开关有效）。"""
    from translator import pipeline
    src = _make_pdf(tmp_path, text="render cache disabled probe.")
    cfg = _cfg(src, tmp_path, render_cache=False)
    calls = {"n": 0}
    orig = pipeline.render_page

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)

    monkeypatch.setattr(pipeline, "render_page", counting)
    pipeline.translate_document(cfg, client=None)
    calls["n"] = 0
    s2 = pipeline.translate_document(cfg, client=None)
    assert calls["n"] >= 1 and s2["render_cache_hit"] is False


# ---------- 管线 actor 化 ----------

def test_layout_actor_exception_propagation(monkeypatch, tmp_path):
    """LayoutActor 内部异常经 outbox 传回消费者就地 raise；线程收尾。"""
    from translator import layout as lay_mod
    from translator.actors import LayoutActor
    src = _make_pdf(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("layout exploded")

    monkeypatch.setattr(lay_mod, "layout_page", boom)
    actor = LayoutActor(pymupdf.open(src), tmp_path / "tmp.pdf", 1,
                        None, "heuristic", None, None).start()
    with pytest.raises(RuntimeError, match="layout exploded"):
        for _item in actor.results():
            pass
    deadline = time.time() + 5
    while actor.alive and time.time() < deadline:
        time.sleep(0.05)
    assert not actor.alive


def test_layout_actor_streams_all_pages(tmp_path):
    """自驱逐页产出：pno 连续 0..n-1，layout 携带 paragraphs。"""
    from translator.actors import LayoutActor
    src = _make_pdf(tmp_path)
    doc = pymupdf.open(src)
    actor = LayoutActor(doc, tmp_path / "tmp.pdf", doc.page_count,
                        None, "heuristic", None, None).start()
    seen, lays = [], []
    for pno, lay, _pix in actor.results():
        seen.append(pno)
        lays.append(lay)
    assert seen == list(range(doc.page_count))
    assert all("paragraphs" in lay for lay in lays)
    doc.close()


def _two_page_pdf(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 90), "First page body text here.",
                               fontsize=11)
    doc.new_page().insert_text((72, 90), "Second page body text here.",
                               fontsize=11)
    src = tmp_path / "two.pdf"
    doc.save(str(src))
    doc.close()
    return src


def _cache_cfg(src, tmp_path, **perf_kw):
    from translator.config import (Config, FeatureConfig, IOConfig, LLMConfig,
                                   PerformanceConfig)
    perf = {"layout_workers": 1, "cache_dir": str(tmp_path / "cache")}
    perf.update(perf_kw)
    return Config(
        io=IOConfig(input=str(src), output_dir=str(tmp_path / "out")),
        llm=LLMConfig(batch_char_budget=10),   # 每段独立成批（按页断言调用）
        features=FeatureConfig(watermark_removal=False,
                               preserve_formatting=False,
                               translation_cache=True),
        performance=PerformanceConfig(**perf),
    )


def test_retranslate_page_param_bypasses_cache(tmp_path):
    """retranslate_pages={0}：只重付第 1 页调用，第 2 页缓存命中；缓存更新。"""
    from translator import pipeline
    src = _two_page_pdf(tmp_path)
    cfg = _cache_cfg(src, tmp_path)
    f1 = EchoLLM()
    s1 = pipeline.translate_document(cfg, client=f1)
    assert len(f1.calls) == 2, "两段各自成批"

    f2 = EchoLLM()
    s2 = pipeline.translate_document(cfg, client=f2, retranslate_pages={0})
    assert len(f2.calls) == 1, "只有被点名的页 1 重付调用"
    asked = json.dumps(f2.calls[0])
    assert "First page" in asked and "Second page" not in asked
    assert s2["cache_hits"] >= 1, "未点名页必须缓存命中"
    assert s2["calls"] == 1

    # 新译文已覆盖缓存：再次运行 0 调用
    f3 = EchoLLM()
    s3 = pipeline.translate_document(cfg, client=f3)
    assert len(f3.calls) == 0 and s3["cache_hits"] >= 2


def test_retranslate_command_via_sink_streaming(tmp_path):
    """EventSink.post("retranslate") 命令总线入口（流式路径页粒度消费）。"""
    from translator import pipeline
    from translator.control import JobControl
    from translator.events import EventSink
    src = _two_page_pdf(tmp_path)
    cfg = _cache_cfg(src, tmp_path, pipeline_overlap="on")
    pipeline.translate_document(cfg, client=EchoLLM())   # 预热缓存

    sink = EventSink()
    sink.post("retranslate", pages=[1])                   # 0-based 第 2 页
    f2 = EchoLLM()
    s2 = pipeline.translate_document(cfg, client=f2, sink=sink,
                                     control=JobControl())
    assert len(f2.calls) == 1, "只有命令点名的页 2 重付调用"
    asked = json.dumps(f2.calls[0])
    assert "Second page" in asked and "First page" not in asked
    assert s2["cache_hits"] >= 1


def test_retranslate_midrun_command_streaming(tmp_path):
    """运行中到达的 retranslate 命令在流式路径同样生效（v0.8.5 审查修复）。

    旧版 _translate_streaming 里 `force_pages = force_pages or set()` 在
    初始空集时重建对象——命令总线钩子往 translate_document 的外层集合
    添加页号，引用断开：命令被消费并打出 "marked" 日志但页喂入读的是
    局部绑定，永不生效。首帧事件到达后再投递命令锁定该时序。"""
    from translator import pipeline
    from translator.control import JobControl
    from translator.events import EventSink
    src = _two_page_pdf(tmp_path)
    cfg = _cache_cfg(src, tmp_path, pipeline_overlap="on")
    pipeline.translate_document(cfg, client=EchoLLM())   # 预热缓存

    sink = EventSink()
    posted = {"v": False}

    def _on_ev(ev):
        # 首个进度事件（管线已开跑）后投递——区别于上例的「跑前入队」
        if ev.get("kind") == "progress" and not posted["v"]:
            posted["v"] = True
            sink.post("retranslate", pages=[1])

    sink.subscribe(_on_ev)
    f2 = EchoLLM()
    s2 = pipeline.translate_document(cfg, client=f2, sink=sink,
                                     control=JobControl())
    assert len(f2.calls) == 1, "运行中到达的命令必须让页 2 绕过缓存"
    asked = json.dumps(f2.calls[0])
    assert "Second page" in asked and "First page" not in asked
    assert s2["cache_hits"] >= 1


def test_retranslate_with_io_pages_subset(tmp_path):
    """--retranslate × io.pages 子集：原始页号映射到 select 后页码。

    旧版按原始页号直接过 `0 <= p < n_pages`（n_pages 已是子集数）——
    子集不从头起时点名页全部越界被静默丢弃，重译请求无效。"""
    from translator import pipeline
    import pymupdf
    d = pymupdf.open()
    for i in range(4):
        d.new_page().insert_text(
            (72, 90), f"Long body text of document page number {i + 1}"
                      f" with padding words here.", fontsize=11)
    src = tmp_path / "four.pdf"
    d.save(str(src))
    d.close()
    cfg = _cache_cfg(src, tmp_path)
    cfg.io.pages = "2-3"
    cfg.llm.batch_char_budget = 200     # 单页长句整批
    pipeline.translate_document(cfg, client=EchoLLM())   # 预热子集缓存

    f2 = EchoLLM()
    s2 = pipeline.translate_document(cfg, client=f2,
                                     retranslate_pages={2})  # 原始第 3 页
    assert len(f2.calls) == 1, "原始第 3 页在子集内，必须重译"
    asked = json.dumps(f2.calls[0])
    assert "page number 3" in asked and "page number 2" not in asked
    assert s2["cache_hits"] >= 1
    # 不在子集内的点名页：静默忽略（不炸、不重译）
    f3 = EchoLLM()
    pipeline.translate_document(cfg, client=f3, retranslate_pages={0})
    assert len(f3.calls) == 0


def test_fonts_body_override_flows_to_render_font(monkeypatch, tmp_path):
    """fonts.body（中性名）覆盖必须到达渲染字体解析（v0.8.5 审查修复）。

    旧版 pipeline 只把 cfg.fonts["cjk"] 传给 find_cjk_font——显式配置
    fonts.body 的用户拿到的是自动探测字体（Typography 校验读 body、
    渲染读 cjk，配置被静默忽略；config.example.yaml 文档化了该键）。"""
    from translator import pipeline
    src = _two_page_pdf(tmp_path)
    cfg = _cache_cfg(src, tmp_path)
    cfg.fonts = {"body": "C:/Windows/Fonts/some_body_font.ttc", "cjk": ""}

    seen = {}
    orig = pipeline.find_cjk_font

    def spy(explicit=None, lang="zh"):
        seen["explicit"] = explicit
        return orig(None, lang=lang)   # 后续渲染走自动探测（本测只验证接线）

    monkeypatch.setattr(pipeline, "find_cjk_font", spy)
    pipeline.translate_document(cfg, client=None)
    assert seen["explicit"] == "C:/Windows/Fonts/some_body_font.ttc", \
        "fonts.body 必须以与 langs.resolve_output_fonts 相同的优先级传入"


def test_reflow_fontless_latin_with_figure(monkeypatch, tmp_path):
    """reflow × 无字体环境 × 位图：不再 AttributeError（v0.8.5 审查修复）。

    拉丁目标且候选链全空（无字体容器）时 _build_font_archive 返回
    (None, "")——有图/表/公式位图的文档在 archive.add 上直接崩溃。
    修复后建空 Archive 承载位图，字体由引擎内置衬线兜底。"""
    import pymupdf
    from translator import pipeline
    from translator.config import (Config, FeatureConfig, IOConfig,
                                   OutputConfig, ReflowConfig)

    doc = pymupdf.open()
    pg = doc.new_page()
    img = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40))
    img.set_rect(pymupdf.IRect(0, 0, 60, 40), (51, 102, 230))
    pg.insert_image(pymupdf.Rect(300, 60, 460, 160), pixmap=img)
    pg.insert_text((72, 220), "An experimental study of sample drift.",
                   fontsize=11)
    src = tmp_path / "fig.pdf"
    doc.save(str(src))
    doc.close()

    cfg = Config(
        io=IOConfig(input=str(src), output_dir=str(tmp_path / "out"),
                    target_lang="de"),
        features=FeatureConfig(watermark_removal=False,
                               preserve_formatting=False),
        output=OutputConfig(mode="reflow"),
        reflow=ReflowConfig(),
    )
    monkeypatch.setattr(pipeline, "find_cjk_font",
                        lambda explicit=None, lang="zh": "")
    stats = pipeline.translate_document(cfg, client=None)
    out = pymupdf.open(stats["output"])
    assert len(out) >= 1
    assert len(out[0].get_images()) >= 1, "位图必须进输出"
    out.close()


# ---------- config fonts 标量归一 ----------

def test_fonts_scalar_config_normalized(tmp_path):
    """fonts: simsun.ttc（标量）按正文字体归一，不再 AttributeError。"""
    from translator.config import load_config
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text("io:\n  input: x.pdf\nfonts: simsun.ttc\n",
                    encoding="utf-8")
    cfg = load_config(cfgp)
    assert cfg.fonts["cjk"] == "simsun.ttc" and cfg.fonts["body"] == "simsun.ttc"


def test_fonts_bad_type_raises(tmp_path):
    from translator.config import load_config
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text("io:\n  input: x.pdf\nfonts: [1, 2]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fonts"):
        load_config(cfgp)


# ---------- v0.8.5 发布前终检（第二轮） ----------

def test_algorithm_remnant_bold_title_exempt():
    """粗体主导块（标题续行）不被伪代码关键词误判 verbatim（终检修复）。

    真网关小测实证：两行大标题的第二行 'for Spintronic Device
    Fabrication' 命中 `for\\s` 关键词被整行保留原文（页首最显眼处
    中英夹杂）。伪代码从不加粗——粗体份额 ≥50% 豁免。"""
    from translator.layout import _is_algorithm_remnant
    bold_span = [{"text": "for Spintronic Device Fabrication",
                  "font": "ABCDEE+Georgia-Bold", "flags": 16, "size": 16.0}]
    plain_span = [{"text": "for i = 1 to n do",
                   "font": "NimbusRomNo9L-Regu", "flags": 0, "size": 9.0}]
    assert _is_algorithm_remnant("for Spintronic Device Fabrication",
                                 bold_span) is False
    # 非粗体伪代码行为不变（关键词/等宽两条路径都保留）
    assert _is_algorithm_remnant("for i = 1 to n do", plain_span) is True
    mono_span = [{"text": "while x > 0", "font": "NimbusMonL-Regu",
                  "flags": 0, "size": 9.0}]
    assert _is_algorithm_remnant("while x > 0", mono_span) is True


def test_bold_title_line_translated_not_verbatim(tmp_path):
    """端到端：加粗标题续行（for 开头）进翻译队列，不再整行保留原文。"""
    import pymupdf
    from translator.layout import layout_page
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((150, 96), "Gradient Annealing of Thin Films",
                   fontsize=16, fontname="hebo")
    pg.insert_text((150, 116), "for Spintronic Device Fabrication",
                   fontsize=16, fontname="hebo")
    lay = layout_page(pg, engine="heuristic")
    doc.close()
    verbatim = [p for p in lay["paragraphs"] if p.get("is_verbatim")]
    assert not verbatim, "加粗标题行不得被判 verbatim 保留原文"


def test_pseudo_cluster_absorbs_formula_strips():
    """v0.8.5: reflow 伪代码框吸收框内公式条（paper3 p5 Algorithm 2 实证）。

    旧版：框内 'if/sample/nk=' 数学行被 collect_display_formulas 抢判为
    独立显示公式 → 框拆成头块位图+3 条公式条+'Break' 尾块散落文流。
    修复：联合框迭代吸收相交公式条，长高后回吸原不相交的尾块。"""
    import pymupdf
    from translator.render_reflow import _pseudo_clusters
    paras = [
        {"bbox": [55.5, 77.0, 279.9, 173.9], "is_verbatim": True,
         "text": "Algorithm 2: Path Planning\n2 while nk not in ci do\n"
                 "3 for c in Ci do"},
        {"bbox": [56.7, 210.4, 198.1, 220.3], "is_verbatim": True,
         "text": "Break out of for loop\n7"},
        {"bbox": [54.0, 261.1, 300.0, 366.7], "is_verbatim": False,
         "text": "the data set, this would mean we cannot generate a next node"},
    ]
    formulas = [
        {"bbox": [92.6, 172.1, 178.2, 187.1]},    # '4 if {nk, c}∈D then'
        {"bbox": [108.0, 184.1, 241.0, 199.0]},   # '5 sample nk+1 ∼p(...)'
        {"bbox": [108.0, 196.2, 156.6, 211.0]},   # '6 nk = nk+1'
    ]
    clusters = _pseudo_clusters(paras, formulas)
    assert len(clusters) == 1, "整框必须聚成单簇出一张位图"
    idx, fidx, u = clusters[0]
    assert idx == {0, 1}, "'Break out' 尾块应经公式条长高后回吸"
    assert fidx == {0, 1, 2}, "框内三条公式条应并入联合框"
    assert 220 <= u.y1 and abs(u.x0 - 55.5) < 0.1 and abs(u.x1 - 279.9) < 0.1
    # 无公式条时尾块不相交 → 独立成簇（不误并）
    clusters2 = _pseudo_clusters(paras, [])
    assert len(clusters2) == 2
