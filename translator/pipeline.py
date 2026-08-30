"""总编排：preprocess → extract → layout → crop公式 → cache/llm翻译 → render → 输出。

进度日志走 stderr（stdout 留给验收脚本断言）。

v0.5.0:
- 渲染器可选 htmlbox（features.renderer）：insert_htmlbox HTML+CSS 排版
  引擎接管段落回灌（自带 shaping/bidi/两端对齐），writer 仍是默认
- 版面引擎可选 pymupdf-layout（performance.layout_engine）：GNN 版面
  检测产出图/表/公式结构化区域，未装包自动回退启发式
- OCR 原位回贴（ocr.mode=inplace）：OCR 行 bbox 聚合成块 → 白块覆盖
  + 译文原位回灌；与图形重叠的块自动跳过（appendix 仍是默认）
- LLM 配额自适应（llm.rpm_limit/tpm_limit）：自动换算调用间隔与批预算

v0.4.3:
- 布局阶段多进程并行（逐页布局互相独立，ProcessPoolExecutor；
  水印清理后的文档先落临时文件供 worker 读取，任何并行故障自动
  回退串行路径，行为不回退）
- 扫描页惰性 OCR（ocr.py）：OCR 译文以附录页插在扫描页之后
- LLM 组批按字符预算（llm.batch_char_budget）
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pymupdf

from .config import Config
from .control import JobControl, JobCancelled
from .events import EventSink
from .extract import page_has_text_layer
from .fit import FitConfig, compute_style_factors, estimate_char_budget
from .glossary import Glossary
from .layout import layout_page
from .langs import is_rtl, lang_info, output_tag
from .llm import TranslationClient
from .render import _build_font_archive, _clean_zh_text, _draw_para, \
    _html_escape, _insert_one_htmlbox, _wrap_cjk, cell_spec_css, \
    collect_cell_specs, collect_para_specs, crop_formula_pixmaps, \
    find_cjk_font, render_page, spec_css
from .render_reflow import render_reflow_document

_VERBOSE_TS = False

# ---- v0.5.1: 版面结果落盘缓存（段落级断点续跑）----
# 版面启发式变更时 bump 此版本号（旧缓存 key 不同自动失效）
_LAYOUT_CACHE_VER = 2


def _layout_cache_encode(o):
    """layout 结构 → JSON 可序列化（pymupdf.Rect → 标记 dict）。"""
    if isinstance(o, pymupdf.Rect):
        return {"__rect__": [o.x0, o.y0, o.x1, o.y1]}
    if isinstance(o, dict):
        return {k: _layout_cache_encode(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_layout_cache_encode(v) for v in o]
    return o


def _layout_cache_decode(o):
    """JSON → layout 结构（标记 dict → pymupdf.Rect）。"""
    if isinstance(o, dict):
        if set(o.keys()) == {"__rect__"} and len(o["__rect__"]) == 4:
            return pymupdf.Rect(o["__rect__"])
        return {k: _layout_cache_decode(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_layout_cache_decode(v) for v in o]
    return o


def _load_layout_cache(path: Path | None) -> list[dict] | None:
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        layouts = _layout_cache_decode(data)
        if isinstance(layouts, list) and layouts \
                and all(isinstance(l, dict) and "paragraphs" in l
                        for l in layouts):
            return layouts
        return None
    except Exception:
        return None       # 坏缓存按 miss 处理，走正常布局

# ---- 布局并行 worker（模块级函数：Windows spawn 要求可 pickle 引用）----
_LAYOUT_DOC: "pymupdf.Document | None" = None
_LAYOUT_ENGINE = "heuristic"


def _layout_worker_init(pdf_path: str, engine: str = "heuristic") -> None:
    global _LAYOUT_DOC, _LAYOUT_ENGINE
    _LAYOUT_DOC = pymupdf.open(pdf_path)
    _LAYOUT_ENGINE = engine


def _layout_worker_task(pno: int) -> tuple[dict, dict[int, bytes]]:
    lay = layout_page(_LAYOUT_DOC[pno], engine=_LAYOUT_ENGINE)
    pixmaps = crop_formula_pixmaps(_LAYOUT_DOC, pno, lay["formulas"]) \
        if lay["formulas"] else {}
    return lay, pixmaps


def _resolve_layout_workers(cfg: Config, n_pages: int) -> int:
    """v0.4.3: 布局并行数。0=自动 min(4, cpu)；1=串行；页数<3 不值得开池。

    spawn 子进程内强制串行：无 __main__ 保护的用户脚本被 Windows spawn
    子进程重放时，嵌套进程池会指数级爆炸（标准 multiprocessing 约束，
    此处兜底防炸）。
    """
    import multiprocessing as _mp
    if _mp.current_process().name != "MainProcess":
        return 1
    w = int(getattr(cfg.performance, "layout_workers", 0) or 0)
    if w <= 0:
        w = min(4, os.cpu_count() or 1)
    if n_pages < 3:
        w = 1
    return max(1, min(w, n_pages))


def _layout_parallel(doc, tmp_path: Path, workers: int, sink: EventSink,
                     control: "JobControl | None", n_pages: int,
                     engine: str = "heuristic"):
    """并行布局。返回 (layouts, pixmaps)；任何故障返回 None（调用方回退串行）。

    必须落临时文件而不是传原始路径：水印清理发生在内存 doc 上，
    worker 读磁盘文件——直接读源文件会拿到未清理的水印（回贴公式
    位图里带水印）。tmp_path 由调用方负责清理。
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed
    try:
        doc.save(str(tmp_path))
    except Exception as e:
        _log(f"parallel layout: temp save failed ({e})")
        return None
    layouts: list[dict | None] = [None] * n_pages
    pixmaps: list[dict[int, bytes] | None] = [None] * n_pages
    done: set[int] = set()
    next_emit = 0          # page_done 按页序连续前缀推进（进度条不回跳）
    try:
        with ProcessPoolExecutor(
                max_workers=workers,
                initializer=_layout_worker_init,
                initargs=(str(tmp_path), engine)) as pool:
            futs = {pool.submit(_layout_worker_task, p): p for p in range(n_pages)}
            for fut in as_completed(futs):
                if control is not None:
                    control.checkpoint()
                pno = futs[fut]
                layouts[pno], pixmaps[pno] = fut.result()
                done.add(pno)
                while next_emit in done and next_emit < n_pages:
                    sink.page_done(next_emit, n_pages)
                    next_emit += 1
    except JobCancelled:
        raise          # with 退出时 shutdown(wait=True)：等在飞页收尾后干净退出
    except Exception as e:
        _log(f"parallel layout failed ({e}); falling back to sequential")
        return None
    if any(l is None for l in layouts):
        _log("parallel layout incomplete; falling back to sequential")
        return None
    return layouts, pixmaps


def _split_proportional(dst: str, ratio: float) -> tuple[str, str]:
    """v0.2.3 跨页断句：合并译文按原文长度比例在词/标点边界切成两半。

    v0.4.2: 单字符译文守卫——len(dst)==1 时 range(1,1) 为空，
    min() 直接 ValueError（极端短译文整篇崩溃）。

    v0.5.1 修复:边界惩罚原为 dst[i]（B 段首字符）在标点集内记 0 分——
    偏好在'，'前切分，B 段以标点开头（避头尾违例，下页段首悬挂逗号）。
    正确语义是 A 段末字符（dst[i-1]）落在边界集时该切点更优。
    """
    if ratio <= 0.0:
        return "", dst
    if ratio >= 1.0:
        return dst, ""
    if len(dst) < 2:
        return "", dst
    target = len(dst) * ratio
    # v0.6.1: 连词边界优先——跨页拆分落点在长中文串中间时，优先切在
    # 且/和/或/与 等连词前（B 段以连词开头读起来自然），次选标点后；
    # 纯字中断点重罚（实测 paper3 p1 末尾"且人"悬字根因）
    conj = set("且和或与并而但及又以")
    best = min(range(1, len(dst)), key=lambda i: (
        abs(i - target)
        + (0 if dst[i - 1] in " ，。；：、）】" else
           (2 if dst[i] in conj else 6))))
    return dst[:best].rstrip(), dst[best:].lstrip()


def output_pdf_name(stem: str, tgt_lang: str, bilingual: bool,
                    extra: str = "") -> str:
    """输出文件名：{stem}[-bilingual]{extra}-{语言标记}.pdf（zh→-Zh 旧版兼容）。

    extra：io.pages 子集标记（如 "-p1-2"）——试译输出不覆盖全量输出。
    """
    suffix = "-bilingual" if bilingual else ""
    return f"{stem}{suffix}{extra}-{output_tag(tgt_lang)}.pdf"


def _unit_char_budgets(merge_groups: list[list[int]], pending: list,
                       page_layouts: list[dict], cell_pending: list,
                       typo, font_path: str, tgt_lang: str,
                       n_cells: int, n_ocr: int) -> list["int | None"]:
    """v0.6.0 任务 E：每个翻译单元的目标框字符预算（与 all_flat 对齐）。

    段落按样式基准字号/行高/原始 bbox 估；跨页合并单元 = 两框预算之和；
    单元格按格 bbox（基准 8pt/1.25 行高）；OCR 单元无框约束（None）。
    估算只求量级正确——提示词带 15% 容差，渲染阶梯兜底。
    """
    from .langs import lang_info as _lang_info
    from .typography import line_height_factor
    cjk = _lang_info(tgt_lang).script == "cjk"
    font = None
    if not cjk and font_path:
        try:
            font = pymupdf.Font(fontfile=font_path)
        except Exception:
            font = None
    budgets: list[int | None] = []
    for g in merge_groups:
        total = 0
        for fi in g:
            pno, pi = pending[fi]
            p = page_layouts[pno]["paragraphs"][pi]
            base = p.get("size") or 10.0
            lh = 1.32
            if typo is not None:
                try:
                    style = typo.resolve(p, None)
                    base = max(style.size, 6.5)
                    lh = line_height_factor(style.kind)
                except Exception:
                    pass
            total += estimate_char_budget(pymupdf.Rect(p["bbox"]), base, lh,
                                          font, cjk)
        budgets.append(total if total > 0 else None)
    for pno, ci in cell_pending:
        cell = page_layouts[pno]["tables_cells"][ci]
        b = estimate_char_budget(pymupdf.Rect(cell["bbox"]), 8.0, 1.25,
                                 font, cjk)
        budgets.append(b if b > 0 else None)
    budgets.extend([None] * n_ocr)
    if len(budgets) != len(merge_groups) + n_cells + n_ocr:
        return None          # 内部不变量破坏：预算禁用（重问交渲染阶梯）
    return budgets


