"""v0.4.2 多语言注册表：语言元数据 + 跨平台字体解析。

设计：
- LANGUAGES: 目标语言 code → 元数据（LLM 提示用名 / UI 显示名 / 文字系统 /
  覆盖率校验样本 / body·heading 字体候选链，按优先级）
- 字体解析三平台通吃：原生 Windows（%WINDIR%/Fonts）/ WSL（/mnt/c/...）/
  Linux·macOS（/usr/share/fonts 等递归索引）。旧的 WSL-only 路径是原生
  Windows 上"no CJK font found"整体崩溃的根因（v0.4.1 实测）。
- 输出文件名后缀：-{Tag}.pdf（zh→Zh 保持旧版兼容）。

不支持 RTL（阿拉伯语/希伯来语）与需要复杂整形的天城文：TextWriter 逐字
排印无法做双向/shaping，输出会不可读——注册表不收录，避免用户踩坑。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

FONT_EXTS = {".ttf", ".ttc", ".otf", ".otc"}


@dataclass(frozen=True)
class LangInfo:
    code: str
    name: str                 # LLM 提示词用英文全名
    native: str               # UI 显示名
    script: str               # "cjk" | "latin" | "cyrillic"
    sample: str               # 字形覆盖率校验样本（含该语言特有字符）
    body: tuple[str, ...] = ()     # 正文字体候选文件名（按优先级）
    heading: tuple[str, ...] = ()  # 标题字体候选


LANGUAGES: dict[str, LangInfo] = {
    "zh": LangInfo("zh", "Simplified Chinese", "中文（简体）", "cjk", "汉字宋体黑",
                   ("simsun.ttc", "NotoSerifSC-VF.ttf", "SourceHanSansCN-Normal.ttf",
                    "NotoSerifCJK-Regular.ttc", "SourceHanSerifSC-Regular.otf",
                    "msyh.ttc", "NotoSansCJK-Regular.ttc"),
                   ("simhei.ttf", "msyhbd.ttc", "Noto Sans SC Bold (TrueType).otf",
                    "SourceHanSansSC-Bold.otf", "NotoSansCJK-Bold.ttc",
                    "NotoSansSC-VF.ttf")),
    "en": LangInfo("en", "English", "English", "latin", "Aa Qq",
                   _LATIN_BODY := ("times.ttf", "Times New Roman.ttf",
                                   "DejaVuSerif.ttf", "FreeSerif.ttf",
                                   "NotoSerif-Regular.ttf", "LiberationSerif-Regular.ttf"),
                   _LATIN_HEADING := ("timesbd.ttf", "Times New Roman Bold.ttf",
                                      "DejaVuSerif-Bold.ttf", "FreeSerifBold.ttf",
                                      "NotoSerif-Bold.ttf",
                                      "LiberationSerif-Bold.ttf")),
    "ja": LangInfo("ja", "Japanese", "日本語", "cjk", "あいう漢字",
                   ("msgothic.ttc", "YuGothM.ttc", "meiryo.ttc",
                    "NotoSansCJK-Regular.ttc", "ipag.ttf", "NotoSansJP-Regular.otf"),
                   ("YuGothB.ttc", "meiryob.ttc", "NotoSansCJK-Bold.ttc",
                    "msgothic.ttc", "NotoSansJP-Bold.otf")),
    "ko": LangInfo("ko", "Korean", "한국어", "cjk", "한국어 가나다",
                   ("malgun.ttf", "NanumGothic.ttf", "NotoSansCJK-Regular.ttc",
                    "NotoSansKR-Regular.otf", "Dotum.ttf"),
                   ("malgunbd.ttf", "NanumGothicBold.ttf", "NotoSansCJK-Bold.ttc",
                    "NotoSansKR-Bold.otf", "malgun.ttf")),
    "de": LangInfo("de", "German", "Deutsch", "latin", "äßüö Aa",
                   _LATIN_BODY, _LATIN_HEADING),
    "fr": LangInfo("fr", "French", "Français", "latin", "éàùçœ Aa",
                   _LATIN_BODY, _LATIN_HEADING),
    "es": LangInfo("es", "Spanish", "Español", "latin", "ñ¿¡ Aa",
                   _LATIN_BODY, _LATIN_HEADING),
    "it": LangInfo("it", "Italian", "Italiano", "latin", "àèì Aa",
                   _LATIN_BODY, _LATIN_HEADING),
    "pt": LangInfo("pt", "Portuguese", "Português", "latin", "ãõç Aa",
                   _LATIN_BODY, _LATIN_HEADING),
    "ru": LangInfo("ru", "Russian", "Русский", "cyrillic", "Яжф Аа",
                   _LATIN_BODY, _LATIN_HEADING),
    "tr": LangInfo("tr", "Turkish", "Türkçe", "latin", "ığşİ Aa",
                   _LATIN_BODY, _LATIN_HEADING),
    "vi": LangInfo("vi", "Vietnamese", "Tiếng Việt", "latin", "ầơn Aa",
                   _LATIN_BODY, _LATIN_HEADING),
}

# ---- 字体目录解析（平台无关，存在性过滤）----


def font_dirs() -> list[Path]:
    """按平台给出候选字体目录（仅保留存在的，保序去重）。"""
    raw: list[str] = []
    win = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if win:
        raw.append(str(Path(win) / "Fonts"))
    raw += [
        "C:/Windows/Fonts",                    # 原生 Windows 兜底
        "/mnt/c/Windows/Fonts",                # WSL 挂载的 Windows 字体
        "/usr/share/fonts", "/usr/local/share/fonts",
        str(Path.home() / ".fonts"),
        str(Path.home() / ".local/share/fonts"),
        "/Library/Fonts", "/System/Library/Fonts",   # macOS
        str(Path.home() / "Library/Fonts"),
    ]
    seen, out = set(), []
    for r in raw:
        p = Path(r)
        key = str(p).lower()
        if key not in seen and p.is_dir():
            seen.add(key)
            out.append(p)
    return out


_INDEX_CACHE: dict[str, dict[str, str]] = {}


def _index_dir(d: Path) -> dict[str, str]:
    """目录（含子目录，Linux fonts 树有 opentype/noto 等层级）→
    {小写文件名: 完整路径}。模块级缓存，每目录只扫一次。"""
    key = str(d)
    if key in _INDEX_CACHE:
        return _INDEX_CACHE[key]
    idx: dict[str, str] = {}
    try:
        for f in d.rglob("*"):
            try:
                if f.is_file() and f.suffix.lower() in FONT_EXTS:
                    idx.setdefault(f.name.lower(), str(f))
            except OSError:
                continue
    except OSError:
        pass
    _INDEX_CACHE[key] = idx
    return idx


def _lookup_font(names: tuple[str, ...], explicit: str | None = None) -> str | None:
    """候选文件名链 + 显式路径 → 第一个存在的字体文件路径。"""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
    dirs = font_dirs()
    for n in names:
        for d in dirs:
            p = d / n
            if p.is_file():
                return str(p)
    for n in names:                 # 目录树递归索引兜底（Linux/macOS）
        nl = n.lower()
        for d in dirs:
            hit = _index_dir(d).get(nl)
            if hit:
                return hit
    return None


def lang_info(code: str) -> LangInfo:
    """未知 code 回退 zh（旧版一切语言都用 CJK 字体的行为），但 UI/配置
    正常时不会走到回退分支。"""
    return LANGUAGES.get((code or "").strip().lower(), LANGUAGES["zh"])


def is_cjk_script(code: str) -> bool:
    return lang_info(code).script == "cjk"


def prompt_lang_name(code: str) -> str:
    info = LANGUAGES.get((code or "").strip().lower())
    return info.name if info else (code or "English")


def output_tag(code: str) -> str:
    """输出文件名语言标记：zh → 'Zh'（与旧版 -Zh.pdf 兼容），de → 'De'。"""
    c = (code or "zh").strip() or "zh"
    return c[:2].capitalize() if c == "zh" else c.capitalize()


def resolve_output_fonts(target_lang: str,
                         fonts_cfg: dict | None = None) -> tuple[str | None, str | None]:
    """目标语言 → (正文字体路径, 标题字体路径)。找不到返回 (None, None)，
    调用方决定降级策略（CJK 缺字体应报错，西文可退内置衬线）。

    显式覆盖优先级：fonts.body / fonts.heading（新中性名）>
    fonts.cjk_body / fonts.cjk_heading（v0.2.2 旧名）> fonts.cjk（总开关旧名）。
    """
    cfg = fonts_cfg or {}
    info = lang_info(target_lang)
    body_exp = cfg.get("body") or cfg.get("cjk_body") or cfg.get("cjk") or None
    head_exp = cfg.get("heading") or cfg.get("cjk_heading") or None
    body = _lookup_font(info.body, body_exp)
    heading = _lookup_font(info.heading, head_exp) or body
    return body, heading


def resolve_original_font() -> str | None:
    """双语对照原文层衬线字体（Times 系，含西里尔扩展）。"""
    return _lookup_font(("times.ttf", "Times New Roman.ttf", "DejaVuSerif.ttf",
                         "FreeSerif.ttf", "NotoSerif-Regular.ttf"))


def coverage_warnings(font, code: str) -> list[str]:
    """校验字体对目标语言样本字符的字形覆盖（gid 0 = 缺字形 →豆腐块）。
    font 为 pymupdf.Font。"""
    info = lang_info(code)
    missing = [ch for ch in info.sample
               if not ch.isspace() and font.has_glyph(ord(ch)) == 0]
    if not missing:
        return []
    return [
        f"font {getattr(font, 'name', '?')} lacks glyphs for {info.code} "
        f"({info.native}): {''.join(missing)!r} — output may show placeholders; "
        f"set fonts.body/fonts.cjk in config to a font covering the target language"
    ]
