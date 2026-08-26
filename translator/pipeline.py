"""总编排：preprocess → extract → layout → crop公式 → cache/llm翻译 → render → 输出。

进度日志走 stderr（stdout 留给验收脚本断言）。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pymupdf

from .cache import TranslationCache
from .config import Config
from .control import JobControl, JobCancelled
from .events import EventSink
from .glossary import Glossary
from .layout import layout_page
from .llm import TranslationClient
from .render import crop_formula_pixmaps, find_cjk_font, render_page

_VERBOSE_TS = False


def _split_proportional(dst: str, ratio: float) -> tuple[str, str]:
    """v0.2.3 跨页断句：合并译文按原文长度比例在词/标点边界切成两半。"""
    if ratio <= 0.0:
        return "", dst
    if ratio >= 1.0:
        return dst, ""
    target = len(dst) * ratio
    best = min(range(1, len(dst)), key=lambda i: (
        abs(i - target)
        + (0 if i >= len(dst) or dst[i] in " ，。；：、）】 " else 5)))
    return dst[:best].rstrip(), dst[best:].lstrip()


def _log(msg: str) -> None:
    """进度日志走 stderr;verbose 模式加时间戳。"""
    import time as _t
    ts = f"[{_t.strftime('%H:%M:%S')}]" if _VERBOSE_TS else ""
    print(f"{ts}[pipeline] {msg}", file=sys.stderr)


def translate_document(cfg: Config, client=None, verbose: bool = False,
                       sink: "EventSink | None" = None,
                       control: "JobControl | None" = None) -> dict:
    """整册翻译。client 为注入的 OpenAI 兼容实例（None=跳过翻译只测管线）。

    v0.4.0: sink=进度事件流（None=纯 CLI 模式零开销）；
            control=暂停/取消控制（None=不可控，原行为）。
    返回统计：{pages, paragraphs, calls, warnings, output}
    """
    global _VERBOSE_TS
    _VERBOSE_TS = verbose
    sink = sink or EventSink()
    t0 = time.time()
    src = Path(cfg.io.input)
    out_dir = Path(cfg.io.output_dir or src.parent)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-bilingual" if cfg.features.bilingual else ""
    out_path = out_dir / f"{src.stem}{suffix}-Zh.pdf"

    font_path = find_cjk_font(cfg.fonts.get("cjk"))
    glossary = Glossary.load(cfg.glossary_file) if cfg.glossary_file else None
    cache = TranslationCache(out_dir / ".translation_cache.db") \
        if cfg.features.translation_cache else None

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
    from .typography import Typography
    typo = None
    if getattr(cfg.features, "preserve_formatting", True):
        try:
            typo = Typography(cfg.fonts)
            _log(f"typography: body={os.path.basename(typo.body_path)}, "
                 f"heading={os.path.basename(typo.heading_path)}")
        except Exception as e:
            _log(f"typography init failed ({e}); fallback to single-font mode")
            typo = None

    # ---- 逐页：布局 → 裁公式 → 收集段落 ----
    page_layouts: list[dict | None] = []
    page_pixmaps: list[dict[int, bytes]] = []
    pending: list[tuple[int, int]] = []   # (page_no, para_index)
    cell_pending: list[tuple[int, int]] = []   # v0.2.3: (page_no, cell_index)

    for pno in range(len(doc)):
        if control:
            control.checkpoint()
        lay = layout_page(doc[pno])
        pixmaps = crop_formula_pixmaps(doc, pno, lay["formulas"]) \
            if lay["formulas"] else {}
        page_layouts.append(lay)
        page_pixmaps.append(pixmaps)
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
        _log(f"page {pno + 1}/{len(doc)}: {lay['mode']}-col, "
             f"{len(lay['paragraphs'])} paras, {len(lay['formulas'])} formulas, "
             f"{len(lay.get('tables_cells', []))} table cells")
        sink.page_done(pno, n_pages)

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
        all_flat = unit_texts + cell_flat

        tc = TranslationClient(
            client, model=cfg.llm.model,
            temperature=cfg.llm.temperature,
            glossary_prompt=glossary.prompt_block() if glossary else "",
            src_lang=cfg.io.source_lang, tgt_lang=cfg.io.target_lang,
            batch_size=cfg.llm.batch_size,
            max_llm_calls=cfg.llm.max_llm_calls,
            min_call_interval=cfg.llm.min_call_interval,
            max_workers=cfg.llm.max_workers,
            fallback_model=cfg.llm.fallback_model,
            timeout=cfg.llm.timeout,
            max_retries=cfg.llm.max_retries,
            backoff_base=cfg.llm.backoff_base,
            backoff_cap=cfg.llm.backoff_cap,
            retry_delay_cap=cfg.llm.retry_delay_cap,
            sink=sink, control=control,
        )
        if control:
            control.checkpoint()
        sink.stage("translate")
        translated_flat, total_calls = tc.translate_paragraphs(all_flat, cache=cache)
        all_warnings.extend(tc.warnings)

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
        for pno, ci in cell_pending:
            cell_texts[(pno, ci)] = translated_flat[len(merge_groups)
                                                    + cell_pending.index((pno, ci))]

    # ---- 渲染回灌 ----
    if control:
        control.checkpoint()
    sink.stage("render")
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
                    cell_texts=page_cell_texts)

    # 原子写:先 .tmp 再 rename,中途崩溃不留半成品 PDF
    tmp_path = out_path.with_suffix(".pdf.tmp")
    doc.save(str(tmp_path), garbage=4, deflate=True)
    doc.close()
    os.replace(tmp_path, out_path)
    if cache:
        cache.close()

    stats = {
        "pages": n_pages,
        "paragraphs": n_paras,
        "calls": total_calls,
        "warnings": all_warnings,
        "output": str(out_path),
    }
    sink.emit("done", output=str(out_path), pages=n_pages,
              paragraphs=n_paras, calls=total_calls,
              elapsed=round(time.time() - t0, 1))
    _log(f"done: {n_paras} paras, {total_calls} LLM calls, "
         f"{len(all_warnings)} warnings, {time.time() - t0:.1f}s")
    return stats