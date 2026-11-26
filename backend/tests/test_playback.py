"""Verify authenticated playback and real HTTP Range forwarding with local media."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from contextlib import asynccontextmanager

import httpx
import pytest
from aiohttp import web
from starlette.requests import ClientDisconnect

from app.config import EnvSettings
from app.db.database import Database
from app.db.repo import Repository
from app.main import create_app
from app.parsers.http_session import PowFetchResult, TaskHttpContext
from app.services import playback
from tests.media_fixtures import mp4_payload


@asynccontextmanager
async def app_client(path, monkeypatch, definitions):
    async def parse_fixture(_ctx, url, **kwargs):
        if url.endswith("/media.json"):
            return PowFetchResult(json.dumps(definitions["remote"]), url, 200, 0, False), None
        assert url == "https://www.pornhub.com/view_video.php?viewkey=fixture"
        data = {"video_title": "Fixture", "mediaDefinitions": definitions["page"]}
        html = f"<script>var flashvars_123 = {json.dumps(data)};</script>"
        return PowFetchResult(html, url, 200, 0, False), None

    monkeypatch.setattr(TaskHttpContext, "fetch_with_pow", parse_fixture)
    env = EnvSettings(data_dir=path, admin_password="local-playback-test", secret_key="x" * 48)
    database = Database(env.db_path)
    await database.connect()
    try:
        repo = Repository(database)
        await repo.update_settings_patch({"schedule_enabled": False, "ph_cookie": "sess=fixture-only"})
        await repo.upsert_video("fixture", title="Fixture", video_page_url="https://foreign.example/nope")
    finally:
        await database.close()
    application = create_app(env_settings=env)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://app.test") as client:
            yield client, application.state.container


def mp4(url, quality):
    return {"format": "mp4", "videoUrl": url, "quality": quality}


async def test_playback_resolves_multiple_qualities_and_keeps_auth_and_cookies_private(tmp_path, monkeypatch):
    definitions = {
        "page": [
            mp4("//ev.phncdn.com/720.mp4", "720"),
            {"format": "hls", "videoUrl": "https://ev.phprcdn.com/master.m3u8", "quality": "2160"},
            mp4("https://ev.phprcdn.com/blocked.m3u8", "2160"),
            {"remote": True, "videoUrl": "/media.json"},
        ],
        "remote": [
            mp4("https://ev.phprcdn.com/1080.mp4", "1080p"),
            mp4("https://ev.phprcdn.com/duplicate.mp4", "1080"),
            mp4("https://foreign.example/private.mp4", "2160"),
        ],
    }
    async with app_client(tmp_path / "app", monkeypatch, definitions) as (client, container):
        path = "/api/videos/fixture/playback"
        assert (await client.get(path)).status_code == 401
        assert (await client.post("/api/auth/login", json={"password": "local-playback-test"})).status_code == 200
        assert (await client.get("/api/videos/unknown/playback")).status_code == 404
        reply = await client.get(path)
        assert reply.status_code == 200
        assert reply.headers["cache-control"] == "private, no-store"
        assert reply.json()["format"] == "mp4"
        sources = reply.json()["sources"]
        mp4_ticket = sources[0]["relay_url"].rsplit("/", 2)[-2]
        assert [row["quality"] for row in sources] == ["1080p", "720p"]
        assert [source.url for source in container.playback._sessions[mp4_ticket].sources] == [
            "https://ev.phprcdn.com/1080.mp4", "https://ev.phncdn.com/720.mp4",
        ]
        assert "https://" not in reply.text
        assert "sess=fixture-only" not in reply.text
        assert sources[0]["relay_url"].startswith(f"{path}/")
        assert (await client.get(sources[0]["relay_url"].replace("/fixture/", "/other/"))).status_code == 410
        assert (await client.get(sources[0]["relay_url"][:-1] + "9")).status_code == 404
        assert await container.repo.list_jobs() == []
        definitions["page"] = [mp4("https://foreign.example/private.mp4", "720")]
        assert (await client.get(path)).status_code == 502
        await client.post("/api/auth/logout")
        assert (await client.get(sources[0]["relay_url"])).status_code == 401


async def test_relay_forwards_range_response_and_releases_resources(tmp_path, monkeypatch):
    media_path = tmp_path / "sample.mp4"
    payload = mp4_payload()
    media_path.write_bytes(payload)
    observed = []
    counts = Counter()

    async def serve(request):
        counts[request.path] += 1
        observed.append((request.headers.get("Range"), request.headers.get("Referer"), request.headers.get("Cookie")))
        if request.path == "/redirect":
            return web.Response(status=302, headers={"Location": "/sample.mp4"})
        if request.path == "/unavailable":
            return web.Response(status=403)
        if request.path == "/badrange":
            return web.Response(status=206, headers={"Content-Range": "bytes 0-3/300"},
                                content_type="video/mp4", body=b"xxxx")
        return web.FileResponse(media_path, headers={"Content-Type": "video/mp4", "Set-Cookie": "test=keep-private"})

    upstream = web.Application()
    upstream.router.add_get("/{fixture_path}", serve)
    runner = web.AppRunner(upstream, access_log=None)
    await runner.setup()
    server = web.TCPSite(runner, "127.0.0.1", 0)
    await server.start()
    origin = f"http://127.0.0.1:{runner.addresses[0][1]}"
    is_allowed = playback._media_url
    monkeypatch.setattr(playback, "_media_url", lambda url: url.startswith(origin + "/") or is_allowed(url))
    definitions = {
        "page": [mp4(origin + "/redirect", "1080"), mp4(origin + "/unavailable", "720"),
                 mp4(origin + "/badrange", "360")],
        "remote": [],
    }
    try:
        async with app_client(tmp_path / "app", monkeypatch, definitions) as (client, container):
            await client.post("/api/auth/login", json={"password": "local-playback-test"})
            reply = await client.get("/api/videos/fixture/playback")
            hi, unavailable, bad_range = [row["relay_url"] for row in reply.json()["sources"]]
            assert (await client.get(hi, headers={"Range": "bytes=2-0"})).status_code == 400
            assert not counts
            ranged = await client.get(hi, headers={"Range": "bytes=20-39"})
            assert ranged.status_code == 206 and ranged.content == payload[20:40]
            assert ranged.headers["content-range"] == f"bytes 20-39/{len(payload)}"
            assert ranged.headers["content-type"] == "video/mp4"
            assert ranged.headers["accept-ranges"] == "bytes"
            assert "set-cookie" not in ranged.headers
            assert all(referer == "https://www.pornhub.com/view_video.php?viewkey=fixture" and cookie is None
                       for _, referer, cookie in observed)
            assert counts == {"/redirect": 1, "/sample.mp4": 1}

            first_http_client = container.playback._http_client
            assert first_http_client is not None and not first_http_client.closed
            ranged_second = await client.get(hi, headers={"Range": "bytes=0-19"})
            assert ranged_second.status_code == 206 and ranged_second.content == payload[0:20]
            assert container.playback._http_client is first_http_client

            assert (await client.get(hi, headers={"Range": f"bytes={len(payload)}-"})).status_code == 416
            assert (await client.get(unavailable)).status_code == 410
            assert (await client.get(bad_range, headers={"Range": "bytes=20-23"})).status_code == 502

            # Model the browser closing a response before consuming another chunk.
            _, ticket, index = hi.rsplit("/", 2)
            response = await container.playback.stream("fixture", ticket, int(index), None)
            async def receive():
                await asyncio.Future()

            async def closed(_message):
                raise OSError("test connection closed")

            with pytest.raises(ClientDisconnect):
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, closed)
            assert container.playback._stream_slots._value == 12
        assert container.playback._http_client is None
    finally:
        await runner.cleanup()


async def test_xhamster_hls_playback_rewrites_playlist_and_relays_segments(tmp_path, monkeypatch):
    init_bytes = mp4_payload()[:64]
    seg1_bytes = b"\x47\x40\x00\x10" + b"TS-SEGMENT-ONE-" * 16
    seg2_bytes = b"\x47\x40\x00\x11" + b"TS-SEGMENT-TWO-" * 16
    observed: list[tuple[str, str | None, str | None]] = []

    async def serve(request):
        observed.append((request.path, request.headers.get("Referer"), request.headers.get("Cookie")))
        if request.path == "/hls/2160p.m3u8":
            playlist = (
                "#EXTM3U\n"
                "#EXT-X-VERSION:7\n"
                "#EXT-X-TARGETDURATION:4\n"
                '#EXT-X-MAP:URI="init.mp4"\n'
                "#EXTINF:4.000,\n"
                "seg-1.ts\n"
                "#EXTINF:4.000,\n"
                "seg-2.ts\n"
                "#EXT-X-ENDLIST\n"
            )
            return web.Response(text=playlist, content_type="application/vnd.apple.mpegurl")
        if request.path == "/hls/init.mp4":
            return web.Response(body=init_bytes, content_type="video/mp4", headers={"Set-Cookie": "secret=drop"})
        if request.path == "/hls/seg-1.ts":
            if request.headers.get("Range") == "bytes=0-15":
                return web.Response(
                    status=206,
                    body=seg1_bytes[:16],
                    content_type="video/MP2T",
                    headers={
                        "Content-Range": f"bytes 0-15/{len(seg1_bytes)}",
                        "Accept-Ranges": "bytes",
                    },
                )
            return web.Response(body=seg1_bytes, content_type="video/MP2T")
        if request.path == "/hls/seg-2.ts":
            return web.Response(body=seg2_bytes, content_type="video/MP2T")
        return web.Response(status=404)

    upstream = web.Application()
    upstream.router.add_get("/hls/{name}", serve)
    runner = web.AppRunner(upstream, access_log=None)
    await runner.setup()
    server = web.TCPSite(runner, "127.0.0.1", 0)
    await server.start()
    origin = f"http://127.0.0.1:{runner.addresses[0][1]}"

    is_allowed = playback._media_url
    monkeypatch.setattr(playback, "_media_url", lambda url: url.startswith(origin + "/") or is_allowed(url))

    xh_page_url = "https://xhamster.com/videos/sample-clip-xhOwQIZ"
    master_url = f"{origin}/hls/master.m3u8"
    dead_mp4_url = f"{origin}/hls/dead-720p.mp4"
    page_html = (
        "<script>window.initials="
        + json.dumps(
            {
                "videoModel": {"id": 1, "idHashSlug": "xhOwQIZ", "title": "XH HLS Fixture"},
                "xplayerSettings": {
                    "sources": {
                        "hls": {"h264": {"url": master_url}},
                        "standard": {"h264": [{"quality": "720p", "url": dead_mp4_url}]},
                    }
                },
            }
        )
        + ";</script>"
    )
    master_m3u8 = (
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=12000000,RESOLUTION=3840x2160,CODECS="mp4a.40.2,avc1.4d4015"\n'
        "2160p.m3u8\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720,CODECS="mp4a.40.2,avc1.4d4015"\n'
        "720p.m3u8\n"
    )

    async def fake_fetch(_ctx, url, **kwargs):
        if url == xh_page_url:
            return PowFetchResult(page_html, xh_page_url, 200, 0, False), None
        if url == master_url:
            return PowFetchResult(master_m3u8, master_url, 200, 0, False), None
        raise AssertionError(f"Unexpected fetch URL: {url}")

    monkeypatch.setattr(TaskHttpContext, "fetch_with_pow", fake_fetch)

    env = EnvSettings(data_dir=tmp_path / "xh_app", admin_password="local-playback-test", secret_key="x" * 48)
    database = Database(env.db_path)
    await database.connect()
    try:
        repo = Repository(database)
        await repo.update_settings_patch({"schedule_enabled": False, "xh_cookie": "UID=42; _id=xh-secret"})
        await repo.upsert_video(
            "xhOwQIZ",
            title="XH HLS Fixture",
            video_page_url=xh_page_url,
            site="xhamster",
        )
    finally:
        await database.close()

    try:
        application = create_app(env_settings=env)
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://app.test"
            ) as client:
                assert (await client.post("/api/auth/login", json={"password": "local-playback-test"})).status_code == 200
                reply = await client.get("/api/videos/xhOwQIZ/playback")
                assert reply.status_code == 200
                body = reply.json()
                assert body["format"] == "hls"
                assert [s["quality"] for s in body["sources"]] == ["2160p", "720p"]
                assert "http://" not in reply.text and "https://" not in reply.text
                assert "xh-secret" not in reply.text

                playlist_url = body["sources"][0]["relay_url"]
                assert playlist_url.endswith("/0/playlist.m3u8")

                pl_resp = await client.get(playlist_url)
                assert pl_resp.status_code == 200
                assert "application/vnd.apple.mpegurl" in pl_resp.headers["content-type"]
                assert "http://" not in pl_resp.text and "https://" not in pl_resp.text
                assert '#EXT-X-MAP:URI="seg/0"' in pl_resp.text
                assert "\nseg/1\n" in pl_resp.text and "\nseg/2\n" in pl_resp.text

                base = playlist_url.rsplit("/", 1)[0]
                r_init = await client.get(f"{base}/seg/0")
                assert r_init.status_code == 200 and r_init.content == init_bytes
                assert "set-cookie" not in r_init.headers

                r_seg1 = await client.get(f"{base}/seg/1", headers={"Range": "bytes=0-15"})
                assert r_seg1.status_code == 206 and r_seg1.content == seg1_bytes[:16]
                assert r_seg1.headers["content-type"] == "video/mp2t"

                r_seg2 = await client.get(f"{base}/seg/2")
                assert r_seg2.status_code == 200 and r_seg2.content == seg2_bytes

                assert (await client.get(f"{base}/seg/99")).status_code == 404
                assert (await client.get(playlist_url.replace("/xhOwQIZ/", "/other/"))).status_code == 410
                assert all(referer == xh_page_url and cookie is None for _, referer, cookie in observed)

                await client.post("/api/auth/logout")
                assert (await client.get(f"{base}/seg/1")).status_code == 401
    finally:
        await runner.cleanup()
