"""Regressions that cross the job queue, storage, parsing, and upload policy."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.jobs import retry_failed_items
from app.config import EnvSettings, SelectorSettings
from app.db.database import Database
from app.db.repo import Repository
from app.engine import task_manager as engine
from app.engine.events import EventBus
from app.main import create_app
from app.parsers.http_session import PowFetchResult, TaskHttpContext
from app.parsers.list_scanner import ListedVideo, scan_list_pages
from app.parsers.video import VideoDetail, fetch_single_video_detail
from app.services import downloader
from app.services.cookie import CookieCheckResult
from app.services.downloader import DownloadOutcome
from app.services.filename import video_filename_base
from app.services.uploader import UploadBatchResult, file_snapshot
from tests.media_fixtures import mp4_payload


def page(key: str) -> str:
    return f"https://www.pornhub.com/view_video.php?viewkey={key}"


def detail(key: str, title: str = "Fixture", quality: str = "1080p", **kwargs) -> VideoDetail:
    return VideoDetail(key, title, page(key), page(key), download_url=f"https://cdn.example/{key}.mp4",
                       quality=quality, uploader_username="fixture", **kwargs)


@pytest.fixture
async def job_engine(tmp_path, monkeypatch):
    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    await repo.update_settings_patch({
        "ph_cookie": "sess=fixture", "schedule_enabled": False, "uploader_enabled": True,
        "min_free_disk_gb": 0, "min_file_size_bytes": 64, "page_delay_max": 0,
    })
    manager = engine.TaskManager(env, repo, EventBus())

    async def valid_cookie(*args, **kwargs):
        return CookieCheckResult(valid=True, message="ok")

    async def forbid_external(*args, **kwargs):
        raise AssertionError("Tests must mock external requests")

    monkeypatch.setattr(engine, "check_cookie_valid", valid_cookie)
    monkeypatch.setattr(TaskHttpContext, "fetch_with_pow", forbid_external)
    monkeypatch.setattr(engine, "upload_file_batch", forbid_external)
    monkeypatch.setattr(engine, "download_mp4_with_resume", forbid_external)
    try:
        yield manager, repo, env
    finally:
        await manager.stop()
        await db.close()


async def run_job(manager, repo, kind, **params):
    job = await repo.create_job(kind, "fixture", params)
    await manager._run_job(job["id"])
    return await repo.get_job(job["id"])


async def fixture_download(**args):
    destination = args["download_dir"]
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{video_filename_base(args['title'], args['viewkey'])}.mp4"
    if path.exists():
        path = path.with_suffix(".other.mp4")
    path.write_bytes(mp4_payload(b"b"))
    return DownloadOutcome(True, local_path=path, file_size=path.stat().st_size,
                           quality=args["quality"], download_url=args["download_url"])


async def test_future_tracking_retries_only_opted_in_files(job_engine, monkeypatch):
    manager, repo, env = job_engine
    keys = ["localonly", "optin"]
    file_paths = {}

    async def details(_ctx, items, *args, **kwargs):
        return [detail(url.split("viewkey=")[1]) for url, _, _ in items]

    async def failed_copy(*args, **kwargs):
        return UploadBatchResult(failed_viewkeys=["optin"], error="simulated copy failure")

    monkeypatch.setattr(engine, "fetch_video_details_batch", details)
    monkeypatch.setattr(engine, "download_mp4_with_resume", fixture_download)
    monkeypatch.setattr(engine, "upload_file_batch", failed_copy)

    for key in keys:
        job = await run_job(manager, repo, "manual_urls", urls=[page(key)], auto_upload=(key == "optin"))
        row = await repo.get_video(key)
        assert row["status"] == "downloaded" and row["upload_requested"] == (key == "optin")
        assert job["status"] == ("partial_failed" if key == "optin" else "completed")
        file_paths[key] = Path(row["local_path"])

    # Old releases did not record why this file stayed on disk.
    legacy = env.downloads_dir / "legacy.mp4"
    legacy.write_bytes(mp4_payload())
    await repo.upsert_video("legacy", status="downloaded", local_path=str(legacy))

    async def verify_copy(*, files_by_viewkey, **kwargs):
        assert set(files_by_viewkey) == {"optin"}
        return UploadBatchResult(
            uploaded_viewkeys=["optin"], remote_dir="fixture:cloud",
            verified_sources={key: file_snapshot(path) for key, path in files_by_viewkey.items()},
        )

    monkeypatch.setattr(engine, "upload_file_batch", verify_copy)
    job = await run_job(manager, repo, "auto_update")
    assert job["status"] == "completed" and job["uploaded_items"] == 1
    assert not file_paths["optin"].exists()
    assert (await repo.get_video("optin"))["status"] == "uploaded"
    assert file_paths["localonly"].read_bytes() == mp4_payload(b"b")
    assert legacy.read_bytes() == mp4_payload()


async def test_resume_button_recovers_original_quality_path_and_no_upload(job_engine, monkeypatch):
    manager, repo, env = job_engine
    parent = env.temp_dir / "fixture"
    parent.mkdir()
    part = parent / "Fixture [oldclip].mp4.tmp"
    original = mp4_payload()[:77]
    part.write_bytes(original)
    source = await repo.create_job("manual_urls", "old", {"auto_upload": False})
    await repo.add_job_item(
        source["id"], viewkey="oldclip", title="Fixture", uploader="fixture", target_type="model",
        page_url=page("oldclip"), status="cancelled", quality="1080p", temp_path=str(part),
    )
    await repo.update_job(source["id"], status="cancelled")

    async def download(**kwargs):
        assert kwargs["partial_path"] == part
        assert kwargs["quality"] == "1080p" and kwargs["preferred_qualities"] == {"1080"}
        assert kwargs["replace_existing"] is False
        return await fixture_download(**kwargs)

    monkeypatch.setattr(engine, "download_mp4_with_resume", download)
    job = await run_job(manager, repo, "resume_temp", auto_upload=True)
    assert job["status"] == "completed" and job["uploaded_items"] == 0
    assert part.read_bytes() == original
    assert (await repo.get_video("oldclip"))["upload_requested"] == 0


async def test_failed_details_can_retry_with_original_filters(job_engine, monkeypatch):
    manager, repo, _ = job_engine
    visits = 0

    async def details(_ctx, _items, *args, **kwargs):
        nonlocal visits
        visits += 1
        assert kwargs["include_keywords"] == {"fixture"}
        assert kwargs["exclude_keywords"] == {"reject"}
        assert kwargs["preferred_qualities"] == {"1080"}
        if visits == 1:
            return [VideoDetail("resolve", "", page("resolve"), page("resolve"), error="HTTP 503")]
        return [detail("resolve")]

    monkeypatch.setattr(engine, "fetch_video_details_batch", details)
    monkeypatch.setattr(engine, "download_mp4_with_resume", fixture_download)
    failed = await run_job(manager, repo, "manual_urls", urls=[page("resolve")], auto_upload=False,
                           quality="1080p", keywords="Fixture", exclude_keywords="Reject")
    assert failed["status"] == "partial_failed" and failed["failed_items"] == failed["total_items"] == 1
    rows = await repo.list_job_items(failed["id"])
    assert rows[0]["status"] == "failed" and rows[0]["error_message"] == "HTTP 503"
    retry = (await retry_failed_items(failed["id"], SimpleNamespace(repo=repo, tasks=manager)))["job"]
    await manager._run_job(retry["id"])
    result = await repo.get_job(retry["id"])
    assert visits == 2 and result["status"] == "completed" and result["done_items"] == 1


async def test_duplicate_targets_and_old_quality_do_not_skip_or_overwrite(job_engine, monkeypatch):
    manager, repo, env = job_engine
    folder = env.downloads_dir / "fixture"
    folder.mkdir()
    first = folder / "Fixture [unique].mp4"
    first.write_bytes(mp4_payload(b"a"))
    await repo.upsert_video("unique", title="Fixture", quality="720p", status="downloaded",
                            local_path=str(first), file_size=first.stat().st_size)
    scans, downloads = [], []

    async def scan(_ctx, url, *args, **kwargs):
        scans.append(url)
        return [ListedVideo("unique", page("unique"), "Fixture", 1, 1, 1)]

    async def details(*args, **kwargs):
        return [detail("unique", quality="2160p")]

    async def download(**kwargs):
        downloads.append(kwargs["viewkey"])
        return await fixture_download(**kwargs)

    monkeypatch.setattr(engine, "scan_list_pages", scan)
    monkeypatch.setattr(engine, "fetch_video_details_batch", details)
    monkeypatch.setattr(engine, "download_mp4_with_resume", download)
    result = await run_job(manager, repo, "manual_target", usernames=["fixture", "Fixture", "overlap"],
                           target_type="model", quality="2160", auto_upload=False)
    assert len(scans) == 2 and downloads == ["unique"]
    assert result["status"] == "completed" and result["done_items"] == result["total_items"] == 1
    saved = await repo.get_video("unique")
    assert saved["quality"] == "2160p" and Path(saved["local_path"]) != first
    assert first.read_bytes() == mp4_payload(b"a")


def page_html(keys):
    items = "".join(f'<li class="videoBox" data-video-vkey="{key}"><a href="{page(key)}">{key}</a></li>' for key in keys)
    return f'<ul class="videos row-5-thumbs" id="mostRecentVideosSection">{items}</ul>'


async def test_paginated_failure_keeps_later_pages_and_marks_baseline_incomplete(job_engine, monkeypatch):
    manager, repo, _ = job_engine
    await repo.update_settings_patch({"max_concurrent_list_pages": 4})
    requested = []

    async def fetch(_ctx, url, **kwargs):
        number = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
        requested.append(number)
        status = {2: 503, 4: 404}.get(number, 200)
        html = page_html([f"page{number}"]) if status == 200 else "<h1>Server response</h1>"
        return PowFetchResult(html, url, status, 0, False), None

    monkeypatch.setattr(TaskHttpContext, "fetch_with_pow", fetch)
    target = await repo.upsert_target("fixture")
    result = await run_job(manager, repo, "baseline_sync", target_id=target["id"], pages="all")
    assert set(requested) == {1, 2, 3, 4}
    assert result["status"] == "partial_failed" and result["total_items"] == result["done_items"] == 2
    assert "503" in result["error_message"]
    assert await repo.get_video("page1") and await repo.get_video("page3")
    assert "503" in (await repo.get_target(target["id"]))["last_error"]


async def test_tracking_catches_a_burst_past_homepage_limit(monkeypatch):
    ctx = TaskHttpContext()
    seen = []
    pages = {1: [f"new{i}" for i in range(1, 14)], 2: ["new14", "new15", "known"]}

    async def fetch(url, **kwargs):
        number = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
        seen.append(number)
        return PowFetchResult(page_html(pages.get(number, [])), url, 200, 0, False), None

    monkeypatch.setattr(ctx, "fetch_with_pow", fetch)
    items = await scan_list_pages(
        ctx, "https://www.pornhub.com/model/fixture/videos", SelectorSettings(),
        catch_up=True, skip_vkeys={"known"},
        page_concurrency=1, delay_max=0,
    )
    assert seen == [1, 2]
    assert [item.viewkey for item in items] == [f"new{i}" for i in range(1, 16)]


async def test_tracking_does_not_skip_a_previously_failed_page_when_head_is_known(job_engine, monkeypatch):
    manager, repo, _ = job_engine
    target = await repo.upsert_target("fixture")
    await repo.update_settings_patch({"uploader_enabled": False})
    errored = True
    requests = []

    async def fetch(_ctx, url, **kwargs):
        number = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
        requests.append(number)
        if number == 2 and errored:
            return PowFetchResult("<h1>Unavailable</h1>", url, 503, 0, False), None
        if number > 3:
            return PowFetchResult("", url, 404, 0, False), None
        return PowFetchResult(page_html([f"new{number}"]), url, 200, 0, False), None

    async def details(_ctx, items, *args, **kwargs):
        return [detail(url.split("viewkey=")[1]) for url, _, _ in items]

    monkeypatch.setattr(TaskHttpContext, "fetch_with_pow", fetch)
    monkeypatch.setattr(engine, "fetch_video_details_batch", details)
    monkeypatch.setattr(engine, "download_mp4_with_resume", fixture_download)
    first = await run_job(manager, repo, "auto_update")
    assert first["status"] == "partial_failed"
    assert await repo.get_video("new1") and not await repo.get_video("new2")
    assert "503" in (await repo.get_target(target["id"]))["last_error"]

    errored = False
    requests.clear()
    second = await run_job(manager, repo, "auto_update")
    assert 2 in requests and await repo.get_video("new2")
    assert second["status"] == "completed" and second["done_items"] == 1
    assert not (await repo.get_target(target["id"]))["last_error"]


async def test_verified_copy_does_not_delete_until_its_ledger_update_succeeds(job_engine, monkeypatch):
    manager, repo, _ = job_engine

    async def details(*args, **kwargs):
        return [detail("keepcopy")]

    async def verified_copy(*, files_by_viewkey, **kwargs):
        return UploadBatchResult(
            uploaded_viewkeys=["keepcopy"], remote_dir="fixture:cloud",
            verified_sources={key: file_snapshot(path) for key, path in files_by_viewkey.items()},
        )

    async def failed_ledger_write(*args, **kwargs):
        return False

    monkeypatch.setattr(engine, "fetch_video_details_batch", details)
    monkeypatch.setattr(engine, "download_mp4_with_resume", fixture_download)
    monkeypatch.setattr(engine, "upload_file_batch", verified_copy)
    monkeypatch.setattr(repo, "record_verified_upload", failed_ledger_write)
    job = await run_job(manager, repo, "manual_urls", urls=[page("keepcopy")], auto_upload=True)
    row = await repo.get_video("keepcopy")
    assert job["status"] == "partial_failed" and job["uploaded_items"] == 0
    assert row["status"] == "downloaded" and row["upload_requested"] == 1
    assert Path(row["local_path"]).read_bytes() == mp4_payload(b"b")


async def test_player_can_supply_direct_mp4_without_a_media_interface(monkeypatch):
    ctx = TaskHttpContext()
    href = page("direct")
    data = {
        "video_title": "Fixture", "mediaDefinitions": [
            {"format": "mp4", "quality": "1080", "videoUrl": "https://cdn.example/1080.mp4"},
            {"format": "mp4", "quality": "2160", "videoUrl": "https://cdn.example/2160.mp4"},
        ],
    }

    async def fetch(url, **kwargs):
        assert url == href
        return PowFetchResult(f'<script>var flashvars_1 = {json.dumps(data)};</script>', href, 200, 0, False), None

    monkeypatch.setattr(ctx, "fetch_with_pow", fetch)
    result = await fetch_single_video_detail(ctx, href, SelectorSettings(), preferred_qualities={"1080"})
    assert not result.error and result.download_url == "https://cdn.example/1080.mp4"
    assert result.quality == "1080p"


async def test_queued_work_survives_restart_and_interrupted_scan_can_be_replayed(job_engine, monkeypatch):
    _, repo, env = job_engine
    a = await repo.create_job("manual_urls", "first", {"urls": [page("first")]})
    b = await repo.create_job("manual_urls", "second", {"urls": [page("second")]})
    interrupted = await repo.create_job("manual_urls", "interrupted", {"urls": [page("recover")], "auto_upload": False})
    await repo.update_job(interrupted["id"], status="scanning")

    second_db = Database(env.db_path)
    await second_db.connect()
    second_repo = Repository(second_db)
    restarted = engine.TaskManager(env, second_repo, EventBus())
    processed = []

    async def simulate_run(job_id):
        processed.append(job_id)
        assert await second_repo.claim_queued_job(job_id)
        await second_repo.update_job(job_id, status="completed")

    monkeypatch.setattr(restarted, "_run_job", simulate_run)
    try:
        assert await second_repo.recover_interrupted_jobs() == 1
        assert await restarted.restore_queued_jobs() == 2
        recovered = (await retry_failed_items(interrupted["id"], SimpleNamespace(repo=second_repo, tasks=restarted)))["job"]
        assert recovered["kind"] == "manual_urls"
        assert recovered["params"]["urls"] == [page("recover")]
        assert recovered["params"]["auto_upload"] is False
        restarted.start()
        await asyncio.wait_for(restarted._queue.join(), 2.0)
        assert processed == [a["id"], b["id"], recovered["id"]]
    finally:
        await restarted.stop()
        await second_db.close()


def test_unicode_login_and_logout_revocation_survive_restart(tmp_path):
    password = "测试密码-strong"
    env = EnvSettings(data_dir=tmp_path / "auth", admin_password=password, secret_key="x" * 48)

    def client():
        return TestClient(create_app(env_settings=env))

    with client() as first:
        assert first.post("/api/auth/login", json={"password": "错误"}).status_code == 401
        assert first.post("/api/auth/login", json={"password": password}).status_code == 200
        token = first.cookies["phdl_session"]

    with client() as second:
        second.cookies.set("phdl_session", token)
        assert second.get("/api/auth/me").json()["authenticated"] is True
        with second.websocket_connect("/api/ws") as stream:
            assert stream.receive_json()["type"] == "snapshot"
            assert second.post("/api/auth/logout").status_code == 200
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    stream.receive_json()
            assert closed.value.code == 4401

    with client() as third:
        assert third.get("/api/overview", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        assert third.post("/api/auth/login", json={"password": password}).status_code == 200


async def test_ghost_pending_uploads_are_cleared_without_starving_real_uploads(job_engine, monkeypatch):
    manager, repo, env = job_engine
    ghost_batch = [
        {
            "viewkey": f"ghost_{idx:03d}",
            "title": f"Ghost {idx}",
            "uploader": "alice",
            "status": "downloaded",
            "local_path": str(env.downloads_dir / "alice" / f"missing_{idx}.mp4"),
            "upload_requested": True,
            "downloaded_at": f"2026-01-01T00:00:{idx % 60:02d}+00:00",
        }
        for idx in range(105)
    ]
    await repo.upsert_videos_batch(ghost_batch)

    real_dir = env.downloads_dir / "alice"
    real_dir.mkdir(parents=True, exist_ok=True)
    real_file = real_dir / "Real [vk_real_106].mp4"
    real_file.write_bytes(mp4_payload())
    await repo.upsert_video(
        "vk_real_106",
        title="Real 106",
        uploader="alice",
        status="downloaded",
        local_path=str(real_file),
        upload_requested=True,
        downloaded_at="2026-02-01T00:00:00+00:00",
    )

    uploaded_seen = []

    async def verify_copy(*, files_by_viewkey, **kwargs):
        uploaded_seen.extend(files_by_viewkey.keys())
        return UploadBatchResult(
            uploaded_viewkeys=list(files_by_viewkey.keys()),
            remote_dir="fixture:cloud",
            verified_sources={k: file_snapshot(p) for k, p in files_by_viewkey.items()},
        )

    monkeypatch.setattr(engine, "upload_file_batch", verify_copy)
    job = await run_job(manager, repo, "auto_update")
    assert job["status"] == "completed" and job["uploaded_items"] == 1
    assert uploaded_seen == ["vk_real_106"]
    ghost_row = await repo.get_video("ghost_000")
    assert ghost_row["upload_requested"] == 0 and ghost_row["local_path"] == ""


async def test_dynamic_download_worker_pool_avoids_head_of_line_blocking(job_engine, monkeypatch):
    manager, repo, _ = job_engine
    await repo.update_settings_patch({"uploader_enabled": False, "max_concurrent_downloads": 2})

    async def details(_ctx, items, *args, **kwargs):
        return [detail(url.split("viewkey=")[1]) for url, _, _ in items]

    finished_order = []
    release_slow = asyncio.Event()

    async def timed_download(**kwargs):
        vk = kwargs["viewkey"]
        if vk == "slow0":
            # Hold slow0 until worker 2 proves it stole and completed all 3 fast items.
            await asyncio.wait_for(release_slow.wait(), timeout=5.0)
        finished_order.append(vk)
        if vk != "slow0" and len(finished_order) == 3:
            release_slow.set()
        return await fixture_download(**kwargs)

    monkeypatch.setattr(engine, "fetch_video_details_batch", details)
    monkeypatch.setattr(engine, "download_mp4_with_resume", timed_download)
    urls = [page(k) for k in ("slow0", "fast1", "fast2", "fast3")]
    job = await run_job(manager, repo, "manual_urls", urls=urls, auto_upload=False)
    assert job["status"] == "completed" and job["done_items"] == 4
    # Worker 2 must steal fast2 & fast3 while worker 1 is still busy with slow0!
    assert finished_order[-1] == "slow0"


async def test_prune_old_jobs_retains_active_and_cascades_job_items(tmp_path: Path):
    db = Database(tmp_path / "prune.db")
    await db.connect()
    repo = Repository(db)
    try:
        finished_ids = []
        for idx in range(5):
            j = await repo.create_job("manual_urls", f"done-{idx}", {})
            await repo.add_job_item(
                j["id"], viewkey=f"vk{idx}", title="T", uploader="u", target_type="model", page_url=page(f"vk{idx}")
            )
            await repo.update_job(j["id"], status="completed")
            finished_ids.append(j["id"])
        queued = await repo.create_job("manual_urls", "still-queued", {})

        pruned = await repo.prune_old_jobs(max_finished_jobs=3)
        assert pruned == 2
        assert await repo.get_job(queued["id"]) is not None
        assert await repo.get_job(finished_ids[0]) is None
        assert await repo.list_job_items(finished_ids[0]) == []
        assert await repo.get_job(finished_ids[-1]) is not None
    finally:
        await db.close()


async def test_large_html_parses_via_offload_thread(monkeypatch):
    ctx = TaskHttpContext()
    href = page("bigpage")
    padding = "<!-- " + ("x" * 40_000) + " -->"
    data = {
        "video_title": "Big HTML Page",
        "mediaDefinitions": [{"format": "mp4", "quality": "1080", "videoUrl": "https://cdn.example/big.mp4"}],
    }
    big_html = f"<html><body>{padding}<script>var flashvars_9 = {json.dumps(data)};</script></body></html>"

    async def fetch(url, **kwargs):
        return PowFetchResult(big_html, href, 200, 0, False), None

    monkeypatch.setattr(ctx, "fetch_with_pow", fetch)
    res = await fetch_single_video_detail(ctx, href, SelectorSettings(), uploader_hint=("alice", "model"))
    assert res.error is None and res.title == "Big HTML Page" and res.quality == "1080p"


async def test_resume_temp_cleans_stale_partial_when_already_uploaded(job_engine):
    manager, repo, env = job_engine
    parent = env.temp_dir / "arinafox"
    parent.mkdir(parents=True, exist_ok=True)
    part = parent / "Already Uploaded [donevk1].mp4.tmp"
    part.write_bytes(mp4_payload()[:96])
    downloader._write_checkpoint(
        part,
        downloader.DownloadCheckpoint(
            viewkey="donevk1",
            quality="2160p",
            request_fingerprint="req",
            response_fingerprint="res",
            etag="",
            total=1024,
        ),
    )
    await repo.upsert_video(
        "donevk1",
        title="Already Uploaded",
        uploader="arinafox",
        target_type="model",
        quality="2160p",
        video_page_url=page("donevk1"),
        status="uploaded",
        force_status=True,
    )

    assert len(manager.list_temp_files()) == 1
    job = await run_job(manager, repo, "resume_temp", auto_upload=True)
    assert job["status"] == "completed"
    assert job["skipped_items"] == 1
    assert not part.exists()
    assert downloader.read_download_checkpoint(part) is None
    assert manager.list_temp_files() == []
