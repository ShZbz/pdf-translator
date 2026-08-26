"""进度事件流（v0.4.0 UI 支持）。

设计：
- 事件是纯 dict（JSON 可序列化），经回调推给消费者（server 端转 JSONL/轮询快照）。
- 回调永远不抛异常出去——UI 故障不能拖死翻译管线。
- CLI 不装回调时零开销（一次 None 判断）。
"""
from __future__ import annotations

import threading
import time
from typing import Callable


class EventSink:
    """线程安全的事件收集器。

    on_event: callable(dict) -> None，在调用者线程内同步执行。
    """

    def __init__(self, on_event: Callable[[dict], None] | None = None):
        self._on_event = on_event
        self._lock = threading.Lock()
        self.events: list[dict] = []   # 全量留档（server 轮询用）

    def emit(self, kind: str, **fields) -> None:
        ev = {"t": round(time.time(), 3), "kind": kind, **fields}
        with self._lock:
            self.events.append(ev)
            cb = self._on_event
        if cb is not None:
            try:
                cb(ev)
            except Exception:
                pass   # UI 消费端故障不拖垮翻译管线

    # ---- 语义化快捷方法 ----
    def stage(self, name: str) -> None:
        self.emit("stage", name=name)

    def progress(self, done: int, total: int, **extra) -> None:
        self.emit("progress", done=done, total=total, **extra)

    def warning(self, msg: str) -> None:
        self.emit("warning", msg=msg)

    def page_done(self, pno: int, total: int) -> None:
        self.progress(done=pno + 1, total=total, unit="page")

    def batch_done(self, ok_batches: int, total_batches: int,
                   calls: int) -> None:
        self.progress(done=ok_batches, total=total_batches, unit="batch",
                      calls=calls)
