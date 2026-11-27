"""Regression coverage for the HTTP, HLS, and playback memory limits."""

import asyncio
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from fastapi import HTTPException

from app.config import RuntimeSettings
from app.engine import memory
from app.parsers import http_session
from app.parsers.video import MediaSource, VideoDetail
from app.services import hls_downloader, playback


def test_hls_downloader_import_does_not_load_yt_dlp():
    subprocess.run(
        [sys.executable, "-c", (
            "import sys; from app.services import hls_downloader; "
            "assert not any(name == 'yt_dlp' or name.startswith('yt_dlp.') for name in sys.modules)"
        )],
        check=True,
        timeout=10,
    )


async def test_small_hls_buffer_applies_backpressure_and_writes_in_order(tmp_path, monkeypatch):
    segments = [bytes([idx]) * 8192 for idx in range(8)]
    started = [asyncio.Event() for _ in segments]
    release_head = asyncio.Event()

    async def playlist(_request):
        return web.Response(text="#EXTM3U\n" + "".join(
            f"#EXTINF:1,\n/{idx}.ts\n" for idx in range(len(segments))
        ) + "#EXT-X-ENDLIST\n")

    async def segment(request):
        idx = int(request.match_info["index"])
        started[idx].set()
        if idx == 0:
            await release_head.wait()
        return web.Response(body=segments[idx])

    app = web.Application()
    app.router.add_get("/test.m3u8", playlist)
    app.router.add_get("/{index}.ts", segment)
    original_write = hls_downloader._write_stream_blocks

    def write_blocks(fh, blocks, *, flush=False):
        assert blocks and all(isinstance(block, bytearray) for block in blocks)
        original_write(fh, blocks, flush=flush)
        assert not blocks

    monkeypatch.setattr(hls_downloader, "_write_stream_blocks", write_blocks)
    hls_dir = tmp_path / "hls"
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        download = asyncio.create_task(hls_downloader._download_hls_aiohttp(
            session=session, playlist_url=str(server.make_url("/test.m3u8")), hls_dir=hls_dir,
            viewkey="tiny", quality="720p", referer="", cookie_str="", proxy_url=None,
            segment_concurrency=2, max_buffer_bytes=1, request_timeout=10, stop=asyncio.Event(),
            prepare_write=lambda _size: None, on_tick=lambda *_: None,
        ))
        try:
            await asyncio.wait_for(started[1].wait(), 3)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(started[2].wait(), 0.15)
            release_head.set()
            assert (await asyncio.wait_for(download, 5)).ok
        finally:
            release_head.set()
            if not download.done():
                download.cancel()
                await asyncio.gather(download, return_exceptions=True)
    assert (hls_dir / "stream.mp4").read_bytes() == b"".join(segments)


async def test_task_http_session_opens_lazily_and_releases_only_when_idle(monkeypatch):
    created = []
    started = asyncio.Event()
    unblock = asyncio.Event()

    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []
            self.closed = False
            created.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def get(self, url, **kwargs):
            self.calls.append(kwargs)
            started.set()
            await unblock.wait()
            return SimpleNamespace(text="test", url=url, status_code=200, headers={"Set-Cookie": "session=server"})

    monkeypatch.setattr(http_session, "CffiSession", FakeSession)
    ctx = http_session.TaskHttpContext(cookie_str="base=1", concurrency=100, min_request_interval=0)
    async with ctx:
        await ctx.release_idle_session()
        assert created == []
        ctx.pow_cookies["KEY"] = "pow"
        request = asyncio.create_task(ctx.fetch_with_pow("https://example.test/page"))
        await asyncio.wait_for(started.wait(), 2)
        await ctx.release_idle_session()
        assert len(created) == 1 and not created[0].closed
        unblock.set()
        assert (await request)[1] is None
        await ctx.release_idle_session()
        assert created[0].closed and ctx._session is None
        assert (await ctx.fetch_with_pow("https://example.test/refresh"))[1] is None
        assert len(created) == 2 and created[1].kwargs["max_clients"] == 16
        assert created[1].calls[0]["cookies"] == {"base": "1", "session": "server", "KEY": "pow"}
        assert created[1].calls[0]["impersonate"] == created[0].calls[0]["impersonate"]
    assert created[1].closed and ctx._session is None


async def test_playback_caps_tickets_and_prunes_expired_tickets_on_lookup(monkeypatch):
    async def detail(_ctx, page_url, _selectors, **kwargs):
        return VideoDetail(
            "fixture", "Fixture", page_url, page_url,
            mp4_sources=(MediaSource("https://ev.phncdn.com/720.mp4", "720p"),),
        )

    monkeypatch.setattr(playback, "fetch_single_video_detail", detail)
    service = playback.PlaybackService()
    settings = RuntimeSettings()
    tickets = []
    for _ in range(34):
        resolved = await service.resolve({"viewkey": "fixture"}, settings)
        tickets.append(resolved["sources"][0]["relay_url"].rsplit("/", 2)[-2])
    assert list(service._sessions) == tickets[-32:]
    service._sessions[tickets[-2]].expires_at = 0
    assert service._session("fixture", tickets[-1]).viewkey == "fixture"
    assert tickets[-2] not in service._sessions
    with pytest.raises(HTTPException) as expired:
        service._session("fixture", tickets[0])
    assert expired.value.status_code == 410
    assert playback._MAX_HLS_ASSETS == 6_000


async def test_playback_evicts_least_recent_socks_client_and_closes_remaining_clients():
    service = playback.PlaybackService()
    try:
        async with service._get_http_client("socks5://127.0.0.1:10001") as (first, _):
            async with service._get_http_client("socks5://127.0.0.1:10002") as (second, _):
                assert not second.closed
            async with service._get_http_client("socks5://127.0.0.1:10003") as (third, _):
                assert not first.closed
            assert not first.closed
        assert len(service._socks_http_clients) == 2
        assert not second.closed and first.closed and not third.closed
    finally:
        await service.aclose()
    assert second.closed and third.closed and not service._socks_http_clients


async def test_reclaim_memory_trims_even_if_sqlite_is_unavailable(monkeypatch):
    calls = []

    async def execute(sql):
        calls.append(sql)
        raise RuntimeError("database closed")

    monkeypatch.setattr(memory, "gc", SimpleNamespace(collect=lambda: calls.append("gc")))
    monkeypatch.setattr(memory, "trim_process_memory", lambda: calls.append("trim"))
    await memory.reclaim_memory(SimpleNamespace(execute=execute))
    assert calls == ["gc", "PRAGMA shrink_memory", "trim"]


def test_dockerfile_avoids_static_malloc_trim_threshold():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    env_lines = [line.strip() for line in dockerfile.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert any("MALLOC_ARENA_MAX=2" in line for line in env_lines)
    assert not any("MALLOC_TRIM_THRESHOLD_" in line for line in env_lines)
