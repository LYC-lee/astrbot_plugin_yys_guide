"""SQLite 缓存。

目的有两个：
1. 定时预热把结果先写进来，用户触发时直接读，不用等网络；
2. 同一条攻略不会被重复抓取和重复摘要。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from typing import Iterable

from astrbot.api import logger

from yys_core.models import GuideItem

_SCHEMA = """
CREATE TABLE IF NOT EXISTS guide_items (
    key          TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    item_id      TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    author       TEXT NOT NULL DEFAULT '',
    published    TEXT NOT NULL DEFAULT '',
    published_ts REAL NOT NULL DEFAULT 0,
    digest       TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL DEFAULT '',
    cover        TEXT NOT NULL DEFAULT '',
    meta         TEXT NOT NULL DEFAULT '{}',
    fetched_at   REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_published ON guide_items (published_ts DESC);
CREATE INDEX IF NOT EXISTS idx_items_fetched   ON guide_items (fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_source    ON guide_items (source);
"""

_SELECT_COLS = (
    "source, item_id, title, url, author, published, digest, content, cover, meta"
)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class GuideCache:
    def __init__(self, db_path: str, ttl_minutes: int = 180, max_rows: int = 2000):
        self.db_path = db_path
        self.ttl_seconds = max(60, int(ttl_minutes) * 60)
        self.max_rows = max(100, int(max_rows))
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # ---------------- 生命周期 ----------------

    async def init(self) -> None:
        def _open() -> sqlite3.Connection:
            parent = os.path.dirname(self.db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            conn.commit()
            return conn

        self._conn = await asyncio.to_thread(_open)
        logger.info(f"[yys-guide] 缓存就绪：{self.db_path}")

    async def close(self) -> None:
        if self._conn is not None:
            conn = self._conn
            self._conn = None
            await asyncio.to_thread(conn.close)

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("缓存未初始化")
        return self._conn

    # ---------------- 写入 ----------------

    async def upsert(self, items: Iterable[GuideItem]) -> int:
        rows = [
            (
                item.key,
                item.source,
                item.item_id,
                item.title,
                item.url,
                item.author,
                item.published.isoformat() if item.published else "",
                item.published_ts,
                item.digest,
                item.content,
                item.cover,
                GuideItem.to_row(item)[-1],  # meta 的 json
                time.time(),
            )
            for item in items
        ]
        if not rows:
            return 0

        sql = """
        INSERT INTO guide_items
            (key, source, item_id, title, url, author, published, published_ts,
             digest, content, cover, meta, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            title        = excluded.title,
            url          = excluded.url,
            author       = excluded.author,
            published    = excluded.published,
            published_ts = excluded.published_ts,
            digest       = excluded.digest,
            content      = excluded.content,
            cover        = excluded.cover,
            meta         = excluded.meta,
            fetched_at   = excluded.fetched_at
        """

        def _write() -> int:
            conn = self._require_conn()
            conn.executemany(sql, rows)
            conn.commit()
            return len(rows)

        async with self._lock:
            return await asyncio.to_thread(_write)

    # ---------------- 查询 ----------------

    async def query(
        self,
        keywords: list[str] | None = None,
        limit: int = 5,
        *,
        allow_stale: bool = False,
        only_fresh_seconds: int | None = None,
        sources: list[str] | None = None,
    ) -> list[GuideItem]:
        """按关键词模糊查缓存。allow_stale=False 时只返回 TTL 内的条目。"""
        limit = max(1, int(limit))
        keywords = [k for k in (keywords or []) if k and k.strip()]
        params: list[object] = []
        clauses: list[str] = []

        if not allow_stale:
            cutoff = time.time() - (
                only_fresh_seconds if only_fresh_seconds is not None else self.ttl_seconds
            )
            clauses.append("fetched_at >= ?")
            params.append(cutoff)

        if sources:
            placeholders = ",".join("?" for _ in sources)
            clauses.append(f"source IN ({placeholders})")
            params.extend(sources)

        if keywords:
            or_parts: list[str] = []
            for kw in keywords:
                pattern = f"%{_escape_like(kw)}%"
                or_parts.append(
                    "(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' "
                    "OR digest LIKE ? ESCAPE '\\')"
                )
                params.extend([pattern, pattern, pattern])
            clauses.append("(" + " OR ".join(or_parts) + ")")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT {_SELECT_COLS} FROM guide_items {where} "
            "ORDER BY published_ts DESC, fetched_at DESC LIMIT ?"
        )
        params.append(limit)

        def _read() -> list[GuideItem]:
            conn = self._require_conn()
            cursor = conn.execute(sql, params)
            return [GuideItem.from_row(tuple(row)) for row in cursor.fetchall()]

        async with self._lock:
            return await asyncio.to_thread(_read)

    async def has_fresh(self, keywords: list[str], limit: int = 1) -> bool:
        items = await self.query(keywords, limit=limit)
        return len(items) >= limit

    async def stats(self) -> dict[str, object]:
        def _read() -> dict[str, object]:
            conn = self._require_conn()
            total = conn.execute("SELECT COUNT(*) AS c FROM guide_items").fetchone()["c"]
            cutoff = time.time() - self.ttl_seconds
            fresh = conn.execute(
                "SELECT COUNT(*) AS c FROM guide_items WHERE fetched_at >= ?", (cutoff,)
            ).fetchone()["c"]
            by_source = {
                row["source"]: row["c"]
                for row in conn.execute(
                    "SELECT source, COUNT(*) AS c FROM guide_items GROUP BY source"
                ).fetchall()
            }
            return {"total": total, "fresh": fresh, "by_source": by_source}

        async with self._lock:
            return await asyncio.to_thread(_read)

    # ---------------- 维护 ----------------

    async def purge(self) -> int:
        """清掉超量旧数据，保持表不会无限增长。"""

        def _purge() -> int:
            conn = self._require_conn()
            total = conn.execute("SELECT COUNT(*) AS c FROM guide_items").fetchone()["c"]
            removed = 0
            if total > self.max_rows:
                overflow = total - self.max_rows
                conn.execute(
                    "DELETE FROM guide_items WHERE key IN ("
                    "SELECT key FROM guide_items ORDER BY fetched_at ASC LIMIT ?)",
                    (overflow,),
                )
                removed = overflow
            conn.execute(
                "DELETE FROM guide_items WHERE fetched_at < ?",
                (time.time() - self.ttl_seconds * 12,),
            )
            conn.commit()
            return removed

        async with self._lock:
            return await asyncio.to_thread(_purge)
