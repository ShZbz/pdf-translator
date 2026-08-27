"""P2 验收单测（SCHEME §6 P2）：
- batch 协议 roundtrip（LLM 返回 JSON → 译文落位）
- 缓存二次运行 0 调用
- max_llm_calls 触顶行为（不崩、保留原文、stderr 警告）
- [FORMULA_n] 计数守恒
- 坏响应重试一次后降级保留原文
"""
from __future__ import annotations

import json
import threading
import time
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from translator.cache import TranslationCache
from translator.llm import TranslationClient


class FakeLLM:
    """模拟 OpenAI 兼容 client：client.chat.completions.create(...)。

    响应队列耗尽后再次调用直接抛 AssertionError —— 防止测试掩盖
    "多调了一次 LLM" 的 bug（缓存 0 调用测试依赖这一点）。
    """

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._i = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        if self._i >= len(self._responses):
            raise AssertionError("unexpected LLM call (queue exhausted)")
        raw = self._responses[self._i]
        self._i += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=raw))]
        )


def test_batch_roundtrip():
    """两段一请求 → 返回两段译文，且只看了一次 LLM。"""
    fake = FakeLLM([
        json.dumps({"1": "你好世界。", "2": "拓扑霍尔效应。"}, ensure_ascii=False)
    ])
    tc = TranslationClient(fake, model="mock", max_llm_calls=5)
    out, calls = tc.translate_paragraphs(["Hello world.", "Topological Hall effect."])
    assert calls == 1
    assert out == ["你好世界。", "拓扑霍尔效应。"]


def test_cache_second_run_zero_calls(tmp_path):
    """二次运行同一输入：0 次 LLM 调用（SCHEME 验收清单 #2）。"""
    db = tmp_path / "t.db"
    cache = TranslationCache(db)
    tc1 = TranslationClient(FakeLLM([json.dumps({"1": "你好。"}, ensure_ascii=False)]),
                            model="mock", max_llm_calls=5)
    out1, calls1 = tc1.translate_paragraphs(["Hello."], cache=cache)
    assert calls1 == 1 and out1 == ["你好。"]

    tc2 = TranslationClient(FakeLLM([]), model="mock", max_llm_calls=5)
    out2, calls2 = tc2.translate_paragraphs(["Hello."], cache=cache)
    assert calls2 == 0 and out2 == ["你好。"]


def test_llm_cap_behavior():
    """max_llm_calls=1 且有两批 → 第二批不调用、保留原文、出警告。"""
    fake = FakeLLM([json.dumps({"1": "甲。"}, ensure_ascii=False)])
    tc = TranslationClient(fake, model="mock", max_llm_calls=1, batch_size=1)
    out, calls = tc.translate_paragraphs(["A.", "B."])
    assert calls == 1
    assert out[0] == "甲。"
    assert out[1] == "B."  # 触顶保留原文
    assert any("max_llm_calls" in w for w in tc.warnings)


def test_formula_token_preserved():
    """[FORMULA_n] 占位符在译文中原样保留。"""
    src = "Spin texture [FORMULA_0] is shown in Fig. 3."
    fake = FakeLLM([json.dumps({"1": "自旋织构 [FORMULA_0] 如图 3 所示。"}, ensure_ascii=False)])
    tc = TranslationClient(fake, model="mock")
    out, _ = tc.translate_paragraphs([src])
    assert "[FORMULA_0]" in out[0]


def test_formula_count_mismatch_keeps_source():
    """LLM 吞掉占位符 → batch 级守恒校验失败 → 重试仍败 → 整批保留原文。"""
    bad = json.dumps({"1": "自旋织构如图。"})  # 少了 [FORMULA_0]
    fake = FakeLLM([bad, bad])
    tc = TranslationClient(fake, model="mock")
    out, calls = tc.translate_paragraphs(["Spin texture [FORMULA_0] shown."])
    assert calls == 2  # 首调 + 重试一次
    assert out == ["Spin texture [FORMULA_0] shown."]
    assert any("keep source" in w for w in tc.warnings)


def test_bad_response_retry_then_keep_source():
    """坏 JSON → 重试一次 → 仍败 → 保留原文 + 警告（共 2 次调用）。"""
    fake = FakeLLM(["not json", "still not json"])
    tc = TranslationClient(fake, model="mock", max_llm_calls=5)
    out, calls = tc.translate_paragraphs(["Keep me."])
    assert calls == 2
    assert out == ["Keep me."]
    assert any("keep source" in w for w in tc.warnings)


