"""v0.7.1 项目级缓存库：翻译缓存 + 文档指纹索引 + 版面缓存（SQLite 单文件）。

旧形态的两个痛点：
- .translation_cache.db 每输出目录一个——同一输入译到不同输出目录不共享，
  句子级缓存的跨文档复用也被输出目录割裂；
- 版面缓存按「路径+大小+mtime」做 key——文件被复制/移动（内容不变）即失效，
  且同样绑死在输出目录。

本模块把三者收敛为**项目级单库**（默认位于输入文件所在目录，内容寻址）：

    <root>/.pdf_translator_cache/cache.db
      ├─ cache   翻译缓存（复用 TranslationCache，key=engine|model|lang|text）
      ├─ layouts 版面缓存（key=文档指纹|引擎|缓存版本——内容寻址，
      │           同一文档复制/改名后布局仍命中；配合翻译缓存形成
      │           「改 1 页只重译 1 页」的文档增量翻译能力）
      └─ docs    文档指纹索引（指纹 → 路径/大小/mtime/页数，簿记+introspection）

根目录解析（resolve_cache_root）：performance.cache_dir 显式配置 >
输入文件所在目录（可写探测）> 输出目录（兜底，行为等同旧版）。
首次创建时自动迁移同输出目录的旧 .translation_cache.db（best-effort）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .cache import TranslationCache

CACHE_DIR_NAME = ".pdf_translator_cache"


def resolve_cache_root(explicit: str, src: Path, out_dir: Path) -> tuple[Path, str]:
    """缓存根目录解析。返回 (root, 来源标签)（日志用）。"""
    if explicit:
        return Path(explicit).expanduser(), "config"
    cand = src.parent
    try:
        d = cand / CACHE_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_probe"
        probe.write_bytes(b"")     # mkdir 成功不代表可写（只读挂载上已存在目录）
        probe.unlink()
        return cand, "input-dir"
    except OSError:
        return out_dir, "output-dir"


class DocumentCache:
    """项目级缓存库句柄。

    tc:       TranslationCache（翻译缓存表，兼容既有 llm.py 缓存接口）
    load_layout / save_layout: 版面缓存的 DB 化读写（JSON 序列化沿用
    pipeline._layout_cache_encode/_decode 的 Rect 标记格式）
    """

    def __init__(self, root: Path, max_entries: int = 0,
                 legacy_sources: "list[Path] | None" = None,
                 log=None):
        self.root = Path(root)
        self.dir = self.root / CACHE_DIR_NAME
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "cache.db"
        self._log = log or (lambda m: None)
        # 翻译缓存表（沿用 TranslationCache：key 空间/容量淘汰/报表不变）
        self.tc = TranslationCache(self.path, max_entries=max_entries)
        # 文档指纹索引 + 版面缓存表（独立连接，同文件 SQLite 并发安全足够）
        self._conn = sqlite3.connect(self.path, timeout=15.0)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS docs ("
            " fp TEXT PRIMARY KEY, path TEXT, size INTEGER, mtime REAL,"
            " pages INTEGER, updated REAL)")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS layouts ("
            " key TEXT PRIMARY KEY, fp TEXT, engine TEXT, ver INTEGER,"
            " pages INTEGER, data TEXT, updated REAL)")
        self._conn.commit()
        self.migrated = 0
        if legacy_sources:
            self.migrated = self._migrate_legacy(
                [Path(p) for p in legacy_sources if Path(p).is_file()])
        if self.migrated:
            self._log(f"cache: migrated {self.migrated} entr(y|ies) from "
                      f"legacy .translation_cache.db")

    # ---- 文档指纹 ----
    @staticmethod
    def fingerprint(src: Path) -> str:
        """文档内容指纹（md5，流式读取）——路径/复制/改名无关。"""
        h = hashlib.md5()
        with open(src, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    # ---- 版面缓存（DB 化）----
    @staticmethod
    def layout_key(fp: str, engine: str, ver: int, sel: str = "") -> str:
        """版面缓存 key：文档指纹|引擎|版本[|页码子集]。

        sel（io.pages 原始串）：--quick 试译只存选中页的版面，不与
        全量版面互相覆盖。
        """
        base = f"{fp}|{engine}|v{ver}"
        return base + (f"|sel{sel}" if sel else "")

    def load_layout(self, fp: str, engine: str, ver: int,
                    sel: str = "") -> "list[dict] | None":
        try:
            row = self._conn.execute(
                "SELECT data, pages FROM layouts WHERE key=?",
                (self.layout_key(fp, engine, ver, sel),)).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        from .pipeline import _layout_cache_decode
        try:
            layouts = _layout_cache_decode(json.loads(row[0]))
        except Exception:
            return None            # 坏缓存按 miss 处理
        if isinstance(layouts, list) and layouts and all(
                isinstance(l, dict) and "paragraphs" in l for l in layouts):
            return layouts
        return None

    def save_layout(self, fp: str, engine: str, ver: int, src: Path,
                    n_pages: int, layouts: "list[dict]", sel: str = "") -> None:
        from .pipeline import _layout_cache_encode
        key = self.layout_key(fp, engine, ver, sel)
        try:
            data = json.dumps(_layout_cache_encode(layouts), ensure_ascii=False)
            st = src.stat()
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO layouts"
                    " (key, fp, engine, ver, pages, data, updated)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (key, fp, engine, ver, n_pages, data, time.time()))
                self._conn.execute(
                    "INSERT OR REPLACE INTO docs"
                    " (fp, path, size, mtime, pages, updated)"
                    " VALUES (?,?,?,?,?,?)",
                    (fp, str(src), st.st_size, st.st_mtime, n_pages, time.time()))
        except Exception:
            pass                   # 缓存写失败不影响主流程

    # ---- 旧库迁移 ----
    def _migrate_legacy(self, legacy_paths: list[Path]) -> int:
        """同输出目录旧 .translation_cache.db → 项目库（首次且空库时）。

        best-effort：任何失败静默跳过（旧库仍在，行为不回退）。
        """
        try:
            if self.tc.count() > 0:
                return 0           # 项目库已有条目：不重复迁移
        except sqlite3.Error:
            return 0
        total = 0
        for i, lp in enumerate(legacy_paths):
            alias = f"legacy_{i}"
            try:
                self._conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(lp),))
                cur = self._conn.execute(
                    f"INSERT OR IGNORE INTO main.cache (k, src, dst, created) "
                    f"SELECT k, src, dst, created FROM {alias}.cache")
                total += max(cur.rowcount, 0)
                self._conn.execute(f"DETACH DATABASE {alias}")
            except sqlite3.Error:
                continue
        if total:
            self._conn.commit()
        return total

    def close(self) -> None:
        for c in (self.tc, self._conn):
            try:
                c.close()
            except sqlite3.Error:
                pass