def _crop_formulas_cached(doc, pno: int, formulas: list[dict],
                          dcache, doc_fp) -> dict[int, bytes]:
    """公式位图裁剪（v0.8.1 S4：doccache 位图缓存）。

    布局/翻译全缓存命中的重跑场景旧版仍付全量 300dpi 裁图成本
    （0.1-0.5s/张）——位图由 (指纹,页,区域,dpi) 内容寻址，跨运行
    字节一致，入项目缓存库。缓存不可用时退直裁（行为不回退）。
    仅主进程调用（并行布局 worker 内不接缓存——重跑场景布局缓存命中
    本就不走并行路径）。
    """
    if dcache is None or doc_fp is None:
        return crop_formula_pixmaps(doc, pno, formulas)
    from .doccache import DocumentCache
    out: dict[int, bytes] = {}
    page = doc[pno]
    for fi, f in enumerate(formulas):
        key = DocumentCache.pixmap_key(doc_fp, pno,
                                       pymupdf.Rect(f["bbox"]), 300)
        png = dcache.load_pixmap(key)
        if png is None:
            png = page.get_pixmap(clip=f["bbox"], dpi=300).tobytes("png")
            dcache.save_pixmap(key, doc_fp, pno, png)
        out[fi] = png
    return out


def _log(msg: str) -> None:
    """进度日志走 stderr;verbose 模式加时间戳。"""
    import time as _t
    ts = f"[{_t.strftime('%H:%M:%S')}]" if _VERBOSE_TS else ""
    print(f"{ts}[pipeline] {msg}", file=sys.stderr)


class _WarningList(list):
    """v0.8.1: all_warnings 收集器（list 子类）——append/extend 同步转发
    sink.warning 事件。

    旧版管线约 12 类警告（fit 溢出/字体覆盖率/OCR 引擎/术语违例/低置信
    单元格/reflow 表缩放/丢段兜底…）只进本列表，仅 2 处走 sink——UI worker
    的 stderr 是 DEVNULL，_log 全部丢弃，UI 与历史面板永远看不到这些警告。
    现统一：任何警告入列即发事件（去重：同一文本只发一次，防止与 llm._warn
    已发的重复）。CLI 行为不变（stats["warnings"] 仍在返回值里）。
    """
    __slots__ = ("_sink", "_seen")

    def __init__(self, sink):
        super().__init__()
        self._sink = sink
        self._seen: set = set()

    def _forward(self, w) -> None:
        if w in self._seen:
            return
        self._seen.add(w)
        try:
            self._sink.warning(str(w))
        except Exception:
            pass          # 事件流故障不拖垮管线

    def append(self, w) -> None:
        super().append(w)
        self._forward(w)

    def extend(self, ws) -> None:
        for w in ws:
            self.append(w)


# ---- v0.7.0: 布局-翻译流水线重叠（页级流水）----

def _overlap_enabled(cfg, client, n_pages: int, layout_engine: str) -> bool:
    """performance.pipeline_overlap: off/on/auto（默认 auto）。

    auto 启用条件：有 LLM client + 页数 ≥12 + 启发式布局——布局耗时
    占比足够大才有流水线价值；pymupdf-layout 引擎本身慢 3-6×，走
    多进程并行布局（workers）+ 顺序翻译更优，不与本特性叠加。
    显式 on 时无条件启用（client 缺失除外——干跑无翻译可重叠）。
    """
    mode = (getattr(cfg.performance, "pipeline_overlap", "auto")
            or "auto").strip().lower()
    if mode in ("off", "0", "false", "no"):
        return False
    if mode in ("on", "1", "true", "yes"):
        return client is not None
    return (client is not None and n_pages >= 12
            and layout_engine == "heuristic")


def _ends_open(txt: str) -> bool:
    t = txt.rstrip()
    return bool(t) and t[-1] not in ".:;!?。：；？！\")]'\""


def _starts_lower(txt: str) -> bool:
    t = txt.lstrip()
    return bool(t) and (t[0].islower() or t[0].isdigit() or t[0] in "([{")


def _cross_merge_ok(a: dict, b: dict) -> bool:
    """跨页断句合并判定（与非流式路径同规则）。"""
    return (_ends_open(a["text"]) and _starts_lower(b["text"])
            and not a.get("is_ref") and not b.get("is_ref")
            and not b.get("is_caption")
            and len(a["text"]) + len(b["text"]) <= 1200)


def _join_group_parts(parts: list[str]) -> str:
    """跨页连字符合并（与非流式路径同规则）。"""
    if len(parts) == 2:
        a, b = parts[0].rstrip(), parts[1].lstrip()
        if a.endswith("-") and b[:1].islower():
            return a[:-1] + b
    return "\n".join(parts)


def _budget_font(font_path: str, cjk: bool):
    """预算估算用字体（拉丁/西里尔目标语言需要实测字宽）。"""
    if not cjk and font_path:
        try:
            return pymupdf.Font(fontfile=font_path)
        except Exception:
            return None
    return None


def _group_budget(g: list[int], pending: list, page_layouts: list, typo,
                  font, cjk: bool) -> "int | None":
    """单合并组的字符预算（与 _unit_char_budgets 同语义，流式增量用）。"""
    from .typography import line_height_factor
    total = 0
    for fi in g:
        pno, pi = pending[fi]
        p = page_layouts[pno]["paragraphs"][pi]
        base = p.get("size") or 10.0
        lh = 1.32
        if typo is not None:
            try:
                style = typo.resolve(p, None)
                base = max(style.size, 6.5)
                lh = line_height_factor(style.kind)
            except Exception:
                pass
        total += estimate_char_budget(pymupdf.Rect(p["bbox"]), base, lh,
                                      font, cjk)
    return total if total > 0 else None


