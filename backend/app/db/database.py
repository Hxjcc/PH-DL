"""Async SQLite connection manager with WAL mode."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Optional

import aiosqlite

from .schema import MIGRATIONS, SCHEMA_SQL, SCHEMA_VERSION


def _sql_touches_video_meta(sql: str) -> bool:
    low = (sql or "").lower()
    return "videos" in low or "targets" in low


class Database:
    """Async SQLite wrapper with WAL mode, serialized writer, and dedicated read connection."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._read_conn: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()
        self._video_meta_version = 0

    @property
    def video_meta_version(self) -> int:
        return self._video_meta_version

    def bump_video_meta_version(self) -> None:
        self._video_meta_version += 1

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        try:
            for pragma in (
                "journal_mode=WAL",
                "synchronous=NORMAL",
                "foreign_keys=ON",
                "busy_timeout=10000",
                "journal_size_limit=16777216",
            ):
                async with self.conn.execute(f"PRAGMA {pragma}"):
                    pass
            async with self.conn.executescript(SCHEMA_SQL):
                pass
            await self._run_migrations()
            if str(self.db_path) != ":memory:":
                read_conn = await aiosqlite.connect(str(self.db_path), timeout=30.0, isolation_level=None)
                read_conn.row_factory = aiosqlite.Row
                for pragma in ("query_only=ON", "busy_timeout=10000", "journal_size_limit=16777216"):
                    async with read_conn.execute(f"PRAGMA {pragma}"):
                        pass
                self._read_conn = read_conn
            else:
                self._read_conn = self._conn
                self._read_lock = self._write_lock
        except BaseException:
            await self.close()
            raise

    async def _get_user_version(self) -> int:
        async with self.conn.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def _run_migrations(self) -> None:
        """Apply versioned migrations, keeping existing databases upgradable."""
        current = await self._get_user_version()
        if current == 0:
            # Databases created before version tracking already contain the
            # baseline schema; treat them as version 1.
            current = 1
            await self.execute(f"PRAGMA user_version = {current}")

        for target_version, statements in sorted(MIGRATIONS, key=lambda item: item[0]):
            if target_version <= current:
                continue
            async with self.transaction() as conn:
                for statement in statements:
                    async with conn.execute(statement):
                        pass
                async with conn.execute(f"PRAGMA user_version = {int(target_version)}"):
                    pass
            current = target_version

        if current < SCHEMA_VERSION:
            await self.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")

    async def schema_version(self) -> int:
        return await self._get_user_version()

    async def close(self) -> None:
        read_conn, self._read_conn = self._read_conn, None
        write_conn, self._conn = self._conn, None
        try:
            if read_conn is not None and read_conn is not write_conn:
                await read_conn.close()
        finally:
            if write_conn is not None:
                await write_conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    @property
    def read_conn(self) -> aiosqlite.Connection:
        if self._read_conn is not None:
            return self._read_conn
        return self.conn

    async def shrink_memory(self) -> None:
        async with self._write_lock:
            async with self.conn.execute("PRAGMA shrink_memory"):
                pass
        if self._read_conn is not None and self._read_conn is not self._conn:
            async with self._read_lock:
                async with self.read_conn.execute("PRAGMA shrink_memory"):
                    pass

    async def fetch_one(
        self, sql: str, params: Iterable[Any] = (), *, conn: Optional[aiosqlite.Connection] = None,
    ) -> Optional[dict[str, Any]]:
        if conn is None:
            async with self._read_lock:
                return await self.fetch_one(sql, params, conn=self.read_conn)
        async with conn.execute(sql, tuple(params)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row is not None else None

    async def fetch_all(
        self, sql: str, params: Iterable[Any] = (), *, conn: Optional[aiosqlite.Connection] = None,
    ) -> list[dict[str, Any]]:
        if conn is None:
            async with self._read_lock:
                return await self.fetch_all(sql, params, conn=self.read_conn)
        async with conn.execute(sql, tuple(params)) as cursor:
            return [dict(row) async for row in cursor]

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self._write_lock:
            async with self.conn.execute(sql, tuple(params)) as cursor:
                lastrowid = int(cursor.lastrowid or 0)
            if _sql_touches_video_meta(sql):
                self._video_meta_version += 1
            return lastrowid

    async def execute_returning(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
        async with self._write_lock:
            result = await self.fetch_one(sql, params, conn=self.conn)
            if _sql_touches_video_meta(sql):
                self._video_meta_version += 1
            return result

    async def executemany(self, sql: str, param_seq: Iterable[Iterable[Any]]) -> None:
        touches_meta = _sql_touches_video_meta(sql)
        async with self.transaction(touches_video_meta=touches_meta) as conn:
            async with conn.executemany(sql, (tuple(p) for p in param_seq)):
                pass

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._read_lock:
            yield self.read_conn

    @asynccontextmanager
    async def transaction(self, *, touches_video_meta: bool = True) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            try:
                async with self.conn.execute("BEGIN IMMEDIATE"):
                    pass
                yield self.conn
                await self.conn.commit()
                if touches_video_meta:
                    self._video_meta_version += 1
            except BaseException:
                await self.conn.rollback()
                raise
