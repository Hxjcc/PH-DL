"""Exercise the stop request, concurrent teardown, and terminal event boundaries."""

import asyncio
import os
import signal
import sys
import threading
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.engine import task_manager as engine
from app.services import hls_downloader, subprocesses
from app.services.downloader import DownloadOutcome
from app.services.uploader import UploadBatchResult
from tests.media_fixtures import mp4_payload
from tests.test_api_e2e import _make_client


@pytest.mark.parametrize("cancel_task", [False, True])
async def test_subprocess_cancellation_kills_and_reaps_a_stubborn_child(monkeypatch, cancel_task):
    processes = []
    ready = asyncio.Event()
    started = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await started(*args, **kwargs)
        processes.append(process)
        return process

    def filter_line(line):
        if line == "child ready":
            ready.set()
        return line

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    if os.name == "nt":
        # Windows terminate() uses TerminateProcess. Simulate an ignored
        # graceful stop, while still testing real process creation and reaping.
        monkeypatch.setattr(asyncio.subprocess.Process, "terminate", lambda self: None)

    stop = asyncio.Event()
    task = asyncio.create_task(subprocesses.run_bounded_process(
        [sys.executable, "-u", "-c", (
            "import signal, sys, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('child ready', file=sys.stderr); time.sleep(30)"
        )],
        timeout=10,
        cancel_event=stop,
        filter_stderr=filter_line,
    ))
    try:
        await asyncio.wait_for(ready.wait(), timeout=4)
        started_at = time.perf_counter()
        if cancel_task:
            task.cancel()
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
        else:
            assert not task.done()
            stop.set()
            assert await asyncio.wait_for(task, timeout=1) == (130, "", "child ready")
        assert time.perf_counter() - started_at < 1
        assert len(processes) == 1
        process = processes[0]
        assert process.returncode is not None and process.returncode != 0
        assert await process.wait() == process.returncode
        if os.name != "nt":
            assert process.returncode == -signal.SIGKILL
        assert process.stdout.at_eof() and process.stderr.at_eof()
    finally:
        stop.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)


