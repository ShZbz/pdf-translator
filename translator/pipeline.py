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

import os
import sys
import tempfile
import time
from pathlib import Path

import pymupdf

from .cache import TranslationCache
from .config import Config
from .control import JobControl, JobCancelled
from .events import EventSink
from .extract import page_has_text_layer
from .glossary import Glossary
from .layout import layout_page
from .langs import output_tag
from .llm import TranslationClient
from .render import _clean_zh_text, _draw_para, _wrap_cjk, crop_formula_pixmaps, find_cjk_font, render_page

_VERBOSE_TS = False

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
    """
    if ratio <= 0.0:
        return "", dst
    if ratio >= 1.0:
        return dst, ""
    if len(dst) < 2:
        return "", dst
    target = len(dst) * ratio
    best = min(range(1, len(dst)), key=lambda i: (
        abs(i - target)
        + (0 if i >= len(dst) or dst[i] in " ，。；：、）】 " else 5)))
    return dst[:best].rstrip(), dst[best:].lstrip()


def output_pdf_name(stem: str, tgt_lang: str, bilingual: bool) -> str:
    """输出文件名：{stem}[-bilingual]-{语言标记}.pdf（zh→-Zh 旧版兼容）。"""
    suffix = "-bilingual" if bilingual else ""
    return f"{stem}{suffix}-{output_tag(tgt_lang)}.pdf"


def _log(msg: str) -> None:
    """进度日志走 stderr;verbose 模式加时间戳。"""
    import time as _t
    ts = f"[{_t.strftime('%H:%M:%S')}]" if _VERBOSE_TS else ""
    print(f"{ts}[pipeline] {msg}", file=sys.stderr)


def _append_ocr_pages(doc, ocr_units: list[tuple[int, str]],
                      translations: list[str], font_path: str,
                      warnings: list[str]) -> int:
    """v0.4.3: OCR 译文附录页——插在对应扫描页之后。

    从高页号往低插（先插高位不影响低位索引）。单页放不下时截断并告警。
    """
    font = pymupdf.Font(fontfile=font_path)
    added = 0
    pairs = sorted(zip(ocr_units, translations), key=lambda x: -x[0][0])
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
        clipped = False
        for ln in _wrap_cjk(text, font, fs, page.rect.width - 84.0):
            if y > page.rect.height - 42.0:
                clipped = True
                break
            tw.append(pymupdf.Point(42.0, y), ln, font=font, fontsize=fs)
            y += fs * 1.5
        tw.write_text(page)
        if clipped:
            warnings.append(
                f"OCR appendix p.{pno + 1}: text truncated (page full)")
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
                       warnings: list[str]) -> int:
    """白块覆盖 + 译文原位回灌（PDFMathTranslate 式，任务 2-4）。

    每块：与页内图形（子图/矢量线）重叠超 30% 面积 → 跳过保留原像素
    （白块会误伤插图）；否则白矩形盖掉原文、译文按块 bbox 试排回灌。
    返回成功回贴的块数。
    """
    page = doc[pno]
    font = pymupdf.Font(fontfile=font_path)
    graphics = _page_graphics_rects(page)
    applied = 0
    heights = sorted(r.height for r, _ in blocks)
    med_h = heights[len(heights) // 2] if heights else 10.0
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
        tw = pymupdf.TextWriter(page.rect)
        base = max(6.5, min(12.0, med_h * 0.85))
        _draw_para(tw, rect, text, font, base, 0, warnings,
                   f"ocr-inplace p{pno + 1}", lh_factor=1.3)
        tw.write_text(page)
        applied += 1
    return applied


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
    out_path = out_dir / output_pdf_name(src.stem, tgt_lang,
                                         cfg.features.bilingual)

    font_path = find_cjk_font(cfg.fonts.get("cjk"), lang=tgt_lang)
    glossary = Glossary.load(cfg.glossary_file) if cfg.glossary_file else None
    cache = TranslationCache(
        out_dir / ".translation_cache.db",
        max_entries=int(getattr(cfg.performance, "cache_max_entries", 0) or 0)
    ) if cfg.features.translation_cache else None

    doc = pymupdf.open(src)
    n_pages = len(doc)
    total_calls = 0
    all_warnings: list[str] = []
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
            from .langs import coverage_warnings, lang_info
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

    # ---- v0.4.3: 布局 + 裁公式（多进程并行，故障自动回退串行）----
    layout_engine = (getattr(cfg.performance, "layout_engine", "heuristic")
                     or "heuristic").strip()
    workers = _resolve_layout_workers(cfg, n_pages)
    page_layouts: list[dict] = []
    page_pixmaps: list[dict[int, bytes]] = []
    if workers > 1:
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
            pixmaps = crop_formula_pixmaps(doc, pno, lay["formulas"]) \
                if lay["formulas"] else {}
            page_layouts.append(lay)
            page_pixmaps.append(pixmaps)
            sink.page_done(pno, n_pages)
    if page_layouts and page_layouts[0].get("layout_engine") == \
            "pymupdf-layout-fallback":
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
        for ci, cell in enumerate(lay.get("tables_cells", [])):
            if cell.get("text", "").strip():
                cell_pending.append((pno, ci))
        n_paras += len(lay["paragraphs"])
        _log(f"page {pno + 1}/{n_pages}: {lay['mode']}-col, "
             f"{len(lay['paragraphs'])} paras, {len(lay['formulas'])} formulas, "
             f"{len(lay.get('tables_cells', []))} table cells")

    # ---- OCR：扫描页检测 + 惰性提取（appendix/inplace 两种呈现）----
    from . import ocr as ocr_mod
    # ocr_jobs: {pno, mode, text(附录全文), blocks(原位块 [(bbox,text)])}
    ocr_jobs: list[dict] = []
    scanned = [p for p in range(n_pages)
               if not page_has_text_layer(doc[p], cfg.ocr.min_chars)]
    ocr_mode = (getattr(cfg.ocr, "mode", "appendix") or "appendix").strip()
    if scanned:
        pages_fmt = ", ".join(f"p.{p + 1}" for p in scanned)
        engine = (cfg.ocr.engine or "").strip()
        if engine in ("", "none"):
            _log(f"scanned pages ({pages_fmt}): OCR engine disabled, kept as-is")
        elif not ocr_mod.engine_available(engine):
            w = (f"scanned pages ({pages_fmt}) but OCR engine '{engine}' "
                 f"not installed (pip install paddleocr); kept as-is")
            _log(f"WARNING: {w}")
            all_warnings.append(w)
        elif client is None:
            _log(f"scanned pages ({pages_fmt}): dry-run, OCR skipped")
        else:
            for pno in scanned:
                if control:
                    control.checkpoint()
                job: dict = {"pno": pno, "mode": "appendix", "text": "",
                             "blocks": []}
                lines = None
                if ocr_mode == "inplace":
                    lines = ocr_mod.ocr_page_lines(
                        doc[pno], engine=engine, src_lang=cfg.io.source_lang)
                if lines:
                    blocks = _group_ocr_lines(lines)
                    if blocks and sum(len(t) for _, t in blocks) >= 10:
                        job["mode"] = "inplace"
                        job["blocks"] = blocks
                        _log(f"page {pno + 1}: scanned, OCR {len(blocks)} "
                             f"block(s) → in-place paste-back")
                    else:
                        lines = None   # 块化失败 → 全文附录兜底
                if lines is None:
                    text = ocr_mod.ocr_page_text(
                        doc[pno], engine=engine, src_lang=cfg.io.source_lang)
                    if text and len(text.strip()) >= 10:
                        job["text"] = text
                        _log(f"page {pno + 1}: scanned, OCR extracted "
                             f"{len(text)} chars → appendix translation")
                    else:
                        w = f"OCR page {pno + 1}: no text recognized; kept as-is"
                        _log(f"WARNING: {w}")
                        all_warnings.append(w)
                        continue
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
    if client is not None:
        # v0.2.3: 合并组拼成翻译单元（组内段落用 \n 连接送译）
        unit_texts: list[str] = []
        for g in merge_groups:
            parts = []
            for fi in g:
                pno, pi = pending[fi]
                parts.append(page_layouts[pno]["paragraphs"][pi]["text"])
            unit_texts.append("\n".join(parts))
        # v0.2.3: 表格单元格并入同一翻译队列（同一批协议，省调用次数）
        cell_flat: list[str] = []
        for pno, ci in cell_pending:
            cell_flat.append(page_layouts[pno]["tables_cells"][ci]["text"])
        # v0.5.0: OCR 翻译单元——inplace 页按块、appendix 页按整页文本
        ocr_flat: list[str] = []
        for jb in ocr_jobs:
            if jb["mode"] == "inplace":
                ocr_flat.extend(t for _, t in jb["blocks"])
            else:
                ocr_flat.append(jb["text"])
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
        )
        if control:
            control.checkpoint()
        sink.stage("translate")
        translated_flat, total_calls = tc.translate_paragraphs(all_flat, cache=cache)
        all_warnings.extend(tc.warnings)
        cell_translations = translated_flat[len(merge_groups):
                                            len(merge_groups) + len(cell_flat)]
        ocr_translations = translated_flat[len(merge_groups) + len(cell_flat):]

        # D4 译后校验（glossary_lock）——只校验单段单元（合并单元跳过）
        if glossary and cfg.features.glossary_lock:
            for gi, g in enumerate(merge_groups):
                if len(g) != 1:
                    continue
                pno, pi = pending[g[0]]
                bad = glossary.check_translation(translated_flat[gi])
                if bad:
                    all_warnings.append(
                        f"glossary violation p{pno + 1}#{pi}: {bad}")

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

    # ---- 渲染回灌 ----
    if control:
        control.checkpoint()
    sink.stage("render")
    renderer = (getattr(cfg.features, "renderer", "writer") or "writer").strip()
    if renderer == "htmlbox":
        _log("renderer: htmlbox (experimental insert_htmlbox engine)")
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
                    renderer=renderer)

    # ---- OCR 译文落页（appendix 插页 / inplace 原位回贴）----
    ocr_pages_added = 0
    ocr_inplace_blocks = 0
    if ocr_jobs:
        appendix_pairs: list[tuple[int, str]] = []
        appendix_texts: list[str] = []
        consumed = 0
        for jb in ocr_jobs:
            if jb["mode"] == "inplace":
                n = len(jb["blocks"])
                ocr_inplace_blocks += _apply_ocr_inplace(
                    doc, jb["pno"], jb["blocks"],
                    ocr_translations[consumed:consumed + n],
                    font_path, all_warnings)
                consumed += n
            else:
                appendix_pairs.append((jb["pno"], jb["text"]))
                appendix_texts.append(ocr_translations[consumed])
                consumed += 1
        if appendix_pairs:
            ocr_pages_added = _append_ocr_pages(
                doc, appendix_pairs, appendix_texts, font_path, all_warnings)
            _log(f"ocr: {ocr_pages_added} appendix page(s) inserted")
        if ocr_inplace_blocks:
            _log(f"ocr: {ocr_inplace_blocks} block(s) pasted in-place")

    # 原子写:先 .tmp 再 rename,中途崩溃不留半成品 PDF
    try:
        tmp_path = out_path.with_suffix(".pdf.tmp")
        doc.save(str(tmp_path), garbage=4, deflate=True)
        doc.close()
        os.replace(tmp_path, out_path)
    finally:
        if cache:
            cache.close()

    stats = {
        "pages": n_pages,
        "paragraphs": n_paras,
        "calls": total_calls,
        "warnings": all_warnings,
        "output": str(out_path),
        "ocr_pages": ocr_pages_added,
        "ocr_inplace_blocks": ocr_inplace_blocks,
    }
    sink.emit("done", output=str(out_path), pages=n_pages,
              paragraphs=n_paras, calls=total_calls,
              ocr_pages=ocr_pages_added,
              ocr_inplace_blocks=ocr_inplace_blocks,
              elapsed=round(time.time() - t0, 1))
    _log(f"done: {n_paras} paras, {total_calls} LLM calls, "
         f"{len(all_warnings)} warnings, {time.time() - t0:.1f}s")
    return stats
