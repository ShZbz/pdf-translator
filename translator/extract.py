"""文字层提取：page → 结构化块列表（BBox/size/flags/font）。

<50 字符的扫描页候选走 OCR 降级（D7），OCR 引擎为可选依赖，
未安装时该页保留空文本并计入警告。
"""
from __future__ import annotations

import pymupdf


def page_has_text_layer(page, min_chars: int = 50) -> bool:
    """D7: 文字层字符数 < min_chars 视为扫描页候选。"""
    return len(page.get_text().strip()) >= min_chars


def get_page_blocks(page) -> list[dict]:
    """提取一页全部文字块。

    返回块列表，每块:
      bbox:   pymupdf.Rect
      text:   块内全文（行内 span 按 x0 排序拼接，行间 \n）
      spans:  [{text, size, flags, font, bbox}]（保持行序）
      lines:  [{bbox, text}]（v0.2.3 新增：行级 bbox/text，供
              公式编号行剥离 / Algorithm caption 带拆分用）
    """
    blocks = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:  # 非文字块（图片）
            continue
        # v0.2.2: 旋转文本块过滤（arXiv 侧边竖排水印 dir=(0,1)），
        # 竖排文字进翻译队列会被当正文横排重灌成乱码（实测 paper3 p1）
        lines_all = b.get("lines", [])
        if lines_all and all(l.get("dir", (1, 0))[0] < 0.9 for l in lines_all):
            continue
        spans = []
        line_texts = []
        line_items = []
        for line in sorted(lines_all, key=lambda l: l["bbox"][1]):
            ls = sorted(line["spans"], key=lambda s: s["bbox"][0])
            for s in ls:
                spans.append({
                    "text": s["text"],
                    "size": s["size"],
                    "flags": s["flags"],
                    "font": s["font"],
                    "bbox": pymupdf.Rect(s["bbox"]),
                })
            lt = "".join(s["text"] for s in ls)
            line_texts.append(lt)
            line_items.append({"bbox": pymupdf.Rect(line["bbox"]), "text": lt})
        if not spans:
            continue
        blocks.append({
            "bbox": pymupdf.Rect(b["bbox"]),
            "text": "\n".join(line_texts),
            "spans": spans,
            "lines": line_items,
        })
    return blocks