def test_cancel_api_wakes_pipeline_and_publishes_terminal_state_before_reclaim(tmp_path, monkeypatch):
    download_started = threading.Event()
    upload_started = threading.Event()
    parser_started = threading.Event()
    pool_woken = threading.Event()
    scheduler_woken = threading.Event()
    terminal_sent = threading.Event()
    reclaim_started = threading.Event()
    finish_download = asyncio.Event()
    finish_reclaim = asyncio.Event()
    enqueued = []
    terminal_state = []
    live_size = len(mp4_payload())
    upload_input = "uploaded-later"
    download_input = "completed-on-stop"
    page = "https://xhamster.com/videos/fixture-1234"

    async def execute(manager, job_id, _params, settings, ctx):
        local = manager.env.downloads_dir / "xHamster" / "fixture" / "ready.mp4"
        local.parent.mkdir(parents=True)
        local.write_bytes(mp4_payload())
        rows = await manager.repo.add_job_items_batch(job_id, [
            {
                "site": "xhamster", "viewkey": key, "title": key, "uploader": "fixture", "target_type": "users",
                "page_url": page, "download_url": "https://cdn.xhcdn.com/fixture.mp4", "quality": "1080p",
                "local_path": str(local) if index == 0 else "", "upload_requested": True,
            }
            for index, key in enumerate([upload_input, download_input, "parse-in-flight", *(f"pending{i}" for i in range(60))])
        ])
        parse_id = rows[2]["id"]
        await manager._run_download_upload_pipeline(
            job_id, [row["id"] for row in rows], settings, ctx, auto_upload=True,
            parse_specs={parse_id: engine._PendingParseSpec(parse_id, page, 1, 1, "fixture", "users", site="xhamster")},
        )

    async def download(**kwargs):
        assert kwargs["viewkey"] == download_input
        kwargs["on_progress"](1, live_size, 64, "downloading")
        download_started.set()
        await kwargs["cancel_event"].wait()
        kwargs["on_progress"](2, live_size, 999, "downloading")
        await finish_download.wait()
        path = kwargs["download_dir"] / "finished.mp4"
        path.write_bytes(mp4_payload())
        return DownloadOutcome(success=True, local_path=path, file_size=live_size, quality="1080p")

    async def upload(*, files_by_viewkey, cancel_event, on_progress, **_kwargs):
        assert list(files_by_viewkey) == [upload_input]
        on_progress(upload_input, 1, live_size, 64, "uploading")
        upload_started.set()
        await cancel_event.wait()
        on_progress(upload_input, 2, live_size, 999, "uploading")
        return UploadBatchResult(error="rclone 执行已取消", failed_viewkeys=[upload_input])

    async def parse(ctx, rows, *_args, **_kwargs):
        parser_started.set()
        await ctx.cancel_event.wait()
        return []

    async def reclaim(_db):
        reclaim_started.set()
        await finish_reclaim.wait()

    original_notify_cancel = engine.SmartUploadPool.notify_cancel
    original_notify_changed = engine.AdaptiveDiskScheduler.notify_changed
    original_put = engine.SmartUploadPool.put

    async def notify_cancel(pool):
        await original_notify_cancel(pool)
        pool_woken.set()

    async def notify_changed(scheduler, **kwargs):
        await original_notify_changed(scheduler, **kwargs)
        scheduler_woken.set()

    async def put(pool, item, **kwargs):
        enqueued.append(item.viewkey)
        return await original_put(pool, item, **kwargs)

    monkeypatch.setattr(engine.TaskManager, "_exec_manual_urls", execute)
    monkeypatch.setattr(engine, "download_mp4_with_resume", download)
    monkeypatch.setattr(engine, "upload_file_batch", upload)
    monkeypatch.setattr(engine, "fetch_video_details_batch", parse)
    monkeypatch.setattr(engine, "reclaim_memory", reclaim)
    monkeypatch.setattr("app.main.UpdateScheduler.start", lambda _self: None)
    monkeypatch.setattr(engine.SmartUploadPool, "notify_cancel", notify_cancel)
    monkeypatch.setattr(engine.SmartUploadPool, "put", put)
    monkeypatch.setattr(engine.AdaptiveDiskScheduler, "notify_changed", notify_changed)

    with _make_client(tmp_path) as client:
        assert client.post("/api/auth/login", json={"password": "super-secret-password"}).status_code == 200
        assert client.put("/api/settings", json={
            "max_concurrent_downloads": 1, "upload_workers": 2, "upload_batch_size": 1,
            "min_free_disk_gb": 0, "min_file_size_bytes": 64,
        }).status_code == 200
        container = client.app.state.container
        events = container.events
        original_broadcast = events.broadcast

        def broadcast(event_type, data):
            if event_type == "job_updated" and data["status"] in {"cancelled", "failed", "partial_failed", "completed"}:
                terminal_state.append((
                    container.tasks.current_job_id, container.tasks._current_cancel_event,
                    container.tasks.cancelling_job_id, container.tasks._current_halt_reason,
                    events.live_progress_snapshot(), events.cancelling_jobs_snapshot(),
                ))
            original_broadcast(event_type, data)
            if terminal_state:
                terminal_sent.set()

        monkeypatch.setattr(events, "broadcast", broadcast)
        try:
            with client.websocket_connect("/api/ws") as stream:
                assert stream.receive_json()["data"]["cancelling_jobs"] == []
                job = client.portal.call(container.tasks.enqueue_job, "manual_urls", "Cancel fixture", {"urls": [page]})
                job_id = job["id"]
                assert download_started.wait(5), "download did not start"
                assert upload_started.wait(5), "upload did not start"
                assert parser_started.wait(5), "parser did not start"
                assert client.get("/api/overview").json()["cancelling_job_id"] is None
                for _ in range(2):
                    assert client.post(f"/api/jobs/{job_id}/cancel").json() == {"cancelled": True}

                assert pool_woken.wait(2) and scheduler_woken.wait(2)
                assert not terminal_sent.is_set()
                overview = client.get("/api/overview").json()
                assert overview["running"] and overview["current_job"]["id"] == overview["cancelling_job_id"] == job_id
                with client.websocket_connect("/api/ws") as reload_stream:
                    snapshot = reload_stream.receive_json()
                    assert snapshot["data"]["cancelling_jobs"] == [job_id]
                    assert all(snapshot["data"][key] == {} for key in ("live_progress", "scheduler_status", "scan_status"))

                client.portal.call(finish_download.set)
                assert terminal_sent.wait(5) and reclaim_started.wait(5)
                observed = []
                while True:
                    event = stream.receive_json()
                    observed.append(event)
                    if event["type"] == "job_updated" and event["data"]["status"] == "cancelled":
                        break
                assert stream.receive_json()["type"] == "overview_changed"
                updates = [e["data"] for e in observed if e["type"] == "job_cancelling"]
                assert updates == [{"job_id": job_id, "cancelling": True}, {"job_id": job_id, "cancelling": False}]
                stop_index = next(i for i, event in enumerate(observed) if event["type"] == "job_cancelling")
                after_stop = observed[stop_index + 1:]
                assert [e["type"] for e in after_stop].count("job_items_changed") == 1
                assert [e["type"] for e in after_stop].count("job_updated") == 1
                assert not any(e["type"] in {"item_updated", "item_progress"} for e in after_stop)
                assert terminal_state == [(None, None, None, "", {}, [])]
                ready = client.get("/api/overview").json()
                assert not ready["running"] and ready["current_job"] is None and ready["cancelling_job_id"] is None
                detail = client.get(f"/api/jobs/{job_id}").json()
                items = {item["viewkey"]: item for item in detail["items"]}
                assert len(items) == 63 and all(item["status"] == "cancelled" for item in items.values())
                assert enqueued == [upload_input]
                for key in (upload_input, download_input):
                    assert items[key]["error_message"] == "上传已取消，保留本地文件"
                    assert items[key]["local_path"]
                logs = [e["data"] for e in after_stop if e["type"] == "log"]
                assert sum("收到停止指令" in log["message"] for log in logs) == 1
                assert sum(
                    log["level"] == "INFO" and log["message"].startswith("[上传已取消]") for log in logs
                ) == 2
                assert not any("[上传失败]" in log["message"] for log in logs)
                assert client.delete(f"/api/jobs/{job_id}").status_code == 200
                assert (container.env.downloads_dir / "xHamster" / "fixture" / "ready.mp4").read_bytes() == mp4_payload()
                assert (container.env.downloads_dir / "xHamster" / "fixture" / "finished.mp4").read_bytes() == mp4_payload()
        finally:
            client.portal.call(finish_download.set)
            client.portal.call(finish_reclaim.set)


