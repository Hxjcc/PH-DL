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
from app.api.targets import TargetBatchCreateRequest, TargetCreateRequest, batch_create_targets, create_target
from app.cancel import StopRequested
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
        "min_free_disk_gb": 0, "min_file_size_bytes": 64, "page_delay_min": 0, "page_delay_max": 0,
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


@pytest.mark.parametrize("batch", [False, True])
async def test_create_target_commits_pending_baseline_before_enqueue(job_engine, batch):
    manager, repo, _ = job_engine
    seen = []

    async def enqueue(**kwargs):
        target = await repo.get_target(kwargs["params"]["target_id"])
        seen.append(target)
        return await repo.create_job(kwargs["kind"], kwargs["label"], kwargs["params"])

    container = SimpleNamespace(repo=repo, tasks=SimpleNamespace(enqueue_job=enqueue), events=manager.events)
    if batch:
        await batch_create_targets(TargetBatchCreateRequest(
            items=[{"username": "initializing", "target_type": "model"}], init_baseline=True,
        ), container)
    else:
        await create_target(TargetCreateRequest(username="initializing", target_type="model", init_baseline=True), container)
    assert len(seen) == 1 and seen[0]["baseline_pending"]
    assert await manager._update_check_targets({}) == []
    assert [target["id"] for target in await manager._update_check_targets({"target_ids": [seen[0]["id"]]})] == [seen[0]["id"]]


@pytest.mark.parametrize("first_result", ["failed", "cancelled", "completed"])
async def test_baseline_releases_pending_target_only_after_success(job_engine, monkeypatch, first_result):
    manager, repo, _ = job_engine
    target = await repo.upsert_target("initializing", init_baseline=True)
    scan_result = first_result

    async def scan(ctx, _list_url, _selectors, **options):
        if scan_result == "failed":
            options["on_error"]("[PornHub 扫描] 列表第 1 页: HTTP 503")
            return []
        if scan_result == "cancelled":
            ctx.cancel_event.set()
            return []
        return [ListedVideo("baseline", "https://example.org/view_video.php?viewkey=baseline", "Fixture", 1, 1, 1)]

    monkeypatch.setattr(engine, "scan_list_pages", scan)
    first = await run_job(manager, repo, "baseline_sync", target_id=target["id"])
    assert first["status"] == ("partial_failed" if first_result == "failed" else first_result)
    assert (await repo.get_target(target["id"]))["baseline_pending"] == (first_result != "completed")
    if first_result != "completed":
        scan_result = "completed"
        assert (await run_job(manager, repo, "baseline_sync", target_id=target["id"]))["status"] == "completed"
    assert (await repo.get_target(target["id"]))["baseline_pending"] is False
    assert (await repo.get_video("baseline"))["video_page_url"] == page("baseline")
    assert not (await repo.upsert_target("INITIALIZING", init_baseline=True))["baseline_pending"]


async def test_skipping_reusable_local_file_preserves_upload_intent(job_engine, monkeypatch):
    manager, repo, env = job_engine
    source = env.downloads_dir / "held.mp4"
    source.write_bytes(mp4_payload())
    await repo.upsert_video(
        "held", status="downloaded", quality="1080p", uploader="fixture",
        local_path=str(source), file_size=source.stat().st_size, upload_requested=True,
    )

    async def parse(*args, **kwargs):
        return [detail("held")]

    monkeypatch.setattr(engine, "fetch_video_details_batch", parse)
    job = await run_job(manager, repo, "manual_urls", urls=[page("held")], auto_upload=False)
    assert job["status"] == "completed" and job["skipped_items"] == 1
    assert (await repo.get_video("held"))["upload_requested"] == 1


