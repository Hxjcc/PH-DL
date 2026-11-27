"""Tests for versioned SQLite schema migrations."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from app.db.database import Database
from app.db.repo import Repository
import pytest

from app.db.schema import MIGRATIONS, SCHEMA_SQL, SCHEMA_VERSION


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


async def test_v14_deduplicates_creators_without_losing_history_or_queued_target_references(tmp_path):
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA_SQL)
        for version, statements in MIGRATIONS:
            if version < 14:
                for statement in statements:
                    conn.execute(statement)
        conn.execute("PRAGMA user_version=13")
        conn.executemany(
            "INSERT INTO targets(id, site, username, target_type, enabled, display_name, created_at) VALUES(?, ?, ?, ?, ?, ?, '2026-01-01')",
            [
                (1, "pornhub", "Alice", "model", 0, "Alice Display"),
                (2, "pornhub", "alice", "pornstar", 1, ""),
                (3, "pornhub", "alice", "favorites", 1, ""),
                (4, "xhamster", "alice", "creator", 1, ""),
            ],
        )
        conn.execute("INSERT INTO jobs(kind, params_json, created_at) VALUES('baseline_sync', ?, '2026-01-01')",
                     (json.dumps({"target_id": 1}),))
        conn.execute("INSERT INTO jobs(kind, params_json, created_at) VALUES('auto_update', ?, '2026-01-01')",
                     (json.dumps({"target_ids": [1, 3, 2, 4]}),))
        conn.execute("INSERT INTO videos(viewkey, uploader, created_at) VALUES('legacy', 'Alice', '2026-01-01')")
        conn.commit()

    db = Database(path)
    await db.connect()
    try:
        repo = Repository(db)
        targets = {target["id"]: target for target in await repo.list_targets()}
        assert set(targets) == {2, 3, 4}
        assert targets[2]["enabled"] and targets[2]["display_name"] == "Alice Display"
        assert all(not target["baseline_pending"] for target in targets.values())
        assert (await repo.get_job(1))["params"]["target_id"] == 2
        assert (await repo.get_job(2))["params"]["target_ids"] == [2, 3, 4]
        assert (await repo.get_video("legacy"))["uploader"] == "Alice"
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute("INSERT INTO targets(username, target_type, created_at) VALUES(' ALICE ', 'model', '2026-01-01')")
    finally:
        await db.close()
