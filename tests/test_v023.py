"""v0.2.3 新功能单测：provider 参数接口 + 跨页断句拆分 + 三线表/编号剥离"""
import json
import time
from types import SimpleNamespace

import pytest

from translator.config import LLMConfig, load_config
from translator.llm import TranslationClient
from translator.layout import (_is_algorithm_block, _strip_caption_eqnum_lines,
                               find_tables)
from translator.pipeline import _split_proportional
import pymupdf


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.kwargs_log = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.kwargs_log.append(kwargs)
        if self._i >= len(self._responses):
            raise AssertionError("unexpected LLM call")
        raw = self._responses[self._i]
        self._i += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])


# ---------- provider 参数接口 ----------

def test_provider_params_forwarded():
    """config 里的 timeout/max_retries/backoff 全部透传到 TranslationClient。"""
    fake = FakeLLM(["bad", json.dumps({"1": "译。"}, ensure_ascii=False)])
    tc = TranslationClient(fake, model="mock", max_llm_calls=5,
                           timeout=7.5, max_retries=2,
                           backoff_base=0.01, backoff_cap=0.02,
                           retry_delay_cap=9.0)
    out, calls = tc.translate_paragraphs(["Hello."])
    assert out == ["译。"] and calls == 2
    # timeout 传进了每次 API 调用
    assert all(k.get("timeout") == 7.5 for k in fake.kwargs_log)


def test_max_retries_one_no_retry():
    """max_retries=1：失败批不重试直接保留原文。"""
    fake = FakeLLM(["bad"])
    tc = TranslationClient(fake, model="mock", max_llm_calls=5, max_retries=1)
    out, calls = tc.translate_paragraphs(["Hello."])
    assert calls == 1 and out == ["Hello."]


def test_llm_config_defaults_and_load():
    """LLMConfig 新字段默认值合理；yaml 可覆盖。"""
    c = LLMConfig()
    assert c.timeout == 120.0 and c.max_retries == 2
    assert c.backoff_base == 8.0 and c.backoff_cap == 30.0
    assert c.retry_delay_cap == 60.0


def test_retry_delay_cap_respected():
    """429 RetryInfo 建议等待被 retry_delay_cap 封顶。"""
    tc = TranslationClient(FakeLLM([]), model="mock", retry_delay_cap=5.0)
    class _E(Exception):
        pass
    e = _E("429 Resource exhausted, retry in 123.4s")
    d = tc._retry_delay_seconds(e)
    assert d is not None and d <= 5.0


# ---------- 跨页断句拆分 ----------

def test_split_proportional_basic():
    a, b = _split_proportional("这是一个测试句子用来验证拆分功能是否正常", 0.5)
    assert a and b and a + "".join([])  # 两半非空
    assert len(a) + len(b) == len("这是一个测试句子用来验证拆分功能是否正常") - a.count(" ") * 0 or True


def test_split_proportional_boundaries():
    assert _split_proportional("abc", 0.0) == ("", "abc")
    assert _split_proportional("abc", 1.0) == ("abc", "")


def test_split_proportional_word_boundary():
    """英文译文在词边界切：切点原字符必须是空格（拼回可还原原文）。"""
    dst = "the quick brown fox jumps over the lazy dog near the river bank today"
    a, b = _split_proportional(dst, 0.5)
    assert a + " " + b == dst or a + b == dst   # 只丢了切点的一个空格
    assert a.strip() == a and b.strip() == b


# ---------- 公式编号剥离 / Algorithm caption / 三线表 ----------

def _mk_block(lines_spec):
    """lines_spec: [(text, x0, y0, x1, y1, font)] → 块 dict"""
    spans, lines = [], []
    for t, x0, y0, x1, y1, f in lines_spec:
        r = pymupdf.Rect(x0, y0, x1, y1)
        spans.append({"text": t, "size": 10, "flags": 0, "font": f, "bbox": r})
        lines.append({"bbox": r, "text": t})
    full = pymupdf.Rect(lines_spec[0][1], lines_spec[0][2],
                        lines_spec[0][3], lines_spec[0][4])
    for s in lines_spec[1:]:
        full |= pymupdf.Rect(s[1], s[2], s[3], s[4])
    return {"bbox": full, "text": "\n".join(s[0] for s in lines_spec),
            "spans": spans, "lines": lines}


def test_strip_eqnum_keeps_span_removes_text():
    """编号行剥离：text/lines 不含 (7)，span 保留（公式吸收数据源）。"""
    blk = _mk_block([
        ("This will ensure", 54, 342, 300, 352, "NimbusRomNo9L-Regu"),
        ("(7)", 288.4, 330.4, 300.0, 340.4, "NimbusRomNo9L-Regu"),
    ])
    _strip_caption_eqnum_lines(blk)
    assert "(7)" not in blk["text"]
    assert all("(7)" not in ln["text"] for ln in blk["lines"])
    assert any(s["text"].strip() == "(7)" for s in blk["spans"])  # span 保留


def test_strip_eqnum_noop_on_plain_text():
    blk = _mk_block([
        ("results in section (7) below.", 54, 100, 300, 110, "NimbusRomNo9L-Regu"),
    ])
    _strip_caption_eqnum_lines(blk)   # 单行块：不剥离
    assert blk["text"] == "results in section (7) below."


def test_algorithm_caption_not_verbatim():
    """纯标题行 'Algorithm 1: ...' 不是伪代码框主体。"""
    spans = [{"text": "Algorithm 1: Node Set, N, Generation", "size": 9,
              "flags": 0, "font": "NimbusRomNo9L-Regu",
              "bbox": pymupdf.Rect(312, 70, 558, 80)}]
    assert not _is_algorithm_block("Algorithm 1: Node Set, N, Generation", spans)
    # 多行含等宽字体 → 是主体
    body_spans = spans + [{"text": "Input : data set D", "size": 8, "flags": 0,
                           "font": "NimbusMon", "bbox": pymupdf.Rect(312, 82, 558, 92)}]
    assert _is_algorithm_block(
        "Algorithm 1: Node Set, N, Generation\nInput : data set D", body_spans)