@pytest.mark.parametrize("resume", [False, True])
async def test_compensation_validates_ledger_size_and_never_uploads_replaced_file(job_engine, monkeypatch, resume):
    manager, repo, env = job_engine
    await repo.update_settings_patch({"ph_cookie": ""})
    source = env.downloads_dir / "replaced.mp4"
    source.write_bytes(mp4_payload())
    wrong_size = source.stat().st_size + 2
    url = "https://xhamster.com/videos/xhClip01"
    await repo.upsert_video(
        "xhClip01", site="xhamster", status="downloaded", quality="1080p", video_page_url=url,
        local_path=str(source), file_size=wrong_size, upload_requested=True,
    )
    saw_download = []

    async def no_download(**kwargs):
        saw_download.append(kwargs["viewkey"])
        return DownloadOutcome(False, error="写入测试替身")

    async def unexpected_cookie(*args, **kwargs):
        pytest.fail("Purely local compensation must not validate a PornHub cookie")

    monkeypatch.setattr(engine, "download_mp4_with_resume", no_download)
    monkeypatch.setattr(engine, "check_cookie_valid", unexpected_cookie)
    if resume:
        job = await repo.create_job("auto_update", "fixture", {"resume_mode": "items"})
        await repo.add_job_items_batch(job["id"], [{
            "viewkey": "xhClip01", "site": "xhamster", "page_url": url,
            "local_path": str(source), "quality": "1080p", "upload_requested": True,
        }])
        await manager._run_job(job["id"])
        job = await repo.get_job(job["id"])
    else:
        job = await run_job(manager, repo, "auto_update")
    assert job["status"] == "partial_failed" and not job["uploaded_items"]
    assert saw_download == ["xhClip01"]
    item = (await repo.list_job_items(job["id"]))[0]
    assert item["bytes_total"] == (0 if resume else wrong_size)
    assert source.exists() and (await repo.get_video("xhClip01"))["status"] == "downloaded"


async def test_empty_updates_and_xhamster_local_compensation_need_no_pornhub_cookie(job_engine, monkeypatch):
    manager, repo, env = job_engine
    await repo.update_settings_patch({"ph_cookie": ""})

    async def unexpected_cookie(*args, **kwargs):
        pytest.fail("No remote PornHub scan was requested")

    monkeypatch.setattr(engine, "check_cookie_valid", unexpected_cookie)
    assert (await run_job(manager, repo, "check_updates"))["status"] == "completed"
    path = env.downloads_dir / "local.mp4"
    path.write_bytes(mp4_payload())
    await repo.upsert_video(
        "xh_1234", site="xhamster", status="downloaded", local_path=str(path),
        file_size=path.stat().st_size, upload_requested=True,
    )

    async def upload(**kwargs):
        assert kwargs["site"] == "xhamster"
        return UploadBatchResult(uploaded_viewkeys=["xh_1234"], verified_sources={"xh_1234": file_snapshot(path)})

    monkeypatch.setattr(engine, "upload_file_batch", upload)
    job = await run_job(manager, repo, "auto_update")
    assert job["status"] == "completed" and job["uploaded_items"] == 1
    assert (await repo.list_job_items(job["id"]))[0]["page_url"] == "https://xhamster.com/videos/video-1234"


@pytest.mark.parametrize("cancel", [False, True])
async def test_parallel_update_reaps_sibling_scans(job_engine, monkeypatch, cancel):
    manager, repo, _ = job_engine
    await repo.upsert_target("alpha")
    await repo.upsert_target("beta")
    sibling_started, sibling_stopped = asyncio.Event(), asyncio.Event()
    ctx = TaskHttpContext()
    job = await repo.create_job("check_updates", "fixture", {})

    async def scan(_job_id, *, list_url, **kwargs):
        if "/alpha/" in list_url:
            await sibling_started.wait()
            if cancel:
                ctx.cancel_event.set()
            return [], []
        sibling_started.set()
        try:
            await asyncio.Future()
        finally:
            sibling_stopped.set()

    async def fail_write(*args, **kwargs):
        raise RuntimeError("fixture write failure")

    monkeypatch.setattr(manager, "_scan_target_with_details", scan)
    if not cancel:
        monkeypatch.setattr(repo, "mark_target_checked", fail_write)
    with pytest.raises(StopRequested if cancel else RuntimeError):
        await asyncio.wait_for(manager._exec_update_check(
            job["id"], {}, await repo.get_settings(), ctx, auto_download=False,
        ), 3)
    assert sibling_stopped.is_set()


