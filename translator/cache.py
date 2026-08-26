"""SQLite 翻译缓存。key = MD5(engine|model|lang_pair|text)。

SCHEME: 二次运行 0 调用（验收清单 #2）。
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path


class TranslationCache:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
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
            "INSERT OR REPLACE INTO cache (k, src, dst) VALUES (?,?,?)",
            (key, src, dst))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
