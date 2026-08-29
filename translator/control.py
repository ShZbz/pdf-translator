"""协作式任务控制：暂停/恢复/取消（v0.4.0 UI 支持）。

语义（批间暂停）：
- 检查点粒度 = 页级（布局/渲染循环）+ 批级（LLM 调用前）。
- pause 后正在飞行中的那一次 LLM 请求会跑完才停，不半路掐断
  （省 token、防半截缓存）。实际延迟几秒内。
- cancel 在下一个检查点立即生效，抛 JobCancelled 向上传播；
  调用方负责收尾（管线内已保证不落半成品 PDF：先写 .tmp 再 rename）。

并发纪律：所有公开方法遵循「锁内改状态、锁外发通知」——
notify 必须在释放 self._lock 之后执行（非重入锁，锁内 notify = 死锁）。
"""
from __future__ import annotations

import threading


class JobCancelled(Exception):
    """用户取消任务时在检查点抛出。"""


class JobControl:
    """线程安全的运行状态机：running ⇄ paused，任意态 → cancelled。

    用法：worker 线程在合适位置调 checkpoint()；
    UI 线程调 pause()/resume()/cancel()。
    """

    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = self.RUNNING
        # Condition 绑定同一把锁：wait() 时自动放锁、返回前重新拿锁
        self._cond = threading.Condition(self._lock)
        # v0.7.1: 命令总线绑定（EventSink.drain）——checkpoint 在每个
        # 检查点消费命令通道，命令粒度=检查点粒度（页级循环即页间）
        self._command_source = None

    # ---- v0.7.1: 命令总线绑定（任务 2-4 预备）----
    def bind_commands(self, drain: "callable") -> None:
        """绑定命令源（如 EventSink.drain）；checkpoint 检查点消费。"""
        self._command_source = drain

    def _apply_commands(self) -> None:
        """消费命令通道（幂等：与直接调用 pause/resume/cancel 同语义）。"""
        drain = self._command_source
        if drain is None:
            return
        try:
            cmds = drain()
        except Exception:
            return
        for c in cmds:
            cmd = (c.get("cmd") or "").strip().lower()
            if cmd == "pause":
                self.pause()
            elif cmd == "resume":
                self.resume()
            elif cmd == "cancel":
                self.cancel()

    # ---- UI 线程侧 ----
    def pause(self) -> bool:
        with self._lock:
            if self._state == self.RUNNING:
                self._state = self.PAUSED
                return True
        return False

    def resume(self) -> bool:
        with self._lock:
            if self._state != self.PAUSED:
                return False
            self._state = self.RUNNING
        self._notify()          # 锁外
        return True

    def cancel(self) -> bool:
        with self._lock:
            if self._state == self.CANCELLED:
                return False
            was_paused = self._state == self.PAUSED
            self._state = self.CANCELLED
        if was_paused:
            self._notify()      # 锁外：唤醒阻塞在暂停里的 worker
        return True

    # ---- worker 线程侧 ----
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def checkpoint(self) -> None:
        """在检查点调用：先消费命令通道，paused → 阻塞至恢复，cancelled → 抛。"""
        while True:
            self._apply_commands()
            with self._lock:
                st = self._state
                if st == self.RUNNING:
                    return
                if st == self.CANCELLED:
                    raise JobCancelled("cancelled by user")
                # PAUSED：放锁等待（最多 0.5s 轮询一次，兜底防漏通知）
                self._cond.wait(timeout=0.5)

    # ---- 内部 ----
    def _notify(self) -> None:
        """唤醒所有等待者。调用方必须不持有 self._lock。"""
        with self._cond:
            self._cond.notify_all()