@pytest.mark.parametrize(("site", "prefix"), [("pornhub", "PornHub"), ("xhamster", "xHamster")])
async def test_page_one_failure_keeps_catch_up_mode(job_engine, monkeypatch, site, prefix):
    manager, repo, _ = job_engine
    target = await repo.upsert_target("fixture", site=site)
    await repo.mark_target_checked(target["id"], 0, error=f"[{prefix} 扫描] 列表第 1 页: HTTP 503")
    modes = []

    async def scan(*args, **kwargs):
        modes.append(kwargs["catch_up"])
        return [], []

    monkeypatch.setattr(manager, "_scan_target_with_details", scan)
    assert (await run_job(manager, repo, "check_updates"))["status"] == "completed"
    await repo.mark_target_checked(target["id"], 0, error=f"[{prefix} 扫描] 列表第 1 页: HTTP 503；2 个视频解析失败")
    assert (await run_job(manager, repo, "check_updates"))["status"] == "completed"
    assert modes == [True, False]


async def test_enrich_job_only_fetches_candidates_from_requested_site(job_engine, monkeypatch):
    manager, repo, _ = job_engine
    await repo.upsert_video("unscoped", site="pornhub")
    await repo.upsert_video("xhFixture1", site="xhamster")
    visited = []

    async def parse(ctx, items, *args, **kwargs):
        assert ctx.site == "xhamster"
        [url] = [item[0] for item in items]
        visited.append(url)
        return [VideoDetail("xhFixture1", "Fixture", url, url, download_url="https://media.xhcdn.com/a.m3u8", quality="720p")]

    monkeypatch.setattr(engine, "fetch_video_details_batch", parse)
    assert await manager._job_site_requirements("enrich_metadata", {"site": "xhamster"}) == (False, False, True)
    job = await run_job(manager, repo, "enrich_metadata", site="xhamster")
    assert job["status"] == "completed" and visited == ["https://xhamster.com/videos/xhFixture1"]
    assert (await repo.get_video("xhFixture1"))["quality"] == "720p"
    assert await repo.count_baseline_metadata_candidates("pornhub") == 1


async def test_task_cookie_validation_cannot_publish_after_proxy_changes(job_engine, monkeypatch):
    manager, repo, _ = job_engine
    started, finish = asyncio.Event(), asyncio.Event()

    async def stale_result(*args, **kwargs):
        started.set()
        await finish.wait()
        return CookieCheckResult(True, "valid", "stale-user")

    monkeypatch.setattr(engine, "check_cookie_valid", stale_result)
    request = asyncio.create_task(manager._ensure_valid_cookie(await repo.get_settings(), 1))
    await asyncio.wait_for(started.wait(), 3)
    await repo.update_settings_patch({"proxy_url": "http://127.0.0.1:5432", "ph_logged_in_username": "current-user"})
    finish.set()
    with pytest.raises(RuntimeError, match="代理设置已更新"):
        await request
    assert manager.cookie_status["valid"] is None
    assert (await repo.get_settings()).ph_logged_in_username == "current-user"


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
    assert retry["id"] == failed["id"]
    await manager._run_job(retry["id"])
    result = await repo.get_job(retry["id"])
    assert visits == 2 and result["status"] == "completed" and result["done_items"] == 1


