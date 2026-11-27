"""Async SQLite connection manager with WAL mode."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Iterable, Optional, TypeVar

import aiosqlite

from .schema import MIGRATIONS, SCHEMA_SQL, SCHEMA_VERSION

T = TypeVar("T")


async def _settle_sql(operation: Awaitable[T]) -> T:
    """Keep the connection locked until SQLite has finished queued work."""
    task = asyncio.ensure_future(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


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
        self._read_owner: Optional[asyncio.Task] = None
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
        async def write() -> int:
            async with self.conn.execute(sql, tuple(params)) as cursor:
                return int(cursor.lastrowid or 0)

        async with self._write_lock:
            try:
                return await _settle_sql(write())
            finally:
                if _sql_touches_video_meta(sql):
                    self.bump_video_meta_version()

    async def execute_returning(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
        async with self._write_lock:
            try:
                return await _settle_sql(self.fetch_one(sql, params, conn=self.conn))
            finally:
                if _sql_touches_video_meta(sql):
                    self.bump_video_meta_version()

    async def executemany(self, sql: str, param_seq: Iterable[Iterable[Any]]) -> None:
        touches_meta = _sql_touches_video_meta(sql)
        async with self.transaction(touches_video_meta=touches_meta) as conn:
            async with conn.executemany(sql, (tuple(p) for p in param_seq)):
                pass

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        owner = asyncio.current_task()
        if owner is not None and self._read_owner is owner:
            yield self.read_conn
            return
        async with self._read_lock:
            conn = self.read_conn
            self._read_owner = owner
            try:
                if conn is self.conn or conn.isolation_level is not None or conn.in_transaction:
                    yield conn
                else:
                    try:
                        async with conn.execute("BEGIN DEFERRED"):
                            pass
                        yield conn
                    finally:
                        if conn.in_transaction:
                            await _settle_sql(conn.rollback())
            finally:
                self._read_owner = None

    @asynccontextmanager
    async def transaction(self, *, touches_video_meta: bool = True) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            try:
                async with self.conn.execute("BEGIN IMMEDIATE"):
                    pass
                yield self.conn
                await _settle_sql(self.conn.commit())
            except BaseException:
                await _settle_sql(self.conn.rollback())
                raise
            finally:
                if touches_video_meta:
                    self.bump_video_meta_version()
