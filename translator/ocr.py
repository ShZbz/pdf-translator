"""v0.4.3 惰性 OCR 接入：扫描页文字提取（paddleocr 可选依赖）。

设计（任务 2-3 落地）：
- OCR 引擎完全惰性：只有文档里检出扫描页且 engine 可用时才 import。
  未安装时给出明确警告并保持旧行为（扫描页原样保留），
  绝不因缺依赖崩溃。
- 引擎单例按 (engine, lang) 缓存——paddleocr 初始化（模型加载）很重，
  每页重建会把 8 页文档的 OCR 时间放大 8 倍。
- 兼容 paddleocr 2.x（.ocr() 返回 [box, (text, score)] 列表）与
  3.x（.predict() 返回含 rec_texts 的 dict）两种结果格式。

v0.7.0:
- 多引擎注册：paddle / rapidocr（rapidocr_onnxruntime）/ tesseract
  （pytesseract），全部可选依赖、按需探测
- 多引擎投票（ocr_page_lines_voted）：同行多引擎结果按 IoU 对齐，
  一致取一致、冲突取置信度最高并标冲突警告——识别错误不再单点失败
- 几何版面分割（region_blocks_geometry）：只用行 bbox 做 x 投影分栏 +
  y 间隙分段，完全不依赖识别文本——OCR 认错字不影响版面
- 影子页 GNN 区域（gnn_regions_for_lines）：OCR 行 bbox 写成隐形文字层
  再喂 pymupdf-layout 的 BoxRFDGNN，产出语义区域（text/table/picture/
  formula/page-header…）。pymupdf-layout 的 GNN 输入节点是文字 span，
  纯位图页直接预测返回 0 区域（实测）——影子页是让它服务扫描件的正解
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pymupdf

# 源语言 → 引擎 lang 代码（各家支持集不同，缺省 en）
_PADDLE_LANG = {
    "zh": "ch", "en": "en", "ja": "japan", "ko": "korean",
    "ru": "ru", "de": "german", "fr": "french", "es": "spain",
    "it": "it", "pt": "pt", "tr": "turkish", "vi": "vietnam",
}
_TESSERACT_LANG = {
    "zh": "chi_sim", "en": "eng", "ja": "jpn", "ko": "kor",
    "ru": "rus", "de": "deu", "fr": "fra", "es": "spa",
    "it": "ita", "pt": "por", "tr": "tur", "vi": "vie",
    "ar": "ara", "he": "heb", "hi": "hin",
}
# rapidocr 官方模型以中英为主（ch 模型含英文识别），其余语言不可靠
_RAPID_LANG_OK = {"zh", "en"}

# 引擎注册表（display 顺序即投票优先说明用）
ENGINES = ("paddle", "rapidocr", "tesseract")

_ENGINES: dict[tuple[str, str], object] = {}


def engine_available(engine: str) -> bool:
    """OCR 引擎是否已安装（惰性探测，异常一律视为不可用）。"""
    if engine in ("", "none"):
        return False
    if engine == "paddle":
        try:
            import paddleocr  # noqa: F401
            return True
        except Exception:
            return False
    if engine == "rapidocr":
        try:
            import rapidocr_onnxruntime  # noqa: F401
            return True
        except Exception:
            return False
    if engine == "tesseract":
        try:
            import pytesseract  # noqa: F401
            pytesseract.get_tesseract_version()
            return True
        except Exception:
            return False
    return False


def _get_engine(engine: str, lang: str):
    key = (engine, lang)
    if key in _ENGINES:
        return _ENGINES[key]
    if engine == "paddle":
        from paddleocr import PaddleOCR
        # v3.x 移除了 use_angle_cls 等旧参数，只传 lang 保持两版兼容
        inst = PaddleOCR(lang=lang)
    elif engine == "rapidocr":
        from rapidocr_onnxruntime import RapidOCR
        inst = RapidOCR()
    elif engine == "tesseract":
        import pytesseract
        inst = pytesseract
    else:
        raise ValueError(f"unknown OCR engine: {engine!r}")
    _ENGINES[key] = inst
    return inst


def _render_png(page, dpi: int) -> Path:
    """页面渲染临时 PNG（各引擎统一走文件路径输入——版本兼容最大公约数）。

    v0.8.4 修复：mkstemp 返回的 fd 必须即刻关闭——旧版只取文件名，
    泄漏的打开句柄在 Windows 上令 finally 里的 unlink 报 WinError 32
    （被 OSError 吞掉），每 OCR 一页×每引擎永久泄漏一个 %TEMP% PNG。
    """
    fd, name = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    tmp = Path(name)
    tmp.write_bytes(page.get_pixmap(dpi=dpi).tobytes("png"))
    return tmp


def _paddle_lines(page, src_lang: str, dpi: int):
    lang = _PADDLE_LANG.get((src_lang or "en").strip().lower(), "en")
    inst = _get_engine("paddle", lang)
    tmp = _render_png(page, dpi)
    try:
        try:
            result = inst.predict(str(tmp))     # paddleocr 3.x
        except AttributeError:
            result = inst.ocr(str(tmp))         # paddleocr 2.x
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return _extract_paddle(result)


def _extract_paddle(result) -> list:
    """paddle 2.x/3.x 结果 → [(Rect, text, score)]。"""
    out: list[tuple[pymupdf.Rect, str, float]] = []

    def _rect_from_pts(pts) -> pymupdf.Rect:
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        return pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))

    def _scan(r):
        if isinstance(r, dict):
            # numpy 只在 paddleocr 3.x 的 dict 分支需要（依赖链自带）——
            # 2.x 列表分支不触达；旧版函数级 import 令无 numpy 环境连
            # 2.x 解析都 ImportError（v0.8.4 修复）
            import numpy as _np
            texts = r.get("rec_texts")
            if texts:
                boxes = r.get("rec_boxes") or r.get("rec_polys")
                scores = r.get("rec_scores") or []
                for i, t in enumerate(texts or []):
                    if not t or boxes is None or i >= len(boxes):
                        continue
                    b = _np.asarray(boxes[i])
                    pts = b.reshape(-1, 2)[:4] if b.size >= 8 else None
                    if pts is None:
                        continue
                    sc = float(scores[i]) if i < len(scores) else 1.0
                    out.append((_rect_from_pts(pts), str(t), sc))
                return
            for v in r.values():
                _scan(v)
        elif isinstance(r, (list, tuple)):
            # 2.x 条目形如 [box, (text, score)]
            if (len(r) == 2 and isinstance(r[1], (list, tuple))
                    and len(r[1]) == 2 and isinstance(r[1][0], str)):
                try:
                    out.append((_rect_from_pts(r[0]), r[1][0],
                                float(r[1][1])))
                except Exception:
                    pass
                return
            for v in r:
                _scan(v)

    _scan(result)
    return out


def _rapidocr_lines(page, src_lang: str, dpi: int):
    from rapidocr_onnxruntime import RapidOCR
    inst = _get_engine("rapidocr", src_lang)
    tmp = _render_png(page, dpi)
    try:
        result, _elapse = inst(str(tmp))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    out: list[tuple[pymupdf.Rect, str, float]] = []
    if not result:
        return out
    for item in result:
        try:
            box, text, score = item[0], str(item[1]), float(item[2])
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            out.append((pymupdf.Rect(min(xs), min(ys), max(xs), max(ys)),
                        text, score))
        except Exception:
            continue
    return out


def _tesseract_lines(page, src_lang: str, dpi: int):
    import pytesseract
    from pytesseract import Output
    lang = _TESSERACT_LANG.get((src_lang or "en").strip().lower(), "eng")
    inst = _get_engine("tesseract", lang)
    tmp = _render_png(page, dpi)
    try:
        data = inst.image_to_data(str(tmp), lang=lang,
                                  output_type=Output.DICT)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    # 词级结果按 (block, par, line) 聚成行；conf 是 0-100 量纲
    rows: dict[tuple, list] = {}
    n = len(data.get("text", []))
    for i in range(n):
        txt = (data["text"][i] or "").strip()
        conf = float(data["conf"][i]) if str(data["conf"][i]).lstrip("-").isdigit() else -1.0
        if not txt or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        r = pymupdf.Rect(data["left"][i], data["top"][i],
                         data["left"][i] + data["width"][i],
                         data["top"][i] + data["height"][i])
        row = rows.setdefault(key, {"rect": pymupdf.Rect(r), "words": [],
                                    "confs": []})
        row["rect"] |= r
        row["words"].append((data["left"][i], txt))
        row["confs"].append(conf)
    out = []
    for row in rows.values():
        text = " ".join(t for _, t in sorted(row["words"]))
        if text.strip():
            out.append((row["rect"], text, sum(row["confs"]) / len(row["confs"]) / 100.0))
    return out


def ocr_page_lines(page, engine: str = "paddle", src_lang: str = "en",
                   dpi: int = 200) -> list[tuple["pymupdf.Rect", str]] | None:
    """OCR 单页，返回 [(bbox, text)]（bbox 已换算回 PDF 点坐标）。

    失败/无识别结果返回 None。坐标换算：OCR 在 dpi 渲染图上出像素
    坐标，× 72/dpi 回到页面点。
    """
    triples = ocr_page_lines_scored(page, engine=engine, src_lang=src_lang,
                                    dpi=dpi)
    if triples is None:
        return None
    return [(r, t) for r, t, _s in triples] or None


def ocr_page_lines_scored(page, engine: str = "paddle", src_lang: str = "en",
                          dpi: int = 200) \
        -> "list[tuple[pymupdf.Rect, str, float]] | None":
    """v0.7.0: OCR 单页带置信度 [(bbox, text, score)]（0-1 量纲归一）。

    引擎不支持置信度时按 1.0 记。坐标换算同 ocr_page_lines。
    """
    try:
        if not engine_available(engine):
            return None
        if engine == "paddle":
            raw = _paddle_lines(page, src_lang, dpi)
        elif engine == "rapidocr":
            if (src_lang or "en").strip().lower() not in _RAPID_LANG_OK:
                return None      # rapidocr 只有中英模型，其他语言不掺和
            raw = _rapidocr_lines(page, src_lang, dpi)
        elif engine == "tesseract":
            raw = _tesseract_lines(page, src_lang, dpi)
        else:
            return None
        if not raw:
            return None
        k = 72.0 / dpi
        page_rect = page.rect
        scaled = []
        for r, t, s in raw:
            rr = pymupdf.Rect(r.x0 * k, r.y0 * k, r.x1 * k, r.y1 * k)
            rr = rr & page_rect if page_rect.width else rr
            if not rr.is_empty and t.strip():
                scaled.append((rr, t.strip(), max(0.0, min(1.0, s))))
        return scaled or None
    except Exception:
        return None


def _iou(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    if not a.intersects(b):
        return 0.0
    inter = pymupdf.Rect(a)
    inter.intersect(b)
    union = a.get_area() + b.get_area() - inter.get_area()
    return inter.get_area() / union if union > 0 else 0.0


def _norm_text(s: str) -> str:
    import re as _re
    return _re.sub(r"\s+", "", s).lower()


def ocr_page_lines_voted(page, engines: "list[str] | None" = None,
                         src_lang: str = "en", dpi: int = 200,
                         warnings: "list[str] | None" = None):
    """v0.7.0 多引擎投票：返回 ([(bbox, text)], n_conflicts)。

    行对齐：以行数最多的引擎为基准，其余引擎按 IoU>0.5 匹配；
    一致（归一化后相等）→ 直接取；冲突 → 置信度最高者胜 + 冲突计数。
    冲突行交给调用方告警（"建议人工核对"），文本选错的风险被显式暴露
    而不是静默吃下。单引擎可用时退化为该引擎结果（零冲突）。
    """
    engines = [e for e in (engines or ["paddle"]) if e]
    results: dict[str, list] = {}
    for e in engines:
        lines = ocr_page_lines_scored(page, engine=e, src_lang=src_lang,
                                      dpi=dpi)
        if lines:
            results[e] = lines
    if not results:
        return None, 0
    if len(results) == 1:
        lines = next(iter(results.values()))
        return [(r, t) for r, t, _ in lines], 0

    # 基准 = 行数最多（大行号引擎一般分割更稳）
    base_name = max(results, key=lambda e: len(results[e]))
    base = results[base_name]
    others = {e: list(v) for e, v in results.items() if e != base_name}
    voted: list[tuple[pymupdf.Rect, str]] = []
    conflicts = 0
    for r, t, s in base:
        cands = [(t, s)]
        for e, lines in others.items():
            best_iou, best_j = 0.0, -1
            for j, (r2, t2, s2) in enumerate(lines):
                iou = _iou(r, r2)
                if iou > best_iou:
                    best_iou, best_j = iou, j
            if best_iou > 0.5 and best_j >= 0:
                r2, t2, s2 = lines.pop(best_j)
                cands.append((t2, s2))
        texts = {_norm_text(c[0]) for c in cands}
        if len(texts) > 1:
            conflicts += 1
        # 置信度最高者胜；平票取基准引擎（排序稳定）
        cands.sort(key=lambda c: -c[1])
        voted.append((r, cands[0][0]))
    return voted, conflicts


# ---- v0.7.0 版面自监督重建：区域几何（不依赖识别文本）----

def region_blocks_geometry(lines: list[tuple["pymupdf.Rect", str]],
                           page_rect) -> list[tuple["pymupdf.Rect", str]]:
    """几何版面分割：x 投影分栏 → 栏内 y 间隙分段 → 区域块列表。

    只用行 bbox——OCR 识别文本完全不参与（认错字不影响版面，只影响
    原文层保真，这正是版面自监督重建的分工边界）。
    返回 [(区域 bbox, "\n".join(行文本))]，阅读序（栏左→右、y 上→下）。
    """
    if not lines:
        return []
    pw = page_rect.width or 1.0
    heights = sorted(r.height for r, _ in lines)
    med_h = heights[len(heights) // 2] or 10.0

    # ① x 投影分栏：行覆盖区间直方图，中带 [8%,92%] 内零覆盖带 ≥6% 页宽即切栏
    cover = [0] * int(pw)
    for r, _ in lines:
        for x in range(max(0, int(r.x0)), min(int(pw), int(r.x1) + 1)):
            cover[x] += 1
    lo, hi = int(0.08 * pw), int(0.92 * pw)
    gap_w = max(6.0, 0.06 * pw)
    cols: list[tuple[float, float]] = []
    run_start = None
    for x in range(lo, hi + 1):
        if cover[x] == 0:
            if run_start is None:
                run_start = x
        else:
            if run_start is not None and x - run_start >= gap_w:
                cols.append((run_start, x))
            run_start = None
    # 切点 = 各零覆盖带中心 → 栏区间（首栏从 0 起，末栏到页宽）
    centers = [(g[0] + g[1]) / 2 for g in cols]
    bounds = [0.0] + centers + [pw] if centers else [0.0, pw]
    col_ranges = [(bounds[i], bounds[i + 1])
                  for i in range(len(bounds) - 1)]

    out: list[tuple[pymupdf.Rect, str]] = []
    for cx0, cx1 in col_ranges:
        col_lines = [(r, t) for r, t in lines
                     if cx0 <= (r.x0 + r.x1) / 2 < cx1]
        if not col_lines:
            continue
        col_lines.sort(key=lambda it: (it[0].y0, it[0].x0))
        # ② y 间隙分段：v_gap ≥ 1.8×行高 或 x 不重叠（同段纵向延续）
        #   → 开新区块
        blocks: list[dict] = []
        for r, t in col_lines:
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
        out.extend((b["bbox"], "\n".join(b["texts"])) for b in blocks)
    return out


def gnn_regions_for_lines(page, lines: "list[tuple[pymupdf.Rect, str]]") \
        -> "list[tuple[pymupdf.Rect, str]] | None":
    """影子页 GNN：OCR 行 bbox 写成隐形文字层 → BoxRFDGNN 语义区域。

    返回 [(region bbox, kind)]（kind ∈ text/section-header/title/list-item/
    caption/table/picture/formula/page-header/page-footer…），未装
    pymupdf.layout 或推理失败返回 None（调用方回退几何分割）。
    """
    if not lines:
        return None
    try:
        from pymupdf.layout import DocumentLayoutAnalyzer
    except Exception:
        return None
    shadow = pymupdf.open()
    try:
        p = shadow.new_page(width=page.rect.width, height=page.rect.height)
        font = pymupdf.Font("helv")
        tw = pymupdf.TextWriter(p.rect)
        for r, t in lines:
            fs = max(5.0, min(14.0, r.height * 0.8))
            y = min(r.y1 - 1.0, (r.y0 + r.y1) / 2 + fs * 0.3)
            tw.append(pymupdf.Point(r.x0, y), t[:200], font=font,
                      fontsize=fs)
        tw.write_text(p, render_mode=3)   # 隐形文字（只供 GNN 取 bbox/文本特征）
        model = DocumentLayoutAnalyzer.get_model()
        items = model.predict(p)
        out: list[tuple[pymupdf.Rect, str]] = []
        for it in items or []:
            try:
                if isinstance(it, (list, tuple)) and len(it) >= 5 \
                        and not isinstance(it[-1], (int, float)):
                    out.append((pymupdf.Rect(it[0], it[1], it[2], it[3]),
                                str(it[-1])))
            except Exception:
                continue
        return out or None
    except Exception:
        return None
    finally:
        try:
            shadow.close()
        except Exception:
            pass


def ocr_page_text(page, engine: str = "paddle", src_lang: str = "en",
                  dpi: int = 200) -> str | None:
    """OCR 单页，返回拼接文本（按行序）；失败返回 None。

    走临时 PNG 文件而非 ndarray——paddleocr 各版本对 ndarray/路径
    的接受度不一，路径输入是最大公约数。
    """
    lines = ocr_page_lines(page, engine=engine, src_lang=src_lang, dpi=dpi)
    if lines is None:
        return None
    return "\n".join(t.strip() for _, t in lines if t.strip()) or None
