"""v0.4.3 惰性 OCR 接入：扫描页文字提取（paddleocr 可选依赖）。

设计（任务 2-3 落地）：
- OCR 引擎完全惰性：只有文档里检出扫描页且 engine 可用时才 import。
  未安装 paddleocr 时给出明确警告并保持旧行为（扫描页原样保留），
  绝不因缺依赖崩溃。
- 引擎单例按 (engine, lang) 缓存——paddleocr 初始化（模型加载）很重，
  每页重建会把 8 页文档的 OCR 时间放大 8 倍。
- 输出仅取文本行（按 y 排序拼接）。bbox 不用：扫描页的"翻译回贴"
  需要覆盖原图再排版（PDFMathTranslate 式），风险高；v0.4.3 采用
  附录页方案——OCR 译文以附加页插在扫描页之后（见 pipeline._append_ocr_pages），
  原扫描页不动，零排版风险。
- 兼容 paddleocr 2.x（.ocr() 返回 [box, (text, score)] 列表）与
  3.x（.predict() 返回含 rec_texts 的 dict）两种结果格式。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pymupdf

# 源语言 → paddleocr lang 代码（paddle 支持集有限，缺省 en）
_PADDLE_LANG = {
    "zh": "ch", "en": "en", "ja": "japan", "ko": "korean",
    "ru": "ru", "de": "german", "fr": "french", "es": "spain",
    "it": "it", "pt": "pt", "tr": "turkish", "vi": "vietnam",
}

_ENGINES: dict[tuple[str, str], object] = {}


def engine_available(engine: str) -> bool:
    """OCR 引擎是否已安装（惰性探测，异常一律视为不可用）。"""
    if engine in ("", "none"):
        return False
    if engine != "paddle":
        return False
    try:
        import paddleocr  # noqa: F401
        return True
    except Exception:
        return False


def _get_engine(engine: str, lang: str):
    key = (engine, lang)
    if key in _ENGINES:
        return _ENGINES[key]
    if engine == "paddle":
        from paddleocr import PaddleOCR
        # v3.x 移除了 use_angle_cls 等旧参数，只传 lang 保持两版兼容
        inst = PaddleOCR(lang=lang)
    else:
        raise ValueError(f"unknown OCR engine: {engine!r}")
    _ENGINES[key] = inst
    return inst


def _extract_texts(result) -> list[str]:
    """从 paddleocr 结果中提取文本行（兼容 2.x/3.x 格式，防御式遍历）。"""
    return [t for _, t in _extract_lines(result)]


def _extract_lines(result) -> list[tuple["pymupdf.Rect", str]]:
    """v0.5.0: 提取 (bbox, text) 行列表（原位回贴模式用）。

    兼容两种结果格式：
    - paddleocr 3.x predict(): dict 含 rec_texts + rec_boxes/rec_polys
    - paddleocr 2.x .ocr(): [box, (text, score)] 列表，box 是四点 polygon

    坐标是像素坐标（OCR 渲染 dpi），调用方负责按 dpi/72 缩放回 PDF 点。
    """
    import numpy as _np  # paddleocr 依赖链自带；仅 3.x 分支触达
    import pymupdf

    out: list[tuple[pymupdf.Rect, str]] = []

    def _rect_from_pts(pts) -> pymupdf.Rect:
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        return pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))

    def _scan(r):
        if isinstance(r, dict):
            texts = r.get("rec_texts")
            if texts:
                boxes = r.get("rec_boxes") or r.get("rec_polys")
                for i, t in enumerate(texts or []):
                    if not t or boxes is None or i >= len(boxes):
                        continue
                    b = _np.asarray(boxes[i])
                    pts = b.reshape(-1, 2)[:4] if b.size >= 8 else None
                    if pts is None:
                        continue
                    out.append((_rect_from_pts(pts), str(t)))
                return
            for v in r.values():
                _scan(v)
        elif isinstance(r, (list, tuple)):
            # 2.x 条目形如 [box, (text, score)]
            if (len(r) == 2 and isinstance(r[1], (list, tuple))
                    and len(r[1]) == 2 and isinstance(r[1][0], str)):
                try:
                    out.append((_rect_from_pts(r[0]), r[1][0]))
                except Exception:
                    pass
                return
            for v in r:
                _scan(v)

    _scan(result)
    return out


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


def ocr_page_lines(page, engine: str = "paddle", src_lang: str = "en",
                   dpi: int = 200) -> list[tuple["pymupdf.Rect", str]] | None:
    """v0.5.0: OCR 单页，返回 [(bbox, text)]（bbox 已换算回 PDF 点坐标）。

    失败/无识别结果返回 None。坐标换算：OCR 在 dpi 渲染图上出像素
    坐标，× 72/dpi 回到页面点。
    """
    try:
        lang = _PADDLE_LANG.get((src_lang or "en").strip().lower(), "en")
        inst = _get_engine(engine, lang)
        pix = page.get_pixmap(dpi=dpi)
        tmp = Path(tempfile.mkstemp(suffix=".png")[1])
        try:
            tmp.write_bytes(pix.tobytes("png"))
            try:
                result = inst.predict(str(tmp))     # paddleocr 3.x
            except AttributeError:
                result = inst.ocr(str(tmp))         # paddleocr 2.x
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        lines = _extract_lines(result)
        if not lines:
            return None
        k = 72.0 / dpi
        page_rect = page.rect
        scaled = []
        for r, t in lines:
            rr = pymupdf.Rect(r.x0 * k, r.y0 * k, r.x1 * k, r.y1 * k)
            rr = rr & page_rect if page_rect.width else rr   # 夹回页面
            if not rr.is_empty and t.strip():
                scaled.append((rr, t))
        return scaled or None
    except Exception:
        return None
