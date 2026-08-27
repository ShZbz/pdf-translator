"""任务管理器：子进程隔离跑翻译，UI 崩了不影响翻译。

每个任务 = 独立子进程（python -m translator.cli 变体），通过
JSONL 事件流文件与父进程通信。单任务模型：
同一时刻只允许一个翻译在跑（LLM 并发/缓存 DB 都是单写者设计）。

v0.5.0:
- 任务持久化（store.py）：队列/历史落 SQLite（.ui_jobs.db），服务重启
  恢复未完成任务（重新排队重跑，配合翻译缓存只剩增量段），历史可查
- 事件广播（SSE 源头）：Job 事件泵把 (snapshot, event) 推给所有订阅
  队列，/api/jobs/current/stream 据此做服务端推送，替代 1.2s 轮询
- 警告接活：warning 事件不再被丢弃（旧版 _pump_events 只认
  stage/progress/done/exit，UI 的 warn-list 从不更新），快照带最近警告
- 子进程无 exit 事件崩溃时标 error（旧版一律 cancelled，误导排查）

v0.4.3:
- 提交忙时入队而非拒绝（最多 MAX_QUEUE 个排队任务，前一个终态后
  自动开跑下一个）
- history 字段接活：任务终态时归档快照（上限 MAX_HISTORY 条），
  GET /api/jobs 可查历史
- 运行配置文件按任务独立（app.py 生成 .ui_run_config_<id>.yaml）
- 控制通道从 stdin 管道改为**控制文件轮询**（.ui_ctl_<jobid>.txt）：
  Windows 上「子进程线程阻塞读 stdin 管道 + multiprocessing spawn
  进程池」组合会死锁池引导。控制文件不占管道，轮询粒度 0.4s。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .store import JobStore

MAX_QUEUE = 5       # 排队上限（防 UI 连点堆积一堆子进程）
MAX_HISTORY = 50    # 历史归档上限（内存；DB 侧 prune 上限更大）
MAX_WARNINGS = 40   # 单任务内存保留的最近警告条数

# 子进程入口脚本：加载 config → translate_document(sink/control) → JSONL 事件到 stdout
# ⚠️ 保持 `-c` 形态（无 __file__）：spawn 池子进程不会重放本段代码。
#    写成文件反而要求调用方加 if __name__ == "__main__" 保护。
# 控制命令走控制文件（argv[2]），绝不读 stdin（见模块 docstring）。
_WORKER = r'''
import json, sys, time
sys.path.insert(0, {root!r})
from translator.control import JobControl, JobCancelled
from translator.events import EventSink
from translator.config import load_config
from translator.pipeline import translate_document

def flush(ev):
    sys.stdout.write(json.dumps(ev, ensure_ascii=False) + "\n")
    sys.stdout.flush()

cfg_path = sys.argv[1]
ctl_path = sys.argv[2]
sink = EventSink(on_event=flush)
control = JobControl()

def ctl_loop():
    pos = 0
    while True:
        time.sleep(0.4)
        try:
            with open(ctl_path, "r", encoding="utf-8") as f:
                f.seek(pos)
                data = f.read()
                pos = f.tell()
        except OSError:
            continue
        for cmd in (c.strip() for c in data.split("\n")):
            if cmd == "pause":
                control.pause()
            elif cmd == "resume":
                control.resume()
            elif cmd == "cancel":
                control.cancel()

import threading
threading.Thread(target=ctl_loop, daemon=True).start()

try:
    cfg = load_config(cfg_path)
    base_url, api_key = cfg.llm.resolve()
    client = None
    if base_url:
        from openai import OpenAI
        client = OpenAI(base_url=base_url, api_key=api_key or "sk-noop")
    stats = translate_document(cfg, client=client, sink=sink, control=control)
    flush({{"kind": "exit", "code": 0, "output": stats["output"],
            "pages": stats["pages"], "calls": stats["calls"]}})
except JobCancelled:
    flush({{"kind": "exit", "code": 2, "cancelled": True}})
except Exception as e:
    flush({{"kind": "exit", "code": 1, "error": str(e)}})
'''

_TERMINAL = ("done", "cancelled", "error")


class Job:
    """单个翻译任务的句柄。"""

    def __init__(self, job_id: str, config_path: str,
                 created: float | None = None):
        self.id = job_id
        self.config_path = config_path
        self.status = "queued"      # queued/running/paused/done/cancelled/error
        self.output_path = ""
        self.error = ""
        self.created = created if created is not None else time.time()
        # v0.4.2: 进度/统计快照字段（UI 状态行与完成行展示用）
        self.stage = ""             # layout / translate / render
        self.progress: dict = {}    # {done, total, unit, calls?}
        self.pages = 0
        self.paragraphs = 0
        self.calls = 0
        self.elapsed = 0.0
        self.warnings: list[str] = []   # v0.5.0: 最近 N 条管线警告
        self._proc: subprocess.Popen | None = None
        self.ctl_path: Path | None = None   # v0.4.3 控制文件
        self._lock = threading.Lock()
        self._terminal_fired = False        # on_terminal 只触发一次
        self.on_terminal = None             # JobManager 注入的排队推进回调
        self.on_event = None                # v0.5.0: JobManager 注入的广播回调
        self._last_persist = 0.0            # 持久化节流

    # ---- 事件（事件泵线程调用；锁外转发）----
    def _handle_event(self, ev: dict) -> None:
        kind = ev.get("kind")
        with self._lock:
            if kind == "stage":
                self.stage = ev.get("name", "")
                if self.stage:
                    self.progress = {}     # 阶段切换，进度清零
            elif kind == "progress":
                self.progress = {k: ev.get(k) for k in
                                 ("done", "total", "unit", "calls")}
            elif kind == "warning":
                self.warnings.append(ev.get("msg", ""))
                self.warnings = self.warnings[-MAX_WARNINGS:]
            elif kind == "done":
                self.pages = ev.get("pages", 0)
                self.paragraphs = ev.get("paragraphs", 0)
                self.calls = ev.get("calls", 0)
                self.elapsed = ev.get("elapsed", 0.0)
            elif kind == "exit":
                code = ev.get("code", 1)
                if ev.get("cancelled"):
                    self.status = "cancelled"
                elif code == 0:
                    self.status = "done"
                    self.output_path = ev.get("output", "")
                else:
                    self.status = "error"
                    self.error = ev.get("error", f"exit {code}")
        cb = self.on_event
        if cb is not None:
            try:
                cb(self, ev)
            except Exception:
                pass

    def start(self, project_root: Path) -> None:
        worker_src = _WORKER.format(root=str(project_root))
        self.ctl_path = project_root / f".ui_ctl_{self.id}.txt"
        self.ctl_path.write_text("", encoding="utf-8")
        self._proc = subprocess.Popen(
            [sys.executable, "-c", worker_src, self.config_path, str(self.ctl_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
            cwd=str(project_root),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.status = "running"
        threading.Thread(target=self._pump_events, daemon=True).start()
        threading.Thread(target=self._reap, daemon=True).start()

    # ---- UI 控制（v0.4.3: 写控制文件，worker 轮询消费）----
    def _send_cmd(self, cmd: str) -> None:
        if self.ctl_path is None:
            return
        try:
            with open(self.ctl_path, "a", encoding="utf-8") as f:
                f.write(cmd + "\n")
        except OSError:
            pass

    def pause(self) -> None:
        if self.status == "running":
            self._send_cmd("pause")
            self.status = "paused"

    def resume(self) -> None:
        if self.status == "paused":
            self._send_cmd("resume")
            self.status = "running"

    def cancel(self) -> None:
        if self.status in _TERMINAL:
            return
        if self.status == "queued":
            # 排队中的任务没有子进程，直接标记取消；
            # _on_terminal 的接力循环会跳过 cancelled 状态的任务
            self.status = "cancelled"
            self._fire_terminal()
            return
        self._send_cmd("cancel")

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "output_path": self.output_path,
                "error": self.error,
                "stage": self.stage,
                "progress": dict(self.progress) if self.progress else None,
                "pages": self.pages,
                "paragraphs": self.paragraphs,
                "calls": self.calls,
                "elapsed": self.elapsed,
                "created": round(self.created, 3),
                "config_path": self.config_path,
                "warnings": list(self.warnings),   # v0.5.0: 最近警告
            }

    # ---- 内部 ----
    def _fire_terminal(self) -> None:
        """终态回调（只发一次）：JobManager 借此归档 + 推进队列。"""
        cb = None
        with self._lock:
            if not self._terminal_fired:
                self._terminal_fired = True
                cb = self.on_terminal
        if cb is not None:
            try:
                cb(self)
            except Exception:
                pass

    def _pump_events(self) -> None:
        """读子进程 stdout 的 JSONL 事件流，更新状态。"""
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._handle_event(ev)
        # 收到 exit 事件（或 stdout EOF）= 子进程已出完任务/已退出，
        # 此时清理临时文件是安全的——不等 _reap()。_reap() 里的 cleanup
        # 保留为崩溃路径（无 exit 事件）兜底，二者幂等。
        self._cleanup_files()
        self._fire_terminal()

    def _reap(self) -> None:
        """兜底：子进程退出但没发 exit 事件时更新状态。"""
        assert self._proc is not None
        rc = self._proc.wait()
        time.sleep(0.3)   # 让 _pump_events 先处理完尾部事件
        with self._lock:
            if self.status in ("running", "paused"):
                # v0.5.0: 崩溃（无 exit 事件）按 error 归档——旧版一律
                # cancelled，用户会以为是自己取消的，误导排查
                if rc != 0:
                    self.status = "error"
                    self.error = f"worker exited unexpectedly (code {rc})"
                else:
                    self.status = "error"
                    self.error = "worker exited without completion event"
        self._fire_terminal()
        self._cleanup_files()

    def _cleanup_files(self) -> None:
        """删除 app.py 生成的临时运行配置与控制文件（best-effort）。"""
        for p in ([self.ctl_path] if self.ctl_path else []) + \
                 [Path(self.config_path)]:
            try:
                if p.name.startswith((".ui_run_config_", ".ui_ctl_")) \
                        and p.exists():
                    p.unlink(missing_ok=True)
            except OSError:
                pass


class JobManager:
    """单任务槽位 + 排队管理器（v0.4.3: 忙时入队自动接力）。

    v0.5.0: store（JobStore）非空时队列/历史持久化，并在构造时恢复
    上次未完成的任务（重跑语义——配合翻译缓存只剩增量段）。
    """

    def __init__(self, project_root: Path, store: JobStore | None = None):
        self.project_root = project_root
        self.job: Job | None = None
        self._lock = threading.Lock()
        self.history: list[dict] = []
        self.queue: list[Job] = []
        self.store = store
        # v0.5.0: SSE 订阅者（queue.SimpleQueue，事件泵线程生产）
        self._listeners: set["queue.SimpleQueue"] = set()
        if store is not None:
            self._restore_from_store()

    # ---- 持久化 ----
    def _persist(self, job: Job, seq: int | None = None) -> None:
        if self.store is None:
            return
        try:
            self.store.upsert(job.snapshot(), seq=seq)
        except Exception:
            pass   # 持久化故障不拖垮任务执行

    def _restore_from_store(self) -> None:
        """服务重启：恢复上次未完成任务（重排队）+ 历史。"""
        assert self.store is not None
        restored = 0
        try:
            unfinished = self.store.unfinished()
        except Exception:
            unfinished = []
        for i, snap in enumerate(unfinished):
            cfgp = snap.get("config_path") or ""
            job = Job(snap["id"], cfgp, created=snap.get("created"))
            if not cfgp or not Path(cfgp).is_file():
                job.status = "error"
                job.error = "config file missing after restart"
                job._terminal_fired = True   # 不触发排队推进
                self._persist(job)
                continue
            # 上次运行中被中断 → 重新排队重跑（缓存命中只剩增量段）
            job.status = "queued"
            self.queue.append(job)
            restored += 1
        try:
            self.history = self.store.history(limit=MAX_HISTORY)
        except Exception:
            self.history = []
        if restored:
            with self._lock:
                self._start_next_locked()

    def _start_next_locked(self) -> None:
        """从队列取下一个任务开跑（调用方持锁；无任务则归档当前）。"""
        while self.queue:
            nxt = self.queue.pop(0)
            if nxt.status == "cancelled":
                self._persist(nxt)
                continue       # 排队期间被取消的直接跳过
            nxt.on_terminal = self._on_terminal
            nxt.on_event = self._on_job_event
            nxt.start(self.project_root)
            self.job = nxt
            self._persist(nxt, seq=None)
            return

    # ---- 事件广播（SSE 源头）----
    def _on_job_event(self, job: Job, ev: dict) -> None:
        snap = job.snapshot()
        self._maybe_persist_progress(job, snap, ev)
        self._broadcast({"kind": "job_event", "event": ev, "job": snap})

    def _maybe_persist_progress(self, job: Job, snap: dict, ev: dict) -> None:
        """进度持久化节流：stage/exit 立即落盘，progress ≥2s 一次。"""
        if ev.get("kind") not in ("progress", "warning"):
            self._persist(job, seq=self._seq_of(job))
            job._last_persist = time.time()
            return
        now = time.time()
        if now - job._last_persist >= 2.0:
            job._last_persist = now
            self._persist(job, seq=self._seq_of(job))

    def _seq_of(self, job: Job) -> int | None:
        """排队位次（持久化恢复顺序用）；非排队态返回 None。"""
        try:
            return self.queue.index(job)
        except ValueError:
            return None

    def _broadcast(self, payload: dict) -> None:
        dead = []
        for q in list(self._listeners):
            try:
                q.put_nowait(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            self._listeners.discard(q)

    def subscribe(self) -> "queue.SimpleQueue":
        q = queue.SimpleQueue()
        with self._lock:
            self._listeners.add(q)
        return q

    def unsubscribe(self, q: "queue.SimpleQueue") -> None:
        with self._lock:
            self._listeners.discard(q)

    def current_snapshot(self) -> dict:
        """当前任务 + 队列概览（SSE 初始帧与轮询共用）。"""
        with self._lock:
            cur = self.job.snapshot() if self.job else None
            return {
                "current": cur,
                "queued": [j.snapshot() for j in self.queue],
                "queue_len": len(self.queue),
            }

    # ---- 提交/查询/控制 ----
    def submit(self, config_path: str) -> dict:
        with self._lock:
            busy = self.job is not None and self.job.status not in _TERMINAL
            if busy and len(self.queue) >= MAX_QUEUE:
                return {"ok": False,
                        "error": f"队列已满（{MAX_QUEUE} 个排队任务）"}
            job = Job(uuid.uuid4().hex[:12], config_path)
            job.on_terminal = self._on_terminal
            job.on_event = self._on_job_event
            if busy:
                self.queue.append(job)
                queued = True
                seq = len(self.queue) - 1
            else:
                # 槽位空闲（首次提交或上个任务已终态）：先归档旧的再开跑
                self._archive_locked()
                job.start(self.project_root)
                self.job = job
                queued = False
                seq = None
            self._persist(job, seq=seq)
            self._broadcast({"kind": "manager", "action": "submit",
                             "job_id": job.id, "queued": queued})
            return {"ok": True, "job_id": job.id, "queued": queued,
                    "queue_len": len(self.queue)}

    def current(self) -> Job | None:
        return self.job

    def find(self, job_id: str) -> Job | None:
        """按 id 找任务：当前任务或排队任务（v0.5.0: 排队任务可取消）。"""
        with self._lock:
            if self.job is not None and self.job.id == job_id:
                return self.job
            for j in self.queue:
                if j.id == job_id:
                    return j
        return None

    def act(self, job_id: str, action: str) -> Job | None:
        """对任务执行 pause/resume/cancel（白名单由 API 层校验）。

        v0.5.0: 执行后立即持久化——排队任务被取消后必须落盘，否则服务
        重启会把它当未完成任务恢复重跑（用户明明已经取消）。
        """
        job = self.find(job_id)
        if job is None:
            return None
        getattr(job, action)()
        self._persist(job, seq=self._seq_of(job))
        return job

    def _archive_locked(self) -> None:
        """把已终态的当前任务快照归档进 history（调用方持锁）。"""
        if self.job is not None and self.job.status in _TERMINAL:
            self.history.append(self.job.snapshot())
            self.history = self.history[-MAX_HISTORY:]
            self.job = None
            if self.store is not None:
                try:
                    self.store.prune(keep=200)
                except Exception:
                    pass

    def _on_terminal(self, job: Job) -> None:
        """当前任务终态：归档 + 排队任务接力开跑。"""
        with self._lock:
            if self.job is not job:
                return          # 非当前任务（历史残留回调），忽略
            self._persist(job, seq=None)
            self._archive_locked()
            self._start_next_locked()
        self._broadcast({"kind": "manager", "action": "advanced",
                         "job_id": job.id})

    def archive(self, job: Job) -> None:
        """显式归档（外部用；submit/_on_terminal 内部走 _archive_locked）。"""
        with self._lock:
            if self.job is job:
                self._archive_locked()
