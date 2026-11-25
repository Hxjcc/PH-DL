"""Tests for TaskManager end-to-end pipelines (manual download, auto-update, upload, cancellation)."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Dict

from app.config import EnvSettings
from app.db.database import Database
from app.db.repo import Repository
from app.engine.events import EventBus
from app.engine.task_manager import TaskManager
from app.parsers.list_scanner import ListedVideo
from app.parsers.video import VideoDetail
from app.services.cookie import CookieCheckResult
from app.services.downloader import DownloadOutcome
from app.services.uploader import UploadBatchResult, file_snapshot
from tests.media_fixtures import mp4_payload


async def _wait_job_done(repo: Repository, job_id: int, timeout: float = 5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        job = await repo.get_job(job_id)
        if job and job["status"] in {"completed", "partial_failed", "failed", "cancelled"}:
            return job
        await asyncio.sleep(0.05)
    return await repo.get_job(job_id)


async def test_auto_update_skips_baseline_and_uploads_only_new_videos(tmp_path: Path, monkeypatch):
    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        # Prepare settings, target, and one existing baseline video
        await repo.update_settings_patch(
            {
                "ph_cookie": "sess=valid",
                "uploader_enabled": True,
                "upload_batch_size": 2,
                "min_free_disk_gb": 0.0,
                "serverchan_enabled": False,
            }
        )
        await repo.upsert_target("alice", "model", enabled=True)
        await repo.upsert_video("vk_old", title="Old Baseline", uploader="alice", status="baseline")

        # Mock external network/rclone calls
        from app.engine import task_manager as tm_mod

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        async def fake_scan_list(ctx, initial_url, selectors, *, skip_vkeys=None, **kwargs):
            all_items = [
                ListedVideo("vk_old", "https://www.pornhub.com/view_video.php?viewkey=vk_old", "Old", 1, 1, 2),
                ListedVideo("vk_new", "https://www.pornhub.com/view_video.php?viewkey=vk_new", "Brand New", 1, 2, 2),
            ]
            return [x for x in all_items if x.viewkey not in (skip_vkeys or set())]

        async def fake_fetch_batch(ctx, items, selectors, **kwargs):
            return [
                VideoDetail(
                    viewkey="vk_new",
                    title="Brand New",
                    page_url="https://www.pornhub.com/view_video.php?viewkey=vk_new",
                    referer_url="https://www.pornhub.com/view_video.php?viewkey=vk_new",
                    download_url="https://cdn.example/new.mp4",
                    quality="1080p",
                    uploader_username="alice",
                    uploader_type="model",
                    page_num=1,
                    item_num=2,
                )
            ]

        async def fake_download(*, download_dir: Path, viewkey: str, title: str, **kwargs):
            download_dir.mkdir(parents=True, exist_ok=True)
            out = download_dir / f"{title} [{viewkey}].mp4"
            out.write_bytes(mp4_payload())
            return DownloadOutcome(
                success=True,
                local_path=out,
                file_size=out.stat().st_size,
                quality="1080p",
                download_url="https://cdn.example/new.mp4",
            )

        uploaded_batches = []

        async def fake_upload(download_dir: Path, uploader_folder: str, files_by_viewkey: Dict[str, Path], settings, cancel_event=None):
            uploaded_batches.append((uploader_folder, list(files_by_viewkey.keys())))
            return UploadBatchResult(
                uploaded_viewkeys=list(files_by_viewkey.keys()),
                failed_viewkeys=[],
                verified_sources={vk: file_snapshot(path) for vk, path in files_by_viewkey.items()},
                remote_dir=f"pikpak:Pornhub/{uploader_folder}",
            )

        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(tm_mod, "scan_list_pages", fake_scan_list)
        monkeypatch.setattr(tm_mod, "fetch_video_details_batch", fake_fetch_batch)
        monkeypatch.setattr(tm_mod, "download_mp4_with_resume", fake_download)
        monkeypatch.setattr(tm_mod, "upload_file_batch", fake_upload)

        tm.start()
        job = await tm.enqueue_job("auto_update", "测试自动追更", {})
        done = await _wait_job_done(repo, job["id"])

        assert done is not None
        assert done["status"] == "completed"
        assert done["total_items"] == 1
        assert done["done_items"] == 1
        assert done["uploaded_items"] == 1
        assert uploaded_batches == [("alice", ["vk_new"])]

        # Old video remained baseline; new video is now marked uploaded in unified DB!
        v_old = await repo.get_video("vk_old")
        v_new = await repo.get_video("vk_new")
        assert v_old and v_old["status"] == "baseline"
        assert v_new and v_new["status"] == "uploaded"
        assert v_new["remote_path"] == "pikpak:Pornhub/alice/Brand New [vk_new].mp4"
    finally:
        await tm.stop()
        await db.close()


async def test_backpressure_flushes_without_deadlock_and_reports_upload_failure(tmp_path: Path, monkeypatch):
    """Verify multi-uploader backpressure + upload failure never deadlocks and surfaces failed URLs in report."""
    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        await repo.update_settings_patch(
            {
                "ph_cookie": "sess=valid",
                "uploader_enabled": True,
                "max_concurrent_downloads": 2,
                "upload_batch_size": 10,  # Larger than max_pending_upload_files!
                "max_pending_upload_files": 2,
                "min_free_disk_gb": 0.0,
                "preferred_quality": "1080",
                "serverchan_enabled": True,
                "serverchan_send_key": "SCT_TEST",
            }
        )
        await repo.upsert_target("alice", "model", enabled=True, preferred_quality="720")
        await repo.upsert_target("bob", "model", enabled=True, preferred_quality="")

        from app.engine import task_manager as tm_mod

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        async def fake_scan_list(ctx, initial_url, selectors, *, skip_vkeys=None, **kwargs):
            if "/alice/" in initial_url:
                return [
                    ListedVideo("vka1", "https://www.pornhub.com/view_video.php?viewkey=vka1", "Alice 1", 1, 1, 2),
                    ListedVideo("vka2", "https://www.pornhub.com/view_video.php?viewkey=vka2", "Alice 2", 1, 2, 2),
                ]
            return [
                ListedVideo("vkb1", "https://www.pornhub.com/view_video.php?viewkey=vkb1", "Bob 1", 1, 1, 2),
                ListedVideo("vkb2", "https://www.pornhub.com/view_video.php?viewkey=vkb2", "Bob 2", 1, 2, 2),
            ]

        async def fake_fetch_batch(ctx, items, selectors, **kwargs):
            res = []
            for url, pn, itn in items:
                vk = url.split("viewkey=")[1]
                uploader_name = "alice" if vk.startswith("vka") else "bob"
                res.append(
                    VideoDetail(
                        viewkey=vk,
                        title=f"Title {vk}",
                        page_url=url,
                        referer_url=url,
                        download_url=f"https://cdn.example/{vk}.mp4",
                        quality="720p" if uploader_name == "alice" else "1080p",
                        uploader_username=uploader_name,
                        uploader_type="model",
                        page_num=pn,
                        item_num=itn,
                    )
                )
            return res

        seen_dl_qualities: Dict[str, object] = {}

        async def fake_download(*, download_dir: Path, viewkey: str, title: str, preferred_qualities=None, **kwargs):
            seen_dl_qualities[viewkey] = preferred_qualities
            download_dir.mkdir(parents=True, exist_ok=True)
            out = download_dir / f"{title} [{viewkey}].mp4"
            out.write_bytes(mp4_payload())
            return DownloadOutcome(
                success=True,
                local_path=out,
                file_size=out.stat().st_size,
                quality="720p",
                download_url=f"https://cdn.example/{viewkey}.mp4",
            )

        async def fake_upload(download_dir: Path, uploader_folder: str, files_by_viewkey: Dict[str, Path], settings, cancel_event=None):
            # Simulate bob failing upload while alice succeeds
            if uploader_folder == "bob":
                return UploadBatchResult(
                    uploaded_viewkeys=[],
                    failed_viewkeys=list(files_by_viewkey.keys()),
                    remote_dir=f"pikpak:Pornhub/{uploader_folder}",
                    error="remote quota exceeded",
                )
            return UploadBatchResult(
                uploaded_viewkeys=list(files_by_viewkey.keys()),
                failed_viewkeys=[],
                verified_sources={vk: file_snapshot(path) for vk, path in files_by_viewkey.items()},
                remote_dir=f"pikpak:Pornhub/{uploader_folder}",
            )

        sent_reports = []

        async def fake_send_notify(title: str, content: str, settings, **kwargs):
            sent_reports.append((title, content))
            return True, "ok"

        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(tm_mod, "scan_list_pages", fake_scan_list)
        monkeypatch.setattr(tm_mod, "fetch_video_details_batch", fake_fetch_batch)
        monkeypatch.setattr(tm_mod, "download_mp4_with_resume", fake_download)
        monkeypatch.setattr(tm_mod, "upload_file_batch", fake_upload)
        monkeypatch.setattr(tm_mod, "send_serverchan", fake_send_notify)

        tm.start()
        job = await tm.enqueue_job("auto_update", "测试背压与单目标画质", {})
        done = await _wait_job_done(repo, job["id"], timeout=5.0)

        assert done is not None
        assert done["status"] == "partial_failed"
        assert done["total_items"] == 4
        assert done["uploaded_items"] == 2
        assert done["failed_items"] == 2
        # Per-target preferred_quality preserved inside download pipeline!
        assert seen_dl_qualities["vka1"] == {"720"}
        assert seen_dl_qualities["vkb1"] == {"1080"}
        # Upload failures MUST be included in ServerChan report!
        assert len(sent_reports) == 1
        assert "vkb1" in sent_reports[0][1] and "vkb2" in sent_reports[0][1]
    finally:
        await tm.stop()
        await db.close()


async def test_baseline_sync_and_export_only_preserve_job_counts(tmp_path: Path, monkeypatch):
    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        await repo.update_settings_patch({"ph_cookie": "sess=valid"})
        target = await repo.upsert_target("alice", "model", enabled=True)

        from app.engine import task_manager as tm_mod

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        async def fake_scan_list(ctx, initial_url, selectors, **kwargs):
            return [
                ListedVideo("vk1", "https://www.pornhub.com/view_video.php?viewkey=vk1", "One", 1, 1, 2),
                ListedVideo("vk2", "https://www.pornhub.com/view_video.php?viewkey=vk2", "Two", 1, 2, 2),
            ]

        async def fake_fetch_batch(ctx, items, selectors, **kwargs):
            return [
                VideoDetail(
                    viewkey="vk1",
                    title="One",
                    page_url="https://www.pornhub.com/view_video.php?viewkey=vk1",
                    referer_url="https://www.pornhub.com/view_video.php?viewkey=vk1",
                    download_url="https://cdn.example/1.mp4",
                    quality="1080p",
                )
            ]

        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(tm_mod, "scan_list_pages", fake_scan_list)
        monkeypatch.setattr(tm_mod, "fetch_video_details_batch", fake_fetch_batch)

        tm.start()

        # 1. baseline_sync job MUST preserve total_items=2, done_items=2
        j_base = await tm.enqueue_job("baseline_sync", "基线同步", {"target_id": target["id"], "pages": "1"})
        done_base = await _wait_job_done(repo, j_base["id"])
        assert done_base is not None
        assert done_base["status"] == "completed"
        assert done_base["total_items"] == 2
        assert done_base["done_items"] == 2

        # 2. export_only job MUST preserve total_items=1, done_items=1
        j_exp = await tm.enqueue_job(
            "manual_urls",
            "导出测试",
            {"urls": ["https://www.pornhub.com/view_video.php?viewkey=vk1"], "export_only": True},
        )
        done_exp = await _wait_job_done(repo, j_exp["id"])
        assert done_exp is not None
        assert done_exp["status"] == "completed"
        assert done_exp["total_items"] == 1
        assert done_exp["done_items"] == 1
        assert done_exp["export_text"].splitlines() == [
            "https://www.pornhub.com/view_video.php?viewkey=vk1"
        ]
        assert "#" not in done_exp["export_text"]
        assert "https://cdn.example/1.mp4" not in done_exp["export_text"]
    finally:
        await tm.stop()
        await db.close()


async def test_baseline_scopes_scan_requested_pages_without_update_limits_or_downloads(tmp_path: Path, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    from app.engine import task_manager as tm_mod
    from app.parsers.http_session import PowFetchResult, TaskHttpContext

    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    tm = TaskManager(env=env, repo=repo, events=EventBus())
    requested_pages = []

    async def fake_check_cookie(*args, **kwargs):
        return CookieCheckResult(valid=True, message="ok")

    async def fake_fetch(self, url, **kwargs):
        page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
        requested_pages.append(page)
        if page > 7:
            return PowFetchResult("", url, 404, 0, False), None
        items = "".join(
            f'<li class="videoBox" data-video-vkey="vk{page}{index}">'
            f'<a href="/view_video.php?viewkey=vk{page}{index}" title="Video {page}-{index}"></a></li>'
            for index in (1, 2)
        )
        html = f'<ul class="videos" id="mostRecentVideosSection">{items}</ul>'
        return PowFetchResult(html, url, 200, 0, False), None

    async def forbid_media_work(*args, **kwargs):
        raise AssertionError("baseline initialization must only scan lists, without media parsing, downloads or uploads")

    monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
    monkeypatch.setattr(TaskHttpContext, "fetch_with_pow", fake_fetch)
    monkeypatch.setattr(tm_mod, "fetch_video_details_batch", forbid_media_work)
    monkeypatch.setattr(tm_mod, "download_mp4_with_resume", forbid_media_work)
    monkeypatch.setattr(tm_mod, "upload_file_batch", forbid_media_work)

    try:
        await repo.update_settings_patch({
            "ph_cookie": "sess=valid",
            "page_delay_min": 0,
            "page_delay_max": 0,
        })
        target = await repo.upsert_target("alice", "model", enabled=True)
        tm.start()
        for pages, expected_pages in [("1", {1}), ("3-5", {3, 4, 5}), ("all", set(range(1, 8)))]:
            requested_pages.clear()
            job = await tm.enqueue_job("baseline_sync", "Baseline scope test", {"target_id": target["id"], "pages": pages})
            done = await _wait_job_done(repo, job["id"])
            assert done["status"] == "completed"
            assert done["total_items"] == done["done_items"] == len(expected_pages) * 2
            assert {page for page in requested_pages if page <= 7} == expected_pages
            assert not await repo.list_job_items(job["id"])
            for page in expected_pages:
                for index in (1, 2):
                    video = await repo.get_video(f"vk{page}{index}")
                    assert video and video["status"] == "baseline"
        assert not list(env.downloads_dir.rglob("*.mp4"))
    finally:
        await tm.stop()
        await db.close()


async def test_manual_target_prefetches_pages_and_streams_details_with_shared_semaphore(
    tmp_path: Path, monkeypatch
):
    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        await repo.update_settings_patch(
            {
                "ph_cookie": "sess=valid",
                "uploader_enabled": False,
                "max_concurrent_list_pages": 4,
                "max_concurrent_video_details": 40,
            }
        )

        from app.engine import task_manager as tm_mod

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        detail_started = asyncio.Event()
        callback_pages = []
        semaphore_ids = []

        async def fake_scan_list(
            ctx,
            initial_url,
            selectors,
            *,
            on_page=None,
            page_concurrency=None,
            **kwargs,
        ):
            assert page_concurrency == 4
            all_items = []
            for page_num in range(1, 4):
                items = [
                    ListedVideo(
                        f"vk{page_num}",
                        f"https://www.pornhub.com/view_video.php?viewkey=vk{page_num}",
                        f"Page {page_num}",
                        page_num,
                        1,
                        1,
                    )
                ]
                all_items.extend(items)
                assert on_page is not None
                await on_page(items)
                callback_pages.append(page_num)
                await asyncio.sleep(0.02)
                if page_num == 1:
                    assert detail_started.is_set()
            return all_items

        async def fake_fetch_batch(ctx, items, selectors, **kwargs):
            detail_started.set()
            semaphore_ids.append(id(kwargs.get("semaphore")))
            await asyncio.sleep(0.01)
            return [
                VideoDetail(
                    viewkey=url.rsplit("=", 1)[1],
                    title=f"Video {page_num}",
                    page_url=url,
                    referer_url=url,
                    download_url=f"https://cdn.example/{page_num}.mp4",
                    quality="1080p",
                    page_num=page_num,
                    item_num=item_num,
                )
                for url, page_num, item_num in items
            ]

        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(tm_mod, "scan_list_pages", fake_scan_list)
        monkeypatch.setattr(tm_mod, "fetch_video_details_batch", fake_fetch_batch)

        tm.start()
        job = await tm.enqueue_job(
            "manual_target",
            "测试多页流水线",
            {
                "usernames": ["alice"],
                "target_type": "model",
                "pages": "all",
                "export_only": True,
                "auto_upload": False,
            },
        )
        done = await _wait_job_done(repo, job["id"])

        assert done is not None
        assert done["status"] == "completed"
        assert done["total_items"] == 3
        assert done["done_items"] == 3
        assert callback_pages == [1, 2, 3]
        assert len(semaphore_ids) == 3
        assert len(set(semaphore_ids)) == 1
        assert detail_started.is_set()
    finally:
        await tm.stop()
        await db.close()


async def test_auto_update_uses_same_prefetch_pipeline_as_manual_target(
    tmp_path: Path, monkeypatch
):
    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        await repo.update_settings_patch(
            {
                "ph_cookie": "sess=valid",
                "uploader_enabled": False,
                "serverchan_enabled": False,
                "max_concurrent_list_pages": 4,
                "max_concurrent_video_details": 40,
            }
        )
        await repo.upsert_target("alice", "model", enabled=True)

        from app.engine import task_manager as tm_mod

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        detail_started = asyncio.Event()
        callback_pages = []
        semaphore_ids = []

        async def fake_scan_list(
            ctx,
            initial_url,
            selectors,
            *,
            on_page=None,
            page_concurrency=None,
            catch_up=None,
            **kwargs,
        ):
            assert page_concurrency == 4
            assert catch_up is True
            all_items = []
            for page_num in range(1, 4):
                items = [
                    ListedVideo(
                        f"auto{page_num}",
                        f"https://www.pornhub.com/view_video.php?viewkey=auto{page_num}",
                        f"Auto {page_num}",
                        page_num,
                        1,
                        1,
                    )
                ]
                all_items.extend(items)
                assert on_page is not None
                await on_page(items)
                callback_pages.append(page_num)
                await asyncio.sleep(0.02)
                if page_num == 1:
                    assert detail_started.is_set()
            return all_items

        async def fake_fetch_batch(ctx, items, selectors, **kwargs):
            detail_started.set()
            semaphore_ids.append(id(kwargs.get("semaphore")))
            await asyncio.sleep(0.01)
            return [
                VideoDetail(
                    viewkey=url.rsplit("=", 1)[1],
                    title=f"Auto Video {page_num}",
                    page_url=url,
                    referer_url=url,
                    download_url=f"https://cdn.example/auto{page_num}.mp4",
                    quality="1080p",
                    page_num=page_num,
                    item_num=item_num,
                )
                for url, page_num, item_num in items
            ]

        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(tm_mod, "scan_list_pages", fake_scan_list)
        monkeypatch.setattr(tm_mod, "fetch_video_details_batch", fake_fetch_batch)

        tm.start()
        job = await tm.enqueue_job("check_updates", "自动追更流水线测试", {})
        done = await _wait_job_done(repo, job["id"])

        assert done is not None
        assert done["status"] == "completed"
        assert done["total_items"] == 3
        assert done["done_items"] == 3
        assert callback_pages == [1, 2, 3]
        assert len(semaphore_ids) == 3
        assert len(set(semaphore_ids)) == 1
        assert detail_started.is_set()
    finally:
        await tm.stop()
        await db.close()


async def test_enrich_metadata_updates_baseline_quality_and_size(tmp_path: Path, monkeypatch):
    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        await repo.update_settings_patch(
            {
                "ph_cookie": "sess=valid",
                "uploader_enabled": False,
                "max_concurrent_video_details": 40,
            }
        )
        await repo.upsert_video(
            "meta1",
            title="Metadata One",
            uploader="alice",
            target_type="model",
            video_page_url="https://www.pornhub.com/view_video.php?viewkey=meta1",
            status="baseline",
        )
        await repo.upsert_video(
            "meta2",
            title="Metadata Two",
            uploader="alice",
            target_type="model",
            video_page_url="https://www.pornhub.com/view_video.php?viewkey=meta2",
            status="baseline",
        )

        from app.engine import task_manager as tm_mod

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        fetch_kwargs = {}

        async def fake_fetch_batch(ctx, items, selectors, **kwargs):
            fetch_kwargs.update(kwargs)
            return [
                VideoDetail(
                    viewkey=url.rsplit("=", 1)[1],
                    title=f"Title {idx}",
                    page_url=url,
                    referer_url=url,
                    download_url=f"https://cdn.example/{idx}.mp4",
                    quality="1080p" if idx == 1 else "720p",
                )
                for idx, (url, _, _) in enumerate(items, start=1)
            ]

        async def fake_probe(session, url, **kwargs):
            return 1000 if url.endswith("/1.mp4") else 2000

        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(tm_mod, "fetch_video_details_batch", fake_fetch_batch)
        monkeypatch.setattr(tm_mod, "probe_download_size", fake_probe)

        tm.start()
        job = await tm.enqueue_job("enrich_metadata", "元数据补全", {})
        done = await _wait_job_done(repo, job["id"])

        assert done is not None
        assert done["status"] == "completed"
        assert done["total_items"] == 2
        assert done["done_items"] == 2
        assert done["failed_items"] == 0
        assert done["uploaded_items"] == 0

        v1 = await repo.get_video("meta1")
        second_video = await repo.get_video("meta2")
        assert v1 and v1["status"] == "baseline"
        assert v1["quality"] == "1080p" and v1["file_size"] == 1000
        assert second_video and second_video["status"] == "baseline"
        assert second_video["quality"] == "720p" and second_video["file_size"] == 2000
        assert len(await repo.list_job_items(job["id"])) == 2
        assert fetch_kwargs.get("skip_dom") is True
    finally:
        await tm.stop()
        await db.close()


async def test_scheduler_persists_last_run_date_across_instances(tmp_path: Path):
    from datetime import datetime
    from app.engine.scheduler import UpdateScheduler

    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()

    try:
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        await repo.update_settings_patch({"schedule_enabled": True, "schedule_time": now.strftime("%H:%M")})
        await repo.set_scheduler_last_run_date(today_str)

        async def _noop_trigger():
            return 1

        sched = UpdateScheduler(repo=repo, events=events, trigger_auto_update=_noop_trigger)
        next_iso = await sched.next_run_iso()
        assert next_iso is not None
        # Since today already ran in persisted SQLite state, next run MUST be scheduled for tomorrow!
        assert not next_iso.startswith(today_str)
    finally:
        await db.close()


async def test_manual_download_respects_disk_reserve_without_upload(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        await repo.update_settings_patch(
            {
                "ph_cookie": "sess=valid",
                "uploader_enabled": False,
                "min_free_disk_gb": 1.0,
            }
        )

        from app.engine import task_manager as tm_mod

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        async def fake_fetch_batch(ctx, items, selectors, **kwargs):
            return [
                VideoDetail(
                    viewkey="vk_nodisk",
                    title="No Disk",
                    page_url="https://www.pornhub.com/view_video.php?viewkey=vk_nodisk",
                    referer_url="https://www.pornhub.com/view_video.php?viewkey=vk_nodisk",
                    download_url="https://cdn.example/nodisk.mp4",
                    quality="1080p",
                )
            ]

        async def fail_download(**kwargs):
            raise AssertionError("download must not start when disk reserve is breached")

        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(tm_mod, "fetch_video_details_batch", fake_fetch_batch)
        monkeypatch.setattr(tm_mod, "download_mp4_with_resume", fail_download)
        monkeypatch.setattr(
            tm_mod.shutil,
            "disk_usage",
            lambda _path: SimpleNamespace(free=0, total=1, used=1),
        )

        tm.start()
        job = await tm.enqueue_job(
            "manual_urls",
            "磁盘保护测试",
            {
                "urls": ["https://www.pornhub.com/view_video.php?viewkey=vk_nodisk"],
                "auto_upload": False,
            },
        )
        done = await _wait_job_done(repo, job["id"])

        assert done is not None
        assert done["status"] == "partial_failed"
        assert done["failed_items"] == 1
    finally:
        await tm.stop()
        await db.close()


async def test_targeted_update_does_not_reconcile_other_target_files(tmp_path: Path, monkeypatch):
    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        await repo.update_settings_patch(
            {
                "ph_cookie": "sess=valid",
                "uploader_enabled": True,
                "serverchan_enabled": False,
                "min_free_disk_gb": 0.0,
                "min_file_size_bytes": 1,
            }
        )
        target = await repo.upsert_target("kkfox-baby", "model", enabled=True)
        local_dir = env.downloads_dir / "milapixie"
        local_dir.mkdir(parents=True, exist_ok=True)
        local_file = local_dir / "Other Target [vkother].mp4"
        local_file.write_bytes(mp4_payload())
        await repo.upsert_video(
            "vkother",
            title="Other Target",
            uploader="milapixie",
            target_type="model",
            video_page_url="https://www.pornhub.com/view_video.php?viewkey=vkother",
            status="downloaded",
            local_path=str(local_file),
            upload_requested=True,
        )

        from app.engine import task_manager as tm_mod

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        async def fake_scan(*args, **kwargs):
            return []

        async def fail_upload(*args, **kwargs):
            raise AssertionError("targeted update must not upload another target's files")

        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(tm_mod, "scan_list_pages", fake_scan)
        monkeypatch.setattr(tm_mod, "upload_file_batch", fail_upload)

        tm.start()
        job = await tm.enqueue_job(
            "auto_update",
            "指定追更: KKFox baby",
            {"target_ids": [target["id"]]},
        )
        done = await _wait_job_done(repo, job["id"])

        assert done is not None
        assert done["status"] == "completed"
        assert done["total_items"] == 0
        assert await repo.list_job_items(job["id"]) == []
        assert local_file.exists()
    finally:
        await tm.stop()
        await db.close()


async def test_auto_update_reconciles_downloaded_local_files(tmp_path: Path, monkeypatch):
    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        await repo.update_settings_patch(
            {
                "ph_cookie": "sess=valid",
                "uploader_enabled": True,
                "serverchan_enabled": False,
                "min_free_disk_gb": 0.0,
                "min_file_size_bytes": 1,
            }
        )
        local_dir = env.downloads_dir / "alice"
        local_dir.mkdir(parents=True, exist_ok=True)
        local_file = local_dir / "Pending Upload [vkpending].mp4"
        local_file.write_bytes(mp4_payload())
        await repo.upsert_video(
            "vkpending",
            title="Pending Upload",
            uploader="alice",
            target_type="model",
            quality="1080p",
            video_page_url="https://www.pornhub.com/view_video.php?viewkey=vkpending",
            status="downloaded",
            local_path=str(local_file),
            upload_requested=True,
        )

        from app.engine import task_manager as tm_mod

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        uploaded = []

        async def fake_upload(download_dir, uploader_folder, files_by_viewkey, settings, cancel_event=None):
            uploaded.extend(files_by_viewkey.keys())
            return UploadBatchResult(
                uploaded_viewkeys=list(files_by_viewkey.keys()),
                failed_viewkeys=[],
                verified_sources={vk: file_snapshot(path) for vk, path in files_by_viewkey.items()},
                remote_dir=f"pikpak:Pornhub/{uploader_folder}",
            )

        async def fail_download(**kwargs):
            raise AssertionError("reconciled local file must not be downloaded again")

        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(tm_mod, "upload_file_batch", fake_upload)
        monkeypatch.setattr(tm_mod, "download_mp4_with_resume", fail_download)

        tm.start()
        job = await tm.enqueue_job("auto_update", "补偿上传测试", {})
        done = await _wait_job_done(repo, job["id"])

        assert done is not None
        assert done["status"] == "completed"
        assert done["total_items"] == 1
        assert done["uploaded_items"] == 1
        assert uploaded == ["vkpending"]
        video = await repo.get_video("vkpending")
        assert video and video["status"] == "uploaded"
        assert not local_file.exists() and video["local_path"] == ""
    finally:
        await tm.stop()
        await db.close()


async def test_target_scan_errors_mark_job_partial_failed(tmp_path: Path, monkeypatch):
    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        await repo.update_settings_patch({"ph_cookie": "sess=valid", "uploader_enabled": False})
        await repo.upsert_target("alice", "model", enabled=True)

        from app.engine import task_manager as tm_mod

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        async def broken_scan(*args, **kwargs):
            raise RuntimeError("list page unavailable")

        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(tm_mod, "scan_list_pages", broken_scan)

        tm.start()
        job = await tm.enqueue_job("check_updates", "失败目标测试", {})
        done = await _wait_job_done(repo, job["id"])

        assert done is not None
        assert done["status"] == "partial_failed"
        assert "目标检查失败" in done["error_message"]
    finally:
        await tm.stop()
        await db.close()


async def test_scheduler_catches_up_after_scheduled_time(tmp_path: Path):
    from datetime import datetime
    from app.engine.scheduler import UpdateScheduler

    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()

    try:
        await repo.update_settings_patch({"schedule_enabled": True, "schedule_time": "00:00"})
        triggered = []

        async def _trigger():
            triggered.append(True)
            return 42

        sched = UpdateScheduler(repo=repo, events=events, trigger_auto_update=_trigger)
        sched.start()
        await asyncio.sleep(0.1)
        await sched.stop()

        assert triggered == [True]
        assert await repo.get_scheduler_last_run_date() == datetime.now().strftime("%Y-%m-%d")
    finally:
        await db.close()


async def test_retry_items_broadcasts_all_pending_items_and_prioritizes_partial_tmp(tmp_path: Path, monkeypatch):
    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        await repo.update_settings_patch({"ph_cookie": "sess=valid", "uploader_enabled": False, "max_concurrent_downloads": 1})

        # Seed a cancelled source job with 3 items; item #2 has a partial .mp4.tmp file on disk
        src = await repo.create_job("manual_target", "下载目标: lovely-asuna", {"usernames": ["lovely-asuna"], "auto_upload": False})
        await repo.update_job(src["id"], status="cancelled")
        partial_dir = env.temp_dir / "lovely-asuna"
        partial_dir.mkdir(parents=True, exist_ok=True)
        partial_file = partial_dir / "Clip_2_[vk2].mp4.tmp"
        partial_file.write_bytes(b"x" * 2048)

        for idx, (vk, tmp_p) in enumerate([("vk1", ""), ("vk2", str(partial_file)), ("vk3", "")], start=1):
            await repo.add_job_item(
                src["id"],
                viewkey=vk,
                title=f"Clip {idx}",
                uploader="lovely-asuna",
                target_type="model",
                page_url=f"https://www.pornhub.com/view_video.php?viewkey={vk}",
                download_url=f"https://cdn.example/{vk}.mp4",
                referer_url=f"https://www.pornhub.com/view_video.php?viewkey={vk}",
                quality="1080p",
                status="cancelled",
                temp_path=tmp_p,
            )

        from app.engine import task_manager as tm_mod

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        recorded_events = []
        orig_broadcast = events.broadcast

        def spy_broadcast(event_type, data):
            recorded_events.append((event_type, dict(data) if isinstance(data, dict) else data))
            return orig_broadcast(event_type, data)

        monkeypatch.setattr(events, "broadcast", spy_broadcast)
        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)

        dl_order = []

        async def fake_download(**kwargs):
            dl_order.append(kwargs["viewkey"])
            out = kwargs["download_dir"] / f"{kwargs['viewkey']}.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(mp4_payload())
            return DownloadOutcome(success=True, local_path=out, file_size=out.stat().st_size, quality="1080p")

        monkeypatch.setattr(tm_mod, "download_mp4_with_resume", fake_download)

        tm.start()
        retry_job = await tm.enqueue_job(
            "retry_items",
            "续传 · 下载目标: lovely-asuna (3 项)",
            {"source_job_id": src["id"], "auto_upload": False},
        )
        done = await _wait_job_done(repo, retry_job["id"])
        assert done is not None and done["status"] == "completed"
        assert done["total_items"] == 3 and done["done_items"] == 3

        # 1. All 3 pending items MUST be broadcast as item_updated BEFORE downloading starts!
        pending_broadcasts = [
            data for et, data in recorded_events
            if et == "item_updated" and data.get("job_id") == retry_job["id"] and data.get("status") == "pending"
        ]
        assert len(pending_broadcasts) == 3
        # Partial tmp file item vk2 is prioritized first and carries its existing bytes_downloaded!
        assert pending_broadcasts[0]["viewkey"] == "vk2"
        assert pending_broadcasts[0]["bytes_downloaded"] == 2048
        assert dl_order == ["vk2", "vk1", "vk3"]

        # 2. job_updated with total_items=3 MUST be broadcast as soon as items are created
        job_totals = [
            data.get("total_items") for et, data in recorded_events
            if et == "job_updated" and data.get("id") == retry_job["id"]
        ]
        assert 3 in job_totals
    finally:
        await tm.stop()
        await db.close()


async def test_upload_streams_per_file_and_cancel_marks_remaining_without_spurious_batches(
    tmp_path: Path, monkeypatch
):
    """Per-file upload commits immediately, and cancelling stops new batches and marks leftovers cancelled."""
    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        await repo.update_settings_patch(
            {
                "ph_cookie": "sess=valid",
                "uploader_enabled": True,
                "rclone_transfers": 1,
                "upload_workers": 1,
                "upload_batch_size": 3,
                "max_pending_upload_files": 20,
                "min_free_disk_gb": 0.0,
                "min_file_size_bytes": 1,
                "page_delay_min": 0.0,
                "page_delay_max": 0.0,
            }
        )
        dl_dir = env.downloads_dir / "eve"
        dl_dir.mkdir(parents=True, exist_ok=True)
        local_files = {}
        for idx in range(1, 6):
            vk = f"vk{idx}"
            fp = dl_dir / f"Clip {idx} [{vk}].mp4"
            fp.write_bytes(mp4_payload())
            local_files[vk] = fp
            await repo.upsert_video(
                vk,
                title=f"Clip {idx}",
                uploader="eve",
                target_type="model",
                quality="1080p",
                video_page_url=f"https://www.pornhub.com/view_video.php?viewkey={vk}",
                status="downloaded",
                local_path=str(fp),
                file_size=fp.stat().st_size,
                upload_requested=True,
                downloaded_at=f"2026-11-25T00:00:0{idx}+00:00",
            )

        from app.engine import task_manager as tm_mod

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        upload_calls = []

        async def fake_upload(download_dir, uploader_folder, files_by_viewkey, settings, cancel_event=None):
            vkeys = list(files_by_viewkey.keys())
            upload_calls.append(vkeys)
            # First file succeeds immediately; second file sees cancel signal mid-upload.
            if len(upload_calls) == 1:
                return UploadBatchResult(
                    uploaded_viewkeys=vkeys,
                    failed_viewkeys=[],
                    verified_sources={vk: file_snapshot(p) for vk, p in files_by_viewkey.items()},
                    remote_dir=f"pikpak:Pornhub/{uploader_folder}",
                )
            if cancel_event is not None:
                cancel_event.set()
            return UploadBatchResult(
                uploaded_viewkeys=[],
                failed_viewkeys=vkeys,
                remote_dir=f"pikpak:Pornhub/{uploader_folder}",
                error="rclone 执行已取消",
            )

        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(tm_mod, "upload_file_batch", fake_upload)

        tm.start()
        job = await tm.enqueue_job("auto_update", "全量自动追更", {})
        done = await _wait_job_done(repo, job["id"], timeout=5.0)

        assert done is not None
        assert done["status"] == "cancelled"
        # File 1 uploaded & committed before cancel; files 2..5 must ALL be cancelled (NOT left as 'downloaded'!)
        assert upload_calls == [["vk1"], ["vk2"]]
        assert done["total_items"] == 5
        assert done["uploaded_items"] == 1
        assert done["done_items"] == 1
        assert done["failed_items"] == 0

        items = await repo.list_job_items(job["id"])
        statuses = {it["viewkey"]: it["status"] for it in items}
        assert statuses == {
            "vk1": "done",
            "vk2": "cancelled",
            "vk3": "cancelled",
            "vk4": "cancelled",
            "vk5": "cancelled",
        }
        # File 1 was cleaned up locally; files 2..5 remain untouched on disk.
        assert not local_files["vk1"].exists()
        assert all(local_files[f"vk{i}"].exists() for i in range(2, 6))
    finally:
        await tm.stop()
        await db.close()


async def test_page1_only_error_keeps_catch_up_enabled_on_next_update(tmp_path: Path, monkeypatch):
    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        await repo.update_settings_patch(
            {"ph_cookie": "sess=valid", "uploader_enabled": False, "page_delay_min": 0.0, "page_delay_max": 0.0}
        )
        target = await repo.upsert_target("miuzxc", "model", enabled=True)
        await repo.mark_target_checked(target["id"], 0, error="列表第 1 页: HTTP 403")

        from app.engine import task_manager as tm_mod

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        seen_catch_up = []

        async def fake_scan(ctx, initial_url, selectors, *, catch_up=False, **kwargs):
            seen_catch_up.append(catch_up)
            return []

        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(tm_mod, "scan_list_pages", fake_scan)

        tm.start()
        job = await tm.enqueue_job("auto_update", "指定追更: Miuzxc", {"target_ids": [target["id"]]})
        done = await _wait_job_done(repo, job["id"], timeout=5.0)
        assert done is not None and done["status"] == "completed"
        assert seen_catch_up == [True]
    finally:
        await tm.stop()
        await db.close()


async def test_adaptive_disk_scheduler_waterlines_priority_and_large_file_fairness(tmp_path: Path):
    from types import SimpleNamespace
    from app.engine.disk_scheduler import AdaptiveDiskScheduler, GIB, MIB, SmartUploadPool, UploadQueuedItem
    from app.services.downloader import DownloadDiskBudget

    simulated_free = {"bytes": 20 * GIB}
    budget = DownloadDiskBudget(tmp_path, keep_free_bytes=2 * GIB)
    sched = AdaptiveDiskScheduler(
        tmp_path,
        budget,
        max_downloads=5,
        max_uploads=2,
        min_free_disk_gb=2.0,
        disk_usage_fn=lambda _p: SimpleNamespace(free=simulated_free["bytes"]),
    )

    # Green zone: >= 8 GB headroom -> full 5 downloads, FIFO upload
    lvl, allowed, _, _ = sched.compute_waterline()
    assert lvl == "green" and allowed == 5 and sched.prefer_largest_upload is False

    # Yellow zone: 2 GB floor + 5 GB headroom = 7 GB free -> throttled to 2 downloads, Largest-First upload
    simulated_free["bytes"] = 7 * GIB
    lvl, allowed, _, _ = sched.compute_waterline()
    assert lvl == "yellow" and allowed == 2 and sched.prefer_largest_upload is True

    # Orange zone: 2 GB floor + 1 GB headroom = 3 GB free -> 1 download
    simulated_free["bytes"] = 3 * GIB
    lvl, allowed, _, _ = sched.compute_waterline()
    assert lvl == "orange" and allowed == 1 and sched.prefer_largest_upload is True

    # Red zone: <= 2 GB floor -> 0 downloads
    simulated_free["bytes"] = 2 * GIB
    lvl, allowed, _, _ = sched.compute_waterline()
    assert lvl == "red" and allowed == 0

    # SmartUploadPool switches between FIFO (green) and Largest-First (tight)
    pool = SmartUploadPool(sched, batch_trigger_size=1, chunk_size=1, allow_small_micro_batch=False)
    small = UploadQueuedItem(1, "vk_s", "alice", tmp_path, tmp_path / "s.mp4", file_size=200 * MIB)
    big = UploadQueuedItem(2, "vk_b", "alice", tmp_path, tmp_path / "b.mp4", file_size=1500 * MIB)
    mid = UploadQueuedItem(3, "vk_m", "alice", tmp_path, tmp_path / "m.mp4", file_size=600 * MIB)
    for item in (small, big, mid):
        await pool.put(item)

    cancel = asyncio.Event()
    # Tight (red) -> largest first (big: 1500 MiB)
    first_chunk = await pool.take_next_chunk(cancel)
    assert first_chunk is not None and [x.viewkey for x in first_chunk] == ["vk_b"]

    # Back to green -> FIFO (small before mid)
    simulated_free["bytes"] = 20 * GIB
    second_chunk = await pool.take_next_chunk(cancel)
    assert second_chunk is not None and [x.viewkey for x in second_chunk] == ["vk_s"]

    # True physical oversize fails fast when no upload/download can free space
    simulated_free["bytes"] = 3 * GIB  # only 1 GiB above 2 GiB floor
    ok, err = await sched.acquire_download_slot(will_upload=True, required_bytes=2 * GIB, cancel_event=cancel)
    assert ok is False and "超过 VPS 可用空间" in err


async def test_tight_disk_suspends_download_until_upload_frees_space(tmp_path: Path, monkeypatch):
    """When disk_wait_bytes triggers while uploads are pending, download suspends and resumes instead of failing."""
    from types import SimpleNamespace
    from app.engine import task_manager as tm_mod
    from app.engine.disk_scheduler import GIB

    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        await repo.update_settings_patch(
            {
                "ph_cookie": "sess=valid",
                "uploader_enabled": True,
                "max_concurrent_downloads": 2,
                "upload_workers": 1,
                "upload_batch_size": 1,
                "min_free_disk_gb": 2.0,
                "min_file_size_bytes": 1,
            }
        )

        # Start with 3.5 GiB free (1.5 GiB headroom above 2 GiB floor).
        disk_free = {"bytes": int(3.5 * GIB)}
        monkeypatch.setattr(tm_mod.shutil, "disk_usage", lambda _p: SimpleNamespace(free=disk_free["bytes"]))

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        async def fake_fetch_batch(ctx, items, selectors, **kwargs):
            res = []
            for url, pn, itn in items:
                vk = url.split("viewkey=")[1]
                res.append(
                    VideoDetail(
                        viewkey=vk,
                        title=f"Video {vk}",
                        page_url=url,
                        referer_url=url,
                        download_url=f"https://cdn.example/{vk}.mp4",
                        quality="2160p",
                        uploader_username="alice",
                        uploader_type="model",
                        page_num=pn,
                        item_num=itn,
                    )
                )
            return res

        dl_attempts = {"vk1": 0, "vk2": 0}
        vk1_uploaded = asyncio.Event()

        async def fake_download(*, download_dir: Path, viewkey: str, title: str, **kwargs):
            dl_attempts[viewkey] += 1
            download_dir.mkdir(parents=True, exist_ok=True)
            # First attempt of vk2 needs 2 GiB (> 1.5 GiB headroom) -> triggers suspend/wait!
            if viewkey == "vk2" and dl_attempts["vk2"] == 1:
                return DownloadOutcome(
                    success=False,
                    error="磁盘余量不足，无法为此下载预留空间",
                    disk_wait_bytes=2 * GIB,
                )
            out = download_dir / f"{title} [{viewkey}].mp4"
            out.write_bytes(mp4_payload())
            return DownloadOutcome(
                success=True,
                local_path=out,
                file_size=out.stat().st_size,
                quality="2160p",
                download_url=f"https://cdn.example/{viewkey}.mp4",
            )

        async def fake_upload(download_dir, uploader_folder, files_by_viewkey, settings, cancel_event=None, **kwargs):
            await asyncio.sleep(0.05)
            # Uploading vk1 frees 2 GiB of disk space, raising headroom to 3.5 GiB >= 2 GiB!
            if "vk1" in files_by_viewkey:
                disk_free["bytes"] = int(5.5 * GIB)
                vk1_uploaded.set()
            return UploadBatchResult(
                uploaded_viewkeys=list(files_by_viewkey.keys()),
                failed_viewkeys=[],
                verified_sources={vk: file_snapshot(p) for vk, p in files_by_viewkey.items()},
                remote_dir=f"pikpak:Pornhub/{uploader_folder}",
            )

        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(tm_mod, "fetch_video_details_batch", fake_fetch_batch)
        monkeypatch.setattr(tm_mod, "download_mp4_with_resume", fake_download)
        monkeypatch.setattr(tm_mod, "upload_file_batch", fake_upload)

        tm.start()
        job = await tm.enqueue_job(
            "manual_urls",
            "动态磁盘挂起恢复测试",
            {
                "urls": [
                    "https://www.pornhub.com/view_video.php?viewkey=vk1",
                    "https://www.pornhub.com/view_video.php?viewkey=vk2",
                ],
                "auto_upload": True,
            },
        )
        done = await _wait_job_done(repo, job["id"], timeout=5.0)
        assert done is not None
        assert done["status"] == "completed"
        assert done["uploaded_items"] == 2
        assert done["failed_items"] == 0
        assert dl_attempts["vk2"] == 2
        assert vk1_uploaded.is_set()
    finally:
        await tm.stop()
        await db.close()


async def test_streaming_preparse_overlaps_download_and_runs_multi_worker_uploads(tmp_path: Path, monkeypatch):
    """manual_target starts downloading before later items finish parsing and runs 2 upload workers concurrently."""
    from app.engine import task_manager as tm_mod

    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        await repo.update_settings_patch(
            {
                "ph_cookie": "sess=valid",
                "uploader_enabled": True,
                "max_concurrent_downloads": 3,
                "max_concurrent_video_details": 2,
                "upload_workers": 2,
                "upload_batch_size": 1,
                "min_free_disk_gb": 0.0,
                "min_file_size_bytes": 1,
                "page_delay_min": 0.0,
                "page_delay_max": 0.0,
            }
        )

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        async def fake_scan_list(ctx, initial_url, selectors, **kwargs):
            return [
                ListedVideo(f"vk{i}", f"https://www.pornhub.com/view_video.php?viewkey=vk{i}", f"Clip {i}", 1, i, 4)
                for i in range(1, 5)
            ]

        first_download_started = asyncio.Event()
        parse_overlapped_download = False

        async def fake_fetch_batch(ctx, items, selectors, **kwargs):
            nonlocal parse_overlapped_download
            url, pn, itn = items[0]
            vk = url.split("viewkey=")[1]
            if vk == "vk4":
                # Wait briefly to prove earlier items already entered download while vk4 is still parsing
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(first_download_started.wait(), timeout=0.5)
                if first_download_started.is_set():
                    parse_overlapped_download = True
            return [
                VideoDetail(
                    viewkey=vk,
                    title=f"Clip {vk}",
                    page_url=url,
                    referer_url=url,
                    download_url=f"https://cdn.example/{vk}.mp4",
                    quality="1080p",
                    uploader_username="alice",
                    uploader_type="model",
                    page_num=pn,
                    item_num=itn,
                )
            ]

        async def fake_download(*, download_dir: Path, viewkey: str, title: str, **kwargs):
            first_download_started.set()
            download_dir.mkdir(parents=True, exist_ok=True)
            out = download_dir / f"{title} [{viewkey}].mp4"
            out.write_bytes(mp4_payload())
            return DownloadOutcome(
                success=True,
                local_path=out,
                file_size=out.stat().st_size,
                quality="1080p",
                download_url=f"https://cdn.example/{viewkey}.mp4",
            )

        active_uploads = 0
        max_concurrent_uploads = 0
        seen_upload_phases: list[str] = []

        async def fake_upload(
            download_dir,
            uploader_folder,
            files_by_viewkey,
            settings,
            cancel_event=None,
            *,
            session_cache=None,
            on_progress=None,
        ):
            nonlocal active_uploads, max_concurrent_uploads
            assert session_cache is not None
            active_uploads += 1
            max_concurrent_uploads = max(max_concurrent_uploads, active_uploads)
            try:
                for vk, p in files_by_viewkey.items():
                    if on_progress is not None:
                        sz = p.stat().st_size
                        on_progress(vk, sz // 2, sz, 120_000_000.0, "uploading")
                        seen_upload_phases.append("uploading")
                await asyncio.sleep(0.08)
                for vk, p in files_by_viewkey.items():
                    if on_progress is not None:
                        sz = p.stat().st_size
                        on_progress(vk, sz, sz, 0.0, "verifying")
                        seen_upload_phases.append("verifying")
                return UploadBatchResult(
                    uploaded_viewkeys=list(files_by_viewkey.keys()),
                    failed_viewkeys=[],
                    verified_sources={vk: file_snapshot(p) for vk, p in files_by_viewkey.items()},
                    remote_dir=f"pikpak:Pornhub/{uploader_folder}",
                )
            finally:
                active_uploads -= 1

        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(tm_mod, "scan_list_pages", fake_scan_list)
        monkeypatch.setattr(tm_mod, "fetch_video_details_batch", fake_fetch_batch)
        monkeypatch.setattr(tm_mod, "download_mp4_with_resume", fake_download)
        monkeypatch.setattr(tm_mod, "upload_file_batch", fake_upload)

        tm.start()
        job = await tm.enqueue_job(
            "manual_target",
            "下载目标: alice",
            {"usernames": ["alice"], "target_type": "model", "pages": "1", "auto_upload": True},
        )
        done = await _wait_job_done(repo, job["id"], timeout=5.0)
        assert done is not None
        assert done["status"] == "completed"
        assert done["uploaded_items"] == 4
        assert parse_overlapped_download is True
        assert max_concurrent_uploads == 2
        assert "uploading" in seen_upload_phases and "verifying" in seen_upload_phases
        assert events.scheduler_status_snapshot() == {}
    finally:
        await tm.stop()
        await db.close()


async def test_adaptive_scheduler_emits_active_download_changes_immediately(tmp_path: Path):
    """Acquiring/releasing download slots right after initial emit must never be throttled away."""
    from types import SimpleNamespace
    from app.engine.disk_scheduler import AdaptiveDiskScheduler, GIB
    from app.services.downloader import DownloadDiskBudget

    emitted: list[dict] = []
    budget = DownloadDiskBudget(tmp_path, 4 * GIB)
    sched = AdaptiveDiskScheduler(
        tmp_path,
        budget,
        max_downloads=5,
        max_uploads=4,
        min_free_disk_gb=4.0,
        disk_usage_fn=lambda _: SimpleNamespace(free=40 * GIB),
        on_status=emitted.append,
    )
    cancel = asyncio.Event()

    sched.emit_status(force=True)
    ok1, _ = await sched.acquire_download_slot(will_upload=True, cancel_event=cancel)
    ok2, _ = await sched.acquire_download_slot(will_upload=True, cancel_event=cancel)
    assert ok1 and ok2
    # Calling emit_status again with identical counts within 0.8s should still be throttled
    before_noop = len(emitted)
    sched.emit_status()
    assert len(emitted) == before_noop

    await sched.release_download_slot()
    await sched.release_download_slot()

    assert [s["active_downloads"] for s in emitted] == [0, 1, 2, 1, 0]


async def test_check_updates_emits_target_scan_progress_and_clears_on_finish(tmp_path: Path, monkeypatch):
    """check_updates broadcasts per-target scan progress (0..N/N) and clears scan_status when done."""
    from app.engine import task_manager as tm_mod

    env = EnvSettings(data_dir=tmp_path / "data")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = TaskManager(env=env, repo=repo, events=events)

    try:
        await repo.update_settings_patch(
            {
                "ph_cookie": "sess=valid",
                "uploader_enabled": False,
                "page_delay_min": 0.0,
                "page_delay_max": 0.0,
            }
        )
        await repo.upsert_target("alice", "model", display_name="Alice Star")
        await repo.upsert_target("bob", "model", display_name="Bob Pro")

        scan_events: list[dict] = []
        orig_emit_scan = events.emit_scan_status

        def spy_emit_scan(job_id: int, status: dict) -> None:
            scan_events.append(dict(status))
            orig_emit_scan(job_id, status)

        monkeypatch.setattr(events, "emit_scan_status", spy_emit_scan)

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        async def fake_scan_list(ctx, initial_url, selectors, **kwargs):
            return []

        monkeypatch.setattr(tm_mod, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(tm_mod, "scan_list_pages", fake_scan_list)

        tm.start()
        job = await tm.enqueue_job("check_updates", "全量检查更新", {"target_ids": None})
        done = await _wait_job_done(repo, job["id"], timeout=5.0)
        assert done is not None
        assert done["status"] == "completed"
        assert [e["scanned"] for e in scan_events] == [0, 1, 1, 2]
        assert all(e["mode"] == "targets" and e["total"] == 2 for e in scan_events)
        assert scan_events[0]["current_target"] == "Alice Star"
        assert scan_events[2]["current_target"] == "Bob Pro"
        assert events.scan_status_snapshot() == {}
    finally:
        await tm.stop()
        await db.close()