async def test_resume_items_preserves_identity_and_completed_rows(job_engine, monkeypatch):
    manager, repo, env = job_engine
    await repo.update_settings_patch({"max_concurrent_downloads": 1})
    original_params = {
        "site": "pornhub", "usernames": ["fixture"], "target_type": "model", "auto_upload": False,
        "redownload": True, "quality": None, "profile_avatars": {"fixture": "https://cdn.example/avatar.jpg"},
        "profile_names": {"fixture": "Fixture Creator"},
    }
    job = await repo.create_job("manual_target", "下载目标: fixture", original_params)
    job_id = job["id"]
    rows = {}
    for key, status in [("done", "done"), ("skip", "skipped"), ("local", "downloaded"),
                        ("unparsed", "cancelled"), ("partial", "failed")]:
        rows[key] = await repo.add_job_item(
            job_id, viewkey=key, title="Fixture", uploader="fixture", target_type="model", page_url=page(key),
            status=status, upload_requested=True, error_message="broken" if status in {"failed", "cancelled"} else "",
            download_url=f"https://cdn.example/{key}.mp4" if key == "partial" else "", quality="1080p",
        )
    await repo.update_job(job_id, status="partial_failed", error_message="previous error",
                          started_at="2026-01-01", finished_at="2026-01-02")
    newer = await repo.create_job("manual_urls", "newer history", {})
    await repo.update_job(newer["id"], status="completed")
    checkpoint = env.temp_dir / "fixture" / "Fixture [partial].mp4.tmp"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"checkpoint")

    downloaded = []

    async def tracked_download(**kwargs):
        downloaded.append((kwargs["viewkey"], kwargs["partial_path"]))
        return await fixture_download(**kwargs)

    async def details(_ctx, items, *args, **kwargs):
        assert [url for url, _, _ in items] == [page("unparsed")]
        return [detail("unparsed")]

    monkeypatch.setattr(engine, "download_mp4_with_resume", tracked_download)
    monkeypatch.setattr(engine, "fetch_video_details_batch", details)
    events = manager.events.subscribe()
    queued = (await retry_failed_items(job_id, SimpleNamespace(repo=repo, tasks=manager)))["job"]
    assert queued["id"] == job_id and queued["kind"] == job["kind"] and queued["label"] == job["label"]
    assert queued["params"] == {**original_params, "resume_count": 1, "resume_mode": "items"}
    assert queued["status"] == "queued" and queued["started_at"] is None and queued["finished_at"] is None
    assert queued["error_message"] == "" and queued["failed_items"] == 0 and queued["done_items"] == 2
    assert [row["id"] for row in await repo.list_jobs(limit=1)] == [job_id]
    assert {events.get_nowait()["type"] for _ in range(events.qsize())} >= {
        "job_updated", "job_items_changed", "overview_changed",
    }
    pending = await repo.get_job_item(rows["partial"]["id"])
    assert (pending["status"], pending["error_message"]) == ("pending", "")
    assert (pending["temp_path"], pending["bytes_downloaded"]) == (str(checkpoint), checkpoint.stat().st_size)

    manager.start()
    await asyncio.wait_for(manager._queue.join(), 5)
    after = {row["viewkey"]: row for row in await repo.list_job_items(job_id)}
    assert downloaded == [("partial", checkpoint), ("unparsed", None)]
    assert {key: row["id"] for key, row in after.items()} == {key: row["id"] for key, row in rows.items()}
    assert all(after[key] == rows[key] for key in ("done", "skip", "local"))
    assert after["partial"]["status"] == after["unparsed"]["status"] == "downloaded"
    final = await repo.get_job(job_id)
    assert final["status"] == "completed" and final["params"] == {**original_params, "resume_count": 1}
    assert (final["total_items"], final["done_items"], final["uploaded_items"], final["skipped_items"]) == (5, 4, 1, 1)
    assert {row["id"] for row in await repo.list_jobs()} == {job_id, newer["id"]}
    assert (await manager.resume_job_in_place(job_id))[2] == 400


