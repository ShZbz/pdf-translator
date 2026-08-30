"""v0.4.2 单测：多语言注册表 / 跨平台字体 / 语言化排版 / Unicode 断行 / 修复回归。

全部零网络零 API key；字体目录相关用 tmp_path + monkeypatch 隔离，
不依赖本机是否装了对应字体。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import translator.langs as langs
from translator.langs import (LANGUAGES, coverage_warnings, is_cjk_script,
                              output_tag, prompt_lang_name,
                              resolve_output_fonts)
from translator.pipeline import _split_proportional, output_pdf_name
from translator.wrap_mixed import _has_latin, _tokenize, wrap_mixed


# ---------- 注册表完整性 ----------

def test_languages_registry_structure():
    """15 门语言元数据齐全；脚本/名称唯一；v0.5.1 起 RTL/天城文收录
    （htmlbox 渲染引擎自带 shaping/bidi，writer 路径由 pipeline 强制切换）。"""
    assert len(LANGUAGES) >= 15
    codes = set(LANGUAGES)
    assert {"zh", "en", "ja", "ko", "de", "fr", "es", "it", "pt",
            "ru", "tr", "vi", "ar", "he", "hi"} <= codes
    names = [v.name for v in LANGUAGES.values()]
    assert len(names) == len(set(names))          # LLM 提示名唯一
    for code in ("ar", "he"):
        assert LANGUAGES[code].rtl is True       # RTL 书写方向标记
    assert LANGUAGES["hi"].rtl is False
    for info in LANGUAGES.values():
        assert info.script in ("cjk", "latin", "cyrillic", "arabic",
                               "hebrew", "indic")
        assert info.sample.strip()
        assert info.body and info.heading


def test_output_tag_backcompat():
    """zh → 'Zh'（旧版 -Zh.pdf 文件名兼容），其余语言首字母大写。"""
    assert output_tag("zh") == "Zh"
    assert output_tag("de") == "De"
    assert output_tag("ja") == "Ja"
    assert output_tag("") == "Zh"          # 空配置回退中文
    assert output_tag("PT") == "Pt"


def test_output_pdf_name():
    assert output_pdf_name("paper", "zh", False) == "paper-Zh.pdf"
    assert output_pdf_name("paper", "de", True) == "paper-bilingual-De.pdf"
    assert output_pdf_name("论文 v2", "ko", False) == "论文 v2-Ko.pdf"


def test_prompt_lang_name():
    assert prompt_lang_name("zh") == "Simplified Chinese"
    assert prompt_lang_name("ru") == "Russian"
    assert prompt_lang_name("xx-UNKNOWN") == "xx-UNKNOWN"   # 未知码原样
    assert prompt_lang_name("") == "English"


def test_is_cjk_script():
    assert is_cjk_script("zh") and is_cjk_script("ja") and is_cjk_script("ko")
    assert not is_cjk_script("de") and not is_cjk_script("ru")
    assert is_cjk_script("nope") is True   # 未知码回退 zh 链（旧行为）


# ---------- 字体解析（tmp 目录隔离）----------

@pytest.fixture()
def fake_font_dirs(tmp_path, monkeypatch):
    """两个假字体目录 + 清空递归索引缓存。"""
    d1 = tmp_path / "fonts1"
    d2 = tmp_path / "fonts2"
    d1.mkdir()
    d2.mkdir()
    (d1 / "simsun.ttc").write_bytes(b"fake")
    (d1 / "simhei.ttf").write_bytes(b"fake")
    (d2 / "times.ttf").write_bytes(b"fake")
    monkeypatch.setattr(langs, "font_dirs", lambda: [d1, d2])
    monkeypatch.setattr(langs, "_INDEX_CACHE", {})
    return d1, d2


def test_resolve_fonts_priority_and_fallback(fake_font_dirs):
    d1, d2 = fake_font_dirs
    body, head = resolve_output_fonts("zh")
    assert body == str(d1 / "simsun.ttc")
    assert head == str(d1 / "simhei.ttf")
    # 西文：Windows 常用 times 在第二目录也应命中
    body, head = resolve_output_fonts("fr")
    assert body == str(d2 / "times.ttf")


def test_resolve_fonts_explicit_override(fake_font_dirs):
    d1, d2 = fake_font_dirs
    custom = d2 / "myfont.otf"
    custom.write_bytes(b"x")
    body, head = resolve_output_fonts(
        "zh", {"cjk": str(custom)})            # 旧版总开关键
    assert body == str(custom)
    body, _ = resolve_output_fonts("zh", {"body": str(custom)})   # 新中性键
    assert body == str(custom)
    # 不存在的显式路径：静默跳过走候选链（旧行为）
    body, _ = resolve_output_fonts("zh", {"cjk": str(d1 / "nope.ttf")})
    assert body == str(d1 / "simsun.ttc")


def test_resolve_fonts_missing_returns_none(fake_font_dirs, monkeypatch):
    monkeypatch.setattr(langs, "font_dirs", lambda: [])
    body, head = resolve_output_fonts("zh")
    assert body is None and head is None


def test_recursive_index_lookup(tmp_path, monkeypatch):
    """Linux fonts 树（opentype/noto/ 子目录）递归索引命中。"""
    deep = tmp_path / "usr" / "share" / "fonts" / "opentype" / "noto"
    deep.mkdir(parents=True)
    (deep / "NotoSansCJK-Regular.ttc").write_bytes(b"fake")
    monkeypatch.setattr(langs, "font_dirs",
                        lambda: [tmp_path / "usr" / "share" / "fonts"])
    monkeypatch.setattr(langs, "_INDEX_CACHE", {})
    body, _ = resolve_output_fonts("ja")
    assert body == str(deep / "NotoSansCJK-Regular.ttc")


class _FakeFont:
    """has_glyph 桩：只含 ascii。"""

    name = "fake"

    def has_glyph(self, cp: int) -> int:
        return 1 if cp < 128 else 0


def test_coverage_warnings():
    w = coverage_warnings(_FakeFont(), "de")
    assert w and "ß" in w[0]
    assert coverage_warnings(_FakeFont(), "en") == []     # 样本全 ascii
    w_ru = coverage_warnings(_FakeFont(), "ru")
    assert w_ru and "Я" in w_ru[0]


# ---------- Unicode 断行（欧洲/西里尔语言）----------

def test_has_latin_broadened():
    """变音符拉丁/西里尔也走词边界断行（旧版只有 A-Za-z，
    俄语会掉进 CJK 逐字断行=单词被任意腰斩）。"""
    assert _has_latin("Über eyes")
    assert _has_latin("Квантовая гипотеза")
    assert _has_latin("Điện biên")
    assert not _has_latin("纯中文没有字母")


def test_tokenize_keeps_accented_words_whole():
    toks = _tokenize("Über Straße Мn₃Sn")
    assert "Über" in toks and "Straße" in toks
    toks_ru = _tokenize("Квантовая гипотеза")
    assert "Квантовая" in toks_ru and "гипотеза" in toks_ru


def test_wrap_mixed_russian_word_boundary():
    """俄语文本窄行断行必须落在词间空格，不拆散单词。"""
    font = SimpleNamespace(
        text_length=lambda s, fontsize=10: 6.0 * len(s))
    text = "Квантовая гипотеза используется"
    lines = wrap_mixed(text, font, 10.0, width=80.0)
    assert len(lines) >= 2
    for ln in lines:
        assert ln in text                      # 每行都是原文连续子串
    rejoined = " ".join(lines)
    for word in text.split():
        assert word in rejoined                # 词未被拆散


def test_wrap_mixed_german_compound_not_split():
    """超行宽整词下移（硬切仅兜底 > 整行宽的单 token，如 URL）。"""
    font = SimpleNamespace(text_length=lambda s, fontsize=10: 6.0 * len(s))
    text = "Supraleitungsexperiment Energie"
    lines = wrap_mixed(text, font, 10.0, width=160.0)  # 第一词 24 字符=144pt
    assert any("Supraleitungsexperiment" in ln for ln in lines)   # 整词成行
    assert "Energie" in " ".join(lines)                          # 未被腰斩


# ---------- 修复回归 ----------

def test_split_proportional_single_char_no_crash():
    """v0.4.2：单字符译文旧版 min(range(1,1)) 直接 ValueError。"""
    assert _split_proportional("x", 0.5) == ("", "x")
    assert _split_proportional("好", 0.3) == ("", "好")


def test_typography_indent_by_script(monkeypatch):
    """CJK 目标正文首行缩进 2 字符；西文/西里尔顶格（学术惯例）。

    字体目录指空 → body 用 pymupdf 内置兜底（不依赖本机字体安装）。
    """
    monkeypatch.setattr(langs, "font_dirs", lambda: [])
    monkeypatch.setattr(langs, "_INDEX_CACHE", {})
    from translator.typography import Typography
    para = {"text": "A regular body paragraph of normal length here.",
            "size": 10.0, "is_heading": False, "is_caption": False,
            "is_ref": False}
    ty_zh = Typography({}, lang="zh")
    ty_de = Typography({}, lang="de")
    assert ty_zh.resolve(para, 10.0).indent == 2
    assert ty_de.resolve(para, 10.0).indent == 0
    assert ty_de.cjk is False and ty_zh.cjk is True


def test_find_cjk_font_lang_aware(fake_font_dirs):
    """find_cjk_font 旧函数名新语义：按目标语言解析；西文缺失时
    允许内置衬线兜底（返回 ""），CJK/西里尔缺失时抛错。"""
    from translator.render import find_cjk_font
    d1, d2 = fake_font_dirs
    assert find_cjk_font(None, lang="zh").endswith("simsun.ttc")
    assert find_cjk_font(None, lang="de").endswith("times.ttf")
    assert find_cjk_font(str(d2 / "times.ttf"), lang="zh").endswith("times.ttf")


def test_find_cjk_font_cjk_missing_raises(fake_font_dirs, monkeypatch):
    from translator.render import find_cjk_font
    monkeypatch.setattr(langs, "font_dirs", lambda: [])
    monkeypatch.setattr(langs, "_INDEX_CACHE", {})
    with pytest.raises(FileNotFoundError):
        find_cjk_font(None, lang="zh")
    with pytest.raises(FileNotFoundError):     # 西里尔无字体文件也不许豆腐块
        find_cjk_font(None, lang="ru")
    assert find_cjk_font(None, lang="fr") == ""   # 拉丁系 → 内置衬线


def test_llm_prompt_uses_language_names():
    from translator.llm import TranslationClient
    tc = TranslationClient(None, model="m", tgt_lang="de")
    prompt = tc._system_prompt()
    assert "German" in prompt
    tc_ru = TranslationClient(None, model="m", tgt_lang="ru")
    assert "Russian" in tc_ru._system_prompt()


# ---------- 服务端 /api/browse 与 /api/output ----------

def test_api_browse_and_output(tmp_path, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from server.app import app

    with fastapi_testclient.TestClient(app) as c:
        # 空路径：应返回一个真实存在的目录（旧版原生 Windows 上
        # 拿到不存在的 /mnt/c/Users 直接 404）
        r = c.get("/api/browse")
        assert r.status_code == 200
        assert Path(r.json()["path"]).is_dir()
        # /api/output：非 PDF 拒绝
        bad = tmp_path / "x.txt"
        bad.write_text("hi")
        assert c.get("/api/output", params={"path": str(bad)}).status_code == 404
        assert c.get("/api/output", params={"path": str(tmp_path / "none.pdf")}).status_code == 404