def _translate_streaming(doc, cfg, client, typo, font_path, tgt_lang,
                         glossary, cache, sink, control, fit_cfg,
                         layout_engine, dcache, doc_fp, out_dir, all_warnings):
    """v0.7.0 布局-翻译流水线重叠路径（任务 2-2b）。

    布局在后台线程逐页产出（布局线程持有独立打开的 Document——
    PyMuPDF 非线程安全，主线程的 doc 只做 OCR 渲染/公式裁图）；
    主线程每收到一页：收集段/格 → 跨页合并判定（1 页前视）→ 预算 →
    缓存 → 未命中进 StreamingTranslator 开批——批满即发车，布局继续
    跑下一页。页 N 布局完 → 该页段落立即进组批队列，50+ 页大文档
    省整段布局时间（与版面缓存互补：缓存命中时页瞬间产出）。

    返回 dict（与非流式路径同构的下游状态）：
    page_layouts / page_pixmaps / texts_by_page / cell_texts /
    ocr_jobs / ocr_translations / n_paras / total_calls / tc
    """
    import queue as _queue
    import threading as _threading
    from .langs import lang_info as _lang_info
    from .llm import StreamingTranslator
    from . import ocr as ocr_mod

    n_pages = len(doc)
    cjk = _lang_info(tgt_lang).script == "cjk"
    budget_font = _budget_font(font_path, cjk) if fit_cfg.mode == "auto" else None
    budgets_on = fit_cfg.mode == "auto"

    # 布局线程的独立 Document（水印清理只发生在主 doc，落盘后重开）
    fd, tmp_name = tempfile.mkstemp(suffix=".pdf", dir=str(out_dir))
    os.close(fd)
    layout_tmp = Path(tmp_name)
    q: "_queue.Queue" = _queue.Queue()

    cached = dcache.load_layout(doc_fp, layout_engine, _LAYOUT_CACHE_VER) \
        if (dcache is not None and doc_fp is not None) else None
    cache_hit = cached is not None and len(cached) == n_pages

    def _producer():
        pdoc = None
        try:
            doc.save(str(layout_tmp))
            pdoc = pymupdf.open(str(layout_tmp))
            for pno in range(n_pages):
                if control is not None:
                    control.checkpoint()
                if cache_hit:
                    lay = cached[pno]
                else:
                    lay = layout_page(pdoc[pno], engine=layout_engine)
                # v0.8.1 S4: 公式裁图走项目位图缓存（重跑 0 调用也不再重裁）
                pixmaps = _crop_formulas_cached(
                    pdoc, pno, lay.get("formulas") or [], dcache, doc_fp) \
                    if lay.get("formulas") else {}
                q.put((pno, lay, pixmaps))
            q.put(None)
        except BaseException as e:      # JobCancelled/布局异常都交给消费侧
            q.put(e)
        finally:
            if pdoc is not None:
                try:
                    pdoc.close()
                except Exception:
                    pass

    llm_eff = cfg.llm.effective()
    if (llm_eff.min_call_interval, llm_eff.batch_char_budget) != \
            (cfg.llm.min_call_interval, cfg.llm.batch_char_budget):
        _log(f"llm quota auto: interval={llm_eff.min_call_interval}s, "
             f"batch_char_budget={llm_eff.batch_char_budget} "
             f"(rpm={cfg.llm.rpm_limit}, tpm={cfg.llm.tpm_limit})")
    tc = TranslationClient(
        client, model=llm_eff.model,
        temperature=llm_eff.temperature,
        glossary_prompt=glossary.prompt_block() if glossary else "",
        src_lang=cfg.io.source_lang, tgt_lang=cfg.io.target_lang,
        batch_size=llm_eff.batch_size,
        batch_char_budget=llm_eff.batch_char_budget,
        max_llm_calls=llm_eff.max_llm_calls,
        min_call_interval=llm_eff.min_call_interval,
        max_workers=llm_eff.max_workers,
        fallback_model=llm_eff.fallback_model,
        timeout=llm_eff.timeout,
        max_retries=llm_eff.max_retries,
        backoff_base=llm_eff.backoff_base,
        backoff_cap=llm_eff.backoff_cap,
        retry_delay_cap=llm_eff.retry_delay_cap,
        sink=sink, control=control,
        stream=bool(getattr(cfg.llm, "stream", True)),
        sentence_cache=bool(getattr(cfg.llm, "sentence_cache", True)),
    )
    streamer = StreamingTranslator(tc, cache=cache)

    page_layouts: list[dict] = []
    page_pixmaps: list[dict[int, bytes]] = []
    pending: list[tuple[int, int]] = []
    cell_pending: list[tuple[int, int]] = []
    flat_idx_of: dict[tuple[int, int], int] = {}
    unit_targets: list = []      # 与 streamer 单元对齐：("para",g)/("cell",..)/("ocr",..)
    open_group: "list[int] | None" = None
    n_paras = 0
    ocr_jobs: list[dict] = []
    engines_cfg = _resolve_ocr_engines(cfg)
    engines_avail = [e for e in engines_cfg if ocr_mod.engine_available(e)]
    ocr_mode = (getattr(cfg.ocr, "mode", "appendix") or "appendix").strip()

    def _emit_para_group(g: list[int]) -> None:
        parts = [page_layouts[pending[fi][0]]["paragraphs"][pending[fi][1]]["text"]
                 for fi in g]
        kind = None
        if len(g) == 1:
            para = page_layouts[pending[g[0]][0]]["paragraphs"][pending[g[0]][1]]
            kind = "ref" if para.get("is_ref") \
                else "caption" if para.get("is_caption") else None
        budget = _group_budget(g, pending, page_layouts, typo, budget_font,
                               cjk) if budgets_on else None
        unit_targets.append(("para", g))
        streamer.add_unit(_join_group_parts(parts), budget=budget, kind=kind)

    def _feed_page(pno: int, lay: dict, pixmaps: dict) -> None:
        nonlocal open_group, n_paras
        page_layouts.append(lay)
        page_pixmaps.append(pixmaps)
        sink.page_done(pno, n_pages)
        n_paras += len(lay["paragraphs"])
        # 单元格（v0.7.0 流式：随页进队，批协议不变；低置信格保守不译）
        for ci, cell in enumerate(lay.get("tables_cells", [])):
            if not cell.get("text", "").strip():
                continue
            if not _cell_translatable(cell):
                all_warnings.append(
                    f"table cell p.{pno + 1} kept original (low split "
                    f"confidence) - manual check suggested")
                continue
            cell_pending.append((pno, ci))
            budget = estimate_char_budget(
                pymupdf.Rect(cell["bbox"]), 8.0, 1.25, budget_font, cjk
            ) if budgets_on else None
            unit_targets.append(("cell", pno, ci))
            streamer.add_unit(cell["text"], budget=budget or None)
        # OCR 扫描页（v0.8.1: 提取派给后台单线程——paddle 1-3s/页、多引擎
        # 投票 ×2-3，同步执行会卡住主循环的翻译喂批；引擎实例非线程安全，
        # 单 worker 串行消费。结果在布局全部到齐后按页序补进翻译队列）
        if client is not None and engines_avail \
                and not page_has_text_layer(doc[pno], cfg.ocr.min_chars):
            ocr_q.put(pno)
        # 段落：先决上一页遗留的跨页合并组，再收本页
        paras = lay["paragraphs"]
        skip_fi = None
        if open_group is not None:
            fi_prev = open_group[0]
            prev = page_layouts[pending[fi_prev][0]]["paragraphs"][
                pending[fi_prev][1]]
            fi_next = flat_idx_of.get((pno, 0))
            if fi_next is not None and _cross_merge_ok(prev, paras[0]):
                _emit_para_group([fi_prev, fi_next])
                skip_fi = fi_next
            else:
                _emit_para_group([fi_prev])
            open_group = None
        page_fis = []
        for i in range(len(paras)):
            if not paras[i].get("is_verbatim"):
                flat_idx_of[(pno, i)] = len(pending)
                pending.append((pno, i))
                page_fis.append(len(pending) - 1)
        for fi in page_fis:
            if fi == skip_fi:
                continue
            if fi == page_fis[-1]:
                open_group = [fi]      # 末段留待下页首段判定
            else:
                _emit_para_group([fi])

    t_prod = _threading.Thread(target=_producer, daemon=True)
    t_prod.start()

    # v0.8.1: OCR 后台单线程（引擎实例非线程安全→单 worker；独立 Document
    # ——PyMuPDF 非线程安全，与布局线程的 pdoc 同款纪律。layout_tmp 由
    # _producer 在首个 q.put 前落盘，首个 OCR 任务入队时必然已存在）
    ocr_q: "_queue.Queue" = _queue.Queue()
    ocr_results: dict[int, "dict | None"] = {}
    ocr_err: list = []

    def _ocr_worker():
        odoc = None
        try:
            while True:
                pno = ocr_q.get()
                if pno is None:
                    break
                if odoc is None:
                    odoc = pymupdf.open(str(layout_tmp))
                ocr_results[pno] = _ocr_page_job(
                    odoc, pno, cfg, ocr_mode, engines_avail, all_warnings)
        except BaseException as e:      # 引擎崩溃/取消交主线程统一处理
            ocr_err.append(e)
        finally:
            if odoc is not None:
                try:
                    odoc.close()
                except Exception:
                    pass

    t_ocr = None
    if client is not None and engines_avail:
        t_ocr = _threading.Thread(target=_ocr_worker, daemon=True)
        t_ocr.start()

    _log(f"layout: streaming overlap on ({n_pages} page(s), "
         f"engine={layout_engine})")
    err: "BaseException | None" = None
    try:
        while True:
            item = q.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                err = item
                break
            if control is not None:
                control.checkpoint()
            _feed_page(*item)
        if err is None and open_group is not None:
            _emit_para_group(open_group)
            open_group = None
    finally:
        # OCR 线程收尾（join 必须先于 layout_tmp 删除——worker 的 odoc 句柄
        # 开着时 Windows unlink 会失败留残文件；异常路径同样要收口）
        if t_ocr is not None:
            ocr_q.put(None)
            t_ocr.join()
    try:
        layout_tmp.unlink(missing_ok=True)
    except OSError:
        pass
    if err is not None:
        raise err
    if ocr_err:
        raise ocr_err[0]
    # OCR 结果按页序补进翻译队列（布局/翻译全程与 OCR 并行——旧版同步
    # 提取逐页阻塞主循环）；ocr_jobs 的构造顺序与 unit_targets 对齐
    for pno in sorted(ocr_results):
        job = ocr_results[pno]
        if job is None:
            w = f"OCR page {pno + 1}: no text recognized; kept as-is"
            _log(f"WARNING: {w}")
            all_warnings.append(w)
            continue
        ocr_jobs.append(job)
        jidx = len(ocr_jobs) - 1
        if job["mode"] in ("inplace", "reconstruct"):
            for bi, (_r, t) in enumerate(job["blocks"]):
                unit_targets.append(("ocr", jidx, bi))
                streamer.add_unit(t)
        else:
            unit_targets.append(("ocr", jidx, None))
            streamer.add_unit(job["text"])
    if not cache_hit and dcache is not None and doc_fp is not None \
            and page_layouts and len(page_layouts) == n_pages:
        dcache.save_layout(doc_fp, layout_engine, _LAYOUT_CACHE_VER,
                           Path(cfg.io.input), n_pages, page_layouts)

    sink.stage("translate")
    translated, total_calls = streamer.finish()
    all_warnings.extend(tc.warnings)
    if tc.sent_cache_hits:
        _log(f"cache: {tc.sent_cache_hits} template unit(s) served by "
             f"sentence-level cache")

    # 分发译文（含 v0.7.0 术语锁确定性修复——与非流式路径同语义）
    texts_by_page: dict[int, list["str | None"]] = {}
    cell_texts: dict[tuple[int, int], str] = {}
    # v0.8.0 P3: 跨页合并单元的整段译文（reflow 不拆回两页；faithful 忽略）
    cross_full: dict[tuple[int, int], str] = {}
    cross_skip: set = set()
    ocr_flat_out: list["str | None"] = []
    ocr_counts: list[int] = []         # 每 job 的单元数（粘贴阶段按序消费）
    for job in ocr_jobs:
        n = len(job["blocks"]) if job["mode"] in ("inplace", "reconstruct") else 1
        ocr_counts.append(n)
    ocr_flat_out = [None] * sum(ocr_counts)
    ocr_cursor = 0
    gloss_fixed = 0
    for i, (target, dst) in enumerate(zip(unit_targets, translated)):
        if target[0] == "para":
            g = target[1]
            if glossary is not None and cfg.features.glossary_lock \
                    and len(g) == 1:
                fixed, done = glossary.fix_translation(dst)
                if done:
                    dst = fixed
                    gloss_fixed += 1
            if len(g) == 1:
                pno, pi = pending[g[0]]
                texts_by_page.setdefault(
                    pno, [None] * len(page_layouts[pno]["paragraphs"]))
                texts_by_page[pno][pi] = dst
                if glossary is not None and cfg.features.glossary_lock:
                    bad = glossary.check_translation(dst)
                    if bad:
                        pno_, pi_ = pending[g[0]]
                        all_warnings.append(
                            f"glossary violation p{pno_ + 1}#{pi_}: {bad}")
                continue
            pno_a, pi_a = pending[g[0]]
            pno_b, pi_b = pending[g[1]]
            # v0.8.0 P3: reflow 需要整段译文（faithful 拆回两页原位）
            cross_full[(pno_a, pi_a)] = dst
            cross_skip.add((pno_b, pi_b))
            len_a = len(page_layouts[pno_a]["paragraphs"][pi_a]["text"])
            len_b = len(page_layouts[pno_b]["paragraphs"][pi_b]["text"])
            part_a, part_b = _split_proportional(
                dst, len_a / max(len_a + len_b, 1))
            texts_by_page.setdefault(
                pno_a, [None] * len(page_layouts[pno_a]["paragraphs"]))
            texts_by_page.setdefault(
                pno_b, [None] * len(page_layouts[pno_b]["paragraphs"]))
            texts_by_page[pno_a][pi_a] = part_a
            texts_by_page[pno_b][pi_b] = part_b
        elif target[0] == "cell":
            _, pno, ci = target
            cell_texts[(pno, ci)] = dst
        else:
            # OCR 单元按喂入序（job 顺序 × 块顺序）线性消费
            ocr_flat_out[ocr_cursor] = dst
            ocr_cursor += 1
    if gloss_fixed:
        _log(f"glossary: {gloss_fixed} paragraph(s) fixed in place")

    for pno, lay in enumerate(page_layouts):
        _log(f"page {pno + 1}/{n_pages}: {lay['mode']}-col, "
             f"{len(lay['paragraphs'])} paras, {len(lay['formulas'])} formulas, "
             f"{len(lay.get('tables_cells', []))} table cells")

    return {
        "page_layouts": page_layouts,
        "page_pixmaps": page_pixmaps,
        "texts_by_page": texts_by_page,
        "cell_texts": cell_texts,
        "ocr_jobs": ocr_jobs,
        "ocr_translations": ocr_flat_out,
        "n_paras": n_paras,
        "total_calls": total_calls,
        "tc": tc,
    }


def _append_ocr_pages(doc, ocr_units: list[tuple[int, str]],
                      translations: list[str], font_path: str,
                      warnings: list[str], renderer: str = "writer",
                      lang: str = "zh") -> int:
    """v0.4.3: OCR 译文附录页——插在对应扫描页之后。

    从高页号往低插（先插高位不影响低位索引）。单页放不下时截断并告警。
    v0.5.1: renderer=htmlbox 时译文体走 insert_htmlbox（RTL/天城文整形；
    标头行保持 TextWriter——纯 ASCII 无整形需求）。
    """
    font = pymupdf.Font(fontfile=font_path)
    added = 0
    pairs = sorted(zip(ocr_units, translations), key=lambda x: -x[0][0])
    from .render import _build_font_archive, _insert_one_htmlbox
    arch, font_css = _build_font_archive(font_path, None) \
        if renderer == "htmlbox" else (None, "")
    family = "ptbody, serif" if font_path else "serif"
    d = "direction:rtl;" if is_rtl(lang) else ""
    for (pno, src), dst in pairs:
        text = _clean_zh_text(dst if dst and dst.strip() else src)
        doc.insert_page(pno + 1)
        page = doc[pno + 1]        # insert_page 返回值版本不一，按索引取页
        tw = pymupdf.TextWriter(page.rect)
        fs = 9.5
        y = 42.0 + font.ascender * 10.5
        tw.append(pymupdf.Point(42.0, y), f"[OCR · p.{pno + 1}]",
                  font=font, fontsize=10.5)
        y += 16.0
        if renderer == "htmlbox":
            body_rect = pymupdf.Rect(42.0, y, page.rect.width - 42.0,
                                     page.rect.height - 42.0)
            css = (font_css +
                   f" p {{font-family:{family}; font-size:{fs}pt;"
                   f" line-height:1.5; margin:0; text-align:left;{d}}}")
            _insert_one_htmlbox(page, body_rect,
                                f"<p>{_html_escape(text)}</p>", css,
                                f"ocr-appendix p{pno + 1}", warnings,
                                archive=arch)
        else:
            clipped = False
            for ln in _wrap_cjk(text, font, fs, page.rect.width - 84.0):
                if y > page.rect.height - 42.0:
                    clipped = True
                    break
                tw.append(pymupdf.Point(42.0, y), ln, font=font, fontsize=fs)
                y += fs * 1.5
            if clipped:
                warnings.append(
                    f"OCR appendix p.{pno + 1}: text truncated (page full)")
        tw.write_text(page)
        added += 1
    return added