@pytest.mark.parametrize("opt_in", [True, None])
async def test_resume_forced_download_uploads_valid_own_file_for_pending_site(job_engine, monkeypatch, opt_in):
    manager, repo, env = job_engine
    xh_url = "https://xhamster.com/videos/fixture-xhClip01"
    job = await repo.create_job("manual_urls", "Mixed links", {
        "urls": [page("done"), xh_url], "redownload": True, "auto_upload": opt_in,
    })
    job_id = job["id"]
    local = env.downloads_dir / "Fixture [xhClip01].mp4"
    local.write_bytes(mp4_payload())
    complete = await repo.add_job_item(
        job_id, viewkey="done", title="Done", uploader="fixture", target_type="model", page_url=page("done"),
        status="downloaded", upload_requested=False,
    )
    pending = await repo.add_job_item(
        job_id, site="xhamster", viewkey="xhClip01", title="Fixture", uploader="fixture", target_type="creator",
        page_url=xh_url, status="failed", local_path=str(local), quality="1080p",
    )
    await repo.update_job(job_id, status="partial_failed")
    uploaded = []

    async def forbidden(*args, **kwargs):
        raise AssertionError("Resume should upload the existing xHamster file without a download or PornHub request")

    async def verify_copy(*, files_by_viewkey, **kwargs):
        assert files_by_viewkey == {"xhClip01": local}
        uploaded.extend(files_by_viewkey)
        return UploadBatchResult(
            uploaded_viewkeys=list(files_by_viewkey), remote_dir="fixture:cloud",
            verified_sources={"xhClip01": file_snapshot(local)},
        )

    monkeypatch.setattr(engine, "check_cookie_valid", forbidden)
    monkeypatch.setattr(engine, "fetch_video_details_batch", forbidden)
    monkeypatch.setattr(engine, "download_hls_to_mp4", forbidden)
    monkeypatch.setattr(engine, "upload_file_batch", verify_copy)
    queued, _, status = await manager.resume_job_in_place(job_id)
    assert status == 200
    assert await manager._job_site_requirements(job["kind"], queued["params"], job_id=job_id) == (False, False, True)
    await manager._run_job(job_id)
    assert uploaded == ["xhClip01"]
    assert (await repo.get_job(job_id))["status"] == "completed"
    assert (await repo.get_job_item(pending["id"]))["status"] == "done"
    assert await repo.get_job_item(complete["id"]) == complete


@pytest.mark.parametrize(("kind", "interrupted"), [
    ("manual_target", False), ("manual_target", True), ("manual_urls", False), ("auto_update", False),
])
async def test_resume_scan_keeps_existing_items_and_adds_each_new_video_once(job_engine, monkeypatch, kind, interrupted):
    manager, repo, _ = job_engine
    target = await repo.upsert_target("fixture", "model", enabled=True)
    keys = ["complete", "waiting", "fresh"]
    params = {"site": "pornhub", "auto_upload": False, "redownload": True}
    if kind == "manual_target":
        params.update(usernames=["fixture"], target_type="model")
    elif kind == "manual_urls":
        params.update(urls=[page(key) for key in keys] + [page("fresh")])
    else:
        params["target_ids"] = [target["id"]]
    job = await repo.create_job(kind, "Original scan", params)
    done = await repo.add_job_item(
        job["id"], viewkey="complete", title="Fixture", uploader="fixture", target_type="model",
        page_url=page("complete"), status="done",
    )
    waiting = await repo.add_job_item(
        job["id"], viewkey="waiting", title="Fixture", uploader="fixture", target_type="model",
        page_url=page("waiting"), download_url="https://cdn.example/waiting.mp4", status="failed", quality="1080p",
    )
    if interrupted:
        await repo.update_job(job["id"], status="scanning")
        assert await repo.recover_interrupted_jobs() == 1
    else:
        reason = "1 个目标检查失败" if kind == "auto_update" else "2 页扫描未完成"
        await repo.update_job(job["id"], status="partial_failed", error_message=reason)
    downloads = []

    async def tracked_download(**kwargs):
        downloads.append(kwargs["viewkey"])
        return await fixture_download(**kwargs)

    async def scan(*args, **kwargs):
        assert kwargs["skip_vkeys"] >= {"complete", "waiting"}
        return [ListedVideo(key, page(key), "Fixture", 1, idx, 4)
                for idx, key in enumerate([*keys, "fresh"], start=1)]

    async def scan_with_details(*args, **kwargs):
        assert kwargs["catch_up"] is False
        return await scan(*args, **kwargs), [detail(key) for key in [*keys, "fresh"]]

    async def details(_ctx, items, *args, **kwargs):
        assert [url for url, _, _ in items] == [page("fresh")]
        return [detail("fresh")]

    monkeypatch.setattr(engine, "scan_list_pages", scan)
    monkeypatch.setattr(manager, "_scan_target_with_details", scan_with_details)
    monkeypatch.setattr(engine, "fetch_video_details_batch", details)
    monkeypatch.setattr(engine, "download_mp4_with_resume", tracked_download)
    queued, _, status = await manager.resume_job_in_place(job["id"])
    assert status == 200 and queued["params"]["resume_mode"] == "rescan"
    await manager._run_job(job["id"])
    rows = await repo.list_job_items(job["id"])
    assert rows[0] == done and rows[1]["id"] == waiting["id"]
    assert [(row["viewkey"], row["status"]) for row in rows] == [
        ("complete", "done"), ("waiting", "downloaded"), ("fresh", "downloaded"),
    ]
    assert sorted(downloads) == ["fresh", "waiting"]
    final = await repo.get_job(job["id"])
    assert final["status"] == "completed" and final["params"] == {**params, "resume_count": 1}
    assert final["kind"] == kind and final["total_items"] == final["done_items"] == 3


