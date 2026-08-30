#!/usr/bin/env python
"""全流程分阶段计时（零 LLM 成本）：关键函数 monkey-patch 打点。

用法（项目根目录）:
  .venv/bin/python tools/profile_stages.py -c CONFIG.yaml [--fake-llm]
      [--tag NAME]

打点范围（perf_counter 累计，含调用次数）:
  fingerprint      文档指纹 md5
  layout*          layout_page 冷布局（逐页）
  load_layout      版面缓存读（含 JSON 解码）
  save_layout      版面缓存写
  crop*            公式/图表位图裁剪（含缓存命中与未命中）
  llm*             TranslationClient._request 墙钟（fake-llm 时≈本地开销）
  collect_specs    collect_para_specs + collect_cell_specs（测量 pass 收集）
  fit_pass         compute_style_factors（样式因子测量）
  render_page      逐页渲染回灌（faithful）
  reflow_model     build_document_model（reflow 文档模型）
  reflow_write     render_reflow_document 总耗时
  doc_save         doc.save（faithful 输出落盘）
  total            translate_document 总耗时

--fake-llm: 注入进程内伪 OpenAI 客户端（即时返回「【译】原文」），
隔离本地管线耗时；不带该参数且 client 无法创建时退 dry-run。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

from translator import pipeline  # noqa: E402
from translator.config import load_config  # noqa: E402
from translator.events import EventSink  # noqa: E402

_STATS = defaultdict(lambda: [0.0, 0])   # name -> [total_s, calls]


def _tick(name: str):
    t0 = time.perf_counter()
    try:
        return t0
    finally:
        pass


class _Timer:
    __slots__ = ("name", "t0")

    def __init__(self, name: str):
        self.name = name

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        _STATS[self.name][0] += time.perf_counter() - self.t0
        _STATS[self.name][1] += 1


def _wrap(cls_or_mod, attr, name=None):
    name = name or attr
    orig = getattr(cls_or_mod, attr)

    def timed(*a, **kw):
        with _Timer(name):
            return orig(*a, **kw)

    setattr(cls_or_mod, attr, timed)
    return orig


def _wrap_method(cls, attr, name=None):
    name = name or f"{cls.__name__}.{attr}"
    orig = getattr(cls, attr)
    import inspect
    if isinstance(inspect.getattr_static(cls, attr), staticmethod):

        def timed_static(*a, **kw):
            with _Timer(name):
                return orig(*a, **kw)

        setattr(cls, attr, staticmethod(timed_static))
        return

    def timed(self, *a, **kw):
        with _Timer(name):
            return orig(self, *a, **kw)

    setattr(cls, attr, timed)


class FakeCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, model=None, messages=None, stream=False, **kw):
        self.calls += 1
        user = messages[-1]["content"]
        try:
            data = json.loads(user)
        except Exception:
            data = {"1": user}
        out = {}
        for k, v in data.items():
            ph = " ".join(__import__("re").findall(r"\[FORMULA_\d+\]", v or ""))
            out[k] = f"【译】{(v or '')[:60]}" + (f" {ph}" if ph else "")

        class _R:
            class choices:  # noqa: N801
                pass

        r = _R()

        class _C:
            message = type("M", (), {"content": json.dumps(
                out, ensure_ascii=False)})()

        class _Ch:
            pass

        ch = _Ch()
        ch.choices = [_C()]
        return ch


class FakeChat:
    def __init__(self):
        self.completions = FakeCompletions()


class FakeOpenAI:
    """进程内伪 OpenAI 客户端：非流式即时响应（隔离本地管线耗时）。"""

    def __init__(self):
        self.chat = FakeChat()


def install_hooks() -> None:
    _wrap(pipeline, "layout_page", "layout(cold)")
    _wrap(pipeline, "_crop_formulas_cached", "crop_formulas")
    _wrap(pipeline, "render_page", "render_page")
    _wrap(pipeline, "collect_para_specs", "collect_specs")
    _wrap(pipeline, "collect_cell_specs", "collect_specs")
    _wrap(pipeline, "compute_style_factors", "fit_pass")
    _wrap(pipeline, "render_reflow_document", "reflow_write")
    from translator import doccache as _dc
    from translator import llm as _llm
    _wrap_method(_dc.DocumentCache, "fingerprint", "fingerprint")
    _wrap_method(_dc.DocumentCache, "load_layout", "load_layout")
    _wrap_method(_dc.DocumentCache, "save_layout", "save_layout")
    _wrap_method(_dc.DocumentCache, "load_pixmap", "pixmap_get")
    _wrap_method(_dc.DocumentCache, "save_pixmap", "pixmap_put")
    _wrap_method(_llm.TranslationClient, "_request", "llm_request")
    from translator import render_reflow as _rr
    _wrap(_rr, "build_document_model", "reflow_model")

    def _make_io_timer(orig, name):
        def timed_io(self, *a, **kw):
            with _Timer(name):
                return orig(self, *a, **kw)
        return timed_io

    pymupdf.Document.save = _make_io_timer(pymupdf.Document.save, "doc_save")
    pymupdf.Document.tobytes = _make_io_timer(
        pymupdf.Document.tobytes, "doc_tobytes")
    # render_page 内部细打点
    pymupdf.Page.insert_htmlbox = _make_io_timer(
        pymupdf.Page.insert_htmlbox, "htmlbox")
    pymupdf.Page.apply_redactions = _make_io_timer(
        pymupdf.Page.apply_redactions, "apply_redactions")
    pymupdf.Page.get_links = _make_io_timer(
        pymupdf.Page.get_links, "get_links")
    from translator import render_story as _rs
    from translator import preprocess as _pp
    _wrap(_rs, "try_render_page_story", "page_story")
    _wrap(_rs, "_precheck_local", "story_precheck")
    _wrap(_rs, "_verify_flow", "story_verify")
    _wrap(_rs, "measure_fit_factor", "measure_fit")
    from translator import fit as _fitmod
    _wrap(_fitmod, "measure_fit_factor", "measure_fit")
    _wrap(_pp, "remove_watermarks", "watermark")
    from translator import layout as _lay
    _wrap(_lay, "link_crosspage_tables", "xpage_tables")
    orig_open = pymupdf.open

    def timed_open(*a, **kw):
        with _Timer("pdf_open"):
            return orig_open(*a, **kw)

    pipeline.pymupdf.open = timed_open
    # init/收尾细打点
    from translator import doccache as _dc2
    from translator import typography as _tp
    from translator import langs as _lg
    from translator import render as _rd
    _wrap(_dc2.DocumentCache, "__init__", "dcache_init")
    _wrap_method(_dc2.DocumentCache, "close", "dcache_close")
    _wrap(_tp, "Typography", "typography_init")
    _wrap(_lg, "resolve_output_fonts", "font_resolve")
    _wrap(_rd, "_build_font_archive", "font_archive")
    _wrap(_lg, "coverage_warnings", "font_coverage")
    from translator import extract as _ex
    _wrap(_ex, "page_has_text_layer", "text_layer_probe")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", required=True)
    ap.add_argument("--fake-llm", action="store_true",
                    help="注入进程内伪 LLM（即时返回，隔离本地耗时）")
    ap.add_argument("--tag", default="run")
    args = ap.parse_args()

    install_hooks()
    cfg = load_config(args.config)
    client = FakeOpenAI() if args.fake_llm else None

    t0 = time.perf_counter()
    stats = pipeline.translate_document(cfg, client=client,
                                        sink=EventSink(), control=None)
    total = time.perf_counter() - t0

    print(f"\n===== stage profile [{args.tag}] "
          f"({stats['pages']}p, {stats['paragraphs']} paras, "
          f"{stats['calls']} llm calls, output={Path(stats['output']).name})")
    rows = sorted(_STATS.items(), key=lambda kv: -kv[1][0])
    for name, (sec, n) in rows:
        print(f"  {name:<18} {sec:8.2f}s  ({n} calls, "
              f"{1000 * sec / max(n, 1):6.1f} ms/call)")
    print(f"  {'TOTAL':<18} {total:8.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
