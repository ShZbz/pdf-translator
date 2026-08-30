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

import re

# 罗马数字节标题: "I. INTRODUCTION" / "IV. FINITE ELEMENT MODELING"
_SEC_TITLE_RE = re.compile(r"^[IVX]+[\.\)]?\s+\S", re.A)
_SUBSEC_RE = re.compile(r"^[A-D][\.\)]\s+\S", re.A)
_ABSTRACT_RE = re.compile(r"^Abstract[:\s]*", re.I)
_INDEX_TERMS_RE = re.compile(r"^Index Terms[:\s]*", re.I)


def max_span_size(para: dict) -> float:
    """块内最大 span 字号。small caps 标题（'I.' 9.96pt + 'NTRODUCTION'
    7.97pt）的主字号按字符数加权会取到 small caps 的小字号（v0.5.1
    实测：'I. 引言' 以 7.97pt 起排被压到 7.4pt，比正文 10pt 还小）——
    标题类样式的字号基准必须用最大 span。"""
    sizes = [s.get("size") for s in (para.get("spans") or [])
             if s.get("size")]
    return max(sizes) if sizes else 0.0


def _looks_like_sec_title(txt: str) -> bool:
    """罗马数字 + 全大写短行的文本形态兜底（layout.is_heading 对
    small caps 节标题检测不到：非粗体且字号不大于正文）。"""
    if len(txt) > 64 or not _SEC_TITLE_RE.match(txt):
        return False
    letters = [c for c in txt if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) >= 0.8


class Typography:
    """字体族加载 + 段落→样式解析。

    用法:
        ty = Typography(cfg_fonts, lang="zh")  # 加载目标语言字体族
        style = ty.resolve(para_dict)          # → ParaStyle
        tw, fs, align = ty.draw_prepare(page_rect)

    v0.4.2: 字体候选链改为三平台（Windows 原生 / WSL / Linux·macOS）+
    按目标语言解析（langs.py 注册表）。旧版 WSL-only 路径在原生 Windows
    上全部落空 → 静默退到 pymupdf 内置 Noto Serif（无 CJK 字形，中文
    全豆腐块）——本次修复的根因之一。
    """

    def __init__(self, fonts_cfg: dict | None = None, lang: str = "zh"):
        from . import langs
        from .langs import is_cjk_script
        cfg = fonts_cfg or {}
        self.lang = (lang or "zh").strip().lower()
        self.cjk = is_cjk_script(self.lang)
        # ---- 目标语言字体族（跨平台候选链 + 显式覆盖）----
        body_path, heading_path = langs.resolve_output_fonts(self.lang, cfg)
        self.body_path = body_path or ""
        self.heading_path = heading_path or self.body_path
        # ---- 原文层字体（双语对照/保留原文段，Times 系含西里尔）----
        self.en_body_path = cfg.get("en") or langs.resolve_original_font()
        self.en_bold_path = self.en_body_path
        # v0.8.2: Font 对象懒加载——实测 CJK 字体（simsun.ttc 18MB）每个
        # ~1s，而 htmlbox 主路径只用路径串（Archive/Story 自行加载）、
        # Font 对象仅字形覆盖率校验与 writer 路径需要；旧版构造即加载
        # 三个 Font，每文档白付 ~2s
        self._fonts: dict = {}

    def _font(self, key: str, path: str, fallback: str = ""):
        if key not in self._fonts:
            import pymupdf
            try:
                self._fonts[key] = pymupdf.Font(fontfile=path) if path \
                    else pymupdf.Font(fallback)
            except Exception:
                self._fonts[key] = pymupdf.Font(fallback)
        return self._fonts[key]

    @property
    def f_body(self):
        return self._font("body", self.body_path)

    @property
    def f_head(self):
        return self._font("head", self.heading_path)

    @property
    def f_en(self):
        return self._font("en", self.en_body_path, fallback="tiro")

    @property
    def f_en_bold(self):
        return self.f_en

    def resolve(self, para: dict, body_size: float | None) -> "ParaStyle":
        """段落元数据 → 排版样式（期刊映射规则）。"""
        txt = (para.get("text") or "").strip()
        size = para.get("size") or body_size or 10.0
        is_h = bool(para.get("is_heading"))
        # v0.6.0: 标题可用字号上限（small caps 的主字号在最大 span 上）
        size_hi = max(size, max_span_size(para))

        # 文档大标题:is_heading 且字号显著大于正文(>1.3x)
        if is_h and body_size and size_hi > body_size * 1.3:
            return ParaStyle("title", size=max(size_hi * 1.05, body_size * 1.45),
                             bold=True, center=True)

        # 罗马数字一级节标题: "IV. FINITE ELEMENT MODELING"
        # v0.6.0: is_heading 检测不到的 small caps 节标题（非粗体、字号
        # 不大于正文）按文本形态兜底；字号基准取最大 span 且不低于正文
        # ——v0.5.1 实测该类标题以 7.4pt 渲染比正文小的根因修复
        if (is_h or _looks_like_sec_title(txt)) and _SEC_TITLE_RE.match(txt):
            return ParaStyle("sec_title",
                             size=max(size_hi, body_size or size_hi) * 1.02,
                             bold=True, indent=0)

        # 二级小标题: "A. Fabrication"（粗体主导的小块）
        if is_h and _SUBSEC_RE.match(txt):
            return ParaStyle("subsec_title", size=max(size_hi, size) * 1.0,
                             bold=True, indent=0)

        # Abstract/Index Terms 前缀优先于通用 heading 兜底——整块粗体
        # 的摘要被 is_heading 误收时（v0.6.0 前实测），前缀样式才是对的
        if _ABSTRACT_RE.match(txt) or _INDEX_TERMS_RE.match(txt):
            return ParaStyle("abstract", size=size * 0.95, indent=0, bold_lead=True)

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

        # v0.4.2: 首行缩进 2 字符仅 CJK 目标语言（中文期刊惯例），
        # 西文/西里尔按学术惯例顶格
        return ParaStyle("body", size=size, indent=2 if self.cjk else 0)


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