async def test_cancel_queued_job_then_resume_uses_only_the_new_queue_token(job_engine, monkeypatch):
    manager, repo, _ = job_engine
    first = await manager.enqueue_job("manual_urls", "First", {})
    other = await manager.enqueue_job("manual_urls", "Other", {})
    assert manager.queued_count == 2
    assert await manager.cancel_job(first["id"])
    assert manager.queued_count == 1
    attempts = await asyncio.gather(*(manager.resume_job_in_place(first["id"]) for _ in range(2)))
    assert sorted(result[2] for result in attempts) == [200, 409]
    assert manager.queued_count == 2
    processed = []

    async def run(job_id):
        processed.append(job_id)
        assert await repo.claim_queued_job(job_id)
        await repo.update_job(job_id, status="completed")

    monkeypatch.setattr(manager, "_run_job", run)
    manager.start()
    await asyncio.wait_for(manager._queue.join(), 5)
    assert processed == [other["id"], first["id"]]
    assert manager.queued_count == 0


async def test_cancel_queued_rescan_preserves_scan_gap_for_the_next_attempt(job_engine):
    manager, repo, _ = job_engine
    job = await repo.create_job("manual_target", "Original scan", {"usernames": ["fixture"]})
    await repo.add_job_item(
        job["id"], viewkey="waiting", title="Fixture", uploader="fixture", target_type="model",
        page_url=page("waiting"), status="failed",
    )
    await repo.update_job(job["id"], status="partial_failed", error_message="1 页扫描未完成")
    queued, _, _ = await manager.resume_job_in_place(job["id"])
    assert queued["params"]["resume_mode"] == "rescan"
    assert await manager.cancel_job(job["id"])
    cancelled = await repo.get_job(job["id"])
    assert "扫描未完成" in cancelled["error_message"] and "resume_mode" not in cancelled["params"]
    again, _, status = await manager.resume_job_in_place(job["id"])
    assert status == 200 and again["params"]["resume_mode"] == "rescan"
    assert again["params"]["resume_count"] == 2


async def test_rescan_export_clears_previous_links_and_reuses_job(job_engine, monkeypatch):
    manager, repo, _ = job_engine
    job = await repo.create_job("manual_urls", "Original export", {"export_only": True, "urls": [page("fresh")]})
    await repo.update_job(job["id"], status="partial_failed", export_text=page("stale"))

    async def details(*args, **kwargs):
        return [detail("fresh")]

    monkeypatch.setattr(engine, "fetch_video_details_batch", details)
    queued, _, status = await manager.resume_job_in_place(job["id"])
    assert status == 200 and queued["id"] == job["id"] and queued["export_text"] == ""
    assert queued["params"]["resume_mode"] == "rescan"
    await manager._run_job(job["id"])
    final = await repo.get_job(job["id"])
    assert final["status"] == "completed" and final["export_text"] == f"{page('fresh')}\n"
    assert not await repo.list_job_items(job["id"])


async def test_resume_legacy_retry_without_own_items_falls_back_to_its_source(job_engine, monkeypatch):
    manager, repo, _ = job_engine
    source = await repo.create_job("manual_urls", "Source", {"auto_upload": False})
    item = await repo.add_job_item(
        source["id"], viewkey="waiting", title="Fixture", uploader="fixture", target_type="model",
        page_url=page("waiting"), download_url="https://cdn.example/waiting.mp4", status="failed",
    )
    await repo.update_job(source["id"], status="partial_failed")
    legacy = await repo.create_job("retry_items", "Legacy", {"source_job_id": source["id"], "auto_upload": False})
    await repo.update_job(legacy["id"], status="cancelled")
    monkeypatch.setattr(engine, "download_mp4_with_resume", fixture_download)

    queued, _, status = await manager.resume_job_in_place(legacy["id"])
    assert status == 200 and queued["id"] == legacy["id"] and queued["params"]["resume_mode"] == "rescan"
    await manager._run_job(legacy["id"])
    own_items = await repo.list_job_items(legacy["id"])
    assert [(row["viewkey"], row["status"]) for row in own_items] == [("waiting", "downloaded")]
    assert await repo.get_job_item(item["id"]) == item
    assert (await repo.get_job(legacy["id"]))["status"] == "completed"


