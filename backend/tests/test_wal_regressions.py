"""Concurrent SQLite read, write, and target identity regressions."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.api.videos import list_videos
from app.db.database import Database
from app.db.repo import Repository


@pytest.fixture
async def repo(tmp_path):
    db = Database(tmp_path / "ledger.db")
    await db.connect()
    try:
        yield Repository(db)
    finally:
        await db.close()


@pytest.mark.parametrize("include_meta", [False, True])
async def test_history_count_rows_and_metadata_use_one_snapshot(repo, monkeypatch, include_meta):
    await repo.upsert_video("old", uploader="first", quality="720p")
    original = repo.db.fetch_one
    inserted = False

    async def inject_write(sql, params=(), **kwargs):
        nonlocal inserted
        row = await original(sql, params, **kwargs)
        if "COUNT(*)" in sql and "FROM videos" in sql and not inserted:
            inserted = True
            await repo.upsert_video("new", uploader="second", quality="1080p")
        return row

    monkeypatch.setattr(repo.db, "fetch_one", inject_write)
    response = await asyncio.wait_for(list_videos(
        site="", search="", uploader="", status="", quality="", time_range="",
        sort_by="activity", page=1, page_size=30, include_meta=include_meta,
        container=SimpleNamespace(repo=repo),
    ), 3)
    assert response["total"] == 1 and [item["viewkey"] for item in response["items"]] == ["old"]
    if include_meta:
        assert response["stats"]["total"] == 1
        assert response["facets"]["qualities"] == ["720p"]
    assert (await repo.query_videos())[1] == 2


async def test_job_detail_keeps_job_and_items_in_same_snapshot(repo, monkeypatch):
    job = await repo.create_job("check_updates", "fixture", {})
    jid = job["id"]
    original = repo.get_job
    changed = False

    async def add_item_after_job_read(job_id, **kwargs):
        nonlocal changed
        row = await original(job_id, **kwargs)
        if not changed:
            changed = True
            await repo.add_job_items_batch(jid, [{"viewkey": "new"}])
            await repo.db.execute("UPDATE jobs SET total_items = 1 WHERE id = ?", (jid,))
        return row

    monkeypatch.setattr(repo, "get_job", add_item_after_job_read)
    row, items = await asyncio.wait_for(repo.get_job_detail(jid), 3)
    assert row["total_items"] == 0 and items == []
    row, items = await repo.get_job_detail(jid)
    assert row["total_items"] == 1 and len(items) == 1


async def test_nested_read_scope_reuses_and_does_not_end_outer_snapshot(repo):
    async with repo.db.read() as outer:
        async with repo.db.read() as inner:
            assert inner is outer and inner.in_transaction
        assert outer.in_transaction
    assert not repo.db.read_conn.in_transaction


@pytest.mark.parametrize("operation", ["execute", "execute_returning", "commit"])
async def test_cancelled_sql_with_committed_write_invalidates_cached_metadata(repo, monkeypatch, operation):
    assert (await repo.get_video_stats())["total"] == 0
    assert (await repo.get_video_filter_facets())["qualities"] == []
    version = repo.db.video_meta_version
    committed, release = asyncio.Event(), asyncio.Event()
    sql = "INSERT INTO videos(viewkey, quality, created_at) VALUES('new', '1080p', '2026-01-01')"

    async def wait_after_commit():
        committed.set()
        await release.wait()

    if operation == "execute":
        original_execute = repo.db.conn.execute

        @asynccontextmanager
        async def execute(statement, params=()):
            async with original_execute(statement, params) as cursor:
                yield cursor
            await wait_after_commit()

        monkeypatch.setattr(repo.db.conn, "execute", execute)
        write = repo.db.execute(sql)
    elif operation == "execute_returning":
        original_fetch = repo.db.fetch_one

        async def fetch_one(statement, params=(), **kwargs):
            row = await original_fetch(statement, params, **kwargs)
            if statement == sql + " RETURNING viewkey":
                await wait_after_commit()
            return row

        monkeypatch.setattr(repo.db, "fetch_one", fetch_one)
        write = repo.db.execute_returning(sql + " RETURNING viewkey")
    else:
        original_commit = repo.db.conn.commit

        async def commit():
            await original_commit()
            await wait_after_commit()

        async def transact():
            async with repo.db.transaction() as conn:
                async with conn.execute(sql):
                    pass

        monkeypatch.setattr(repo.db.conn, "commit", commit)
        write = transact()

    task = asyncio.create_task(write)
    await asyncio.wait_for(committed.wait(), 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repo.db.video_meta_version > version
    assert (await repo.get_video_stats())["total"] == 1
    assert (await repo.get_video_filter_facets())["qualities"] == ["1080p"]


async def test_parallel_target_upserts_and_ensure_converge_on_one_identity(repo):
    results = await asyncio.gather(
        repo.upsert_target("Alice", "model"),
        repo.upsert_target("ALICE", "pornstar"),
        repo.ensure_target("alice", "model", display_name="Alice Display"),
    )
    assert results[0]["id"] == results[1]["id"] == results[2][0]["id"]
    assert len(await repo.list_targets()) == 1


async def test_target_rename_migrates_both_ledgers_or_rolls_back_all(repo):
    target = await repo.upsert_target("original", "model")
    await repo.upsert_video("old", uploader="original", target_type="model")
    job = await repo.create_job("manual_urls", "fixture", {})
    await repo.add_job_items_batch(job["id"], [{"viewkey": "old", "uploader": "original", "target_type": "model"}])
    await repo.db.execute("""
        CREATE TRIGGER reject_rename BEFORE UPDATE OF uploader ON job_items
        BEGIN SELECT RAISE(ABORT, 'rename failed'); END
    """)
    with pytest.raises(sqlite3.IntegrityError, match="rename failed"):
        await repo.update_target(target["id"], {"username": "renamed", "target_type": "pornstar"})
    assert (await repo.get_target(target["id"]))["username"] == "original"
    assert (await repo.get_video("old"))["uploader"] == "original"
    await repo.db.execute("DROP TRIGGER reject_rename")
    await repo.update_target(target["id"], {"username": "renamed", "target_type": "pornstar"})
    item = (await repo.get_job_detail(job["id"]))[1][0]
    video = await repo.get_video("old")
    assert (item["uploader"], item["target_type"]) == (video["uploader"], video["target_type"]) == ("renamed", "pornstar")


async def test_display_name_search_is_site_scoped_and_baseline_keeps_local_uploaded_path(repo):
    await repo.upsert_target("same", display_name="Only PornHub")
    await repo.upsert_target("same", "creator", display_name="Only xHamster", site="xhamster")
    await repo.upsert_video("ph", uploader="same", status="uploaded", local_path="/kept.mp4")
    await repo.upsert_video("xhVideo", uploader="SAME", site="xhamster")
    await repo.upsert_video("ph", uploader="same", status="baseline")
    retained = await repo.get_video("ph")
    assert retained["status"] == "uploaded" and retained["local_path"] == "/kept.mp4"
    for search, key in [("Only PornHub", "ph"), ("Only xHamster", "xhVideo")]:
        items, total = await repo.query_videos(search=search)
        assert total == 1 and [item["viewkey"] for item in items] == [key]
    await repo.upsert_video("ph", status="uploaded")
    assert (await repo.get_video("ph"))["local_path"] == ""


async def test_list_videos_uses_snapshot_safe_caches_and_invalidates_on_write(repo, monkeypatch):
    await repo.upsert_target("alice", display_name="Alice Display")
    await repo.upsert_video("v1", uploader="alice", quality="1080p", status="uploaded", published_at="2026-11-20")
    container = SimpleNamespace(repo=repo)

    # First call populates stats and facets caches.
    first = await list_videos(
        site="pornhub", search="", uploader="", status="", quality="", time_range="",
        sort_by="published", page=1, page_size=30, include_meta=True, container=container,
    )
    assert first["total"] == 1 and first["stats"]["total"] == 1 and first["facets"]["qualities"] == ["1080p"]

    # Second call with no writes must serve stats & facets from cache (only 1 SELECT for video rows).
    seen_sql: list[str] = []
    original_fetch_one = repo.db.fetch_one
    original_fetch_all = repo.db.fetch_all

    async def spy_one(sql, params=(), **kwargs):
        seen_sql.append(sql)
        return await original_fetch_one(sql, params, **kwargs)

    async def spy_all(sql, params=(), **kwargs):
        seen_sql.append(sql)
        return await original_fetch_all(sql, params, **kwargs)

    monkeypatch.setattr(repo.db, "fetch_one", spy_one)
    monkeypatch.setattr(repo.db, "fetch_all", spy_all)

    second = await list_videos(
        site="pornhub", search="", uploader="", status="", quality="", time_range="",
        sort_by="published", page=1, page_size=30, include_meta=True, container=container,
    )
    assert second["total"] == 1 and second["stats"]["total"] == 1
    assert not any("COUNT(*)" in sql for sql in seen_sql)
    assert not any("GROUP BY" in sql for sql in seen_sql)

    # Writing a new video invalidates both stats/facets and list_targets count caches.
    targets_before = await repo.list_targets()
    assert targets_before[0]["video_total_count"] == 1
    await repo.upsert_video("v2", uploader="alice", quality="720p", status="downloaded", published_at="2026-11-25")
    third = await list_videos(
        site="pornhub", search="", uploader="", status="", quality="", time_range="",
        sort_by="published", page=1, page_size=30, include_meta=True, container=container,
    )
    assert [it["viewkey"] for it in third["items"]] == ["v2", "v1"]
    assert third["total"] == 2 and third["stats"]["total"] == 2 and third["facets"]["qualities"] == ["1080p", "720p"]
    targets_after = await repo.list_targets()
    assert targets_after[0]["video_total_count"] == 2


async def test_list_videos_recovers_when_write_races_cached_stats_before_snapshot_pin(repo, monkeypatch):
    await repo.upsert_video("v1", uploader="alice", quality="720p", status="uploaded")
    container = SimpleNamespace(repo=repo)
    # Warm stats/facets cache at v1.
    await list_videos(
        site="", search="", uploader="", status="", quality="", time_range="",
        sort_by="activity", page=1, page_size=30, include_meta=True, container=container,
    )

    original_query = repo.query_videos
    raced = False

    async def query_after_concurrent_write(**kwargs):
        nonlocal raced
        if not raced:
            raced = True
            await repo.upsert_video("v2", uploader="bob", quality="1080p", status="uploaded")
        return await original_query(**kwargs)

    monkeypatch.setattr(repo, "query_videos", query_after_concurrent_write)
    res = await list_videos(
        site="", search="", uploader="", status="", quality="", time_range="",
        sort_by="activity", page=1, page_size=30, include_meta=True, container=container,
    )
    assert res["total"] == 2 and len(res["items"]) == 2
    assert res["stats"]["total"] == 2 and res["facets"]["qualities"] == ["1080p", "720p"]
