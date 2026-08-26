"""期刊级排版系统：中文期刊样式映射（字号/加粗/对齐/字体族）。

设计目标（v0.2.2 任务1+5）：
- 标题层级映射：文档大标题居中加粗、一级节标题（I. II.）左顶格加粗、
  二级小标题（A. B.）斜体或加粗、图注/表注小字号居中、参考文献小字号
- 字体族：正文宋体（SimSun，学术印刷标准）、标题黑体（SimHei 或雅黑 Bold）、
  英文层 Times New Roman
- 样式决策基于 layout 层已有的 is_heading/is_caption/is_ref 元数据，
  加上文本模式识别（罗马数字节标题等）
"""
from __future__ import annotations

import os
import re

# 罗马数字节标题: "I. INTRODUCTION" / "IV. FINITE ELEMENT MODELING"
_SEC_TITLE_RE = re.compile(r"^[IVX]+[\.\)]?\s+\S", re.A)
_SUBSEC_RE = re.compile(r"^[A-D][\.\)]\s+\S", re.A)
_ABSTRACT_RE = re.compile(r"^Abstract[:\s]*", re.I)
_INDEX_TERMS_RE = re.compile(r"^Index Terms[:\s]*", re.I)


def _first_existing(paths: list) -> str | None:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


class Typography:
    """字体族加载 + 段落→样式解析。

    用法:
        ty = Typography(cfg_fonts)          # 加载字体族
        style = ty.resolve(para_dict)       # → ParaStyle
        tw, fs, align = ty.draw_prepare(page_rect)
    """

    def __init__(self, fonts_cfg: dict | None = None):
        cfg = fonts_cfg or {}
        # ---- 中文字体族 ----
        self.body_path = _first_existing([
            cfg.get("cjk_body"),
            "/mnt/c/Windows/Fonts/simsun.ttc",
            "/mnt/c/Windows/Fonts/NotoSerifSC-VF.ttf",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
            "/mnt/c/Windows/Fonts/msyh.ttc",
        ]) or ""
        self.heading_path = _first_existing([
            cfg.get("cjk_heading"),
            "/mnt/c/Windows/Fonts/simhei.ttf",
            "/mnt/c/Windows/Fonts/msyhbd.ttc",
            "/mnt/c/Windows/Fonts/Noto Sans SC Bold (TrueType).otf",
        ]) or ""
        # 兜底:标题字体找不到时退回正文字体
        self.heading_path = self.heading_path or self.body_path
        # ---- 英文层字体（双语原文/保留原文段）----
        self.en_body_path = _first_existing([
            "/mnt/c/Windows/Fonts/times.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        ])
        self.en_bold_path = _first_existing([
            "/mnt/c/Windows/Fonts/timesbd.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        ])

        import pymupdf
        self.f_body = pymupdf.Font(fontfile=self.body_path)   # 宋体正文
        self.f_head = pymupdf.Font(fontfile=self.heading_path)  # 黑体标题
        self.f_en = pymupdf.Font(fontfile=self.en_body_path) \
            if self.en_body_path else pymupdf.Font("tiro")     # Times 兜底内置
        self.f_en_bold = pymupdf.Font(fontfile=self.en_bold_path) \
            if self.en_bold_path else self.f_en

    def resolve(self, para: dict, body_size: float | None) -> "ParaStyle":
        """段落元数据 → 排版样式（期刊映射规则）。"""
        txt = (para.get("text") or "").strip()
        size = para.get("size") or body_size or 10.0
        is_h = bool(para.get("is_heading"))

        # 文档大标题:is_heading 且字号显著大于正文(>1.3x)
        if is_h and body_size and size > body_size * 1.3:
            return ParaStyle("title", size=max(size * 1.05, body_size * 1.45),
                             bold=True, center=True)

        # 罗马数字一级节标题: "IV. FINITE ELEMENT MODELING"
        if is_h and _SEC_TITLE_RE.match(txt):
            return ParaStyle("sec_title", size=size * 1.02,
                             bold=True, indent=0)

        # 二级小标题: "A. Fabrication"（粗体主导的小块）
        if is_h and _SUBSEC_RE.match(txt):
            return ParaStyle("subsec_title", size=size * 1.0,
                             bold=True, indent=0)

        # 其余 heading（作者行/单位行被误判 heading 的兜底→按普通段处理）
        if is_h:
            return ParaStyle("head_plain", size=size, bold=False)

        if para.get("is_caption"):
            return ParaStyle("caption", size=min(size, 8.6), center=False)

        if para.get("is_ref"):
            # v0.2.2: 条目起排字号压到 0.9x（原文 bbox 是英文行数高度,
            # 中文译文虽短但最小字号 6.5pt 时 4 行仍可能超出被相邻条目
            # 侵入的盒高;更小的起排字号给试排降字号留出空间）
            return ParaStyle("ref_entry", size=min(size, 8.2) * 0.9, indent=0)

        if _ABSTRACT_RE.match(txt) or _INDEX_TERMS_RE.match(txt):
            return ParaStyle("abstract", size=size * 0.95, indent=0, bold_lead=True)

        return ParaStyle("body", size=size, indent=2)


class ParaStyle:
    """一个段落的完整排版参数。"""

    def __init__(self, kind: str, size: float, bold: bool = False,
                 center: bool = False, indent: int = 0, bold_lead: bool = False):
        self.kind = kind          # title/sec_title/subsec_title/head_plain/
                                  # caption/ref_entry/abstract/body
        self.size = size
        self.bold = bold
        self.center = center
        self.indent = indent              # 首行缩进字符数
        self.bold_lead = bold_lead        # Abstract:/Index Terms: 前导词加粗

    def __repr__(self):
        return f"<{self.kind} {self.size:.1f}pt b={self.bold} c={self.center}>"


def line_height_factor(kind: str) -> float:
    """行距系数：标题紧凑、正文舒展（中文期刊惯例）。"""
    if kind in ("title", "sec_title"):
        return 1.22
    if kind in ("subsec_title", "head_plain"):
        return 1.28
    if kind == "ref_entry":
        return 1.22   # 参考文献条目:盒高被相邻条目侵入,行距收紧防溢出
    return 1.35