async def test_resume_temp_rescan_continues_existing_items(job_engine, monkeypatch):
    manager, repo, _ = job_engine
    job = await repo.create_job("resume_temp", "Original checkpoint scan", {"auto_upload": False})
    item = await repo.add_job_item(
        job["id"], viewkey="waiting", title="Fixture", uploader="fixture", target_type="model",
        page_url=page("waiting"), download_url="https://cdn.example/waiting.mp4", status="cancelled",
    )
    await repo.update_job(job["id"], status="cancelled", error_message="扫描未完成，已取消")
    monkeypatch.setattr(engine, "download_mp4_with_resume", fixture_download)

    queued, _, status = await manager.resume_job_in_place(job["id"])
    assert status == 200 and queued["params"]["resume_mode"] == "rescan"
    await manager._run_job(job["id"])
    rows = await repo.list_job_items(job["id"])
    assert [(row["id"], row["status"]) for row in rows] == [(item["id"], "downloaded")]
    assert (await repo.get_job(job["id"]))["status"] == "completed"


async def test_resume_metadata_reconciles_completed_and_outstanding_items(job_engine, monkeypatch):
    manager, repo, _ = job_engine
    job = await repo.create_job("enrich_metadata", "Original metadata task", {})
    rows = {}
    for key, state in [("xhAlready", "done"), ("xhSettled", "failed"),
                       ("xhPending", "cancelled"), ("xhNew", None)]:
        url = f"https://xhamster.com/videos/fixture-{key}"
        await repo.upsert_video(
            key, site="xhamster", video_page_url=url, status="baseline",
            quality="1080p" if key in {"xhAlready", "xhSettled"} else "",
        )
        if state is not None:
            rows[key] = await repo.add_job_item(
                job["id"], viewkey=key, page_url=url, title=key, uploader="fixture", target_type="creator",
                status=state, error_message="" if state == "done" else "previous error",
            )
    await repo.update_job(job["id"], status="cancelled")
    seen = []

    async def details(_ctx, items, *args, **kwargs):
        result = []
        for url, _, _ in items:
            key = url.rsplit("-", 1)[1]
            seen.append(key)
            result.append(VideoDetail(key, key, url, url, download_url="https://cdn.example/hls.m3u8", quality="1080p"))
        return result

    monkeypatch.setattr(engine, "fetch_video_details_batch", details)
    queued, _, status = await manager.resume_job_in_place(job["id"])
    assert status == 200 and queued["params"]["resume_mode"] == "enrich"
    await manager._run_job(job["id"])
    final_rows = {row["viewkey"]: row for row in await repo.list_job_items(job["id"])}
    assert seen == ["xhPending", "xhNew"]
    assert final_rows["xhAlready"] == rows["xhAlready"]
    assert all(final_rows[key]["id"] == rows[key]["id"] for key in rows)
    assert len(final_rows) == 4 and all(row["status"] == "done" and not row["error_message"] for row in final_rows.values())
    final = await repo.get_job(job["id"])
    assert final["status"] == "completed" and final["params"] == {"resume_count": 1}
    assert final["total_items"] == final["done_items"] == 4 and final["uploaded_items"] == 0
    assert (await manager.resume_job_in_place(job["id"]))[2] == 400


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
        assert recovered["id"] == interrupted["id"]
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