# ---- v0.5.0: OCR 原位回贴（ocr.mode=inplace）----

def _group_ocr_lines(lines: list[tuple["pymupdf.Rect", str]]) \
        -> list[tuple["pymupdf.Rect", str]]:
    """OCR 行聚合成块：纵向间距 < 1.8×行高且横向重叠 > 0.35 的相邻行并块。

    块是原位回贴的翻译/覆盖单元——按行翻译会被断句毁掉质量，
    按整页翻译又无法原位对齐，行聚类块是两者的折中。
    """
    if not lines:
        return []
    items = sorted(lines, key=lambda it: (it[0].y0, it[0].x0))
    heights = sorted(r.height for r, _ in items)
    med_h = heights[len(heights) // 2] or 10.0
    blocks: list[dict] = []
    for r, t in items:
        placed = False
        for b in blocks:
            br = b["bbox"]
            x_ov = (min(br.x1, r.x1) - max(br.x0, r.x0)) \
                / max(min(br.width, r.width), 1.0)
            v_gap = r.y0 - br.y1
            if x_ov > 0.35 and -med_h <= v_gap < 1.8 * med_h:
                b["bbox"] |= r
                b["texts"].append(t)
                placed = True
                break
        if not placed:
            blocks.append({"bbox": pymupdf.Rect(r), "texts": [t]})
    return [(b["bbox"], "\n".join(b["texts"])) for b in blocks]


def _page_graphics_rects(page) -> list["pymupdf.Rect"]:
    """页面上可能被白块误伤的图形元素（矢量绘图 + 小尺寸插图）。

    扫描页背景图（占页面大比例）不算——白块画在扫描底图上是原位回贴
    的预期行为；只有页内小插图（真插图/装饰图，<50% 页面）才需要避让。
    """
    pr = page.rect
    page_area = pr.get_area() or 1.0
    out: list[pymupdf.Rect] = []
    try:
        for d in page.get_drawings():
            r = d["rect"]
            if r.width > 0.5 and r.height > 0.5 and r.get_area() < 0.9 * page_area:
                out.append(pymupdf.Rect(r))
    except Exception:
        pass
    try:
        for info in page.get_image_info():
            r = pymupdf.Rect(info["bbox"])
            if r.get_area() < 0.5 * page_area:   # 小图=插图要避让；大图=扫描背景
                out.append(r)
    except Exception:
        pass
    return out


def _apply_ocr_inplace(doc, pno: int, blocks: list[tuple["pymupdf.Rect", str]],
                       translations: list[str], font_path: str,
                       warnings: list[str], renderer: str = "writer",
                       lang: str = "zh") -> int:
    """白块覆盖 + 译文原位回灌（PDFMathTranslate 式，任务 2-4）。

    每块：与页内图形（子图/矢量线）重叠超 30% 面积 → 跳过保留原像素
    （白块会误伤插图）；否则白矩形盖掉原文、译文按块 bbox 试排回灌。
    返回成功回贴的块数。
    v0.5.1: renderer=htmlbox 时译文走 insert_htmlbox（RTL/天城文整形）。
    v0.7.0: reconstruct 模式复用本函数——blocks 换成版面区域
    （GNN 语义区域/几何分割产出），区域几何与 OCR 识别质量解耦。
    """
    page = doc[pno]
    font = pymupdf.Font(fontfile=font_path)
    graphics = _page_graphics_rects(page)
    applied = 0
    heights = sorted(r.height for r, _ in blocks)
    med_h = heights[len(heights) // 2] if heights else 10.0
    from .render import _build_font_archive, _insert_one_htmlbox
    arch, font_css = _build_font_archive(font_path, None) \
        if renderer == "htmlbox" else (None, "")
    family = "ptbody, serif" if font_path else "serif"
    d = "direction:rtl;" if is_rtl(lang) else ""
    for (bbox, src), dst in zip(blocks, translations):
        rect = pymupdf.Rect(bbox)
        g_inter = 0.0
        for g in graphics:
            if rect.intersects(g):
                ir = pymupdf.Rect(rect)
                ir.intersect(g)
                g_inter = max(g_inter, ir.get_area())
        if g_inter > 0.30 * max(rect.get_area(), 1e-6):
            warnings.append(
                f"OCR inplace p.{pno + 1}: block overlaps graphics, kept original")
            continue
        text = _clean_zh_text(dst if dst and dst.strip() else src)
        if not text.strip():
            continue
        page.draw_rect(rect, color=None, fill=(1, 1, 1), overlay=True)
        base = max(6.5, min(12.0, med_h * 0.85))
        if renderer == "htmlbox":
            css = (font_css +
                   f" p {{font-family:{family}; font-size:{base:.2f}pt;"
                   f" line-height:1.3; margin:0; text-align:left;{d}}}")
            _insert_one_htmlbox(page, rect,
                                f"<p>{_html_escape(text)}</p>", css,
                                f"ocr-inplace p{pno + 1}", warnings,
                                archive=arch)
            applied += 1
            continue
        tw = pymupdf.TextWriter(page.rect)
        _draw_para(tw, rect, text, font, base, 0, warnings,
                   f"ocr-inplace p{pno + 1}", lh_factor=1.3)
        tw.write_text(page)
        applied += 1
    return applied


# ---- v0.7.0: OCR 引擎解析 + 版面自监督重建 ----

_CELL_CONF_FLOOR = 0.5     # 切分置信度低于此值的单元格保守不译


def _cell_translatable(cell: dict) -> bool:
    """v0.7.0 置信度分级：conf ≥ 0.5 的格才送译（错切保留原文更安全）。"""
    conf = cell.get("conf", 1.0)
    return conf is None or float(conf) >= _CELL_CONF_FLOOR


def _resolve_ocr_engines(cfg) -> list[str]:
    """OCR 引擎清单：ocr.engines 优先，缺省回落单引擎 ocr.engine。"""
    raw = getattr(cfg.ocr, "engines", None) or []
    engines = [str(e).strip() for e in raw if str(e or "").strip()]
    if not engines:
        e = (getattr(cfg.ocr, "engine", "") or "").strip()
        engines = [e] if e and e != "none" else []
    # 去重保序
    seen, out = set(), []
    for e in engines:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


# GNN 语义区域 → 可译/保留分类（pymupdf-layout kind 字符串）
_OCR_REGION_SKIP = ("header", "footer")
_OCR_REGION_PRESERVE = ("table", "fig", "pic", "image", "formul", "equation",
                        "math")


def _blocks_from_gnn_regions(regions: list, lines: list,
                             page) -> list[tuple["pymupdf.Rect", str]]:
    """GNN 语义区域 + OCR 行 → 可译区域块 [(rect, text)]。

    行按中心点落区归属；表/图/公式区不产块（保留原像素，区内的
    图内标注随位图存活——区内的行也标已消费，不再当漏检行复活）；
    页眉页脚区丢弃（与文字层页同策略）；未落入任何区域的行（GNN
    漏检）按行聚类兜底成块，宁多译不漏译。
    """
    blocks: list[tuple[pymupdf.Rect, str]] = []
    used = [False] * len(lines)

    def _mark_covered(rrect: pymupdf.Rect) -> None:
        for i, (lr, _lt) in enumerate(lines):
            c = pymupdf.Point((lr.x0 + lr.x1) / 2, (lr.y0 + lr.y1) / 2)
            if rrect.contains(c):
                used[i] = True

    for rrect, kind in regions:
        k = str(kind or "").strip().lower()
        if any(s in k for s in _OCR_REGION_SKIP + _OCR_REGION_PRESERVE):
            _mark_covered(pymupdf.Rect(rrect))
            continue
        br = pymupdf.Rect(rrect)
        texts = []
        for i, (lr, lt) in enumerate(lines):
            c = pymupdf.Point((lr.x0 + lr.x1) / 2, (lr.y0 + lr.y1) / 2)
            if br.contains(c):
                texts.append(lt)
                used[i] = True
        if texts:
            blocks.append((br, "\n".join(texts)))
    if not all(used):
        leftover = [it for i, it in enumerate(lines) if not used[i]]
        blocks.extend(_group_ocr_lines(leftover))
    # 阅读序（y 主序 x 次序）
    blocks.sort(key=lambda b: (b[0].y0, b[0].x0))
    return blocks


def _ocr_page_job(doc, pno: int, cfg, ocr_mode: str, engines_avail: list[str],
                  warnings: list[str]) -> "dict | None":
    """单扫描页 OCR 提取 → 翻译 job（v0.7.0：多引擎投票 + reconstruct）。

    返回 job dict（见调用方）；OCR 引擎全空/识别失败返回 None。
    """
    from . import ocr as ocr_mod
    lines, conflicts = ocr_mod.ocr_page_lines_voted(
        doc[pno], engines=engines_avail, src_lang=cfg.io.source_lang)
    if conflicts:
        warnings.append(
            f"OCR p.{pno + 1}: {conflicts} line(s) conflicted across "
            f"engines; picked highest confidence - consider manual check")
    if not lines:
        return None
    page_text = "\n".join(t for _, t in lines)
    job: dict = {"pno": pno, "mode": ocr_mode, "text": "", "blocks": []}
    if ocr_mode == "reconstruct":
        # 版面自监督重建：区域几何来自 GNN（影子页）/几何分割，识别
        # 文本只负责原文层——认错字不影响版面只影响对照层保真
        regions = None
        try:
            regions = ocr_mod.gnn_regions_for_lines(doc[pno], lines)
        except Exception:
            regions = None
        if regions is not None:
            blocks = _blocks_from_gnn_regions(regions, lines, doc[pno])
        else:
            blocks = ocr_mod.region_blocks_geometry(lines, doc[pno].rect)
        if blocks and sum(len(t) for _, t in blocks) >= 10:
            job["blocks"] = blocks
            job["text"] = page_text      # 原文层（附录对照用）
            return job
        # 区域化失败 → 退附录模式
        job["mode"] = "appendix"
    if ocr_mode == "inplace":
        blocks = _group_ocr_lines(lines)
        if blocks and sum(len(t) for _, t in blocks) >= 10:
            job["blocks"] = blocks
            return job
    # appendix（或区域化失败的兜底）：全文附录——v0.7.0 修复：复用已
    # 提取的行，不再整页二次 OCR（旧版块化失败会重跑一遍 OCR 翻倍耗时）
    if page_text and len(page_text.strip()) >= 10:
        job["mode"] = "appendix"
        job["text"] = page_text
        return job
    return None


def _append_ocr_original(doc, pno: int, text: str, font_path: str,
                         warnings: list[str], renderer: str = "writer",
                         lang: str = "zh") -> int:
    """v0.7.0 reconstruct 模式的原文对照附录页：插在扫描页之后。

    与 _append_ocr_pages（译文附录）成对；reconstruct 主页已原位回贴
    译文，附录页承载 OCR 原文（识别层保真对照——认字错误的查验入口）。
    """
    from .render import _build_font_archive, _insert_one_htmlbox
    font = pymupdf.Font(fontfile=font_path)
    doc.insert_page(pno + 1)
    page = doc[pno + 1]
    tw = pymupdf.TextWriter(page.rect)
    fs = 9.5
    y = 42.0 + font.ascender * 10.5
    tw.append(pymupdf.Point(42.0, y), f"[OCR original · p.{pno + 1}]",
              font=font, fontsize=10.5)
    tw.write_text(page)
    y += 16.0
    body = _clean_zh_text(text)
    arch, font_css = _build_font_archive(font_path, None) \
        if renderer == "htmlbox" else (None, "")
    family = "ptbody, serif" if font_path else "serif"
    d = "direction:rtl;" if is_rtl(lang) else ""
    if renderer == "htmlbox":
        body_rect = pymupdf.Rect(42.0, y, page.rect.width - 42.0,
                                 page.rect.height - 42.0)
        css = (font_css +
               f" p {{font-family:{family}; font-size:{fs}pt;"
               f" line-height:1.5; margin:0; text-align:left;{d}}}")
        _insert_one_htmlbox(page, body_rect,
                            f"<p>{_html_escape(body)}</p>", css,
                            f"ocr-original p{pno + 1}", warnings, archive=arch)
    else:
        for ln in _wrap_cjk(body, font, fs, page.rect.width - 84.0):
            if y > page.rect.height - 42.0:
                warnings.append(
                    f"OCR original p.{pno + 1}: text truncated (page full)")
                break
            tw = pymupdf.TextWriter(page.rect)
            tw.append(pymupdf.Point(42.0, y), ln, font=font, fontsize=fs)
            tw.write_text(page)
            y += fs * 1.5
    return 1


def translate_document(cfg: Config, client=None, verbose: bool = False,
                       sink: "EventSink | None" = None,
                       control: "JobControl | None" = None) -> dict:
    """整册翻译。client 为注入的 OpenAI 兼容实例（None=跳过翻译只测管线）。

    v0.4.0: sink=进度事件流（None=纯 CLI 模式零开销）；
            control=暂停/取消控制（None=不可控，原行为）。
    返回统计：{pages, paragraphs, calls, warnings, output, ocr_pages}
    """
    global _VERBOSE_TS
    _VERBOSE_TS = verbose
    sink = sink or EventSink()
    t0 = time.time()
    src = Path(cfg.io.input)
    out_dir = Path(cfg.io.output_dir or src.parent)
    out_dir.mkdir(parents=True, exist_ok=True)
    tgt_lang = (cfg.io.target_lang or "zh").strip() or "zh"
    doc = pymupdf.open(src)
    orig_pages = len(doc)
    n_pages = orig_pages
    # ---- v0.7.1: io.pages 页码子集（--quick 试译/抽样）----
    # doc.select 保留选中页（后续布局/翻译/渲染索引全部一致）；
    # 输出名带 -p<sel> 标记，试译不覆盖全量输出
    sel_spec = (getattr(cfg.io, "pages", "") or "").strip()
    sel_extra = ""
    if sel_spec:
        from .config import parse_page_ranges
        sel_idx = parse_page_ranges(sel_spec, n_pages)
        if not sel_idx:
            raise ValueError(
                f"io.pages 未选中任何有效页（共 {n_pages} 页）: {sel_spec!r}")
        if len(sel_idx) < n_pages:
            doc.select(sel_idx)
            n_pages = len(doc)
            sel_extra = "-p" + sel_spec.replace(",", "-").replace(" ", "")
            _log(f"pages: selected {n_pages}/{orig_pages} page(s) "
                 f"[{sel_spec}]")
    out_path = out_dir / output_pdf_name(src.stem, tgt_lang,
                                         cfg.features.bilingual,
                                         extra=sel_extra)
    # v0.8.1: 警告收集器升级——入列即转发 sink.warning（UI 可见），
    # CLI stats["warnings"] 行为不变
    all_warnings: list[str] = _WarningList(sink)

    # v0.5.1: 渲染器选择 + RTL/天城文强制 htmlbox（writer 逐字排印无
    # shaping/bidi，阿拉伯语/希伯来语/天城文输出不可读——自动切换并告警）
    renderer = (getattr(cfg.features, "renderer", "htmlbox") or "htmlbox").strip()
    info = lang_info(tgt_lang)
    if renderer == "writer" and (info.rtl or info.script == "indic"):
        w = (f"target language {tgt_lang} ({info.native}) requires the "
             f"htmlbox renderer for shaping/bidi; switched writer -> htmlbox")
        _log(f"WARNING: {w}")
        all_warnings.append(w)
        renderer = "htmlbox"

    # v0.6.0: 排版自适配（fit.mode 缺省 auto——旧配置无 fit 段即生效；
    # off=回到 v0.5.1 元素级引擎缩放行为）
    fit_cfg = cfg.fit if cfg.fit is not None else FitConfig()
    if renderer != "htmlbox" and fit_cfg.mode == "auto":
        _log("fit: renderer != htmlbox, typography auto-fit disabled")
        fit_cfg = FitConfig(mode="off")
    elif fit_cfg.mode == "auto":
        _log(f"fit: mode=auto (expand={fit_cfg.expand_lines} "
             f"lead={fit_cfg.lead_steps} tracking={fit_cfg.tracking} "
             f"min_scale={fit_cfg.min_scale} boost={fit_cfg.body_boost})")

    font_path = find_cjk_font(cfg.fonts.get("cjk"), lang=tgt_lang)
    glossary = Glossary.load(cfg.glossary_file) if cfg.glossary_file else None
    # ---- v0.7.1: 项目级缓存库（翻译缓存 + 文档指纹索引 + 版面缓存）----
    # 默认随输入文件目录（可写探测失败退输出目录）——同一输入译到不同
    # 输出目录共享缓存；legacy 迁移仅翻译缓存开启时执行
    dcache = None
    doc_fp = None
    cache = None
    layout_cache_on = bool(getattr(cfg.performance, "layout_cache", True))
    if cfg.features.translation_cache or layout_cache_on:
        from .doccache import DocumentCache, resolve_cache_root
        croot, csrc = resolve_cache_root(
            (getattr(cfg.performance, "cache_dir", "") or "").strip(),
            src, out_dir)
        dcache = DocumentCache(
            croot,
            max_entries=int(getattr(cfg.performance, "cache_max_entries", 0) or 0),
            legacy_sources=[out_dir / ".translation_cache.db"] \
                if cfg.features.translation_cache else None,
            log=_log)
        _log(f"cache: project store {dcache.path} (root={csrc})")
        cache = dcache.tc if cfg.features.translation_cache else None
        if layout_cache_on:
            doc_fp = dcache.fingerprint(src)

    total_calls = 0
    n_paras = 0
    if control:
        control.checkpoint()   # 开工前最后一刻取消（排队即取消场景）
    sink.stage("layout")

    # ---- D5 水印移除（文字层三层策略，在布局前执行）----
    if cfg.features.watermark_removal:
        from .preprocess import remove_watermarks
        wm_stats = remove_watermarks(doc)
        _log(f"watermark: {wm_stats['streams_cleaned']} streams cleaned, "
             f"{wm_stats['text_blocks_removed']} text blocks removed, "
             f"{wm_stats['annots_removed']} annots removed")

    # ---- v0.8.1: reflow × 扫描页前置拦截（翻译开始前，不是渲染期）----
    # 旧版在渲染阶段才 raise——布局+翻译全部完成后才报错，扫描件用户
    # 白烧全部 LLM 调用费。此时尚未产生任何调用成本即失败。
    if cfg.output.mode == "reflow" and client is not None:
        from . import ocr as _ocr_mod_pre
        _engines_pre = _resolve_ocr_engines(cfg)
        _avail_pre = [e for e in _engines_pre
                      if _ocr_mod_pre.engine_available(e)]
        if _engines_pre and _avail_pre:
            _scanned_pre = [p for p in range(n_pages)
                            if not page_has_text_layer(doc[p],
                                                       cfg.ocr.min_chars)]
            if _scanned_pre:
                _pages_fmt = ", ".join(f"p.{p + 1}" for p in _scanned_pre[:8]) \
                    + ("…" if len(_scanned_pre) > 8 else "")
                raise ValueError(
                    f"reflow 模式暂不支持扫描页（检出无文字层页: {_pages_fmt}，"
                    f"将触发 OCR 任务）；请使用 output.mode: faithful")

    # v0.2.2: 期刊级排版系统（宋体正文/黑体标题/Times 英文层）
    # v0.4.2: 字体族按目标语言解析（langs.py），跨平台候选链
    from .typography import Typography
    typo = None
    if getattr(cfg.features, "preserve_formatting", True):
        try:
            typo = Typography(cfg.fonts, lang=tgt_lang)
            _log(f"typography: body={os.path.basename(typo.body_path) or '(builtin)'}, "
                 f"heading={os.path.basename(typo.heading_path) or '(builtin)'}")
            # 字形覆盖率校验（选到的字体缺目标语言字符 → 豆腐块预警）
            # （lang_info 在模块顶部导入；此处再导入会把整个函数的
            #  lang_info 变成局部名，上方引用直接 UnboundLocalError）
            from .langs import coverage_warnings
            for w in coverage_warnings(typo.f_body, tgt_lang):
                _log(f"WARNING: {w}")
                all_warnings.append(w)
            if typo.heading_path != typo.body_path:
                for w in coverage_warnings(typo.f_head, tgt_lang):
                    _log(f"WARNING: {w}")
                    all_warnings.append(w)
            _log(f"target language: {tgt_lang} ({lang_info(tgt_lang).native})")
        except Exception as e:
            _log(f"typography init failed ({e}); fallback to single-font mode")
            typo = None

    # ---- 布局引擎/并行数/版面缓存路径（v0.7.0：两条路径共用）----
    layout_engine = (getattr(cfg.performance, "layout_engine", "heuristic")
                     or "heuristic").strip()
    workers = _resolve_layout_workers(cfg, n_pages)
    page_layouts: list[dict] = []
    page_pixmaps: list[dict[int, bytes]] = []

    # ---- v0.7.0: 布局-翻译流水线重叠（页级流水，任务 2-2b）----
    # 大文档且启发式布局时：布局后台线程逐页产出，主线程页级喂批发车
    # ——翻译与布局重叠，省整段布局时间。任何故障回退顺序路径
    # （翻译缓存使重跑只剩增量段）。
    state = None
    if _overlap_enabled(cfg, client, n_pages, layout_engine):
        try:
            state = _translate_streaming(
                doc, cfg, client, typo, font_path, tgt_lang, glossary,
                cache, sink, control, fit_cfg, layout_engine, dcache,
                doc_fp, out_dir, all_warnings)
        except JobCancelled:
            raise
        except Exception as e:
            _log(f"streaming overlap failed ({e}); "
                 f"falling back to sequential path")
            state = None
    if state is None:
        # ---- v0.4.3 布局本体：缓存命中 → 并行 → 串行回退 ----
        cached = dcache.load_layout(doc_fp, layout_engine, _LAYOUT_CACHE_VER) \
            if (dcache is not None and doc_fp is not None) else None
        if cached is not None and len(cached) == n_pages:
            page_layouts = cached
            _log(f"layout: cache hit (project store), skipping layout "
                 f"for {n_pages} page(s)")
            sink.emit("layout_cache_hit", pages=n_pages)
            sink.progress(done=n_pages, total=n_pages, unit="page")
            for pno in range(n_pages):
                page_pixmaps.append(
                    _crop_formulas_cached(
                        doc, pno, page_layouts[pno]["formulas"], dcache, doc_fp)
                    if page_layouts[pno].get("formulas") else {})
        if not page_layouts and workers > 1:
            fd, layout_tmp_name = tempfile.mkstemp(suffix=".pdf", dir=str(out_dir))
            os.close(fd)
            layout_tmp = Path(layout_tmp_name)
            _log(f"layout: {workers} workers (parallel), engine={layout_engine}")
            got = None
            try:
                got = _layout_parallel(doc, layout_tmp, workers, sink, control,
                                       n_pages, engine=layout_engine)
            finally:
                try:
                    layout_tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            if got is not None:
                page_layouts, page_pixmaps = got
        if not page_layouts:      # 串行路径（workers==1 或并行回退）
            if workers > 1:
                sink.warning("parallel layout failed; sequential fallback "
                             "(progress counter restarts)")
            for pno in range(n_pages):
                if control:
                    control.checkpoint()
                lay = layout_page(doc[pno], engine=layout_engine)
                pixmaps = _crop_formulas_cached(
                    doc, pno, lay["formulas"], dcache, doc_fp) \
                    if lay["formulas"] else {}
                page_layouts.append(lay)
                page_pixmaps.append(pixmaps)
                sink.page_done(pno, n_pages)
        if dcache is not None and doc_fp is not None and page_layouts and \
                len(page_layouts) == n_pages and \
                not any(p for p in page_pixmaps if p is None):
            # 布局完成（非缓存命中路径）→ 落项目库供断点续跑/跨输出目录复用
            dcache.save_layout(doc_fp, layout_engine, _LAYOUT_CACHE_VER,
                               src, n_pages, page_layouts, sel=sel_spec)
        # v0.5.1: 全部页 fallback 才告警——pymupdf-layout 已装且正常运行时，
        # 纯文本页（GNN 检不出图/表/公式区）按页回退启发式属正常行为，
        # 只看第 0 页会在"首页恰好纯文本"时误报"未安装"
        if page_layouts and layout_engine == "pymupdf-layout" and all(
                l.get("layout_engine") == "pymupdf-layout-fallback"
                for l in page_layouts):
            w = ("performance.layout_engine='pymupdf-layout' but package not "
                 "installed/failed; heuristic fallback in use "
                 "(pip install pymupdf-layout)")
            _log(f"WARNING: {w}")
            all_warnings.append(w)

        pending: list[tuple[int, int]] = []   # (page_no, para_index)
        cell_pending: list[tuple[int, int]] = []   # v0.2.3: (page_no, cell_index)
        for pno, lay in enumerate(page_layouts):
            for i in range(len(lay["paragraphs"])):
                # v0.2.2: verbatim 段（Algorithm 伪代码框/数学碎片）不进翻译队列,
                # 渲染时 texts_by_page 缺位 → 原文回灌
                if not lay["paragraphs"][i].get("is_verbatim"):
                    pending.append((pno, i))
            # v0.2.3: 表格单元格进翻译队列（用户要求三线表也翻译）——
            # 之前 cells 只在 layout 里切出但从未送译（"三线表没翻译"根因）
            # v0.7.0: 置信度分级渲染（任务 2-3）——切分置信度极低
            # （conf<0.5，锚点全对不上的间隙兜底行）的格保留原文
            # + 警告"建议手动核对"：错切时代价高，宁可不译
            for ci, cell in enumerate(lay.get("tables_cells", [])):
                if not cell.get("text", "").strip():
                    continue
                if _cell_translatable(cell):
                    cell_pending.append((pno, ci))
                else:
                    all_warnings.append(
                        f"table cell p.{pno + 1} kept original (low split "
                        f"confidence) - manual check suggested")
            n_paras += len(lay["paragraphs"])
            _log(f"page {pno + 1}/{n_pages}: {lay['mode']}-col, "
                 f"{len(lay['paragraphs'])} paras, {len(lay['formulas'])} formulas, "
                 f"{len(lay.get('tables_cells', []))} table cells")

        # ---- OCR：扫描页检测 + 惰性提取（appendix/inplace/reconstruct）----
        from . import ocr as ocr_mod
        # ocr_jobs: {pno, mode, text(附录全文/原文对照), blocks(原位块 [(bbox,text)])}
        ocr_jobs: list[dict] = []
        scanned = [p for p in range(n_pages)
                   if not page_has_text_layer(doc[p], cfg.ocr.min_chars)]
        ocr_mode = (getattr(cfg.ocr, "mode", "appendix") or "appendix").strip()
        if scanned:
            pages_fmt = ", ".join(f"p.{p + 1}" for p in scanned)
            engines_cfg = _resolve_ocr_engines(cfg)
            engines_avail = [e for e in engines_cfg if ocr_mod.engine_available(e)]
            missing = [e for e in engines_cfg if e not in engines_avail]
            if missing:
                w = (f"OCR engine(s) not installed: {', '.join(missing)} "
                     f"(pip install paddleocr / rapidocr-onnxruntime / "
                     f"tesseract); voted on the rest")
                _log(f"WARNING: {w}")
                all_warnings.append(w)
            if not engines_cfg or engines_cfg == [""]:
                _log(f"scanned pages ({pages_fmt}): OCR engine disabled, kept as-is")
            elif not engines_avail:
                w = (f"scanned pages ({pages_fmt}) but no OCR engine installed "
                     f"(pip install paddleocr); kept as-is")
                _log(f"WARNING: {w}")
                all_warnings.append(w)
            elif client is None:
                _log(f"scanned pages ({pages_fmt}): dry-run, OCR skipped")
            else:
                for pno in scanned:
                    if control:
                        control.checkpoint()
                    job = _ocr_page_job(doc, pno, cfg, ocr_mode,
                                        engines_avail, all_warnings)
                    if job is None:
                        w = f"OCR page {pno + 1}: no text recognized; kept as-is"
                        _log(f"WARNING: {w}")
                        all_warnings.append(w)
                        continue
                    if job["mode"] == "reconstruct":
                        _log(f"page {pno + 1}: scanned, reconstruct "
                             f"{len(job['blocks'])} region(s) → in-place + "
                             f"original appendix")
                    elif job["mode"] == "inplace":
                        _log(f"page {pno + 1}: scanned, OCR {len(job['blocks'])} "
                             f"block(s) → in-place paste-back")
                    else:
                        _log(f"page {pno + 1}: scanned, OCR extracted "
                             f"{len(job['text'])} chars → appendix translation")
                    ocr_jobs.append(job)

        # ---- v0.2.3: 跨页断句合并 ----
        # 一句话被分页截断（前半句在 pN 尾、后半句在 pN+1 头）时拆成两段
        # 分别翻译，读起来断裂（用户实测反馈）。判定：pending 里相邻的
        # (pN, 末段) 与 (pN+1, 首段)，且前段末尾无句末标点（.:;!?。：；？！）
        # 且后段以小写字母/数字开头 → 合并为一个翻译单元，译文按原文长度比
        # 在词边界拆回两段原位。
        def _ends_open(txt: str) -> bool:
            t = txt.rstrip()
            return bool(t) and t[-1] not in ".:;!?。：；？！\")]'\""

        def _starts_lower(txt: str) -> bool:
            t = txt.lstrip()
            return bool(t) and (t[0].islower() or t[0].isdigit()
                                or t[0] in "([{")

        merge_groups: list[list[int]] = []   # flat 索引组
        flat_idx_of: dict[tuple[int, int], int] = {}
        for fi, (pno, pi) in enumerate(pending):
            flat_idx_of[(pno, pi)] = fi
        used: set[int] = set()
        for fi, (pno, pi) in enumerate(pending):
            if fi in used:
                continue
            group = [fi]
            used.add(fi)
            # 只在页边界处尝试续接（同页断句由 merge_paragraphs 处理）
            if pi == len(page_layouts[pno]["paragraphs"]) - 1 and pno + 1 < len(doc):
                nxt = flat_idx_of.get((pno + 1, 0))
                if nxt is not None and nxt not in used:
                    a = page_layouts[pno]["paragraphs"][pi]["text"]
                    b = page_layouts[pno + 1]["paragraphs"][0]["text"]
                    if (_ends_open(a) and _starts_lower(b)
                            and not page_layouts[pno]["paragraphs"][pi].get("is_ref")
                            and not page_layouts[pno + 1]["paragraphs"][0].get("is_ref")
                            and not page_layouts[pno + 1]["paragraphs"][0].get("is_caption")
                            and len(a) + len(b) <= 1200):
                        group.append(nxt)
                        used.add(nxt)
            merge_groups.append(group)

        # ---- 批量翻译（全文档统一排队，摊薄调用次数）----
        texts_by_page: dict[int, list[str | None]] = {}
        cell_texts: dict[tuple[int, int], str] = {}   # v0.2.3: (pno, ci) → 译文
        # v0.8.0 P3: 跨页合并单元整段译文（reflow 用；faithful 忽略）
        cross_full: dict[tuple[int, int], str] = {}
        cross_skip: set = set()
        if client is not None:
            # v0.2.3: 合并组拼成翻译单元（组内段落用 \n 连接送译）
            # v0.5.1: 跨页连字符合并——前段尾 '-' + 后段小写开头 = 同词被
            # 页边界切断（LaTeX 排版常见），去连字符直连送译；否则 LLM 会把
            # 'instrumen-\ntation' 当两个残词，译文断句不自然
            def _join_group(parts: list[str]) -> str:
                if len(parts) == 2:
                    a, b = parts[0].rstrip(), parts[1].lstrip()
                    if a.endswith("-") and b[:1].islower():
                        return a[:-1] + b
                return "\n".join(parts)

            unit_texts: list[str] = []
            unit_kinds: list["str | None"] = []   # v0.7.0 句子级缓存的目标单元
            for g in merge_groups:
                parts = []
                for fi in g:
                    pno, pi = pending[fi]
                    para = page_layouts[pno]["paragraphs"][pi]
                    parts.append(para["text"])
                unit_texts.append(_join_group(parts))
                # 模板化文本（ref 条目/图注）启用句级缓存；合并单元/其余不启用
                if len(g) == 1:
                    para = page_layouts[pending[g[0]][0]]["paragraphs"][pending[g[0]][1]]
                    unit_kinds.append("ref" if para.get("is_ref")
                                      else "caption" if para.get("is_caption")
                                      else None)
                else:
                    unit_kinds.append(None)
            # v0.2.3: 表格单元格并入同一翻译队列（同一批协议，省调用次数）
            cell_flat: list[str] = []
            for pno, ci in cell_pending:
                cell_flat.append(page_layouts[pno]["tables_cells"][ci]["text"])
            unit_kinds.extend([None] * len(cell_flat))
            # v0.5.0: OCR 翻译单元——inplace/reconstruct 页按块、appendix 页按整页文本
            ocr_flat: list[str] = []
            for jb in ocr_jobs:
                if jb["mode"] in ("inplace", "reconstruct"):
                    ocr_flat.extend(t for _, t in jb["blocks"])
                else:
                    ocr_flat.append(jb["text"])
            unit_kinds.extend([None] * len(ocr_flat))
            all_flat = unit_texts + cell_flat + ocr_flat

            # v0.5.0: llm.effective()——rpm/tpm 配额自动换算 interval/batch
            # （显式配置优先；dry-run 日志里能看到实际生效值）
            llm_eff = cfg.llm.effective()
            if (llm_eff.min_call_interval, llm_eff.batch_char_budget) != \
                    (cfg.llm.min_call_interval, cfg.llm.batch_char_budget):
                _log(f"llm quota auto: interval={llm_eff.min_call_interval}s, "
                     f"batch_char_budget={llm_eff.batch_char_budget} "
                     f"(rpm={cfg.llm.rpm_limit}, tpm={cfg.llm.tpm_limit})")
            tc = TranslationClient(
                client, model=llm_eff.model,
                temperature=llm_eff.temperature,
                glossary_prompt=glossary.prompt_block() if glossary else "",
                src_lang=cfg.io.source_lang, tgt_lang=cfg.io.target_lang,
                batch_size=llm_eff.batch_size,
                batch_char_budget=llm_eff.batch_char_budget,
                max_llm_calls=llm_eff.max_llm_calls,
                min_call_interval=llm_eff.min_call_interval,
                max_workers=llm_eff.max_workers,
                fallback_model=llm_eff.fallback_model,
                timeout=llm_eff.timeout,
                max_retries=llm_eff.max_retries,
                backoff_base=llm_eff.backoff_base,
                backoff_cap=llm_eff.backoff_cap,
                retry_delay_cap=llm_eff.retry_delay_cap,
                sink=sink, control=control,
                stream=bool(getattr(cfg.llm, "stream", True)),
                sentence_cache=bool(getattr(cfg.llm, "sentence_cache", True)),
            )
            if control:
                control.checkpoint()
            sink.stage("translate")
            # v0.6.0 任务 E：源头控长——翻译前把每段目标框字符预算喂给 LLM，
            # 超预算译文单段带强约束重译一次（重问上限 = max_llm_calls 的 10%）
            unit_budgets = None
            if fit_cfg.mode == "auto":
                try:
                    unit_budgets = _unit_char_budgets(
                        merge_groups, pending, page_layouts, cell_pending, typo,
                        font_path, tgt_lang, len(cell_flat), len(ocr_flat))
                    if unit_budgets is not None:
                        n_bud = sum(1 for b in unit_budgets if b)
                        _log(f"fit: char budgets for {n_bud}/{len(unit_budgets)} "
                             f"unit(s)")
                    else:
                        _log("fit: budget length mismatch; budgets disabled")
                except Exception as e:
                    _log(f"fit: budget estimation failed ({e}); disabled")
                    unit_budgets = None
            translated_flat, total_calls = tc.translate_paragraphs(
                all_flat, cache=cache, budgets=unit_budgets,
                unit_kinds=unit_kinds)
            all_warnings.extend(tc.warnings)
            if tc.sent_cache_hits:
                _log(f"cache: {tc.sent_cache_hits} template unit(s) served by "
                     f"sentence-level cache")
            cell_translations = translated_flat[len(merge_groups):
                                                len(merge_groups) + len(cell_flat)]
            ocr_translations = translated_flat[len(merge_groups) + len(cell_flat):]

            # D4 译后校验（glossary_lock）——只校验单段单元（合并单元跳过）。
            # v0.7.0: 违例先走确定性修复（源词逐字残留 → 原位替换为目标词，
            # 零 LLM 调用——替代 logit_bias 类约束解码的跨 provider 方案），
            # 修复后仍违例才告警
            if glossary and cfg.features.glossary_lock:
                n_fixed = 0
                for gi, g in enumerate(merge_groups):
                    if len(g) != 1:
                        continue
                    pno, pi = pending[g[0]]
                    fixed, done = glossary.fix_translation(translated_flat[gi])
                    if done:
                        translated_flat[gi] = fixed
                        n_fixed += 1
                        _log(f"glossary fix p{pno + 1}#{pi}: "
                             + ", ".join(done))
                    bad = glossary.check_translation(translated_flat[gi])
                    if bad:
                        all_warnings.append(
                            f"glossary violation p{pno + 1}#{pi}: {bad}")
                if n_fixed:
                    _log(f"glossary: {n_fixed} paragraph(s) fixed in place")

            # v0.2.3: 合并单元译文按原文长度比例拆回各段原位
            for gi, g in enumerate(merge_groups):
                dst = translated_flat[gi]
                if len(g) == 1:
                    pno, pi = pending[g[0]]
                    texts_by_page.setdefault(
                        pno, [None] * len(page_layouts[pno]["paragraphs"]))
                    texts_by_page[pno][pi] = dst
                    continue
                # 两段组：按原文长度比在词边界切分
                pno_a, pi_a = pending[g[0]]
                pno_b, pi_b = pending[g[1]]
                # v0.8.0 P3: reflow 需要整段译文（faithful 拆回两页原位）
                cross_full[(pno_a, pi_a)] = dst
                cross_skip.add((pno_b, pi_b))
                len_a = len(page_layouts[pno_a]["paragraphs"][pi_a]["text"])
                len_b = len(page_layouts[pno_b]["paragraphs"][pi_b]["text"])
                part_a, part_b = _split_proportional(dst, len_a / max(len_a + len_b, 1))
                texts_by_page.setdefault(
                    pno_a, [None] * len(page_layouts[pno_a]["paragraphs"]))
                texts_by_page.setdefault(
                    pno_b, [None] * len(page_layouts[pno_b]["paragraphs"]))
                texts_by_page[pno_a][pi_a] = part_a
                texts_by_page[pno_b][pi_b] = part_b
            for ci, dst in enumerate(cell_translations):
                cell_texts[cell_pending[ci]] = dst
        state = {
            "page_layouts": page_layouts,
            "page_pixmaps": page_pixmaps,
            "texts_by_page": texts_by_page,
            "cell_texts": cell_texts,
            "cross_full": cross_full,
            "cross_skip": cross_skip,
            "ocr_jobs": ocr_jobs,
            "ocr_translations": (ocr_translations if client is not None else []),
            "n_paras": n_paras,
            "total_calls": (total_calls if client is not None else 0),
            "tc": (tc if client is not None else None),
        }
    page_layouts = state["page_layouts"]
    page_pixmaps = state["page_pixmaps"]
    texts_by_page = state["texts_by_page"]
    cell_texts = state["cell_texts"]
    cross_full = state.get("cross_full") or {}
    cross_skip = state.get("cross_skip") or set()
    ocr_jobs = state["ocr_jobs"]
    ocr_translations = state["ocr_translations"]
    n_paras = state["n_paras"]
    total_calls = state["total_calls"]
    tc = state["tc"]

    # ---- v0.7.0: 跨页表延续链接（任务 2-3，两条路径汇合处统一跑）----
    # 下页顶部表与上页底部表同表头/同列锚 → 同一逻辑表（gid 共享）：
    # 渲染层同 gid 同 fit 类（跨页字号统一），翻译队列页序相邻天然连续
    from .layout import link_crosspage_tables
    try:
        n_linked = link_crosspage_tables(
            page_layouts, [doc[pno].rect for pno in range(len(page_layouts))])
        if n_linked:
            _log(f"tables: {n_linked} cross-page continuation(s) linked")
    except Exception as e:
        _log(f"table continuation link failed ({e}); skipped")

    # ---- 渲染回灌 ----
    if control:
        control.checkpoint()
    sink.stage("render")

    # ---- v0.8.0 P3: reflow 整文档重排（output.mode: reflow）----
    # 文档模型 → 新模板 → Story 流式写入（render_reflow.py）；不 redact
    # 原 doc、无 fit 因子/降级阶梯（框跟内容走）；翻译层与 faithful 完全
    # 共用（同缓存同调用数）。扫描页拦截已前置到翻译开始前（v0.8.1），
    # 此处 ocr_jobs 恒空——保留断言防御（前置漏判时不带渲染成本报错）。
    if cfg.output.mode == "reflow":
        if ocr_jobs:
            raise ValueError(
                "reflow 模式暂不支持扫描页（检测到 OCR 任务）；"
                "请使用 output.mode: faithful")
        _log("renderer: reflow (whole-document Story re-layout)")
        reflow_warns: list[str] = []
        pixmaps_by_page = {pno: (page_pixmaps[pno] or {})
                           for pno in range(len(page_layouts))
                           if pno < len(page_pixmaps)}
        cache_hits = getattr(tc, "cache_hits", 0) if client is not None else 0
        cache_saved_calls = 0
        if cache_hits:
            bs = max(1, int(getattr(cfg.llm, "batch_size", 6) or 6))
            cache_saved_calls = -(-cache_hits // bs)
        try:
            pdf_bytes = render_reflow_document(
                page_layouts, doc, texts_by_page, cell_texts,
                cross_full, cross_skip, pixmaps_by_page, typo, font_path,
                tgt_lang, cfg.reflow, reflow_warns, _log,
                dcache=dcache, doc_fp=doc_fp)
            all_warnings.extend(reflow_warns)
            out_path = out_dir / output_pdf_name(
                src.stem, tgt_lang, False, extra="-reflow" + sel_extra)
            # v0.8.1: 原子写盘（与 faithful 同纪律：tmp + rename，中途
            # 崩溃不留半成品）+ 缓存统计真实值（旧版硬编码 0，UI 完成行
            # 「省约 N 次调用」缺失）
            cache_entries = 0
            if cache is not None:
                try:
                    cache_entries = cache.count()
                except Exception:
                    pass
            tmp_path = out_path.with_suffix(".pdf.tmp")
            tmp_path.write_bytes(pdf_bytes)
            os.replace(tmp_path, out_path)
        finally:
            # v0.8.1: 资源收口（旧版 reflow 路径直接 return——dcache 的
            # SQLite 连接与 doc 句柄从未关闭，Windows 上锁库锁文件）
            try:
                doc.close()
            except Exception:
                pass
            if dcache is not None:
                dcache.close()
            elif cache:
                cache.close()
        _log(f"done: {n_paras} paras, {total_calls} LLM calls, "
             f"{cache_hits} cache hits, {len(all_warnings)} warnings, "
             f"{time.time() - t0:.1f}s")
        sink.emit("done", output=str(out_path), pages=n_pages,
                  paragraphs=n_paras, calls=total_calls,
                  cache_hits=cache_hits,
                  cache_saved_calls=cache_saved_calls,
                  cache_entries=cache_entries,
                  elapsed=round(time.time() - t0, 1))
        return {
            "pages": n_pages,
            "paragraphs": n_paras,
            "calls": total_calls,
            "warnings": all_warnings,
            "output": str(out_path),
            "cache_db": (str(dcache.path) if dcache is not None else ""),
            "ocr_pages": 0,
            "ocr_inplace_blocks": 0,
            "cache_hits": cache_hits,
            "cache_saved_calls": cache_saved_calls,
            "cache_entries": cache_entries,
        }

    if renderer == "writer":
        _log("renderer: writer (legacy TextWriter engine)")
    # v0.8.1: writer 路径字体对象每文档一次（S2：pymupdf.Font(fontfile=…)
    # 实测 ~93ms/个，旧版 render_page 每页重建；htmlbox 路径已零构建）
    doc_fonts = None
    if renderer == "writer":
        from .langs import resolve_original_font as _rof
        _latin_path = (typo.en_body_path if typo else None) or _rof()
        doc_fonts = {
            "body": pymupdf.Font(fontfile=font_path),
            "latin": (pymupdf.Font(fontfile=_latin_path) if _latin_path
                      else pymupdf.Font("helv")),
        }
    # ---- v0.7.1: 页级 Story 接管（任务 2-3 P1）----
    # render.page_story（auto/on/off）+ 计数（启用率/回退原因汇总日志）
    page_story_cfg = (getattr(cfg.render, "page_story", "auto") or "auto")
    if page_story_cfg != "off" and renderer == "htmlbox" \
            and fit_cfg.mode == "auto":
        _log(f"page_story: {page_story_cfg} (whole-page story接管, "
             f"per-page precheck + page-granularity fallback)")
    story_stats = {"story": 0, "fallback": 0, "reasons": []}

    # ---- v0.6.0 任务 B/C/D：两遍式排版自适配（测量 pass，在渲染前）----
    # 遍历全部页/段收集渲染规格（与渲染 pass 同一函数产出），按样式类
    # 算统一因子——同类元素字号永远一致（DTP 改样式表不改文本框）。
    # 版面缓存不受影响：测量发生在渲染期，不写入 layout 输出。
    fit_factors: dict[str, dict] | None = None
    para_specs_by_page: dict[int, list[dict]] = {}
    cell_specs_by_page: dict[int, list[dict]] = {}
    doc_arch = doc_font_css = None
    if fit_cfg.mode == "auto":
        doc_arch, doc_font_css = _build_font_archive(
            font_path, typo.heading_path if typo else None)
        groups: dict[str, list[dict]] = {}
        for pno in range(n_pages):
            lay = page_layouts[pno]
            paras_ = lay["paragraphs"]
            page_texts_ = texts_by_page.get(pno) or [None] * len(paras_)
            tmap_ = {i: page_texts_[i] for i in range(len(paras_))}
            formula_rects_ = [pymupdf.Rect(f["bbox"])
                              for f in lay.get("formulas", [])]
            para_specs_by_page[pno] = collect_para_specs(
                paras_, tmap_, typo, cfg.features.bilingual, formula_rects_,
                tgt_lang, layout=lay, fit_cfg=fit_cfg,
                page_h=doc[pno].rect.height)
            page_cell_map = {ci: cell_texts.get((pno, ci))
                             for ci in range(len(lay.get("tables_cells", [])))}
            cell_specs_by_page[pno] = collect_cell_specs(
                lay.get("tables_cells") or [], page_cell_map,
                lay.get("tables"), font_path, tgt_lang)
            for s in para_specs_by_page[pno]:
                groups.setdefault(s["cls"], []).append(s)
            for s in cell_specs_by_page[pno]:
                if s.get("cls"):
                    groups.setdefault(s["cls"], []).append(s)

        def _spec_css_factory(spec: dict, factor: float, lead: float,
                              tracking: float) -> str:
            if spec.get("kind") == "cell":
                return cell_spec_css(spec, doc_font_css)
            return spec_css(spec, doc_font_css, lead, tracking,
                            factor=factor)

        _t_fit = time.time()
        fit_warns: list[str] = []
        fit_factors = compute_style_factors(
            groups, fit_cfg, _spec_css_factory, doc_arch,
            warnings=fit_warns, log=lambda m: _log(f"fit: {m}"))
        all_warnings.extend(fit_warns)
        _log("fit pass: {} unit(s) in {:.1f}s; factors: {}".format(
            sum(len(v) for v in groups.values()), time.time() - _t_fit,
            ", ".join(f"{k}x{v['factor']:.2f}"
                      + (f"@lead{v['lead']:.2f}" if abs(v["lead"] - 1.0) > 0.005 else "")
                      for k, v in sorted(fit_factors.items()))))
        sink.emit("fit_pass", factors={k: round(v["factor"], 3)
                                       for k, v in fit_factors.items()})

    for pno in range(len(doc)):
        if control:
            control.checkpoint()
        paras = page_layouts[pno]["paragraphs"]
        page_texts = texts_by_page.get(pno) or [None] * len(paras)
        translated = [{"index": i, "text": page_texts[i]}
                      for i in range(len(paras))]
        # v0.2.3: 表格单元格译文传入渲染层（原位回灌）
        lay = page_layouts[pno] or {}
        page_cell_texts = {ci: cell_texts.get((pno, ci))
                           for ci in range(len(lay.get("tables_cells", [])))}
        render_page(doc[pno], page_layouts[pno], translated,
                    font_path, formula_pixmaps=page_pixmaps[pno],
                    bilingual=cfg.features.bilingual,
                    warnings=all_warnings,
                    typography=typo,
                    cell_texts=page_cell_texts,
                    renderer=renderer,
                    lang=tgt_lang,
                    para_specs=para_specs_by_page.get(pno),
                    cell_specs=cell_specs_by_page.get(pno),
                    factors=fit_factors,
                    fit_cfg=fit_cfg if fit_cfg.mode == "auto" else None,
                    archive=doc_arch,
                    font_css=doc_font_css,
                    page_story=page_story_cfg,
                    story_stats=story_stats,
                    fonts=doc_fonts)
    if story_stats["story"] or story_stats["fallback"]:
        _log(f"page_story: {story_stats['story']} page(s) whole-page story, "
             f"{story_stats['fallback']} per-paragraph fallback")
        for r in story_stats["reasons"]:
            _log(f"page_story: {r}")

    # ---- OCR 译文落页（appendix 插页 / inplace 原位回贴 / reconstruct 重建）----
    ocr_pages_added = 0
    ocr_inplace_blocks = 0
    if ocr_jobs:
        # inserts: (pno, kind, payload)；kind ∈ original（reconstruct 原文
        # 对照）/ translation（appendix 译文，payload=(src, dst)）
        # 两遍制：先全部原位回贴（索引不漂移），再按页号降序插页
        # （先插高位不影响低位索引——与 _append_ocr_pages 同纪律）
        inserts: list[tuple[int, str, object]] = []
        consumed = 0
        for jb in ocr_jobs:
            if jb["mode"] in ("inplace", "reconstruct"):
                n = len(jb["blocks"])
                ocr_inplace_blocks += _apply_ocr_inplace(
                    doc, jb["pno"], jb["blocks"],
                    ocr_translations[consumed:consumed + n],
                    font_path, all_warnings,
                    renderer=renderer, lang=tgt_lang)
                consumed += n
                if jb["mode"] == "reconstruct":
                    inserts.append((jb["pno"], "original", jb["text"]))
            else:
                inserts.append((jb["pno"], "translation",
                                (jb["text"], ocr_translations[consumed])))
                consumed += 1
        for pno, kind, payload in sorted(inserts, key=lambda x: -x[0]):
            if kind == "original":
                ocr_pages_added += _append_ocr_original(
                    doc, pno, payload, font_path, all_warnings,
                    renderer=renderer, lang=tgt_lang)
            else:
                src, dst = payload
                ocr_pages_added += _append_ocr_pages(
                    doc, [(pno, src)], [dst], font_path, all_warnings,
                    renderer=renderer, lang=tgt_lang)
        if ocr_pages_added:
            _log(f"ocr: {ocr_pages_added} appendix page(s) inserted")
        if ocr_inplace_blocks:
            _log(f"ocr: {ocr_inplace_blocks} block(s) pasted in-place")

    # 原子写:先 .tmp 再 rename,中途崩溃不留半成品 PDF
    # v0.7.0 修复：entries 统计在关库前取——旧版在 finally close 后查
    # count() 抛异常被吞，"entries total" 恒打印 0
    cache_entries = 0
    if cache is not None:
        try:
            cache_entries = cache.count()
        except Exception:
            pass
    try:
        tmp_path = out_path.with_suffix(".pdf.tmp")
        doc.save(str(tmp_path), garbage=4, deflate=True)
        doc.close()
        os.replace(tmp_path, out_path)
    finally:
        # v0.7.1: 项目级缓存库统一收口（dcache.close 同时关翻译缓存表）
        if dcache is not None:
            dcache.close()
        elif cache:
            cache.close()

    # ---- 缓存统计（v0.5.1: 按文档维度的节省报表）----
    cache_hits = getattr(tc, "cache_hits", 0) if client is not None else 0
    cache_saved_calls = 0
    if cache_hits:
        bs = max(1, int(getattr(cfg.llm, "batch_size", 6) or 6))
        cache_saved_calls = -(-cache_hits // bs)   # ceil：命中的段折算批次数
    if cache_hits:
        _log(f"cache: {cache_hits} segment hit(s), ~{cache_saved_calls} "
             f"call(s) saved, {cache_entries} entr(y|ies) total")

    stats = {
        "pages": n_pages,
        "paragraphs": n_paras,
        "calls": total_calls,
        "warnings": all_warnings,
        "output": str(out_path),
        "cache_db": (str(dcache.path) if dcache is not None else ""),
        "ocr_pages": ocr_pages_added,
        "ocr_inplace_blocks": ocr_inplace_blocks,
        "cache_hits": cache_hits,
        "cache_saved_calls": cache_saved_calls,
        "cache_entries": cache_entries,
    }
    sink.emit("done", output=str(out_path), pages=n_pages,
              paragraphs=n_paras, calls=total_calls,
              ocr_pages=ocr_pages_added,
              ocr_inplace_blocks=ocr_inplace_blocks,
              cache_hits=cache_hits,
              cache_saved_calls=cache_saved_calls,
              cache_entries=cache_entries,
              elapsed=round(time.time() - t0, 1))
    _log(f"done: {n_paras} paras, {total_calls} LLM calls, "
         f"{cache_hits} cache hits, "
         f"{len(all_warnings)} warnings, {time.time() - t0:.1f}s")
    return stats
