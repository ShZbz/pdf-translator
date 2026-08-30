"""SQLite 翻译缓存。key = MD5(engine|model|lang_pair|text)。

SCHEME: 二次运行 0 调用（验收清单 #2）。
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path


class TranslationCache:
    def __init__(self, db_path: str | Path, max_entries: int = 0):
        """max_entries: v0.4.3 容量上限（0=不限制）。

        超出时按 created/rowid 淘汰最旧条目（翻译缓存的价值随时间衰减：
        换模型/改提示词后旧条目 key 不同自然失效，容量控制只防长年
        累积把 .translation_cache.db 撑到 GB 级）。
        """
        self.db_path = str(db_path)
        self.max_entries = max(0, int(max_entries))
        self._put_count = 0
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache ("
            " k TEXT PRIMARY KEY,"
            " src TEXT NOT NULL,"
            " dst TEXT NOT NULL,"
            " created REAL DEFAULT (datetime('now')))"
        )
        self._conn.commit()

    @staticmethod
    def make_key(engine: str, model: str, src_lang: str, tgt_lang: str, text: str) -> str:
        h = hashlib.md5(f"{engine}|{model}|{src_lang}|{tgt_lang}|{text}".encode("utf-8"))
        return h.hexdigest()

    def get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT dst FROM cache WHERE k=?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, src: str, dst: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO cache (k, src, dst, created) "
            "VALUES (?,?,?, datetime('now'))",
            (key, src, dst))
        self._conn.commit()
        # v0.4.3: 每 256 次写入检查一次容量（避免每次 put 都 COUNT 全表）
        self._put_count += 1
        if self.max_entries and self._put_count % 256 == 0:
            self.prune()

    def put_many(self, rows: "list[tuple[str, str, str]]") -> None:
        """v0.8.1: 批量写（单事务单 commit）。

        旧版逐条 put——每条一个 commit（SQLite 每 commit 一次 fsync），
        新文档数百段就是数百次 fsync。调用方按批聚合后整事务落盘，
        中断语义：事务原子（要么全进要么全无，不留半批）。
        """
        if not rows:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO cache (k, src, dst, created) "
                "VALUES (?,?,?, datetime('now'))",
                rows)
        self._put_count += len(rows)
        if self.max_entries and self._put_count % 256 < len(rows):
            self.prune()

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]

    def prune(self) -> int:
        """淘汰超出容量的最旧条目，返回删除行数。"""
        if not self.max_entries:
            return 0
        cur = self._conn.execute(
            "DELETE FROM cache WHERE k IN ("
            " SELECT k FROM cache ORDER BY created DESC, rowid DESC"
            " LIMIT -1 OFFSET ?)", (self.max_entries,))
        n = cur.rowcount
        if n:
            self._conn.commit()
        return max(n, 0)

    def close(self) -> None:
        if self.max_entries:
            try:
                self.prune()
            except sqlite3.Error:
                pass
        self._conn.close()
