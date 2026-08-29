"""进度事件流 + 命令通道（v0.4.0 UI 支持；v0.7.1 升级为阶段无关总线）。

设计（任务 2-4 预备）：
- 事件是纯 dict（JSON 可序列化），经回调推给消费者（server 端转 JSONL/轮询快照）。
- 回调永远不抛异常出去——UI 故障不能拖死翻译管线。
- CLI 不装回调时零开销（一次 None 判断）。

v0.7.1 EventSink → 命令总线（阶段无关）：
- 自动阶段标签：emit 的事件携带当前 stage（stage() 设置）——消费者
  （UI/订阅者/日志）无需理解生产者内部结构即可按阶段过滤，布局/翻译/
  渲染 actor 化（4-2#3 页级流水线）的事件地基；
- 订阅者：subscribe(fn) 进程内多消费者（CLI 打印器/测试/未来的
  per-stage actor），与 on_event 回调并行派发；
- 命令通道：post(cmd, **fields) 线程安全入队，drain() 批量取出——
  与 EventSink（出站）对偶的入站通道。JobControl.bind(sink) 后
  checkpoint() 在每个检查点消费命令（pause/resume/cancel 与控制文件
  语义一致），命令粒度从批间细化到检查点所在粒度（页级循环即页间）。
"""
from __future__ import annotations

import threading
import time
from typing import Callable


class EventSink:
    """线程安全的事件总线（出站）+ 命令通道（入站）。

    on_event: callable(dict) -> None，在调用者线程内同步执行。
    """

    def __init__(self, on_event: Callable[[dict], None] | None = None):
        self._on_event = on_event
        self._lock = threading.Lock()
        self.events: list[dict] = []   # 全量留档（server 轮询用）
        self._stage = ""               # 当前阶段标签（v0.7.1）
        self._subs: list[Callable[[dict], None]] = []   # v0.7.1 订阅者
        self._cmds: list[dict] = []    # v0.7.1 命令通道（入站队列）

    def emit(self, kind: str, **fields) -> None:
        with self._lock:
            ev = {"t": round(time.time(), 3), "kind": kind,
                  "stage": self._stage, **fields}
            self.events.append(ev)
            cb = self._on_event
            subs = list(self._subs)
        if cb is not None:
            try:
                cb(ev)
            except Exception:
                pass   # UI 消费端故障不拖垮翻译管线
        for fn in subs:
            try:
                fn(ev)
            except Exception:
                pass

    # ---- 语义化快捷方法 ----
    def stage(self, name: str) -> None:
        """进入阶段（布局/翻译/渲染…）：设置标签 + 发 stage 事件。"""
        with self._lock:
            self._stage = name
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

    # ---- v0.7.1: 订阅者（阶段无关消费）----
    def subscribe(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            self._subs.append(fn)

    def unsubscribe(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            try:
                self._subs.remove(fn)
            except ValueError:
                pass

    # ---- v0.7.1: 命令通道（入站，与事件出站对偶）----
    def post(self, cmd: str, **fields) -> None:
        """入队一条控制命令（pause/resume/cancel/未来页级指令）。"""
        with self._lock:
            self._cmds.append({"cmd": cmd, **fields})

    def drain(self) -> list[dict]:
        """取出全部待处理命令（JobControl.checkpoint 在检查点调用）。"""
        with self._lock:
            if not self._cmds:
                return []
            cmds, self._cmds = self._cmds, []
        return cmds
