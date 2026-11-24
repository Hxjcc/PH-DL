"""Exercise the downloader against real loopback responses and disposable files."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

from app.config import SelectorSettings
from app.parsers.http_session import TaskHttpContext
from app.services import downloader
from app.services.filename import video_filename_base
from tests.media_fixtures import mp4_payload


@asynccontextmanager
async def media_server(handler):
    application = web.Application()
    application.router.add_get("/media", handler)
    runner = web.AppRunner(application, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}/media"
    finally:
        await runner.cleanup()


def media_args(tmp_path, session, url, **kwargs):
    return {
        "session": session,
        "parse_ctx": TaskHttpContext(),
        "selectors": SelectorSettings(),
        "viewkey": "fixture",
        "title": "Fixture",
        "download_url": url,
        "page_url": "https://www.pornhub.com/view_video.php?viewkey=fixture",
        "referer_url": "https://www.pornhub.com/",
        "quality": "1080p",
        "download_dir": tmp_path / "downloads",
        "temp_dir": tmp_path / "temp",
        "max_attempts": 1,
        "min_file_size_bytes": 64,
        **kwargs,
    }


async def test_stop_interrupts_a_stalled_read_and_the_saved_checkpoint_resumes(tmp_path):
    media = mp4_payload()
    offset = 64
    etag = '"fixture-version"'
    written, release = asyncio.Event(), asyncio.Event()
    ranged_requests = []

    async def handler(request):
        if request.headers.get("Range"):
            ranged_requests.append((request.headers["Range"], request.headers.get("If-Range")))
            return web.Response(
                status=206, body=media[offset:],
                headers={"ETag": etag, "Content-Range": f"bytes {offset}-{len(media) - 1}/{len(media)}"},
            )
        response = web.StreamResponse(headers={"ETag": etag, "Content-Length": str(len(media))})
        await response.prepare(request)
        await response.write(media[:offset])
        await release.wait()
        return response

    def on_progress(downloaded, total, speed):
        # Set by the downloader after a body chunk is written.
        if downloaded == offset:
            written.set()

    async with media_server(handler) as url:
        timeout = aiohttp.ClientTimeout(total=None, sock_read=2.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            stop = asyncio.Event()
            args = media_args(tmp_path, session, url, cancel_event=stop, on_progress=on_progress)
            first = asyncio.create_task(downloader.download_mp4_with_resume(**args))
            part = args["temp_dir"] / f"{video_filename_base('Fixture', 'fixture')}.mp4.tmp"
            try:
                async def wait_for_partial():
                    while not written.is_set() and (not part.exists() or part.stat().st_size != offset):
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(wait_for_partial(), 1.0)
                stop.set()
                stopped = await asyncio.wait_for(first, 1.0)
                assert stopped.cancelled and stopped.file_size == offset and stopped.temp_path == part
                assert part.read_bytes() == media[:offset]
                assert downloader.read_download_checkpoint(part).etag == etag
            finally:
                release.set()
                if not first.done():
                    first.cancel()
                    await asyncio.gather(first, return_exceptions=True)

            complete = await downloader.download_mp4_with_resume(**media_args(tmp_path, session, url))
            assert complete.success and complete.local_path.read_bytes() == media
            assert not part.exists() and downloader.read_download_checkpoint(part) is None
    assert ranged_requests == [(f"bytes={offset}-", etag)]


@pytest.mark.parametrize("problem", ["wrong_start", "wrong_total", "wrong_etag", "bad_416", "complete_416"])
async def test_range_response_must_prove_the_same_file_and_matching_offsets(tmp_path, problem):
    media = mp4_payload()
    offset = len(media) if problem == "complete_416" else 64
    etag = '"known"'

    async def handler(request):
        assert request.headers["Range"] == f"bytes={offset}-"
        assert request.headers["If-Range"] == etag
        if problem in {"bad_416", "complete_416"}:
            reported = offset - 1 if problem == "bad_416" else offset
            return web.Response(status=416, headers={"ETag": etag, "Content-Range": f"bytes */{reported}"})
        payload = media[offset:]
        start = 0 if problem == "wrong_start" else offset
        end = start + len(payload) - 1
        total = len(media) + 1 if problem == "wrong_total" else len(media)
        return web.Response(status=206, body=payload, headers={
            "ETag": '"different"' if problem == "wrong_etag" else etag,
            "Content-Range": f"bytes {start}-{end}/{total}",
        })

    async with media_server(handler) as url, aiohttp.ClientSession() as session:
        args = media_args(tmp_path, session, url)
        args["temp_dir"].mkdir()
        part = args["temp_dir"] / f"{video_filename_base('Fixture', 'fixture')}.mp4.tmp"
        part.write_bytes(media[:offset])
        url_identity = downloader._fingerprint(url)
        downloader._write_checkpoint(part, downloader.DownloadCheckpoint(
            "fixture", "1080p", url_identity, url_identity, etag, len(media),
        ))
        result = await downloader.download_mp4_with_resume(**args)
        if problem == "complete_416":
            assert result.success and result.local_path.read_bytes() == media
            assert not part.exists()
        else:
            assert not result.success and not list(args["download_dir"].glob("*.mp4"))
            assert part.read_bytes() == media[:offset]


async def test_old_partial_without_identity_is_preserved_instead_of_mixing_qualities(tmp_path):
    original = mp4_payload(b"a")[:90]
    current = mp4_payload(b"b")

    async def handler(request):
        assert "Range" not in request.headers
        return web.Response(body=current)

    async with media_server(handler) as url, aiohttp.ClientSession() as session:
        args = media_args(tmp_path, session, url, quality="2160p")
        args["temp_dir"].mkdir()
        args["download_dir"].mkdir()
        old = args["temp_dir"] / "Old local title [fixture].mp4.tmp"
        old.write_bytes(original)
        existing = args["download_dir"] / "Fixture [fixture].mp4"
        existing.write_bytes(mp4_payload(b"a"))
        result = await downloader.download_mp4_with_resume(**args, partial_path=old)
        assert result.success and result.local_path != existing
        assert result.local_path.read_bytes() == current
        assert old.read_bytes() == original and existing.read_bytes() == mp4_payload(b"a")


async def test_download_disk_reserve_blocks_known_oversize_before_writing(tmp_path, monkeypatch):
    media = mp4_payload()

    async def handler(request):
        return web.Response(body=media)

    monkeypatch.setattr(downloader.shutil, "disk_usage", lambda _: SimpleNamespace(free=1000))
    budget = downloader.DownloadDiskBudget(tmp_path, keep_free_bytes=900)
    async with media_server(handler) as url, aiohttp.ClientSession() as session:
        result = await downloader.download_mp4_with_resume(**media_args(tmp_path, session, url, disk_budget=budget))
    assert not result.success and "磁盘" in result.error
    assert not list(tmp_path.rglob("*.mp4*"))


async def test_concurrent_downloads_cannot_allocate_the_same_free_space(tmp_path, monkeypatch):
    media = mp4_payload()
    release = asyncio.Event()
    requests = 0
    first_part_written = asyncio.Event()

    async def handler(request):
        nonlocal requests
        requests += 1
        if requests > 1:
            return web.Response(body=media)
        response = web.StreamResponse(headers={"Content-Length": str(len(media))})
        await response.prepare(request)
        await response.write(media[:64])
        await release.wait()
        await response.write(media[64:])
        return response

    monkeypatch.setattr(downloader.shutil, "disk_usage", lambda _: SimpleNamespace(free=1500))
    budget = downloader.DownloadDiskBudget(tmp_path, keep_free_bytes=1000)

    async with media_server(handler) as url, aiohttp.ClientSession() as session:
        # The initial progress tick comes after the bytes are reserved.
        first = asyncio.create_task(downloader.download_mp4_with_resume(**media_args(
            tmp_path, session, url, disk_budget=budget, on_progress=lambda *_: first_part_written.set(),
        )))
        try:
            await asyncio.wait_for(first_part_written.wait(), 1.0)
            second = await downloader.download_mp4_with_resume(**media_args(
                tmp_path, session, url, viewkey="second", disk_budget=budget,
            ))
            assert not second.success and "预留" in second.error
        finally:
            release.set()
            assert (await asyncio.wait_for(first, 1.0)).success