async def test_both_sites_broadcast_cookie_validation_and_reset(tmp_path):
    events = engine.EventBus()
    repo = type("Repo", (), {"redact_diagnostic": staticmethod(lambda _message: "redacted")})()
    manager = engine.TaskManager(env=engine.EnvSettings(data_dir=tmp_path), repo=repo, events=events)
    queue = events.subscribe()
    for set_status, reset_status, name in (
        (manager.set_cookie_status, manager.reset_cookie_status, "cookie_status"),
        (manager.set_xh_cookie_status, manager.reset_xh_cookie_status, "xh_cookie_status"),
    ):
        set_status(True, "secret")
        assert queue.get_nowait()["type"] == "overview_changed"
        assert getattr(manager, name) == {"valid": True, "message": "redacted"}
        reset_status()
        assert queue.get_nowait()["type"] == "overview_changed"
        assert getattr(manager, name)["valid"] is None
    assert queue.empty()


async def test_early_hls_stop_leaves_no_segment_or_writer_tasks(tmp_path, monkeypatch):
    stop = asyncio.Event()
    errors = []
    original = hls_downloader._write_hls_manifest_atomic
    budget = hls_downloader.HlsMemoryBudget()

    async def serve(request):
        if request.path != "/playlist":
            errors.append(request.path)
            return web.Response(status=500)
        return web.Response(text="#EXTM3U\n#EXTINF:1,\n/segment.ts\n#EXT-X-ENDLIST\n")

    def persist(folder, meta):
        original(folder, meta)
        stop.set()

    monkeypatch.setattr(hls_downloader, "_write_hls_manifest_atomic", persist)
    app = web.Application()
    app.router.add_get("/{asset}", serve)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        result = await asyncio.wait_for(hls_downloader._download_hls_aiohttp(
            session=session, playlist_url=str(server.make_url("/playlist")), hls_dir=tmp_path,
            viewkey="fixture", quality="1080p", referer="", cookie_str="", proxy_url=None,
            segment_concurrency=12, request_timeout=10, stop=stop, memory_budget=budget, spill_to_disk=True,
            prepare_write=lambda _: None, on_tick=lambda *_: None,
        ), timeout=2)
        assert result.cancelled and not errors and budget.used_bytes == 0
        assert not {task.get_coro().__qualname__ for task in asyncio.all_tasks()} & {
            "_download_hls_aiohttp.<locals>." + name for name in ("_writer", "_fetcher", "_watch_stop")
        }
        assert not list(tmp_path.glob("seg_*.tmp"))
