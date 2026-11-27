"""Regression coverage for resource ownership, transaction boundaries, and outbound policy."""

from __future__ import annotations

import asyncio
import gzip
import sys
import threading
from types import SimpleNamespace

import pytest
from aiohttp import web
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import EnvSettings, RuntimeSettings
from app.db.database import Database
from app.db.repo import Repository
from app.engine.disk_scheduler import AdaptiveDiskScheduler
from app.main import create_app
from app.parsers import challenge, http_session, video
from app.services import hls_downloader, playback, subprocesses, uploader
from app.services.downloader import DownloadDiskBudget, read_download_checkpoint
from app.services.filename import sanitize_filename, video_filename_base
from app.services.http_proxy import MediaPolicyError, open_media_response


class SyntheticResponse:
    def __init__(self, url, *, data=b"", chunks=None, status=200, headers=None):
        self.url = url
        self.status = status
        self.headers = headers or {}
        self.content_length = len(data) if chunks is None else None
        self.content = SimpleNamespace(iter_chunked=chunks or self._iter)
        self.data = data
        self.closed = False

    async def _iter(self, size):
        for start in range(0, len(self.data), size):
            yield self.data[start : start + size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.closed = True


async def test_sqlite_read_visibility_rollback_and_settings_patch(tmp_path):
    db = Database(tmp_path / "audit.db")
    await db.connect()
    repo = Repository(db)
    try:
        job = await repo.create_job("manual_urls", "audit", {})
        with pytest.raises(ValueError):
            async with db.transaction() as conn:
                async with conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],)):
                    pass
                reader = asyncio.create_task(repo.get_job(job["id"]))
                await asyncio.sleep(0)
                assert not reader.done()
                raise ValueError("rollback fixture")
        assert (await reader)["status"] == "queued"
        await asyncio.gather(
            repo.update_settings_patch({"ph_password": "fixture-password"}),
            repo.update_settings_patch({"xh_password": "other-password"}),
        )
        settings = await repo.get_settings()
        assert (settings.ph_password, settings.xh_password) == ("fixture-password", "other-password")
        await repo.update_settings_patch({"ph_cookie": "sess=manually-imported"})
        assert await repo.save_refreshed_cookie("ph", settings, "sess=delayed-login", "") is None
        assert (await repo.get_settings()).ph_cookie == "sess=manually-imported"
        assert (await db.fetch_one("PRAGMA journal_size_limit"))["journal_size_limit"] == 16 * 1024 * 1024
        jobs = await asyncio.gather(*(repo.create_scheduled_job("2026-01-12") for _ in range(5)))
        assert len([job for job in jobs if job]) == 1
        assert await repo.get_scheduler_last_run_date() == "2026-01-12"
    finally:
        await db.close()


async def test_metadata_retry_keeps_original_limit_and_skips_missing_records(tmp_path):
    db = Database(tmp_path / "metadata.db")
    await db.connect()
    repo = Repository(db)
    try:
        job = await repo.create_job("enrich_metadata", "metadata", {"limit": 3})
        await repo.add_job_items_batch(job["id"], [
            {"viewkey": key, "status": status}
            for key, status in [("done", "done"), ("missing", "failed"), ("retry", "failed")]
        ])
        for key in ("done", "retry", "other"):
            await repo.upsert_video(key, status="baseline", video_page_url=f"https://www.pornhub.com/view_video.php?viewkey={key}")
        await repo.reconcile_metadata_job_items(job["id"])
        candidates = await repo.list_baseline_metadata_candidates(limit=3, resume_job_id=job["id"])
        assert [candidate["viewkey"] for candidate in candidates] == ["retry"]
        rows = await repo.list_job_items(job["id"])
        assert {row["viewkey"]: row["status"] for row in rows} == {"done": "done", "missing": "skipped", "retry": "pending"}
    finally:
        await db.close()


