"""Async SQLite connection manager with WAL mode."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Optional

import aiosqlite

from .schema import MIGRATIONS, SCHEMA_SQL, SCHEMA_VERSION


class Database:
    """Thin async SQLite wrapper with serialized write lock for safe concurrency."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        try:
            for pragma in ("journal_mode=WAL", "foreign_keys=ON", "busy_timeout=10000", "journal_size_limit=16777216"):
                async with self.conn.execute(f"PRAGMA {pragma}"):
                    pass
            async with self.conn.executescript(SCHEMA_SQL):
                pass
            await self._run_migrations()
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
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def fetch_one(
        self, sql: str, params: Iterable[Any] = (), *, conn: Optional[aiosqlite.Connection] = None,
    ) -> Optional[dict[str, Any]]:
        if conn is None:
            async with self._write_lock:
                return await self.fetch_one(sql, params, conn=self.conn)
        async with conn.execute(sql, tuple(params)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row is not None else None

    async def fetch_all(
        self, sql: str, params: Iterable[Any] = (), *, conn: Optional[aiosqlite.Connection] = None,
    ) -> list[dict[str, Any]]:
        if conn is None:
            async with self._write_lock:
                return await self.fetch_all(sql, params, conn=self.conn)
        async with conn.execute(sql, tuple(params)) as cursor:
            return [dict(row) async for row in cursor]

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self._write_lock:
            async with self.conn.execute(sql, tuple(params)) as cursor:
                return int(cursor.lastrowid or 0)

    async def execute_returning(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
        async with self._write_lock:
            return await self.fetch_one(sql, params, conn=self.conn)

    async def executemany(self, sql: str, param_seq: Iterable[Iterable[Any]]) -> None:
        async with self.transaction() as conn:
            async with conn.executemany(sql, (tuple(p) for p in param_seq)):
                pass

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            yield self.conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            try:
                async with self.conn.execute("BEGIN IMMEDIATE"):
                    pass
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise
