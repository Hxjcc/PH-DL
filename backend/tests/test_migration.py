"""Tests for versioned SQLite schema migrations."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from app.db.database import Database
from app.db.repo import Repository
from app.db.schema import SCHEMA_SQL, SCHEMA_VERSION


async def test_database_applies_versioned_index_migration(tmp_path: Path):
    db_path = tmp_path / "migrate.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

    db = Database(db_path)
    await db.connect()
    try:
        assert await db.schema_version() == SCHEMA_VERSION
        row = await db.fetch_one(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_videos_status_local_path'"
        )
        assert row is not None
        columns = await db.fetch_all("PRAGMA table_info(targets)")
        column_names = {column["name"] for column in columns}
        assert "avatar_url" in column_names
        assert "display_name" in column_names
    finally:
        await db.close()


async def test_schema_upgrade_preserves_existing_download_and_defaults_to_no_upload(tmp_path: Path):
    from app.db.schema import MIGRATIONS, SCHEMA_SQL, SCHEMA_VERSION
    from tests.media_fixtures import mp4_payload

    path = tmp_path / "phdl.db"
    local = tmp_path / "legacy.mp4"
    partial = tmp_path / "legacy.mp4.tmp"
    local.write_bytes(mp4_payload())
    partial.write_bytes(mp4_payload()[:77])
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA_SQL)
        for version, statements in MIGRATIONS:
            if version <= 4:
                for statement in statements:
                    conn.execute(statement)
        conn.execute("PRAGMA user_version=4")
        conn.execute(
            "INSERT INTO videos (viewkey,status,local_path,created_at) VALUES (?,?,?,?)",
            ("legacy", "downloaded", str(local), "2026-01-01"),
        )
        conn.commit()

    db = Database(path)
    await db.connect()
    try:
        repo = Repository(db)
        assert await db.schema_version() == SCHEMA_VERSION
        row = await repo.get_video("legacy")
        assert row["local_path"] == str(local) and row["status"] == "downloaded" and row["upload_requested"] == 0
        assert await repo.list_pending_upload_videos() == []
        assert local.read_bytes() == mp4_payload() and partial.read_bytes() == mp4_payload()[:77]
    finally:
        await db.close()