async def test_shutdown_finishes_resumed_attempt_and_keeps_queued_work(job_engine, monkeypatch):
    manager, repo, _ = job_engine
    entered = asyncio.Event()
    first = await repo.create_job("manual_urls", "first", {"urls": [page("again")]})
    await repo.update_job(first["id"], status="failed")
    failed, _done = await repo.add_job_items_batch(first["id"], [
        {"viewkey": "again", "status": "failed"}, {"viewkey": "done", "status": "done"},
    ])
    queued, _, code = await manager.resume_job_in_place(first["id"])
    assert code == 200 and queued["failed_items"] == 0

    async def cooperative_executor(job_id, params, settings, ctx):
        await repo.update_job(job_id, status="running")
        await repo.update_job_item(failed["id"], status="uploading")
        entered.set()
        await ctx.cancel_event.wait()

    monkeypatch.setattr(manager, "_exec_resume_job_items", cooperative_executor)
    other = await manager.enqueue_job("manual_urls", "other", {"urls": [page("other")]})
    manager.start()
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(manager.stop(), 3)
    current = await repo.get_job(first["id"])
    assert current["status"] == "cancelled" and current["finished_at"]
    assert current["revision"] > queued["revision"] and current["params"] == {"urls": [page("again")], "resume_count": 1}
    assert (current["total_items"], current["done_items"], current["uploaded_items"], current["failed_items"]) == (2, 1, 1, 0)
    assert (await repo.get_job_item(failed["id"]))["status"] == "cancelled"
    assert (await repo.get_job(other["id"]))["status"] == "queued"
    assert manager.queued_count == 1


async def test_unexpected_upload_worker_failure_finishes_job_and_preserves_local_file(job_engine, monkeypatch):
    manager, repo, _ = job_engine

    async def parse(_ctx, urls, *_args, **_kwargs):
        return [detail(url.split("=")[-1]) for url, *_ in urls]

    async def broken_upload(**_kwargs):
        raise OSError("fixture upload worker failed")

    monkeypatch.setattr(engine, "fetch_video_details_batch", parse)
    monkeypatch.setattr(engine, "download_mp4_with_resume", fixture_download)
    monkeypatch.setattr(engine, "upload_file_batch", broken_upload)
    job = await asyncio.wait_for(run_job(manager, repo, "manual_urls", urls=[page("uploadfail")], auto_upload=True), 4)
    row, = await repo.list_job_items(job["id"])
    assert job["status"] == "failed" and job["failed_items"] == 1
    assert row["status"] == "failed" and Path(row["local_path"]).is_file()
    assert (await repo.get_video("uploadfail"))["upload_requested"] == 1


async def test_finished_job_retry_rules_and_recent_retention(job_engine):
    manager, repo, _ = job_engine
    old = await repo.create_job("manual_target", "old export", {"export_only": True})
    await repo.update_job(old["id"], status="completed", finished_at="2026-01-01T00:00:00+00:00")
    assert (await manager.resume_job_in_place(old["id"]))[2] == 400
    newest_id = await repo.create_job("check_updates", "later id", {})
    await repo.update_job(newest_id["id"], status="completed", finished_at="2026-02-01T00:00:00+00:00")
    await repo.update_job(old["id"], status="partial_failed", finished_at="2026-03-01T00:00:00+00:00")
    assert (await repo.list_jobs(limit=1))[0]["id"] == old["id"]
    assert await repo.prune_old_jobs(1) == 1
    assert await repo.get_job(old["id"])


async def test_queued_resume_cancel_updates_ledger_and_drops_stale_tokens(job_engine):
    manager, repo, _ = job_engine
    job = await repo.create_job("manual_urls", "kept", {"urls": [page("again")]})
    await repo.update_job(job["id"], status="failed")
    item, = await repo.add_job_items_batch(job["id"], [
        {"viewkey": "again", "status": "downloading", "bytes_downloaded": 900},
    ])
    for _ in range(70):
        queued, _, code = await manager.resume_job_in_place(job["id"])
        assert code == 200 and queued["status"] == "queued"
        assert await manager.cancel_job(job["id"])
    current = await repo.get_job(job["id"])
    row = await repo.get_job_item(item["id"])
    assert current["params"]["resume_count"] == 70 and "resume_mode" not in current["params"]
    assert current["status"] == "cancelled" and row["status"] == "cancelled"
    assert row["bytes_downloaded"] == 0 and manager.queued_count == 0 and manager._queue.qsize() <= 64