def test_credential_responses_are_redacted_and_mutations_require_matching_origin(tmp_path):
    env = EnvSettings(data_dir=tmp_path, admin_password="fixture-admin")
    with TestClient(create_app(env)) as client:
        invalid = client.post("/api/auth/login", json={"password": ["fixture-invalid-secret"]})
        assert invalid.status_code == 422 and "fixture-invalid-secret" not in invalid.text
        assert client.post("/api/auth/login", json={"password": "fixture-admin"}).status_code == 200
        credentials = {
            "ph_password": "fixture-site-password",
            "ph_cookie": "sess=fixture-cookie-value",
            "proxy_url": "socks5://proxy-user:fixture-proxy-password@proxy.example:5555",
            "serverchan_send_key": "fixture-send-key",
        }
        rejected = client.put("/api/settings", json=credentials, headers={
            "Origin": "http://testserver:8777", "X-Forwarded-Host": "testserver:8777",
        })
        assert rejected.status_code == 403
        accepted = client.put("/api/settings", json=credentials, headers={"Origin": "http://testserver"})
        assert accepted.status_code == 200 and "no-store" in accepted.headers["cache-control"]
        assert all(accepted.json()["settings"][name] == "********" for name in credentials)
        invalid = client.put("/api/settings", json={"ph_password": ["fixture-config-secret"]})
        assert invalid.status_code == 400 and "fixture-config-secret" not in invalid.text

        container = client.app.state.container
        container.events.emit_log("WARNING", "fixture-site-password fixture-cookie-value fixture-send-key "
                                  "https://media.example/file?auth=fixture-signed-query")
        logs = client.get("/api/settings/logs").text
        assert not any(secret in logs for secret in ("fixture-site-password", "fixture-cookie-value", "fixture-send-key", "fixture-signed-query"))

        async def seed_old_errors():
            job = await container.repo.create_job("manual_urls", "legacy error fixture", {})
            await container.db.execute("UPDATE jobs SET error_message=? WHERE id=?", ("fixture-site-password", job["id"]))
            await container.db.execute(
                "INSERT INTO job_items (job_id, updated_at, error_message) VALUES (?, ?, ?)",
                (job["id"], "2026-01-01", "fixture-cookie-value"),
            )
            return job["id"]

        job_id = client.portal.call(seed_old_errors)
        detail = client.get(f"/api/jobs/{job_id}")
        assert detail.status_code == 200 and "fixture-site-password" not in detail.text and "fixture-cookie-value" not in detail.text


async def test_site_redirects_are_checked_before_sending_credentials(monkeypatch):
    page = "https://www.pornhub.com/view_video.php?viewkey=fixture"
    cdn = "https://cdn.phncdn.com/media"
    calls = []

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            pass

        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            if url == page:
                return SimpleNamespace(url=url, status_code=302, text="", headers={"Location": cdn})
            return SimpleNamespace(url=url, status_code=302, text="", headers={"Location": "https://untrusted.example/"})

    monkeypatch.setattr(http_session, "CffiSession", Session)
    ctx = http_session.TaskHttpContext(site="pornhub", cookie_str="sess=sentinel")
    with pytest.raises(ValueError, match="允许范围"):
        await ctx._raw_get(page, ctx.merged_cookies(), headers={"Cookie": "sess=sentinel"})
    assert [url for url, _ in calls] == [page, cdn]
    assert calls[0][1]["cookies"] == {"sess": "sentinel"}
    assert calls[1][1]["cookies"] is None and "Cookie" not in calls[1][1]["headers"]
    assert not any(kwargs["allow_redirects"] for _, kwargs in calls)

    responses = [SyntheticResponse(page, status=302, headers={"Location": cdn}),
                 SyntheticResponse(cdn, status=302, headers={"Location": "https://untrusted.example/"})]
    requests = []

    def get(url, **kwargs):
        requests.append((url, kwargs))
        return responses[len(requests) - 1]

    with pytest.raises(MediaPolicyError):
        async with open_media_response(SimpleNamespace(get=get), page, headers={"Cookie": "sess=sentinel"}, site="pornhub"):
            pytest.fail("The untrusted redirect should be rejected")
    assert [url for url, _ in requests] == [page, cdn]
    assert not requests[-1][1]["headers"] and all(response.closed for response in responses)


async def test_page_body_and_cookie_jar_are_bounded(monkeypatch):
    monkeypatch.setattr(http_session, "_MAX_PAGE_BYTES", 32)
    monkeypatch.setattr(http_session, "_MAX_SESSION_COOKIES", 2)

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            pass

        async def get(self, url, **kwargs):
            receive = kwargs["content_callback"]
            assert receive(b"a" * 20) == 20
            assert receive(b"b" * 20) == http_session.CURL_WRITEFUNC_ERROR
            raise OSError("response aborted")

    monkeypatch.setattr(http_session, "CffiSession", Session)
    ctx = http_session.TaskHttpContext(site="pornhub")
    with pytest.raises(ValueError, match="体积"):
        await ctx._raw_get("https://www.pornhub.com/", {})
    await ctx._absorb_set_cookies(["a=1", "b=2", "c=3"])
    assert ctx.session_cookies == {"b": "2", "c": "3"}


