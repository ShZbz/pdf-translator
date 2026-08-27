"""任务管理器：子进程隔离跑翻译，UI 崩了不影响翻译。

每个任务 = 独立子进程（python -m translator.cli 变体），通过
JSONL 事件流文件 + 控制管道与父进程通信。单任务模型：
同一时刻只允许一个翻译在跑（LLM 并发/缓存 DB 都是单写者设计）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from translator.config import load_config

# 子进程入口脚本：加载 config → translate_document(sink/control) → JSONL 事件到 stdout
_WORKER = r'''
import json, sys
sys.path.insert(0, {root!r})
from translator.control import JobControl, JobCancelled
from translator.events import EventSink
from translator.config import load_config
from translator.pipeline import translate_document

def flush(ev):
    sys.stdout.write(json.dumps(ev, ensure_ascii=False) + "\n")
    sys.stdout.flush()

cfg_path = sys.argv[1]
sink = EventSink(on_event=flush)
control = JobControl()

# 控制命令从 stdin 读:一行一个 pause/resume/cancel
def stdin_loop():
    for line in sys.stdin:
        cmd = line.strip()
        if cmd == "pause":
            control.pause()
        elif cmd == "resume":
            control.resume()
        elif cmd == "cancel":
            control.cancel()

import threading
threading.Thread(target=stdin_loop, daemon=True).start()

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


class Job:
    """单个翻译任务的句柄。"""

    def __init__(self, job_id: str, config_path: str):
        self.id = job_id
        self.config_path = config_path
        self.status = "queued"      # queued/running/paused/done/cancelled/error
        self.output_path = ""
        self.error = ""
        self.created = time.time()
        # v0.4.2: 进度/统计快照字段（UI 状态行与完成行展示用）
        self.stage = ""             # layout / translate / render
        self.progress: dict = {}    # {done, total, unit, calls?}
        self.pages = 0
        self.paragraphs = 0
        self.calls = 0
        self.elapsed = 0.0
        self._proc: subprocess.Popen | None = None
        self._stdin = None
        self._lock = threading.Lock()

    def start(self, project_root: Path) -> None:
        worker_src = _WORKER.format(root=str(project_root))
        self._proc = subprocess.Popen(
            [sys.executable, "-c", worker_src, self.config_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
            cwd=str(project_root),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self._stdin = self._proc.stdin
        self.status = "running"
        threading.Thread(target=self._pump_events, daemon=True).start()
        threading.Thread(target=self._reap, daemon=True).start()

    # ---- UI 控制 ----
    def pause(self) -> None:
        if self._stdin and self.status == "running":
            try:
                self._stdin.write("pause\n")
                self._stdin.flush()
                self.status = "paused"
            except (BrokenPipeError, ValueError):
                pass

    def resume(self) -> None:
        if self._stdin and self.status == "paused":
            try:
                self._stdin.write("resume\n")
                self._stdin.flush()
                self.status = "running"
            except (BrokenPipeError, ValueError):
                pass

    def cancel(self) -> None:
        if not self._stdin or self.status in ("done", "cancelled", "error"):
            return
        try:
            self._stdin.write("cancel\n")
            self._stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

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
            }

    # ---- 内部 ----
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
            kind = ev.get("kind")
            with self._lock:
                if kind == "stage":
                    self.stage = ev.get("name", "")
                    if self.stage:
                        self.progress = {}     # 阶段切换，进度清零
                elif kind == "progress":
                    self.progress = {k: ev.get(k) for k in
                                     ("done", "total", "unit", "calls")}
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

    def _reap(self) -> None:
        """兜底：子进程退出但没发 exit 事件时更新状态。"""
        assert self._proc is not None
        rc = self._proc.wait()
        time.sleep(0.3)   # 让 _pump_events 先处理完尾部事件
        with self._lock:
            if self.status == "running":
                self.status = "cancelled" if rc != 0 else "done"


class JobManager:
    """单任务槽位管理器。"""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.job: Job | None = None
        self._lock = threading.Lock()
        self.history: list[dict] = []

    def submit(self, config_path: str) -> dict:
        with self._lock:
            if self.job and self.job.status in ("queued", "running", "paused"):
                return {"ok": False, "error": "已有翻译任务在运行"}
            job = Job(uuid.uuid4().hex[:12], config_path)
            job.start(self.project_root)
            self.job = job
            return {"ok": True, "job_id": job.id}

    def current(self) -> Job | None:
        return self.job

    def archive(self, job: Job) -> None:
        with self._lock:
            self.history.append(job.snapshot())
