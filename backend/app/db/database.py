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
        self._conn = await aiosqlite.connect(str(self.db_path), timeout=30.0)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.execute("PRAGMA busy_timeout=10000;")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()
        await self._run_migrations()

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
            await self.conn.execute(f"PRAGMA user_version = {current}")
            await self.conn.commit()

        for target_version, statements in sorted(MIGRATIONS, key=lambda item: item[0]):
            if target_version <= current:
                continue
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    await self.conn.execute(statement)
                await self.conn.execute(f"PRAGMA user_version = {int(target_version)}")
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise
            current = target_version

        if current < SCHEMA_VERSION:
            await self.conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
            await self.conn.commit()

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

    async def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
        async with self.conn.execute(sql, tuple(params)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row is not None else None

    async def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        async with self.conn.execute(sql, tuple(params)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self._write_lock:
            cursor = await self.conn.execute(sql, tuple(params))
            await self.conn.commit()
            return int(cursor.lastrowid or 0)

    async def execute_returning(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
        async with self._write_lock:
            async with self.conn.execute(sql, tuple(params)) as cursor:
                row = await cursor.fetchone()
            await self.conn.commit()
            return dict(row) if row is not None else None

    async def executemany(self, sql: str, param_seq: Iterable[Iterable[Any]]) -> None:
        async with self._write_lock:
            await self.conn.executemany(sql, [tuple(p) for p in param_seq])
            await self.conn.commit()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise
