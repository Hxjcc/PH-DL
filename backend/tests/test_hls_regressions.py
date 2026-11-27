"""Playlist identity, fallbacks, and separate audio regressions."""

import asyncio
import json
import os
import stat
import threading
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.config import SelectorSettings
from app.parsers.http_session import TaskHttpContext
from app.parsers.video import MediaSource
from app.services import hls_downloader as hls, playback
from tests.media_fixtures import mp4_payload
from tests.test_download_performance import hls_args, wait_for


@pytest.mark.parametrize("attribute", ["has_multiple_maps", "has_map_byterange", "is_endlist"])
async def test_native_hls_rejects_playlists_it_cannot_assemble(tmp_path, attribute):
    first = '#EXT-X-MAP:URI="init.mp4"' + (',BYTERANGE="20@0"' if attribute == "has_map_byterange" else "")
    second = '#EXT-X-MAP:URI="other.mp4"\n' if attribute == "has_multiple_maps" else ""
    endlist = "#EXT-X-ENDLIST\n" if attribute != "is_endlist" else ""
    body = f"#EXTM3U\n{first}\n#EXTINF:2,\nfirst.m4s\n{second}#EXTINF:2,\nsecond.m4s\n{endlist}"
    parsed = hls.parse_hls_media_playlist(body, "https://media.xhcdn.com/index.m3u8")
    assert getattr(parsed, attribute) == (attribute != "is_endlist")
    requests = []

    async def serve(request):
        requests.append(request.path)
        return web.Response(text=body)

    app = web.Application()
    app.router.add_get("/{path:.*}", serve)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        outcome = await hls._download_hls_aiohttp(**hls_args(server, session, tmp_path))
    assert outcome.fallback_to_ytdlp and requests == ["/video/index.m3u8"]


@pytest.mark.parametrize("changed", ["signature", "init_query", "segment_query"])
async def test_native_hls_resumes_only_matching_resource_query_parameters(tmp_path, changed):
    renewed = False
    hits = []

    async def serve(request):
        if request.path.endswith(".m3u8"):
            signature = "new" if renewed else "old"
            init = 2 if renewed and changed == "init_query" else 1
            segment = 2 if renewed and changed == "segment_query" else 1
            return web.Response(text=(
                f'#EXTM3U\n#EXT-X-MAP:URI="init.mp4?track={init}&token={signature}"\n'
                f"#EXTINF:4,\nseg0.m4s?track={segment}&sig={signature}\n"
                f"#EXTINF:4,\nseg1.m4s?track={segment}&sig={signature}\n#EXT-X-ENDLIST\n"
            ))
        hits.append(request.path.rsplit("/", 1)[-1])
        return web.Response(body=f"{hits[-1]}:{request.query['track']}".encode())

    app = web.Application()
    app.router.add_get("/{path:.*}", serve)
    folder = tmp_path / "resume.hls"
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        args = hls_args(server, session, folder)
        assert (await hls._download_hls_aiohttp(**args)).ok
        old = hls._read_hls_manifest(folder)
        saved = b"init.mp4:1seg0.m4s:1"
        (folder / "stream.mp4").unlink()
        (folder / "stream.part").write_bytes(saved)
        hls._write_hls_manifest_atomic(folder, {**old, "completed_segments": 1, "stream_bytes": len(saved)})

        renewed = True
        hits.clear()
        assert (await hls._download_hls_aiohttp(**args)).ok

    same_identity = changed == "signature"
    assert (hls._read_hls_manifest(folder)["playlist_id"] == old["playlist_id"]) == same_identity
    assert set(hits) == ({"seg1.m4s"} if same_identity else {"init.mp4", "seg0.m4s", "seg1.m4s"})


def fallback_args(tmp_path, **kwargs):
    url = "https://media.xhcdn.com/stream.m3u8"
    return dict(
        session=None, parse_ctx=TaskHttpContext(), selectors=SelectorSettings(), viewkey="xhFixture1",
        title="Fixture", playlist_url=url, page_url="", referer_url="", quality="720p",
        download_dir=tmp_path / "downloads", temp_dir=tmp_path / "temp", max_attempts=1, min_file_size_bytes=16,
    ) | kwargs


@pytest.mark.parametrize("track", [1, 2])
async def test_ytdlp_checkpoint_matches_normalized_playlist_resource(tmp_path, monkeypatch, track):
    args = fallback_args(tmp_path, playlist_url=f"https://new.xhcdn.com/stream.m3u8?token=new&track={track}")
    folder = args["temp_dir"] / "Fixture [xhFixture1].hls"
    folder.mkdir(parents=True)
    checkpoint = folder / "stream.mp4.ytdl"
    checkpoint.write_text("checkpoint", encoding="utf-8")
    (folder / "stream.mp4.part").write_bytes(b"old media")
    hls._write_hls_manifest_atomic(folder, {
        "engine": "yt-dlp", "version": hls._HLS_MANIFEST_VERSION,
        "viewkey": "xhFixture1", "quality": "720", "playlist_resource": "/stream.m3u8?track=1",
    })

    def worker(**kwargs):
        assert checkpoint.exists() == (track == 1)
        assert hls._read_hls_manifest(folder)["playlist_resource"] == f"/stream.m3u8?track={track}"
        return False, ["fixture"], None

    monkeypatch.setattr(hls, "_run_ytdlp_hls_sync", worker)
    monkeypatch.setattr(hls.shutil, "which", lambda _: "ffmpeg")
    assert not (await hls.download_hls_to_mp4(**args)).success


async def test_ytdlp_cancel_wait_is_bounded_and_checkpoint_is_held_until_worker_stops(tmp_path, monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    stop = asyncio.Event()
    args = fallback_args(tmp_path, cancel_event=stop)
    folder = args["temp_dir"] / "Fixture [xhFixture1].hls"

    def worker(thread_stop, **kwargs):
        (folder / "stream.mp4.part").write_bytes(b"checkpoint")
        entered.set()
        assert release.wait(5) and thread_stop.is_set()
        return False, [], None

    monkeypatch.setattr(hls, "_run_ytdlp_hls_sync", worker)
    monkeypatch.setattr(hls, "_YTDLP_CANCEL_GRACE_SECONDS", 0.03)
    monkeypatch.setattr(hls.shutil, "which", lambda _: "ffmpeg")
    task = asyncio.create_task(hls.download_hls_to_mp4(**args))
    try:
        await wait_for(entered.is_set)
        stop.set()
        cancelled = await asyncio.wait_for(task, 1)
        assert cancelled.cancelled and cancelled.temp_path == folder and hls.ytdlp_hls_dir_in_use(folder)
        retry = await hls.download_hls_to_mp4(**(args | {"cancel_event": asyncio.Event()}))
        assert not retry.success and "仍在停止" in retry.error
        assert (folder / "stream.mp4.part").read_bytes() == b"checkpoint"
    finally:
        release.set()
        await wait_for(lambda: not hls.ytdlp_hls_dir_in_use(folder))


async def test_separate_hls_audio_uses_the_requested_variant_and_is_mapped_into_mp4(tmp_path, monkeypatch):
    master = (
        '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="Default",DEFAULT=YES,URI="audio.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720,AUDIO="a"\n720p.m3u8\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080,AUDIO="a"\n1080p.m3u8\n'
    )
    async def serve_master(_request):
        return web.Response(text=master)

    app = web.Application()
    app.router.add_get("/master.m3u8", serve_master)
    monkeypatch.setattr(hls.shutil, "which", lambda _: "ffmpeg")

    def worker(playlist_url, audio_playlist_url, hls_dir, **kwargs):
        assert playlist_url.endswith("/720p.m3u8") and audio_playlist_url.endswith("/audio.m3u8")
        (hls_dir / "stream.mp4").write_bytes(mp4_payload())
        (hls_dir / "audio.m4a").write_bytes(b"audio track")
        return True, [], None

    async def remux(command, **kwargs):
        assert "0:v:0" in command and "1:a:0" in command
        assert any(arg.endswith("audio.m4a") for arg in command)
        Path(command[-1]).write_bytes(mp4_payload())
        return 0, ""

    monkeypatch.setattr(hls, "_run_ytdlp_hls_sync", worker)
    monkeypatch.setattr(hls, "_run_ffmpeg", remux)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        result = await hls.download_hls_to_mp4(**fallback_args(
            tmp_path, session=session, playlist_url=str(server.make_url("/master.m3u8")),
        ))
    assert result.success and result.quality == "720p"


async def test_playback_rewrites_chosen_video_and_audio_to_authenticated_nested_playlists(tmp_path, monkeypatch):
    app = web.Application()

    async def nested(request):
        return web.Response(text=f"#EXTM3U\n#EXTINF:4,\n{request.path[1:]}-segment\n#EXT-X-ENDLIST\n")

    app.router.add_get("/{track}", nested)
    monkeypatch.setattr(playback, "_media_url", lambda url: url.startswith("http://127.0.0.1:"))
    service = playback.PlaybackService()
    try:
        async with TestServer(app) as server:
            source = MediaSource(str(server.make_url("/video")), "720p", str(server.make_url("/audio")), 1000000)
            entry = playback._Playback("xhFixture1", (source,), "", "", None, float("inf"), format="hls", site="xhamster")
            service._sessions["ticket"] = entry
            master = await service.playlist("xhFixture1", "ticket", 0)
            assert b'TYPE=AUDIO' in master.body and b'URI="seg/1"' in master.body and b"\nseg/0\n" in master.body
            for index in (0, 1):
                response = await service.segment("xhFixture1", "ticket", 0, index, None)
                assert b"#EXT-X-ENDLIST" in response.body and b"127.0.0.1" not in response.body
    finally:
        await service.aclose()


def test_temp_list_reuses_bounded_manifest_reader_when_file_grows_after_stat(tmp_path, monkeypatch):
    from app.config import EnvSettings
    from app.engine.events import EventBus
    from app.engine.task_manager import TaskManager

    env = EnvSettings(data_dir=tmp_path)
    folder = env.temp_dir / "Fixture [xhFixture1].hls"
    folder.mkdir(parents=True)
    (folder / "stream.part").write_bytes(b"resume bytes")
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps({"viewkey": "wrong", "quality": "720"}), encoding="utf-8")
    original_open = hls.os.open

    def grow_on_open(path, flags, *args, **kwargs):
        if path == manifest:
            with manifest.open("ab") as output:
                output.write(b" " * (hls._MAX_MANIFEST_BYTES + 1))
        return original_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(hls.os, "open", grow_on_open)
        assert hls._read_hls_manifest(folder) is None
    manager = TaskManager(env, None, EventBus())
    [checkpoint] = manager.list_temp_files()
    assert checkpoint["viewkey"] == "xhFixture1" and not checkpoint["verified_checkpoint"]


def test_manifest_reader_rejects_symlink_before_opening(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"viewkey":"fixture"}', encoding="utf-8")
    real_lstat = Path.lstat
    link_metadata = os.stat_result((stat.S_IFLNK | 0o777, *real_lstat(manifest)[1:]))
    monkeypatch.setattr(Path, "lstat", lambda path: link_metadata if path == manifest else real_lstat(path))

    def unexpected_open(*args, **kwargs):
        pytest.fail("Manifest symlinks must be rejected before opening")

    monkeypatch.setattr(hls.os, "open", unexpected_open)
    assert hls._read_hls_manifest(tmp_path) is None
