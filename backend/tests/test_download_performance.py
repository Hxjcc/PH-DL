"""Loopback regressions for bounded HLS and MP4 download concurrency."""

import asyncio
import re
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.parsers.http_session import TaskHttpContext
from app.parsers.video import VideoDetail
from app.services import downloader, hls_downloader, http_proxy
from tests.media_fixtures import mp4_payload
from tests.test_download_safety import media_args, media_server

MIB = 1024 * 1024
RANGE_BYTES = 4 * MIB
ETAG = '"fixture-v1"'


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def hls_args(server, session, path, *, folder="video", **kwargs):
    return dict(
        session=session, playlist_url=str(server.make_url(f"/{folder}/index.m3u8")),
        hls_dir=path, viewkey=folder, quality="2160p", referer="", cookie_str="", proxy_url=None,
        segment_concurrency=12, request_timeout=10, stop=asyncio.Event(),
        prepare_write=lambda _: None, on_tick=lambda *_: None, spill_to_disk=True,
    ) | kwargs


@pytest.mark.parametrize("cancel", [False, True])
async def test_large_hls_segments_spill_while_the_head_stalls_and_resume_cleanly(tmp_path, cancel):
    segments = [b"a" * (MIB // 2), *[bytes([i]) * (2 * MIB) for i in range(1, 6)]]
    requests = [0] * len(segments)
    head_started = asyncio.Event()
    release_head = asyncio.Event()
    folder = tmp_path / "video.hls"
    folder.mkdir()
    (folder / "seg_999999.tmp").write_bytes(b"leftover from a stopped process")
    memory = hls_downloader.HlsMemoryBudget(3 * MIB, http_slots=6, writers=1)
    write_sizes = []

    async def playlist(_request):
        assert not (folder / "seg_999999.tmp").exists()
        return web.Response(text="#EXTM3U\n" + "".join(f"#EXTINF:2,\n/{i}.ts\n" for i in range(len(segments))))

    async def segment(request):
        i = int(request.match_info["index"])
        requests[i] += 1
        if i == 1:
            head_started.set()
            await release_head.wait()
        return web.Response(body=segments[i])

    app = web.Application()
    app.router.add_get("/video/index.m3u8", playlist)
    app.router.add_get("/{index}.ts", segment)
    async with TestServer(app) as server, aiohttp.ClientSession(read_bufsize=MIB) as session:
        stop = asyncio.Event()
        args = hls_args(server, session, folder, stop=stop, memory_budget=memory,
                        segment_semaphore=asyncio.Semaphore(6), prepare_write=write_sizes.append)
        task = asyncio.create_task(hls_downloader._download_hls_aiohttp(**args))
        try:
            await asyncio.wait_for(head_started.wait(), 5)

            def finished_spills():
                return [path for path in folder.glob("seg_*.tmp") if path.stat().st_size == 2 * MIB]

            await wait_for(lambda: len(finished_spills()) >= 3)
            assert (folder / "stream.part").stat().st_size == len(segments[0])
            assert requests == [1] * len(segments)
            assert 0 < memory.peak_bytes <= memory.max_bytes - (6 * 256 + 512) * 1024
            if cancel:
                stop.set()
            else:
                release_head.set()
            result = await asyncio.wait_for(task, 5)
            assert memory.used_bytes == 0
            assert not list(folder.glob("seg_*.tmp"))
            if cancel:
                assert result.cancelled
                checkpoint = hls_downloader._read_hls_manifest(folder)
                assert (checkpoint["completed_segments"], checkpoint["stream_bytes"]) == (1, len(segments[0]))
                release_head.set()
                result = await hls_downloader._download_hls_aiohttp(**(args | {"stop": asyncio.Event()}))
                assert requests[0] == 1
            assert result.ok
            assert (folder / "stream.mp4").read_bytes() == b"".join(segments)
            assert sum(write_sizes) > sum(map(len, segments))
            assert not list(folder.glob("seg_*.tmp")) and memory.used_bytes == 0
        finally:
            release_head.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_hls_one_video_uses_twelve_workers_and_concurrent_videos_share_the_pool(tmp_path):
    active = {"first": 0, "second": 0}
    peak = 0
    release = asyncio.Event()

    async def playlist(_request):
        return web.Response(text="#EXTM3U\n" + "".join(f"#EXTINF:1,\n{i}.ts\n" for i in range(14)))

    async def segment(request):
        nonlocal peak
        name = request.match_info["name"]
        active[name] += 1
        peak = max(peak, sum(active.values()))
        try:
            await release.wait()
            return web.Response(body=b"fragment")
        finally:
            active[name] -= 1

    app = web.Application()
    app.router.add_get("/{name}/index.m3u8", playlist)
    app.router.add_get("/{name}/{index}.ts", segment)
    semaphore = asyncio.Semaphore(16)
    budget = hls_downloader.HlsMemoryBudget(12 * MIB, http_slots=16, writers=2)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        tasks = []
        try:
            for name, expected in (("first", 12), ("second", 4)):
                task = asyncio.create_task(hls_downloader._download_hls_aiohttp(**hls_args(
                    server, session, tmp_path / name, folder=name,
                    segment_semaphore=semaphore, memory_budget=budget,
                )))
                tasks.append(task)
                await wait_for(lambda: active[name] == expected)
            assert sum(active.values()) == 16
            release.set()
            assert all(outcome.ok for outcome in await asyncio.wait_for(asyncio.gather(*tasks), 5))
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert peak == 16 and budget.used_bytes == 0


async def test_hls_spill_disk_denial_cleans_temporary_files(tmp_path):
    app = web.Application()

    async def playlist(_):
        return web.Response(text="#EXTM3U\n#EXTINF:1,\nsegment.ts\n")

    async def segment(_):
        return web.Response(body=b"x" * (2 * MIB))

    def deny(size):
        raise downloader._DiskSpaceError("reserve floor", required_bytes=size)

    app.router.add_get("/video/index.m3u8", playlist)
    app.router.add_get("/video/segment.ts", segment)
    semaphore = asyncio.BoundedSemaphore(1)
    budget = hls_downloader.HlsMemoryBudget(MIB, http_slots=1, writers=1)
    folder = tmp_path / "video.hls"
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        result = await hls_downloader._download_hls_aiohttp(**hls_args(
            server, session, folder, segment_semaphore=semaphore, memory_budget=budget, prepare_write=deny,
        ))
    assert result.disk_error.required_bytes <= 256 * 1024
    assert not list(folder.glob("seg_*.tmp")) and budget.used_bytes == 0 and not semaphore.locked()


def large_mp4(size):
    # Keep the ftyp and moov boxes and give each interval distinct payload bytes.
    head = mp4_payload()[:-(256 + 8)]
    media_bytes = size - len(head) - 8
    payload = b"".join(bytes([i // RANGE_BYTES]) * min(media_bytes - i, RANGE_BYTES)
                       for i in range(0, media_bytes, RANGE_BYTES))
    return head + (media_bytes + 8).to_bytes(4, "big") + b"mdat" + payload


def range_bounds(value, total):
    first, last = re.fullmatch(r"bytes=(\d+)-(\d*)", value).groups()
    return int(first), min(total, int(last) + 1) if last else total


def range_response(media, start, end, *, etag=ETAG, encoding="identity"):
    return web.Response(
        status=206, body=media[start:end],
        headers={
            "ETag": etag, "Content-Range": f"bytes {start}-{end - 1}/{len(media)}",
            "Content-Encoding": encoding,
        },
    )


async def test_single_mp4_reads_ahead_and_waits_for_an_inflight_write_on_cancel(tmp_path, monkeypatch):
    payload = large_mp4(2 * MIB)
    next_read = asyncio.Event()
    write_entered = asyncio.Event()
    release_write = threading.Event()
    never = asyncio.Event()
    stop = asyncio.Event()
    checked = []
    loop = asyncio.get_running_loop()
    original = downloader._write_media_chunk

    async def chunks(size):
        assert size == MIB
        yield payload[:MIB]
        next_read.set()
        await never.wait()

    def blocked_write(fp, chunk, prepare):
        loop.call_soon_threadsafe(write_entered.set)
        assert release_write.wait(5)
        assert not fp.closed
        original(fp, chunk, prepare)

    class Disk:
        @contextmanager
        def reserve(self, size):
            assert size == len(payload)
            yield checked.append

    class Response:
        status = 200
        headers = {"Content-Length": str(len(payload)), "ETag": ETAG}
        content = SimpleNamespace(iter_chunked=chunks)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(downloader, "_write_media_chunk", blocked_write)
    session = SimpleNamespace(get=lambda *_, **__: Response())
    args = media_args(tmp_path, session, "https://cdn.example/media", disk_budget=Disk(), cancel_event=stop)
    task = asyncio.create_task(downloader.download_mp4_with_resume(**args))
    try:
        await asyncio.wait_for(asyncio.gather(next_read.wait(), write_entered.wait()), 3)
        stop.set()
        await asyncio.sleep(0.02)
        assert not task.done()
        release_write.set()
        result = await asyncio.wait_for(task, 3)
        assert result.cancelled and checked == [MIB] and result.file_size == MIB
        assert result.temp_path.read_bytes() == payload[:MIB]
        assert downloader.read_download_checkpoint(result.temp_path).etag == ETAG
    finally:
        release_write.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("deny_write", [False, True])
async def test_single_mp4_enforces_disk_guard_and_declared_body_length(tmp_path, deny_write):
    initial = mp4_payload()[:64]
    checked = []

    async def chunks(_size):
        yield initial
        yield b"x" * 32

    def prepare(size):
        checked.append(size)
        if deny_write:
            raise downloader._DiskSpaceError("disk is full", required_bytes=size)

    class Disk:
        @contextmanager
        def reserve(self, _size):
            yield prepare

    class Response:
        status = 200
        content = SimpleNamespace(iter_chunked=chunks)
        headers = {"Content-Length": "90", "ETag": ETAG}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    session = SimpleNamespace(get=lambda *_, **__: Response())
    result = await downloader.download_mp4_with_resume(**media_args(
        tmp_path, session, "https://cdn.example/media", disk_budget=Disk(),
    ))
    assert not result.success and checked == [len(initial)]
    if deny_write:
        assert result.disk_wait_bytes == len(initial) and result.temp_path is None
    else:
        assert "字节范围" in result.error and result.temp_path.read_bytes() == initial


async def test_mp4_ranges_download_concurrently_with_bounded_chunks_and_one_disk_reservation(tmp_path, monkeypatch):
    media = large_mp4(28 * MIB + 23)
    peak = active = max_chunk = 0
    requests = []
    reached_pool_limit = asyncio.Event()
    original_write = downloader._write_media_chunk
    reserved = []

    class Disk(downloader.DownloadDiskBudget):
        @contextmanager
        def reserve(self, size):
            reserved.append(size)
            with super().reserve(size) as prepare:
                yield prepare

    def write(fp, chunk, prepare):
        nonlocal max_chunk
        max_chunk = max(max_chunk, len(chunk))
        original_write(fp, chunk, prepare)

    async def handler(request):
        nonlocal active, peak
        value = request.headers["Range"]
        bounds = range_bounds(value, len(media))
        requests.append(bounds)
        assert request.headers["Accept-Encoding"] == "identity"
        if bounds[0]:
            assert request.headers["If-Range"] == ETAG
            active += 1
            peak = max(peak, active)
            try:
                if active == 3:
                    reached_pool_limit.set()
                await asyncio.wait_for(reached_pool_limit.wait(), 5)
                await asyncio.sleep(0.01)
            finally:
                active -= 1
        return range_response(media, *bounds)

    monkeypatch.setattr(downloader, "_write_media_chunk", write)
    async with media_server(handler) as url, aiohttp.ClientSession(read_bufsize=MIB) as session:
        result = await downloader.download_mp4_with_resume(**media_args(
            tmp_path, session, url, range_connections=4, range_semaphore=asyncio.Semaphore(3),
            disk_budget=Disk(tmp_path, keep_free_bytes=1),
        ))
    assert result.success and result.local_path.read_bytes() == media
    assert peak == 3 and max_chunk <= 512 * 1024
    assert requests[0] == (0, RANGE_BYTES) and sorted(requests) == list(downloader._missing_ranges(len(media), []))
    assert reserved == [len(media)] and not list((tmp_path / "temp").iterdir())


@pytest.mark.parametrize("mode", ["ignored", "weak", "encoded", "etag_change", "precondition", "small", "whole_weak"])
async def test_mp4_ranges_degrade_to_one_stream_when_needed(tmp_path, mode):
    media = large_mp4(3 * MIB if mode == "whole_weak" else 6 * MIB)
    calls = []
    seen_if_range = []

    async def handler(request):
        value = request.headers.get("Range")
        calls.append(value)
        seen_if_range.append(request.headers.get("If-Range"))
        if not value or mode == "ignored":
            return web.Response(body=media, headers={"ETag": ETAG})
        start, end = range_bounds(value, len(media))
        if start and mode == "precondition":
            return web.Response(status=412)
        if mode in {"weak", "whole_weak"}:
            return range_response(media, start, end, etag='W/"weak"')
        return range_response(
            media, start, end, etag='"changed"' if start and mode == "etag_change" else ETAG,
            encoding="gzip" if mode == "encoded" else "identity",
        )

    async with media_server(handler) as url, aiohttp.ClientSession() as session:
        result = await downloader.download_mp4_with_resume(**media_args(
            tmp_path, session, url, range_connections=4, min_multi_range_bytes=8 * MIB if mode == "small" else 5 * MIB,
        ))
    assert result.success and result.local_path.read_bytes() == media
    assert calls[0] == f"bytes=0-{RANGE_BYTES - 1}"
    if mode in {"ignored", "whole_weak"}:
        assert len(calls) == 1
    elif mode == "small":
        assert calls[1:] == [f"bytes={RANGE_BYTES}-"] and seen_if_range[1] == ETAG
    else:
        assert calls[-1] is None
    assert not list((tmp_path / "temp").iterdir())


@pytest.mark.parametrize("renew", [False, True])
async def test_mp4_resumes_only_missing_ranges_after_cancel_or_signed_url_expiry(tmp_path, monkeypatch, renew):
    media = large_mp4(14 * MIB)
    release_early_range = asyncio.Event()
    stop = asyncio.Event()
    renewed = False
    resumed_requests = []
    original_requests = []
    state = tmp_path / "temp"

    async def handler(request):
        early = request.query.get("token") == "first" and not renewed
        start, end = range_bounds(request.headers["Range"], len(media))
        (original_requests if early else resumed_requests).append((start, end))
        if start == RANGE_BYTES and early:
            await release_early_range.wait()
            if renew:
                return web.Response(status=403)
        return range_response(media, start, end)

    checkpoint_with_hole = [[0, RANGE_BYTES], [2 * RANGE_BYTES, len(media)]]

    def read_checkpoint():
        for part in state.glob("*.mp4.tmp"):
            checkpoint = downloader.read_download_checkpoint(part)
            if checkpoint and checkpoint.completed_ranges == checkpoint_with_hole:
                return part, checkpoint
        return None

    monkeypatch.setattr(downloader, "_CHECKPOINT_WRITE_INTERVAL", 0)
    async with media_server(handler) as url, aiohttp.ClientSession() as session:
        async def refresh(*_args, **_kwargs):
            nonlocal renewed
            assert read_checkpoint() is not None
            renewed = True
            return VideoDetail("fixture", "Fixture", url, url, download_url=url + "?token=fresh", quality="1080p")

        monkeypatch.setattr(downloader, "fetch_single_video_detail", refresh)
        args = media_args(
            tmp_path, session, url + "?token=first", cancel_event=stop, range_connections=4,
            min_multi_range_bytes=8 * MIB, max_attempts=2 if renew else 1,
            retry_delay_base=0, retry_delay_max=0,
        )
        task = asyncio.create_task(downloader.download_mp4_with_resume(**args))
        try:
            await wait_for(lambda: read_checkpoint() is not None)
            if renew:
                release_early_range.set()
                result = await asyncio.wait_for(task, 5)
            else:
                stop.set()
                stopped = await asyncio.wait_for(task, 5)
                assert stopped.cancelled and stopped.file_size == len(media) - RANGE_BYTES
                assert stopped.temp_path.stat().st_size == len(media)
                assert read_checkpoint() is not None
                renewed = True
                release_early_range.set()
                result = await downloader.download_mp4_with_resume(**(args | {
                    "cancel_event": asyncio.Event(), "partial_path": stopped.temp_path,
                }))
            assert result.success and result.local_path.read_bytes() == media
        finally:
            release_early_range.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert original_requests[0] == (0, RANGE_BYTES)
    assert resumed_requests == [(RANGE_BYTES, 2 * RANGE_BYTES)]
    assert not list(state.iterdir())


async def test_mp4_ranges_keep_verified_prefix_from_legacy_checkpoint(tmp_path):
    media = large_mp4(11 * MIB)
    prefix_size = 120_001
    requests = []

    async def handler(request):
        assert request.headers["If-Range"] == ETAG
        start, end = range_bounds(request.headers["Range"], len(media))
        requests.append((start, end))
        return range_response(media, start, end)

    async with media_server(handler) as url, aiohttp.ClientSession() as session:
        args = media_args(tmp_path, session, url, range_connections=4, min_multi_range_bytes=8 * MIB)
        args["temp_dir"].mkdir()
        part = args["temp_dir"] / "Fixture [fixture].mp4.tmp"
        part.write_bytes(media[:prefix_size])
        fingerprint = downloader._fingerprint(url)
        downloader._write_checkpoint(part, downloader.DownloadCheckpoint(
            "fixture", "1080p", fingerprint, fingerprint, ETAG, len(media),
        ))
        result = await downloader.download_mp4_with_resume(**args, partial_path=part)
    assert result.success and result.local_path.read_bytes() == media
    assert sorted(requests) == list(downloader._missing_ranges(len(media), [[0, prefix_size]]))


async def test_mp4_truncates_old_entity_before_writing_its_replacement_checkpoint(tmp_path, monkeypatch):
    first = mp4_payload(b"a")
    replacement = mp4_payload(b"b")
    next_etag = '"replacement"'
    responses = 0
    prefix_written = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_checkpoint = downloader._write_checkpoint
    original_write = downloader._write_media_chunk

    def write_checkpoint(path, checkpoint):
        if checkpoint.etag == next_etag:
            assert path.stat().st_size == 0
        original_checkpoint(path, checkpoint)

    def write_chunk(fp, chunk, prepare):
        original_write(fp, chunk, prepare)
        loop.call_soon_threadsafe(prefix_written.set)

    async def handler(request):
        nonlocal responses
        responses += 1
        if responses == 1:
            response = web.StreamResponse(headers={"Content-Length": str(len(first)), "ETag": ETAG})
            await response.prepare(request)
            await response.write(first[:64])
            await asyncio.wait_for(prefix_written.wait(), 3)
            request.transport.close()
            return response
        assert request.headers["Range"] == "bytes=64-"
        return web.Response(body=replacement, headers={"ETag": next_etag})

    monkeypatch.setattr(downloader, "_write_checkpoint", write_checkpoint)
    monkeypatch.setattr(downloader, "_write_media_chunk", write_chunk)
    async with media_server(handler) as url, aiohttp.ClientSession() as session:
        result = await downloader.download_mp4_with_resume(**media_args(
            tmp_path, session, url, max_attempts=2, retry_delay_base=0,
        ))
    assert result.success and result.local_path.read_bytes() == replacement


async def test_secondary_mp4_range_cannot_follow_disallowed_redirects(tmp_path, monkeypatch):
    media = large_mp4(6 * MIB)
    fetched = []
    app = web.Application()

    async def handler(request):
        fetched.append(request.path)
        start, end = range_bounds(request.headers["Range"], len(media))
        if start:
            return web.Response(status=302, headers={"Location": "/private"})
        return range_response(media, start, end)

    async def private(request):
        fetched.append(request.path)
        return web.Response(body=b"private")

    app.router.add_get("/media", handler)
    app.router.add_get("/private", private)
    async with TestServer(app) as server, aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        url = str(server.make_url("/media"))
        monkeypatch.setattr(http_proxy, "is_media_url", lambda candidate, site: site == "pornhub" and candidate == url)
        result = await downloader.download_mp4_with_resume(**media_args(
            tmp_path, session, url, parse_ctx=TaskHttpContext(site="pornhub"), cookie_str="auth=fixture",
            range_connections=4, min_multi_range_bytes=5 * MIB,
        ))
    assert not result.success and "媒体源地址不在允许范围" in result.error
    assert fetched == ["/media", "/media"]
    assert result.file_size == RANGE_BYTES
    assert downloader.read_download_checkpoint(result.temp_path).completed_ranges == [[0, RANGE_BYTES]]