class _RateLimitLLM(FakeLLM):
    """前 N 次调用抛 429 风格异常，之后正常响应。"""

    def __init__(self, fail_times: int, responses: list[str]):
        super().__init__(responses)
        self._fail_times = fail_times

    def create(self, **kwargs):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError(
                "Error code: 429 - {'error': {'code': '1305', "
                "'message': '该模型当前访问量过大'}}")
        return super().create(**kwargs)


def test_rate_limit_failure_does_not_burn_budget():
    """429 失败不计入预算：预算 2，前 3 次请求全 429。
    旧代码下失败烧光预算、B 批不会发起；新代码 B 批重试成功。

    v0.4.2 修复测试自身的两个缺陷：
    - monkeypatch 类属性 _TRANSPORT_BACKOFF_BASE 是死代码（_ask 用的是
      实例属性 self.backoff_base），测试靠硬扛 8s 退避通过 → 改为构造参数
    - 两批默认并发，共享 fail_times 计数，哪批拿到成功响应取决于线程调度
      （全量回归下概率性 flaky，实测触发）→ max_workers=1 串行化
    """
    fake = _RateLimitLLM(fail_times=3, responses=[
        json.dumps({"2": "乙。"}, ensure_ascii=False),
    ])
    tc = TranslationClient(fake, model="mock", max_llm_calls=2, batch_size=1,
                           max_workers=1, backoff_base=0.01, backoff_cap=0.02)
    out, calls = tc.translate_paragraphs(["A.", "B."])
    assert calls == 4          # A批2次全败 + B批1败1成
    assert out == ["A.", "乙。"]  # A留原文，B翻出
    assert tc.api_ok_calls == 1


def test_cap_counts_successful_calls_only():
    """触顶按成功数计：预算 1，首批成功后第二批不调用、留原文。"""
    fake = FakeLLM([json.dumps({"1": "甲。"}, ensure_ascii=False)])
    tc = TranslationClient(fake, model="mock", max_llm_calls=1, batch_size=1)
    out, calls = tc.translate_paragraphs(["A.", "B."])
    assert calls == 1
    assert out[1] == "B."
    assert any("max_llm_calls" in w for w in tc.warnings)

# ---- v0.2.2 并发版新增测试 ----

def test_concurrent_batches_thread_safe():
    """多批并发:结果正确回填、调用数正确、无串扰。

    FakeLLM 响应按批号路由（并发下到达顺序不定,按请求内容返回对应译文）。
    """
    import threading

    class RouterLLM:
        """按 user prompt 内容路由响应,并记录调用时间戳验证并发。"""
        def __init__(self):
            self.lock = threading.Lock()
            self.call_ts = []
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, model, temperature, messages, **kwargs):
            with self.lock:
                self.call_ts.append(time.monotonic())
            user = messages[-1]["content"]
            data = json.loads(user)
            out = {k: f"译{v[-6:]}" for k, v in data.items()}
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(out, ensure_ascii=False)))])

    fake = RouterLLM()
    tc = TranslationClient(fake, model="mock", max_llm_calls=10,
                           batch_size=2, max_workers=3)
    paras = [f"Paragraph number {i} about sensing." for i in range(8)]
    out, calls = tc.translate_paragraphs(paras)
    assert calls == 4
    assert len(out) == 8
    for i, o in enumerate(out):
        assert o.startswith("译"), f"para {i} not translated: {o!r}"
    # 并发验证:至少有两批的调用时间重叠（简化:4批3worker必有并发）
    assert len(fake.call_ts) == 4


def test_concurrent_cap_no_overshoot():
    """并发下预算硬顶不超发:预算1、4批并发 → 成功调用数 ≤1。"""

    class SlowLLM:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def create(self, model, temperature, messages, **kwargs):
            time.sleep(0.05)
            data = json.loads(messages[-1]["content"])
            out = {k: "译" for k in data}
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(out, ensure_ascii=False)))])

    tc = TranslationClient(SlowLLM(), model="mock", max_llm_calls=1,
                           batch_size=1, max_workers=4)
    paras = [f"P{i}." for i in range(4)]
    out, calls = tc.translate_paragraphs(paras)
    assert tc.api_ok_calls <= 1
    translated = sum(1 for o in out if o == "译")
    assert translated == 1


def test_content_failure_no_backoff_delay():
    """内容校验失败(坏JSON)重试不应等待退避——总耗时 < 2s。"""
    fake = FakeLLM(["bad", "bad"])
    tc = TranslationClient(fake, model="mock", max_llm_calls=5)
    t0 = time.monotonic()
    out, calls = tc.translate_paragraphs(["Keep me."])
    dt = time.monotonic() - t0
    assert calls == 2 and dt < 2.0, f"content retry took {dt:.1f}s (backoff leaked?)"


