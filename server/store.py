"""v0.5.0 任务持久化：队列/历史落 SQLite，服务重启恢复未完成任务。

设计（任务 2-3）：
- 单表 jobs，一行一个任务的最新快照（与 Job.snapshot() 字段对齐，
  progress 以 JSON 存储）
- 终态任务（done/cancelled/error）保留为历史，非终态（queued/running/
  paused）在 JobManager 启动时按 seq 顺序恢复重跑——配合翻译缓存，
  重跑只剩增量段（已完成段的译文直接命中缓存零调用）
- 运行配置文件（.ui_run_config_*.yaml）由 app.py 落盘、任务终态才清理，
  服务被杀时文件仍在，重启恢复可直接引用；文件缺失的任务标记 error
- SQLite 单文件本地库（.ui_jobs.db），check_same_thread=False +
  内部锁——Job 事件泵线程与 API 线程都会写
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    config_path TEXT NOT NULL,
    output_path TEXT DEFAULT '',
    error       TEXT DEFAULT '',
    stage       TEXT DEFAULT '',
    progress    TEXT DEFAULT '',
    pages       INTEGER DEFAULT 0,
    paragraphs  INTEGER DEFAULT 0,
    calls       INTEGER DEFAULT 0,
    elapsed     REAL DEFAULT 0,
    created     REAL DEFAULT 0,
    updated     REAL DEFAULT 0,
    seq         INTEGER
)
"""

# v0.5.1 新增列（历史面板：重跑要 input/output_dir，看警告要 warnings，
# 缓存报表要 cache_hits）。CREATE TABLE IF NOT EXISTS 不会给旧库加列，
# 启动时 PRAGMA 探测 + ALTER TABLE 增量迁移。
_SCHEMA_MIGRATIONS = (
    ("input", "TEXT DEFAULT ''"),
    ("output_dir", "TEXT DEFAULT ''"),
    ("warnings", "TEXT DEFAULT '[]'"),
    ("cache_hits", "INTEGER DEFAULT 0"),
    # v0.8.4: 命中段折算批次数（节省报表——前端历史面板「省约 N 次」）
    ("cache_saved_calls", "INTEGER DEFAULT 0"),
)

_SNAP_COLS = ("id", "status", "config_path", "output_path", "error", "stage",
              "pages", "paragraphs", "calls", "elapsed", "created",
              "input", "output_dir", "warnings", "cache_hits",
              "cache_saved_calls")

# SELECT 列序（_row_to_snap 按此取列；progress 是 JSON 列，单独处理）
_SELECT_COLS = ("id", "status", "config_path", "output_path", "error",
                "stage", "progress", "pages", "paragraphs", "calls",
                "elapsed", "created", "updated", "seq",
                "input", "output_dir", "warnings", "cache_hits",
                "cache_saved_calls")

_SELECT_SQL = ("SELECT id,status,config_path,output_path,error,stage,progress,"
               "pages,paragraphs,calls,elapsed,created,updated,seq,"
               "input,output_dir,warnings,cache_hits,cache_saved_calls "
               "FROM jobs")


class JobStore:
    """任务快照的 SQLite 存取（线程安全）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute(_SCHEMA)
        self._migrate()
        self._conn.commit()
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        """v0.5.1: 旧库增量加列（无 ALTER 副作用，幂等）。"""
        have = {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        for col, decl in _SCHEMA_MIGRATIONS:
            if col not in have:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")

    def upsert(self, snap: dict, seq: int | None = None) -> None:
        """写入任务快照（snapshot 字段 → 行）。seq=排队顺序，NULL=非排队态。"""
        prog = snap.get("progress")
        warnings = snap.get("warnings") or []
        row = (snap.get("id"), snap.get("status"), snap.get("config_path"),
               snap.get("output_path") or "", snap.get("error") or "",
               snap.get("stage") or "", json.dumps(prog) if prog else "",
               int(snap.get("pages") or 0), int(snap.get("paragraphs") or 0),
               int(snap.get("calls") or 0), float(snap.get("elapsed") or 0),
               float(snap.get("created") or time.time()), time.time(), seq,
               snap.get("input") or "", snap.get("output_dir") or "",
               json.dumps(warnings, ensure_ascii=False),
               int(snap.get("cache_hits") or 0),
               int(snap.get("cache_saved_calls") or 0))
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO jobs (id,status,config_path,output_path,"
                "error,stage,progress,pages,paragraphs,calls,elapsed,created,"
                "updated,seq,input,output_dir,warnings,cache_hits,"
                "cache_saved_calls) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            self._conn.commit()

    def _row_to_snap(self, row) -> dict:
        idx = {c: i for i, c in enumerate(_SELECT_COLS)}
        snap = {k: row[idx[k]] for k in _SNAP_COLS}
        raw_prog = row[idx["progress"]]
        snap["progress"] = (json.loads(raw_prog) or None) if raw_prog else None
        try:
            snap["warnings"] = json.loads(row[idx["warnings"]] or "[]")
        except (json.JSONDecodeError, TypeError):
            snap["warnings"] = []
        return snap

    def unfinished(self) -> list[dict]:
        """非终态任务快照（按 seq→created 排序，重启恢复队列用）。"""
        with self._lock:
            rows = self._conn.execute(
                _SELECT_SQL + " WHERE status IN ('queued','running','paused') "
                "ORDER BY (seq IS NULL), seq, created").fetchall()
        return [self._row_to_snap(r) for r in rows]

    def history(self, limit: int = 50) -> list[dict]:
        """终态任务快照（最近在前）。"""
        with self._lock:
            rows = self._conn.execute(
                _SELECT_SQL + " WHERE status IN ('done','cancelled','error') "
                "ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_snap(r) for r in rows]

    def prune(self, keep: int = 200) -> int:
        """历史条数超限时删除最旧终态任务（防库无限膨胀）。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM jobs WHERE status IN ('done','cancelled','error') "
                "AND id NOT IN (SELECT id FROM jobs WHERE status IN "
                "('done','cancelled','error') ORDER BY updated DESC LIMIT ?)",
                (keep,))
            n = cur.rowcount
            if n:
                self._conn.commit()
        return max(n, 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