def test_upstream_pow_has_a_finite_work_budget(monkeypatch):
    monkeypatch.setattr(challenge, "_MAX_FACTOR_STEPS", 1)
    assert challenge._least_factor(49) == 7
    with pytest.raises(ValueError, match="预算"):
        challenge._least_factor(65537)


async def test_large_detail_batch_creates_only_bounded_workers(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    count = 0

    async def parse(_ctx, url, *_args, **_kwargs):
        nonlocal count
        count += 1
        if count == 4:
            entered.set()
        await release.wait()
        return video.VideoDetail(url, "fixture", url, url)

    monkeypatch.setattr(video, "fetch_single_video_detail", parse)
    URLs = [(str(index), 1, index) for index in range(200)]
    before = asyncio.all_tasks()
    task = asyncio.create_task(video.fetch_video_details_batch(http_session.TaskHttpContext(), URLs, RuntimeSettings().selectors, concurrency=4))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert len(asyncio.all_tasks() - before) == 5
        release.set()
        results = await task
        assert [item.viewkey for item in results] == [url for url, *_ in URLs]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_playback_reads_all_chunks_and_bounds_gzip_inflation(monkeypatch):
    monkeypatch.setattr(playback, "_MAX_PLAYLIST_BYTES", 256)
    url = "https://cdn.xhcdn.com/playlist.m3u8"
    service = playback.PlaybackService()
    entry = playback._Playback("fixture", (), "https://xhamster.com/", "", None, 1e9, format="hls", site="xhamster")

    async def chunks(_size):
        yield b"#EXTM3U\n"
        yield b"#EXTINF:4,\nsegment.ts\n"

    response = await service._rewrite_playlist(SyntheticResponse(url, chunks=chunks), url, entry, 0)
    assert b"seg/0" in response.body
    assert entry.hls_asset_urls[0] == ["https://cdn.xhcdn.com/segment.ts"]
    zipped = gzip.compress(b"#EXTM3U\n" + b"#" * 1000)
    with pytest.raises(HTTPException, match="体积"):
        await service._rewrite_playlist(SyntheticResponse(url, data=zipped, headers={"Content-Encoding": "gzip"}), url, entry, 0)


@pytest.mark.parametrize("cancel", [False, True])
async def test_hls_bounds_out_of_order_chunks_and_releases_waiters(tmp_path, cancel):
    stop = asyncio.Event()
    let_head_finish = asyncio.Event()
    waiting_for_budget = asyncio.Event()
    read_past_budget = asyncio.Event()
    origin = "https://cdn.xhcdn.com/"
    responses = []

    async def head(_size):
        await let_head_finish.wait()
        yield b"head-segment"

    async def later(_size):
        yield b"later123"
        waiting_for_budget.set()
        yield b"later456"
        read_past_budget.set()

    def get(url, **_kwargs):
        if url.endswith(".m3u8"):
            response = SyntheticResponse(url, data=b"#EXTM3U\n#EXTINF:1,\nhead.ts\n#EXTINF:1,\nlater.ts\n")
        else:
            response = SyntheticResponse(url, chunks=head if url.endswith("head.ts") else later)
        responses.append(response)
        return response

    task = asyncio.create_task(hls_downloader._download_hls_aiohttp(
        session=SimpleNamespace(get=get), playlist_url=origin + "index.m3u8", hls_dir=tmp_path / "resume.hls",
        viewkey="fixture", quality="720p", referer="https://xhamster.com/", cookie_str="", proxy_url=None,
        segment_concurrency=2, max_buffer_bytes=8, request_timeout=10.0, stop=stop,
        prepare_write=lambda _size: None, on_tick=lambda *_args: None, site="xhamster",
    ))
    try:
        await asyncio.wait_for(waiting_for_budget.wait(), 2)
        assert not read_past_budget.is_set()
        (stop if cancel else let_head_finish).set()
        outcome = await asyncio.wait_for(task, 2)
        assert outcome.cancelled if cancel else outcome.ok
        if not cancel:
            assert (tmp_path / "resume.hls" / "stream.mp4").read_bytes() == b"head-segmentlater123later456"
        assert all(response.closed for response in responses)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_subprocess_bounds_output_and_preserves_later_error_lines(monkeypatch):
    monkeypatch.setattr(subprocesses, "_STDOUT_LIMIT", 2048)
    cmd = [sys.executable, "-c", "import sys; sys.stderr.write('x'*150000+'\\nfinal diagnostic\\n'); sys.stdout.write('x'*10000)"]
    lines = []
    rc, stdout, stderr = await uploader._run_cmd(cmd, timeout=5, on_stderr_line=lines.append)
    assert rc != 0 and not stdout and "标准输出超过" in stderr
    assert lines[-1] == "final diagnostic" and max(map(len, lines)) <= subprocesses._PIPE_CHUNK
    rc, err = await hls_downloader._run_ffmpeg(cmd, cancel_event=asyncio.Event(), timeout=5)
    assert rc == 0 and err.endswith("final diagnostic") and len(err) <= subprocesses._STDERR_LIMIT


async def test_native_hls_fallback_keeps_redirects_in_media_scope(tmp_path, monkeypatch):
    paths = []

    async def serve(request):
        paths.append(request.path)
        if request.path == "/outside.ts":
            return web.Response(body=b"should not be requested")
        if request.path == "/media/redirect.ts":
            return web.Response(status=302, headers={"Location": "/outside.ts"})
        if request.path == "/media/part.ts":
            return web.Response(body=b"native-fragment")
        segment = "redirect.ts" if "blocked" in request.path else "part.ts"
        return web.Response(text=f"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\n{segment}\n#EXT-X-ENDLIST\n",
                            content_type="application/vnd.apple.mpegurl")

    app = web.Application()
    app.router.add_get("/{route:.*}", serve)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    origin = f"http://127.0.0.1:{runner.addresses[0][1]}"
    monkeypatch.setattr(hls_downloader, "is_media_url", lambda url, site: site == "xhamster" and url.startswith(origin + "/media/"))

    async def download(name):
        folder = tmp_path / name
        folder.mkdir()
        return await asyncio.to_thread(
            hls_downloader._run_ytdlp_hls_sync, playlist_url=f"{origin}/media/{name}.m3u8", hls_dir=folder,
            referer="https://xhamster.com/", cookie_str="", proxy_url=None, segment_concurrency=1, request_timeout=2,
            ffmpeg_bin="ffmpeg", thread_stop=threading.Event(), prepare_write=lambda _size: None,
            on_tick=lambda *_args: None, site="xhamster",
        )

    try:
        ok, errors, _ = await asyncio.wait_for(download("allowed"), 8)
        assert ok, errors
        assert "/media/part.ts" in paths
        ok, errors, _ = await asyncio.wait_for(download("blocked"), 8)
        assert not ok and errors and "/outside.ts" not in paths
    finally:
        await runner.cleanup()


async def test_cancelled_disk_waiter_releases_condition_lock(tmp_path):
    scheduler = AdaptiveDiskScheduler(tmp_path, DownloadDiskBudget(tmp_path, 0), max_downloads=1, max_uploads=1, min_free_disk_gb=0)
    stop = asyncio.Event()
    assert (await scheduler.acquire_download_slot(will_upload=False, cancel_event=stop))[0]
    blocked = asyncio.create_task(scheduler.acquire_download_slot(will_upload=False, cancel_event=stop))
    await asyncio.sleep(0.01)
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked
    await asyncio.wait_for(scheduler.release_download_slot(), 1)
    assert scheduler.active_downloads == 0


def test_file_and_remote_names_fit_filesystem_and_reject_unsafe_paths(tmp_path):
    assert sanitize_filename("CON.txt") == "_CON.txt"
    assert len((video_filename_base("name." + "长" * 200, "x" * 128) + ".mp4.tmp.json").encode()) <= 255
    with pytest.raises(ValueError, match="远端名称"):
        uploader.build_remote_dir(RuntimeSettings(rclone_remote="unexpected:name"), "fixture")
    with pytest.raises(ValueError, match="目录"):
        uploader.build_remote_dir(RuntimeSettings(rclone_remote_base_dir="archive/../other"), "fixture")
    assert uploader.parse_safe_rclone_extra_flags("--dump headers --buffer-size=4Mi") == ["--buffer-size=4Mi"]
    part = tmp_path / "fixture.mp4.tmp"
    part.with_suffix(".tmp.json").write_bytes(b" " * 20_000)
    assert read_download_checkpoint(part) is None