def test_min_call_interval_global():
    """min_call_interval 全局节流:4批并发,相邻调用间隔 ≥ 间隔值(容差50ms)。"""

    class StampLLM:
        def __init__(self):
            self.ts = []
            self.lock = threading.Lock()
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def create(self, model, temperature, messages, **kwargs):
            with self.lock:
                self.ts.append(time.monotonic())
            data = json.loads(messages[-1]["content"])
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(
                    {k: "译" for k in data}, ensure_ascii=False)))])

    fake = StampLLM()
    tc = TranslationClient(fake, model="mock", max_llm_calls=10,
                           batch_size=1, min_call_interval=0.3,
                           max_workers=4)
    tc.translate_paragraphs([f"P{i}." for i in range(4)])
    ts = sorted(fake.ts)
    assert len(ts) == 4
    gaps = [ts[i+1] - ts[i] for i in range(3)]
    assert all(g >= 0.25 for g in gaps), f"interval violated: {gaps}"


class _DailyQuotaLLM(FakeLLM):
    """前 N 次调用抛日配额耗尽异常（Gemini PerDay 风格），之后正常。"""

    def __init__(self, fail_times: int, responses: list[str]):
        super().__init__(responses)
        self._fail_times = fail_times

    def create(self, **kwargs):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError(
                "Error code: 429 - [{'error': {'code': 429, "
                "'message': 'quota exceeded ... quotaId: "
                "GenerateRequestsPerDayPerProjectPerModel-FreeTier', "
                "'status': 'RESOURCE_EXHAUSTED'}}]")
        return super().create(**kwargs)


def test_daily_quota_switches_fallback_model():
    """日配额耗尽 → 自动切 fallback 模型继续翻译。"""
    fake = _DailyQuotaLLM(fail_times=1, responses=[
        json.dumps({"1": "乙。"}, ensure_ascii=False),
    ])
    tc = TranslationClient(fake, model="gemini-2.5-flash",
                           fallback_model="gemini-3.5-flash-lite",
                           max_llm_calls=5, backoff_base=0.01)
    out, calls = tc.translate_paragraphs(["B."])
    assert out == ["乙。"]
    assert tc.model == "gemini-3.5-flash-lite"
    assert any("switched to" in w for w in tc.warnings)


def test_rpm_quota_does_not_switch_model():
    """PerMinute 限流（等待可恢复）不应触发模型切换。"""
    import translator.llm as llm_mod

    class _RPM(FakeLLM):
        def __init__(self, responses):
            super().__init__(responses)
            self._failed_once = False
        def create(self, **kwargs):
            if not self._failed_once:
                self._failed_once = True
                raise RuntimeError("429 quotaId: GenerateRequestsPerMinutePerProject-FreeTier")
            return super().create(**kwargs)

    fake = _RPM([json.dumps({"1": "甲。"}, ensure_ascii=False)])
    tc = TranslationClient(fake, model="m1", fallback_model="m2", max_llm_calls=5)
    out, _ = tc.translate_paragraphs(["A."])
    assert out == ["甲。"]
    assert tc.model == "m1"   # 未切换


def test_no_fallback_configured_keeps_source():
    """未配置 fallback 且日配额耗尽 → 重试后保留原文（不崩）。"""
    fake = _DailyQuotaLLM(fail_times=99, responses=[])
    tc = TranslationClient(fake, model="m1", fallback_model="", max_llm_calls=5,
                           backoff_base=0.01, backoff_cap=0.02)
    out, _ = tc.translate_paragraphs(["A."])
    assert out == ["A."]
    assert any("keep source" in w for w in tc.warnings)


def test_fallback_chain_multi_model():
    """备用链:主模型+2备用,前两个都日配额耗尽 → 依次切换到第三个。"""
    class _ChainLLM(FakeLLM):
        """按当前请求的 model 抛日配额异常:m1/m2 耗尽,m3 正常。"""
        def create(self, **kwargs):
            if kwargs.get("model") in ("m1", "m2"):
                raise RuntimeError(
                    "429 quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
            return super().create(**kwargs)

    fake = _ChainLLM([json.dumps({"1": "丙。"}, ensure_ascii=False)])
    tc = TranslationClient(fake, model="m1",
                           fallback_model="m2, m3", max_llm_calls=5,
                           backoff_base=0.01)
    out, _ = tc.translate_paragraphs(["C."])
    assert out == ["丙。"]
    assert tc.model == "m3"
    switched = [w for w in tc.warnings if "switched to" in w]
    assert len(switched) == 2   # m1→m2→m3 两次切换
