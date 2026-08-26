"""v0.4.0 UI 集成单测：事件流 / 暂停恢复 / 取消 / 锁安全。

全部零网络零 API key（FakeLLM 复用 test_smoke 的模式）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from translator.control import JobCancelled, JobControl
from translator.events import EventSink
from translator.llm import TranslationClient


class FakeLLM:
    """模拟 OpenAI 兼容 client。响应队列耗尽后抛 AssertionError。"""

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
            choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])


def _resp(**pairs) -> str:
    return json.dumps(pairs, ensure_ascii=False)


# ---- control.py 状态机 ----

def test_control_pause_resume_cycle():
    c = JobControl()
    assert c.state == JobControl.RUNNING
    assert c.pause() is True and c.state == JobControl.PAUSED
    # paused 态再 pause 无效
    assert c.pause() is False
    assert c.resume() is True and c.state == JobControl.RUNNING
    # running 态再 resume 无效
    assert c.resume() is False


def test_control_cancel_from_paused_unblocks():
    """暂停中的 checkpoint 必须能被 cancel 唤醒并抛 JobCancelled。"""
    c = JobControl()
    c.pause()
    raised = threading.Event()

    def worker():
        try:
            c.checkpoint()
        except JobCancelled:
            raised.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    time.sleep(0.1)          # 让 worker 进入等待
    assert not raised.is_set()   # 还在暂停中，不该抛
    c.cancel()
    assert raised.wait(timeout=2.0), "cancel 后 worker 未被唤醒"


def test_control_checkpoint_raises_when_cancelled():
    c = JobControl()
    c.cancel()
    try:
        c.checkpoint()
        assert False, "cancelled 后 checkpoint 应抛 JobCancelled"
    except JobCancelled:
        pass


# ---- events.py ----

def test_event_sink_callback_and_history():
    got: list[dict] = []
    s = EventSink(on_event=got.append)
    s.stage("layout")
    s.progress(done=3, total=8, unit="page")
    s.warning("w1")
    assert [e["kind"] for e in s.events] == ["stage", "progress", "warning"]
    assert got == s.events          # 回调收到同样的对象
    assert s.events[1]["done"] == 3 and s.events[1]["unit"] == "page"
    assert all(isinstance(e["t"], float) for e in s.events)


def test_event_sink_callback_exception_swallowed():
    """回调炸了不能影响 emit 调用方（UI 故障不拖垮翻译管线）。"""
    def boom(_ev):
        raise RuntimeError("ui dead")

    s = EventSink(on_event=boom)
    s.emit("stage", name="x")       # 不应抛
    assert len(s.events) == 1


def test_batch_done_emits_progress():
    # 注意：batch 内 id = 全局段索引+1，两批各自单段 → 响应分别键 1、2
    fake = FakeLLM([_resp(**{"1": "甲。"}), _resp(**{"2": "乙。"})])
    evs: list[dict] = []
    sink = EventSink(on_event=evs.append)
    tc = TranslationClient(fake, model="mock", max_llm_calls=5,
                           batch_size=1, sink=sink)
    out, calls = tc.translate_paragraphs(["A.", "B."])
    assert calls == 2
    kinds = [e["kind"] for e in evs]
    assert "translate_start" in kinds
    batch_events = [e for e in evs if e["kind"] == "progress"]
    assert len(batch_events) == 2
    last = batch_events[-1]
    assert last["unit"] == "batch" and last["total"] == 2
    assert last["done"] == 2 and last["calls"] == 2
    # 每个成功批恰好一条 batch_done 事件
    ok_batches = [e for e in batch_events if e["done"] <= e["total"]]
    assert len(ok_batches) == 2


# ---- 取消语义（LLM 层）----

def test_cancel_before_translate_aborts():
    """取消后进 translate：JobCancelled 直接向上传播（不产出半成品 PDF），
    已发车批在检查点放弃。"""
    fake = FakeLLM([])   # 队列空：任何调用都 AssertionError → 若被调用测试必炸
    ctrl = JobControl()
    ctrl.cancel()
    sink = EventSink()
    tc = TranslationClient(fake, model="mock", max_llm_calls=5,
                           batch_size=1, control=ctrl, sink=sink)
    try:
        tc.translate_paragraphs(["Hello.", "World.", "Third."])
        assert False, "取消后应抛 JobCancelled"
    except JobCancelled:
        pass
    assert fake._i == 0                  # 0 次真实 LLM 调用
    cancelled = [e for e in sink.events if e["kind"] == "cancelled_at_batch"]
    assert len(cancelled) == 3           # 三个批都在检查点取消


def test_worker_exception_does_not_crash_pool():
    """_guarded_batch 防御层：非取消异常按失败批降级，不炸线程池。

    用坏 checkpoint 触发（LLM 异常在 _ask 内部已被吃掉到不了这里）。
    """
    class BadControl:
        def checkpoint(self):
            raise ValueError("worker bug simulation")

    evs: list[dict] = []
    sink = EventSink(on_event=evs.append)
    tc = TranslationClient(FakeLLM([]), model="mock", max_llm_calls=10,
                           batch_size=1, max_workers=2, sink=sink,
                           control=BadControl())
    out, calls = tc.translate_paragraphs(["A one.", "B two.", "C three."])
    assert calls == 0                    # 检查点先炸，没到 LLM
    assert out == ["A one.", "B two.", "C three."]   # 全部原文降级
    warns = [e for e in evs if e["kind"] == "warning"]
    assert any("batch worker exception" in w["msg"] for w in warns)
