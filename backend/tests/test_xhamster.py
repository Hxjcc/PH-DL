"""Comprehensive tests for xHamster parsing, decryption, HLS downloading, DB v9 migration, and multi-site jobs."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app.config import EnvSettings, RuntimeSettings, SelectorSettings
from app.db.database import Database
from app.db.repo import Repository
from app.db.schema import MIGRATIONS, SCHEMA_SQL, SCHEMA_VERSION
from app.engine.events import EventBus
from app.engine.task_manager import TaskManager
from app.main import create_app
from app.parsers.http_session import PowFetchResult, TaskHttpContext
from app.parsers.video import collect_video_urls, extract_viewkey_from_url, fetch_single_video_detail, is_video_url
from app.parsers.xhamster import (
    _prng_bytes,
    build_xhamster_list_url,
    check_xhamster_cookie_valid,
    decode_xhamster_hex,
    decode_xhamster_sources,
    decode_xhamster_url,
    extract_xhamster_initials,
    extract_xhamster_video_cards,
    extract_xhamster_viewkey,
    fetch_xhamster_subscriptions,
    fetch_xhamster_video_detail,
    is_xhamster_url,
    parse_hls_master_variants,
    scan_xhamster_list_pages,
)
from app.services import hls_downloader as hls_mod
from app.services.uploader import UploadBatchResult, build_remote_dir, file_snapshot
from tests.media_fixtures import mp4_payload


def _encode_xhamster_hex(plaintext: str, algo: int, seed: int) -> str:
    seed_u32 = seed & 0xFFFFFFFF
    header = bytes(
        [
            algo & 0xFF,
            seed_u32 & 0xFF,
            (seed_u32 >> 8) & 0xFF,
            (seed_u32 >> 16) & 0xFF,
            (seed_u32 >> 24) & 0xFF,
        ]
    )
    gen = _prng_bytes(algo, seed_u32)
    body = bytes((b ^ next(gen)) & 0xFF for b in plaintext.encode("utf-8"))
    return (header + body).hex()


# ---------------------------------------------------------------------------
# 1. 7-PRNG Hex Decryption & URL Utilities
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("algo", [1, 2, 3, 4, 5, 6, 7])
@pytest.mark.parametrize("seed", [0x00000001, 0x12345678, 0x80000000, 0xDEADBEEF, 0xFFFFFFFF])
def test_xhamster_all_7_prng_algorithms_roundtrip(algo: int, seed: int):
    url = "https://video-nss.xhcdn.com/VdCXnXqq0K6S0B1MUB6X9w==,1795683600/031/415/628/2160p.h264.mp4.m3u8"
    encoded = _encode_xhamster_hex(url, algo, seed)
    assert decode_xhamster_hex(encoded) == url
    assert decode_xhamster_url(encoded) == url


def test_decode_xhamster_url_handles_embedded_segment_and_protocol_relative():
    secret_token = "key=SEdQ7j5RPz73D84fZz4WGg"
    enc_seg = _encode_xhamster_hex(secret_token, algo=6, seed=0x91A2B3C4)
    embedded = f"//video7.xhcdn.com/{enc_seg},end=1795683600/031/415/628/720p.h264.mp4"
    assert (
        decode_xhamster_url(embedded)
        == "https://video7.xhcdn.com/key=SEdQ7j5RPz73D84fZz4WGg,end=1795683600/031/415/628/720p.h264.mp4"
    )

    nested = {
        "hls": {"h264": {"url": _encode_xhamster_hex("https://cdn.xhcdn.com/master.m3u8", 3, 42)}},
        "standard": {"h264": [{"quality": "720p", "url": embedded, "fallback": ""}]},
    }
    decoded = decode_xhamster_sources(nested)
    assert decoded["hls"]["h264"]["url"] == "https://cdn.xhcdn.com/master.m3u8"
    assert decoded["standard"]["h264"][0]["url"].startswith("https://video7.xhcdn.com/key=SEdQ7j5RPz73D84fZz4WGg")


def test_xhamster_url_detection_and_mixed_collect_video_urls():
    xh_modern = "https://xhamster.com/videos/some-great-video-title-xhOwQIZ"
    xh_dup = "https://zh.xhamster.com/videos/other-slug-xhOwQIZ?foo=1"
    xh_legacy = "https://xhamster42.desi/videos/old-video-12345678"
    ph_vid = "https://www.pornhub.com/view_video.php?viewkey=ph64abcdef1234"
    invalid = "https://example.com/videos/foo-xhOwQIZ"

    assert is_xhamster_url(xh_modern) is True
    assert is_video_url(xh_modern) is True
    assert is_video_url(xh_legacy) is True
    assert extract_xhamster_viewkey(xh_modern) == "xhOwQIZ"
    assert extract_viewkey_from_url(xh_modern) == "xhOwQIZ"
    assert extract_xhamster_viewkey(xh_legacy) == "xh_12345678"

    urls, ignored = collect_video_urls([xh_modern, xh_dup, xh_legacy, ph_vid, invalid])
    assert ignored == 1
    assert urls == [xh_modern, xh_legacy, ph_vid]


# ---------------------------------------------------------------------------
# 2. List Page, Video Detail, Codec Preference & Subscriptions
# ---------------------------------------------------------------------------
def test_extract_xhamster_video_cards_for_users_creators_channels_and_favorites():
    sample_thumb = {
        "id": 31415628,
        "title": "Sample &amp; Clip",
        "pageURL": "https://xhamster.com/videos/sample-clip-xhOwQIZ",
        "duration": 460,
        "created": 1767225600,  # 2026-01-01 UTC
    }

    html_user = (
        '<script>window.initials='
        + json.dumps(
            {
                "profile": {"name": "WebToLove", "thumbURL": "https://ic-nss.xhcdn.com/u.jpg"},
                "maxVideoPages": 3,
                "videoListComponent": {"videoThumbProps": [sample_thumb]},
            }
        )
        + ";</script>"
    )
    cards, avatar, name, max_p = extract_xhamster_video_cards(html_user, "users")
    assert len(cards) == 1
    assert cards[0] == ("xhOwQIZ", "https://xhamster.com/videos/sample-clip-xhOwQIZ", "Sample & Clip", "2026-01-01", 460)
    assert avatar == "https://ic-nss.xhcdn.com/u.jpg"
    assert name == "WebToLove"
    assert max_p == 3

    html_creator = (
        "<script>window.initials="
        + json.dumps(
            {
                "infoComponent": {"pageTitle": "Fragile Tempt Porn Videos", "avatarURL": "https://ic-nss.xhcdn.com/c.jpg"},
                "paginationComponent": {"lastPageNumber": 5},
                "trendingVideoSectionComponent": {"videoListProps": {"videoThumbProps": [sample_thumb]}},
            }
        )
        + ";</script>"
    )
    cards2, avatar2, name2, max_p2 = extract_xhamster_video_cards(html_creator, "creator")
    assert len(cards2) == 1 and cards2[0][0] == "xhOwQIZ"
    assert name2 == "Fragile Tempt"
    assert avatar2 == "https://ic-nss.xhcdn.com/c.jpg"
    assert max_p2 == 5

    html_fav = (
        "<script>window.initials="
        + json.dumps(
            {
                "profile": {"name": "OrderlyPaw690"},
                "favoritesVideoPaging": {"maxPages": 2},
                "favoritesVideoCollection": [sample_thumb],
            }
        )
        + ";</script>"
    )
    cards3, _, name3, max_p3 = extract_xhamster_video_cards(html_fav, "favorites")
    assert len(cards3) == 1 and cards3[0][0] == "xhOwQIZ"
    assert name3 == "OrderlyPaw690"
    assert max_p3 == 2


async def test_fetch_xhamster_video_detail_respects_codec_and_direct_mp4_optimization(monkeypatch):
    h264_master_url = "https://video-nss.xhcdn.com/tok/h264.m3u8"
    av1_master_url = "https://video-nss.xhcdn.com/tok/av1.m3u8"
    mp4_720_url = "https://video7.xhcdn.com/tok/720p.h264.mp4"

    page_html = (
        "<script>window.initials="
        + json.dumps(
            {
                "videoModel": {
                    "id": 31415628,
                    "idHashSlug": "xhOwQIZ",
                    "title": "Test 4K Video",
                    "duration": 460,
                    "created": 1767225600,
                    "author": {"name": "Fragile Tempt", "pageURL": "https://xhamster.com/creators/fragile-tempt"},
                },
                "xplayerSettings": {
                    "sources": {
                        "hls": {
                            "h264": {"url": _encode_xhamster_hex(h264_master_url, 2, 1001)},
                            "av1": {"url": _encode_xhamster_hex(av1_master_url, 5, 2002)},
                        },
                        "standard": {
                            "h264": [
                                {"quality": "720p", "url": _encode_xhamster_hex(mp4_720_url, 4, 3003)},
                                {"quality": "auto", "url": _encode_xhamster_hex(h264_master_url, 1, 4004)},
                            ],
                        },
                    }
                },
            }
        )
        + ";</script>"
    )

    h264_m3u8 = (
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=2371122,RESOLUTION=1280x720,CODECS="mp4a.40.2,avc1.4d4015"\n'
        "720p.h264.mp4.m3u8\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=26106121,RESOLUTION=3840x2160,CODECS="mp4a.40.2,avc1.4d4015"\n'
        "2160p.h264.mp4.m3u8\n"
    )
    av1_m3u8 = (
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=12000000,RESOLUTION=3840x2160,CODECS="mp4a.40.2,av01.0.12M.08"\n'
        "2160p.av1.mp4.m3u8\n"
    )

    ctx = TaskHttpContext(concurrency=1)

    async def fake_fetch(url: str, **kwargs):
        if "xhamster.com/videos/" in url:
            return PowFetchResult(text=page_html, final_url=url, status_code=200, challenge_solves=0, retried=False), None
        if url == h264_master_url:
            return PowFetchResult(text=h264_m3u8, final_url=url, status_code=200, challenge_solves=0, retried=False), None
        if url == av1_master_url:
            return PowFetchResult(text=av1_m3u8, final_url=url, status_code=200, challenge_solves=0, retried=False), None
        return None, "unexpected url"

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_fetch)

    vid_url = "https://xhamster.com/videos/test-4k-video-xhOwQIZ"

    # 1. Default h264 picks 2160p H.264 HLS and exposes HLS variants for playback
    d_h264 = await fetch_single_video_detail(
        ctx, vid_url, SelectorSettings(), include_mp4_sources=True, xh_preferred_codec="h264"
    )
    assert d_h264.error is None
    assert d_h264.viewkey == "xhOwQIZ"
    assert d_h264.quality == "2160p"
    assert d_h264.video_format == "hls"
    assert d_h264.download_url == "https://video-nss.xhcdn.com/tok/2160p.h264.mp4.m3u8"
    assert d_h264.uploader_username == "fragile-tempt"
    assert d_h264.uploader_type == "creator"
    assert d_h264.published_at == "2026-01-01"
    assert d_h264.duration_seconds == 460
    assert len(d_h264.mp4_sources) == 1
    assert d_h264.mp4_sources[0].url == mp4_720_url
    assert [(s.quality, s.url) for s in d_h264.hls_sources] == [
        ("2160p", "https://video-nss.xhcdn.com/tok/2160p.h264.mp4.m3u8"),
        ("720p", "https://video-nss.xhcdn.com/tok/720p.h264.mp4.m3u8"),
    ]

    # 2. AV1 preference picks 2160p AV1 HLS
    d_av1 = await fetch_single_video_detail(
        ctx, vid_url, SelectorSettings(), xh_preferred_codec="av1"
    )
    assert d_av1.error is None
    assert d_av1.quality == "2160p"
    assert d_av1.download_url == "https://video-nss.xhcdn.com/tok/2160p.av1.mp4.m3u8"

    # 3. Requesting 720p with H.264 stays on HLS (xHamster progressive MP4 endpoints return 403 Wrong key)
    d_720 = await fetch_single_video_detail(
        ctx, vid_url, SelectorSettings(), preferred_qualities={"720"}, xh_preferred_codec="h264"
    )
    assert d_720.error is None
    assert d_720.quality == "720p"
    assert d_720.video_format == "hls"
    assert d_720.download_url == "https://video-nss.xhcdn.com/tok/720p.h264.mp4.m3u8"


async def test_fetch_xhamster_subscriptions_filters_unsubscribed_recommendations(monkeypatch):
    ctx = TaskHttpContext(concurrency=1)

    pornstars_html = (
        "<script>window.initials="
        + json.dumps(
            {
                "paginationProps": {"lastPageNumber": 1},
                "layoutPage": {
                    "pornstars": [
                        {
                            "name": "SweetGirl69",
                            "pageURL": "https://xhamster.com/creators/sweetgirl69",
                            "imageUserAvatarUrl": "https://ic-ut-nss.xhcdn.com/a/xxx/webp/136/694/167/avatar_150.jpg",
                            "subscriptionModel": {"subscribed": True},
                        },
                        {
                            "name": "Recommended Creator",
                            "pageURL": "https://xhamster.com/creators/rec-creator",
                            "subscriptionModel": {"subscribed": False},
                        },
                    ]
                },
            }
        )
        + ";</script>"
    )
    channels_html = (
        "<script>window.initials="
        + json.dumps(
            {
                "paginationProps": {"lastPageNumber": 1},
                "layoutPage": {
                    "channels": [
                        {
                            "name": "Subscribed Channel",
                            "pageURL": "https://xhamster.com/channels/sub-ch",
                            "logoThumbUrl": "https://ic-nss.xhcdn.com/ch.jpg",
                            "subscriptionModel": {"subscribed": True},
                        },
                        {
                            "name": "Recommended Non-Sub",
                            "pageURL": "https://xhamster.com/channels/rec-ch",
                            "subscriptionModel": {"subscribed": False},
                        },
                    ]
                },
            }
        )
        + ";</script>"
    )
    users_html = (
        "<script>window.initials="
        + json.dumps(
            {
                "subscriptionsUsersPaging": {"maxPages": 1},
                "subscriptionsUsersCollection": [
                    {
                        "id": 136694167,
                        "name": "SweetHot69",
                        "pageURL": "https://xhamster.com/users/sweethot69",
                        "thumbURL": "https://ic-ut-nss.xhcdn.com/a/yyy/webp/136/694/167/avatar_80.jpg",
                    },
                    {
                        "id": 78837540,
                        "name": "WebToLove",
                        "pageURL": "https://xhamster.com/users/webtolove",
                        "thumbURL": "https://ic-ut-nss.xhcdn.com/a/zzz/webp/078/837/540/avatar_80.jpg",
                    },
                ],
            }
        )
        + ";</script>"
    )

    async def fake_fetch(url: str, **kwargs):
        if url.endswith("/my/subscriptions/pornstars"):
            return PowFetchResult(pornstars_html, url, 200, 0, False), None
        if url.endswith("/my/subscriptions/channels"):
            return PowFetchResult(channels_html, url, 200, 0, False), None
        if url.endswith("/my/subscriptions/users"):
            return PowFetchResult(users_html, url, 200, 0, False), None
        raise AssertionError(f"Unexpected subscription URL fetched: {url}")

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_fetch)
    subs = await fetch_xhamster_subscriptions(ctx)
    # SweetHot69 (id=136694167) is auto-deduplicated because SweetGirl69's avatar binds to user 136694167,
    # while standalone subscribed user WebToLove is kept cleanly without scraping the video feed!
    assert subs == [
        ("creator", "sweetgirl69", "SweetGirl69", "https://ic-ut-nss.xhcdn.com/a/xxx/webp/136/694/167/avatar_150.jpg"),
        ("channel", "sub-ch", "Subscribed Channel", "https://ic-nss.xhcdn.com/ch.jpg"),
        ("users", "webtolove", "WebToLove", "https://ic-ut-nss.xhcdn.com/a/zzz/webp/078/837/540/avatar_80.jpg"),
    ]


async def test_scan_xhamster_creator_unions_bound_user_videos_and_respects_explicit_pages(monkeypatch):
    ctx = TaskHttpContext(concurrency=1)

    creator_html = (
        "<script>window.initials="
        + json.dumps(
            {
                "infoComponent": {
                    "pageTitle": "SweetGirl69 Porn Videos",
                    "displayUserModel": {
                        "id": 136694167,
                        "name": "SweetHot69",
                        "pageURL": "https://xhamster.com/users/sweethot69",
                    },
                },
                "paginationComponent": {"lastPageNumber": 1},
                "trendingVideoSectionComponent": {
                    "videoListProps": {
                        "videoThumbProps": [
                            {"id": 1, "title": "Shared Clip", "pageURL": "https://xhamster.com/videos/shared-xhAAA11", "duration": 100, "created": 1767225600},
                            {"id": 2, "title": "Creator Collab", "pageURL": "https://xhamster.com/videos/collab-xhBBB22", "duration": 200, "created": 1767225600},
                        ]
                    }
                },
            }
        )
        + ";</script>"
    )
    bound_user_html = (
        "<script>window.initials="
        + json.dumps(
            {
                "maxVideoPages": 1,
                "videoListComponent": {
                    "videoThumbProps": [
                        {"id": 1, "title": "Shared Clip", "pageURL": "https://xhamster.com/videos/shared-xhAAA11", "duration": 100, "created": 1767225600},
                        {"id": 3, "title": "Untagged User Upload", "pageURL": "https://xhamster.com/videos/untagged-xhCCC33", "duration": 300, "created": 1767225600},
                    ]
                },
            }
        )
        + ";</script>"
    )

    fetched: List[str] = []

    async def fake_fetch(url: str, **kwargs):
        fetched.append(url)
        if "/creators/sweetgirl69/newest" in url:
            return PowFetchResult(creator_html, url, 200, 0, False), None
        if "/users/sweethot69/videos" in url:
            return PowFetchResult(bound_user_html, url, 200, 0, False), None
        return PowFetchResult("", url, 404, 0, False), None

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_fetch)

    # 1. Natural full scan unions creator + bound user videos with deduplication
    full_videos = await scan_xhamster_list_pages(
        ctx,
        "https://xhamster.com/creators/sweetgirl69/newest",
        target_type="creator",
        delay_min=0,
        delay_max=0,
    )
    assert [v.viewkey for v in full_videos] == ["xhAAA11", "xhBBB22", "xhCCC33"]
    assert any("/users/sweethot69/videos" in u for u in fetched)

    # 2. Explicit pages_spec stays strictly scoped to creator pages (no bound-user scan)
    fetched.clear()
    paged_videos = await scan_xhamster_list_pages(
        ctx,
        "https://xhamster.com/creators/sweetgirl69/newest",
        target_type="creator",
        pages_spec="1",
        delay_min=0,
        delay_max=0,
    )
    assert [v.viewkey for v in paged_videos] == ["xhAAA11", "xhBBB22"]
    assert not any("/users/sweethot69/videos" in u for u in fetched)


# ---------------------------------------------------------------------------
# 3. HLS Media Playlist Parsing & Concurrent Resume + Remux
# ---------------------------------------------------------------------------
def test_parse_hls_media_playlist_ts_and_fmp4():
    ts_m3u8 = (
        "#EXTM3U\n#EXT-X-VERSION:3\n"
        "#EXTINF:4.000,\n2160p.h264.mp4/seg-1-v1-a1.ts\n"
        "#EXTINF:3.500,\n2160p.h264.mp4/seg-2-v1-a1.ts\n"
    )
    pl_ts = hls_mod.parse_hls_media_playlist(ts_m3u8, "https://video-nss.xhcdn.com/a/2160p.h264.mp4.m3u8")
    assert pl_ts.is_fmp4 is False
    assert pl_ts.init_url is None
    assert pl_ts.segment_urls == (
        "https://video-nss.xhcdn.com/a/2160p.h264.mp4/seg-1-v1-a1.ts",
        "https://video-nss.xhcdn.com/a/2160p.h264.mp4/seg-2-v1-a1.ts",
    )
    assert abs(pl_ts.total_duration - 7.5) < 0.01

    fmp4_m3u8 = (
        "#EXTM3U\n#EXT-X-VERSION:6\n"
        '#EXT-X-MAP:URI="2160p.av1.mp4/init-v1-a1.mp4"\n'
        "#EXTINF:4.000,\n2160p.av1.mp4/seg-1-v1-a1.m4s\n"
    )
    pl_av1 = hls_mod.parse_hls_media_playlist(fmp4_m3u8, "https://video-nss.xhcdn.com/a/2160p.av1.mp4.m3u8")
    assert pl_av1.is_fmp4 is True
    assert pl_av1.init_url == "https://video-nss.xhcdn.com/a/2160p.av1.mp4/init-v1-a1.mp4"
    assert pl_av1.segment_urls == ("https://video-nss.xhcdn.com/a/2160p.av1.mp4/seg-1-v1-a1.m4s",)


async def test_download_hls_to_mp4_ytdlp_cancel_resume_remux_and_403_refresh(tmp_path: Path, monkeypatch):
    import http.server
    import socketserver
    import threading
    import time

    from app.parsers.video import VideoDetail
    from app.services.downloader import DownloadDiskBudget

    # Build deterministic valid MPEG-TS packets (188-byte sync 0x47 frames) & fMP4 bytes
    ts_packet = bytes([0x47]) + bytes(187)
    ts_seg_bytes = ts_packet * 16  # 3008 bytes per segment, passes _looks_like_ts
    fmp4_init = mp4_payload(b"init")[:64]
    fmp4_seg = b"moof" + bytes(2044)

    srv_dir = tmp_path / "hls_srv"
    srv_dir.mkdir()
    for idx in range(4):
        (srv_dir / f"seg{idx}.ts").write_bytes(ts_seg_bytes)
    ts_playlist = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
        + "".join(f"#EXTINF:4.000,\nseg{idx}.ts\n" for idx in range(4))
        + "#EXT-X-ENDLIST\n"
    )
    (srv_dir / "v1.m3u8").write_text(ts_playlist, encoding="utf-8")
    (srv_dir / "v2.m3u8").write_text(ts_playlist, encoding="utf-8")

    (srv_dir / "init.mp4").write_bytes(fmp4_init)
    (srv_dir / "av1_0.m4s").write_bytes(fmp4_seg)
    (srv_dir / "av1.m3u8").write_text(
        '#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-TARGETDURATION:4\n#EXT-X-MAP:URI="init.mp4"\n'
        "#EXTINF:4.000,\nav1_0.m4s\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )

    hits: List[str] = []
    loop = asyncio.get_running_loop()
    state: Dict[str, Any] = {"fail_403_v1_seg2": False, "cancel_on_seg2": None}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(srv_dir), **kw)

        def do_GET(self):
            hits.append(self.path)
            if state["fail_403_v1_seg2"] and self.path == "/seg2.ts":
                self.send_error(403, "Expired signed URL")
                return
            if state["cancel_on_seg2"] is not None and self.path == "/seg2.ts":
                ev = state["cancel_on_seg2"]
                loop.call_soon_threadsafe(ev.set)
                time.sleep(0.08)
            return super().do_GET()

        def log_message(self, *a):
            pass

    class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True

    httpd = ThreadedServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    srv_thread.start()

    monkeypatch.setattr(hls_mod.shutil, "which", lambda name: "/usr/local/bin/ffmpeg")
    remux_calls: List[List[str]] = []

    async def fake_run_ffmpeg(cmd: List[str], *, cancel_event: asyncio.Event, timeout: float = 600.0):
        remux_calls.append(cmd)
        out_path = Path(cmd[-1])
        out_path.write_bytes(mp4_payload())
        return 0, ""

    monkeypatch.setattr(hls_mod, "_run_ffmpeg", fake_run_ffmpeg)

    try:
        dl_dir = tmp_path / "downloads" / "xHamster" / "creator1"
        tmp_dir = tmp_path / "temp" / "xHamster" / "creator1"
        hls_dir = tmp_dir / "Demo Clip [xhOwQIZ].hls"
        ctx = TaskHttpContext(concurrency=1)

        # 1. Cancel mid-download via cancel_event on seg2 -> preserves .hls dir with yt-dlp manifest & .ytdl
        cancel_ev = asyncio.Event()
        state["cancel_on_seg2"] = cancel_ev
        phases_run1: List[str] = []

        cancelled = await hls_mod.download_hls_to_mp4(
            session=None,  # type: ignore[arg-type]
            parse_ctx=ctx,
            selectors=SelectorSettings(),
            viewkey="xhOwQIZ",
            title="Demo Clip",
            playlist_url=f"http://127.0.0.1:{port}/v1.m3u8",
            page_url="https://xhamster.com/videos/demo-clip-xhOwQIZ",
            referer_url="https://xhamster.com/videos/demo-clip-xhOwQIZ",
            quality="2160p",
            download_dir=dl_dir,
            temp_dir=tmp_dir,
            segment_concurrency=1,
            min_file_size_bytes=16,
            cancel_event=cancel_ev,
            on_progress=lambda dl, tot, spd, phase="downloading": phases_run1.append(phase),
        )
        assert cancelled.cancelled is True
        assert cancelled.success is False
        assert cancelled.temp_path == hls_dir
        assert hls_dir.is_dir()
        manifest = json.loads((hls_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["engine"] == "yt-dlp" and manifest["viewkey"] == "xhOwQIZ"
        assert (hls_dir / "stream.mp4.ytdl").is_file()

        # 2. Resume against refreshed URL (v2.m3u8) -> skips seg0.ts & seg1.ts, emits downloading & remuxing phases, TS remuxes
        hits.clear()
        state["cancel_on_seg2"] = None
        phases_run2: List[str] = []
        outcome = await hls_mod.download_hls_to_mp4(
            session=None,  # type: ignore[arg-type]
            parse_ctx=ctx,
            selectors=SelectorSettings(),
            viewkey="xhOwQIZ",
            title="Demo Clip",
            playlist_url=f"http://127.0.0.1:{port}/v2.m3u8",
            page_url="https://xhamster.com/videos/demo-clip-xhOwQIZ",
            referer_url="https://xhamster.com/videos/demo-clip-xhOwQIZ",
            quality="2160p",
            download_dir=dl_dir,
            temp_dir=tmp_dir,
            partial_path=hls_dir,
            segment_concurrency=2,
            min_file_size_bytes=16,
            on_progress=lambda dl, tot, spd, phase="downloading": phases_run2.append(phase),
        )
        assert outcome.success is True
        assert outcome.local_path is not None and outcome.local_path.is_file()
        assert not hls_dir.exists()
        assert "/seg0.ts" not in hits  # resumed past already appended first fragment!
        assert "remuxing" in phases_run2
        assert len(remux_calls) == 1
        assert "-bsf:a" in remux_calls[0] and "aac_adtstoasc" in remux_calls[0]
        assert "-movflags" in remux_calls[0] and "+faststart" in remux_calls[0]

        # 3. HTTP 403 on expired URL refreshes playlist URL and recovers seamlessly
        hits.clear()
        remux_calls.clear()
        state["fail_403_v1_seg2"] = True
        refresh_calls = 0

        async def fake_fetch_single(*args, **kwargs):
            nonlocal refresh_calls
            refresh_calls += 1
            state["fail_403_v1_seg2"] = False
            return VideoDetail(
                viewkey="xh403ok",
                title="Refresh Clip",
                page_url="https://xhamster.com/videos/refresh-clip-xh403ok",
                referer_url="https://xhamster.com/videos/refresh-clip-xh403ok",
                download_url=f"http://127.0.0.1:{port}/v2.m3u8",
                quality="1080p",
                video_format="hls",
            )

        monkeypatch.setattr(hls_mod, "fetch_single_video_detail", fake_fetch_single)
        outcome_403 = await hls_mod.download_hls_to_mp4(
            session=None,  # type: ignore[arg-type]
            parse_ctx=ctx,
            selectors=SelectorSettings(),
            viewkey="xh403ok",
            title="Refresh Clip",
            playlist_url=f"http://127.0.0.1:{port}/v1.m3u8",
            page_url="https://xhamster.com/videos/refresh-clip-xh403ok",
            referer_url="https://xhamster.com/videos/refresh-clip-xh403ok",
            quality="1080p",
            download_dir=dl_dir,
            temp_dir=tmp_dir,
            max_attempts=2,
            segment_concurrency=2,
            retry_delay_base=0.0,
            retry_delay_max=0.0,
            min_file_size_bytes=16,
        )
        assert outcome_403.success is True
        assert refresh_calls == 1
        assert outcome_403.download_url.endswith("/v2.m3u8")

        # 4. fMP4/AV1 stream remuxes with +faststart without aac_adtstoasc, and low disk pauses before remux without losing stream.mp4
        remux_calls.clear()
        orig_disk_usage = hls_mod.shutil.disk_usage
        from app.services import downloader as dl_mod

        fake_free = [500]  # too small for remux reservation when keep_free_bytes=400 and raw_size ~2100
        monkeypatch.setattr(dl_mod.shutil, "disk_usage", lambda _p: SimpleNamespace(free=fake_free[0]))
        budget = DownloadDiskBudget(tmp_dir, keep_free_bytes=400)
        # Allow download (keep_free checked at 4MB or near 64MB -> let's test pre-remux reservation directly by
        # keeping free > 400 + 2100 during download, then tightening right before remux, OR simply pre-seeding stream.mp4!)
        fmp4_hls_dir = tmp_dir / "AV1 Clip [xhAv100].hls"
        fmp4_hls_dir.mkdir(parents=True)
        (fmp4_hls_dir / "manifest.json").write_text(
            json.dumps({"engine": "yt-dlp", "version": 2, "viewkey": "xhAv100", "quality": "2160"}),
            encoding="utf-8",
        )
        (fmp4_hls_dir / "stream.mp4").write_bytes(fmp4_init + fmp4_seg)
        paused = await hls_mod.download_hls_to_mp4(
            session=None,  # type: ignore[arg-type]
            parse_ctx=ctx,
            selectors=SelectorSettings(),
            viewkey="xhAv100",
            title="AV1 Clip",
            playlist_url=f"http://127.0.0.1:{port}/av1.m3u8",
            page_url="https://xhamster.com/videos/av1-clip-xhAv100",
            referer_url="https://xhamster.com/videos/av1-clip-xhAv100",
            quality="2160p",
            download_dir=dl_dir,
            temp_dir=tmp_dir,
            disk_budget=budget,
            min_file_size_bytes=16,
        )
        assert paused.success is False
        assert paused.disk_wait_bytes == len(fmp4_init + fmp4_seg)
        assert (fmp4_hls_dir / "stream.mp4").is_file()

        # Once space is freed, retry skips re-downloading av1.m3u8 and remuxes fMP4 cleanly!
        hits.clear()
        monkeypatch.setattr(dl_mod.shutil, "disk_usage", orig_disk_usage)
        finished_av1 = await hls_mod.download_hls_to_mp4(
            session=None,  # type: ignore[arg-type]
            parse_ctx=ctx,
            selectors=SelectorSettings(),
            viewkey="xhAv100",
            title="AV1 Clip",
            playlist_url=f"http://127.0.0.1:{port}/av1.m3u8",
            page_url="https://xhamster.com/videos/av1-clip-xhAv100",
            referer_url="https://xhamster.com/videos/av1-clip-xhAv100",
            quality="2160p",
            download_dir=dl_dir,
            temp_dir=tmp_dir,
            min_file_size_bytes=16,
        )
        assert finished_av1.success is True
        assert hits == []  # did NOT re-download a single byte!
        assert len(remux_calls) == 1
        assert "-bsf:a" not in remux_calls[0]
        assert "-movflags" in remux_calls[0] and "+faststart" in remux_calls[0]
    finally:
        httpd.shutdown()


async def test_download_hls_to_mp4_aiohttp_fast_path_resume_403_and_ytdlp_fallback(tmp_path: Path, monkeypatch):
    """Verify the async aiohttp HLS engine: concurrent ordered assembly, cancel+resume, 403 refresh, fMP4 init, and encrypted yt-dlp fallback."""
    import http.server
    import socketserver
    import threading
    import aiohttp

    from app.parsers.video import VideoDetail

    # Distinct valid 188-byte MPEG-TS packets (0x47 sync byte every 188 bytes) so we prove exact in-order assembly!
    ts_segs = [
        (bytes([0x47, idx]) + bytes([idx + 1] * 186)) * 8
        for idx in range(6)
    ]
    fmp4_init = mp4_payload(b"init")[:64]
    fmp4_segs = [b"moof" + bytes([10 + idx] * 2044) for idx in range(3)]

    srv_dir = tmp_path / "hls_aio_srv"
    srv_dir.mkdir()
    for idx, payload in enumerate(ts_segs):
        (srv_dir / f"seg{idx}.ts").write_bytes(payload)
    ts_playlist = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
        + "".join(f"#EXTINF:4.000,\nseg{idx}.ts\n" for idx in range(len(ts_segs)))
        + "#EXT-X-ENDLIST\n"
    )
    (srv_dir / "ts_v1.m3u8").write_text(ts_playlist, encoding="utf-8")
    (srv_dir / "ts_v2.m3u8").write_text(ts_playlist, encoding="utf-8")

    (srv_dir / "init.mp4").write_bytes(fmp4_init)
    for idx, payload in enumerate(fmp4_segs):
        (srv_dir / f"seg{idx}.m4s").write_bytes(payload)
    (srv_dir / "fmp4.m3u8").write_text(
        '#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-TARGETDURATION:4\n#EXT-X-MAP:URI="init.mp4"\n'
        + "".join(f"#EXTINF:4.000,\nseg{idx}.m4s\n" for idx in range(len(fmp4_segs)))
        + "#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    (srv_dir / "enc.m3u8").write_text(
        '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
        "#EXTINF:4.000,\nseg0.ts\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )

    hits: List[str] = []
    loop = asyncio.get_running_loop()
    state: Dict[str, Any] = {"fail_403_seg3": False, "cancel_on_seg2": None}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(srv_dir), **kw)

        def do_GET(self):
            hits.append(self.path)
            if state["fail_403_seg3"] and self.path == "/seg3.ts":
                self.send_error(403, "Expired signed CDN segment")
                return
            if state["cancel_on_seg2"] is not None and self.path == "/seg2.ts":
                ev = state["cancel_on_seg2"]
                loop.call_soon_threadsafe(ev.set)
                import time
                time.sleep(0.08)
            return super().do_GET()

        def log_message(self, *a):
            pass

    class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True

    httpd = ThreadedServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    srv_thread.start()

    monkeypatch.setattr(hls_mod.shutil, "which", lambda name: "/usr/local/bin/ffmpeg")
    captured_inputs: List[bytes] = []
    remux_cmds: List[List[str]] = []

    async def fake_run_ffmpeg(cmd: List[str], *, cancel_event: asyncio.Event, timeout: float = 600.0):
        remux_cmds.append(cmd)
        in_path = Path(cmd[cmd.index("-i") + 1])
        captured_inputs.append(in_path.read_bytes())
        out_path = Path(cmd[-1])
        out_path.write_bytes(mp4_payload())
        return 0, ""

    monkeypatch.setattr(hls_mod, "_run_ffmpeg", fake_run_ffmpeg)

    try:
        dl_dir = tmp_path / "downloads" / "xHamster" / "creator_fast"
        tmp_dir = tmp_path / "temp" / "xHamster" / "creator_fast"
        ctx = TaskHttpContext(concurrency=2)

        async with aiohttp.ClientSession() as session:
            # 1. Cancel on seg2 (sequential concurrency=1) -> seg0 & seg1 persisted in single stream.part + manifest.json
            hls_dir = tmp_dir / "Fast TS [xhFast1].hls"
            cancel_ev = asyncio.Event()
            state["cancel_on_seg2"] = cancel_ev
            cancelled = await hls_mod.download_hls_to_mp4(
                session=session,
                parse_ctx=ctx,
                selectors=SelectorSettings(),
                viewkey="xhFast1",
                title="Fast TS",
                playlist_url=f"http://127.0.0.1:{port}/ts_v1.m3u8",
                page_url="https://xhamster.com/videos/fast-ts-xhFast1",
                referer_url="https://xhamster.com/videos/fast-ts-xhFast1",
                quality="1080p",
                download_dir=dl_dir,
                temp_dir=tmp_dir,
                segment_concurrency=1,
                min_file_size_bytes=16,
                cancel_event=cancel_ev,
            )
            assert cancelled.cancelled is True
            assert cancelled.temp_path == hls_dir
            manifest = json.loads((hls_dir / "manifest.json").read_text(encoding="utf-8"))
            assert manifest["engine"] == "aiohttp"
            assert manifest["completed_segments"] == 2
            assert (hls_dir / "stream.part").stat().st_size == len(ts_segs[0]) + len(ts_segs[1])
            assert not any("-Frag" in p.name for p in hls_dir.iterdir())

            # 2. Resume concurrently (segment_concurrency=4) -> skips seg0 & seg1, writes exact byte-ordered stream!
            hits.clear()
            state["cancel_on_seg2"] = None
            phases: List[str] = []
            resumed = await hls_mod.download_hls_to_mp4(
                session=session,
                parse_ctx=ctx,
                selectors=SelectorSettings(),
                viewkey="xhFast1",
                title="Fast TS",
                playlist_url=f"http://127.0.0.1:{port}/ts_v2.m3u8",
                page_url="https://xhamster.com/videos/fast-ts-xhFast1",
                referer_url="https://xhamster.com/videos/fast-ts-xhFast1",
                quality="1080p",
                download_dir=dl_dir,
                temp_dir=tmp_dir,
                partial_path=hls_dir,
                segment_concurrency=4,
                min_file_size_bytes=16,
                on_progress=lambda dl, tot, spd, phase="downloading": phases.append(phase),
            )
            assert resumed.success is True
            assert not hls_dir.exists()
            assert "/seg0.ts" not in hits and "/seg1.ts" not in hits
            assert captured_inputs[-1] == b"".join(ts_segs)
            assert "downloading" in phases and "remuxing" in phases
            assert "-bsf:a" in remux_cmds[-1]

            # 3. fMP4 with #EXT-X-MAP init segment assembles init + seg0..seg2 in exact order
            hits.clear()
            fmp4_res = await hls_mod.download_hls_to_mp4(
                session=session,
                parse_ctx=ctx,
                selectors=SelectorSettings(),
                viewkey="xhFmp4Fast",
                title="Fast fMP4",
                playlist_url=f"http://127.0.0.1:{port}/fmp4.m3u8",
                page_url="https://xhamster.com/videos/fast-fmp4-xhFmp4Fast",
                referer_url="https://xhamster.com/videos/fast-fmp4-xhFmp4Fast",
                quality="2160p",
                download_dir=dl_dir,
                temp_dir=tmp_dir,
                segment_concurrency=3,
                min_file_size_bytes=16,
            )
            assert fmp4_res.success is True
            assert captured_inputs[-1] == fmp4_init + b"".join(fmp4_segs)
            assert "-bsf:a" not in remux_cmds[-1]

            # 4. HTTP 403 on seg3 refreshes URL and resumes seamlessly without re-downloading seg0..seg2
            hits.clear()
            state["fail_403_seg3"] = True
            refresh_count = 0

            async def fake_fetch_single(*args, **kwargs):
                nonlocal refresh_count
                refresh_count += 1
                state["fail_403_seg3"] = False
                return VideoDetail(
                    viewkey="xhAio403",
                    title="Aio 403",
                    page_url="https://xhamster.com/videos/aio-403-xhAio403",
                    referer_url="https://xhamster.com/videos/aio-403-xhAio403",
                    download_url=f"http://127.0.0.1:{port}/ts_v2.m3u8",
                    quality="1080p",
                    video_format="hls",
                )

            monkeypatch.setattr(hls_mod, "fetch_single_video_detail", fake_fetch_single)
            res_403 = await hls_mod.download_hls_to_mp4(
                session=session,
                parse_ctx=ctx,
                selectors=SelectorSettings(),
                viewkey="xhAio403",
                title="Aio 403",
                playlist_url=f"http://127.0.0.1:{port}/ts_v1.m3u8",
                page_url="https://xhamster.com/videos/aio-403-xhAio403",
                referer_url="https://xhamster.com/videos/aio-403-xhAio403",
                quality="1080p",
                download_dir=dl_dir,
                temp_dir=tmp_dir,
                max_attempts=2,
                segment_concurrency=1,
                retry_delay_base=0.0,
                retry_delay_max=0.0,
                min_file_size_bytes=16,
            )
            assert res_403.success is True
            assert refresh_count == 1
            assert hits.count("/seg0.ts") == 1 and hits.count("/seg1.ts") == 1 and hits.count("/seg2.ts") == 1
            assert captured_inputs[-1] == b"".join(ts_segs)

            # 5. Encrypted HLS playlist seamlessly falls back to yt-dlp engine
            fallback_called: List[str] = []

            def fake_ytdlp_sync(**kwargs):
                fallback_called.append(kwargs["playlist_url"])
                (kwargs["hls_dir"] / "stream.mp4").write_bytes(b"".join(ts_segs))
                return True, [], None

            monkeypatch.setattr(hls_mod, "_run_ytdlp_hls_sync", fake_ytdlp_sync)
            enc_res = await hls_mod.download_hls_to_mp4(
                session=session,
                parse_ctx=ctx,
                selectors=SelectorSettings(),
                viewkey="xhEncFb",
                title="Encrypted Clip",
                playlist_url=f"http://127.0.0.1:{port}/enc.m3u8",
                page_url="https://xhamster.com/videos/enc-xhEncFb",
                referer_url="https://xhamster.com/videos/enc-xhEncFb",
                quality="1080p",
                download_dir=dl_dir,
                temp_dir=tmp_dir,
                min_file_size_bytes=16,
            )
            assert enc_res.success is True
            assert fallback_called == [f"http://127.0.0.1:{port}/enc.m3u8"]
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# 4. Schema v9 Migration, Multi-Site Repo Isolation & Uploader Routing
# ---------------------------------------------------------------------------
async def test_schema_v9_migration_and_multisite_target_isolation(tmp_path: Path):
    db_path = tmp_path / "v8_to_v9.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA_SQL)
        for version, statements in MIGRATIONS:
            if version <= 8:
                for stmt in statements:
                    conn.execute(stmt)
        conn.execute("PRAGMA user_version = 8")
        conn.execute(
            "INSERT INTO targets (username, target_type, enabled, created_at) VALUES (?, ?, 1, '2026-01-01')",
            ("webtolove", "model"),
        )
        conn.execute(
            "INSERT INTO videos (viewkey, title, uploader, target_type, status, created_at) VALUES (?, ?, ?, ?, 'uploaded', '2026-01-01')",
            ("ph111", "PH Video", "webtolove", "model"),
        )
        conn.commit()

    db = Database(db_path)
    await db.connect()
    try:
        assert await db.schema_version() == SCHEMA_VERSION
        repo = Repository(db)

        # Can track the same slug `webtolove` on xHamster independently!
        xh_target = await repo.upsert_target("webtolove", "users", site="xhamster", display_name="WebToLove XH")
        assert xh_target["site"] == "xhamster"

        await repo.upsert_video(
            "xhOwQIZ",
            site="xhamster",
            title="XH Video",
            uploader="webtolove",
            target_type="users",
            video_page_url="https://xhamster.com/videos/xh-video-xhOwQIZ",
            status="uploaded",
        )

        targets = await repo.list_targets()
        by_site = {t["site"]: t for t in targets}
        assert set(by_site.keys()) == {"pornhub", "xhamster"}
        assert by_site["pornhub"]["video_total_count"] == 1
        assert by_site["xhamster"]["video_total_count"] == 1

        # Query filtering by site works cleanly
        xh_rows, xh_total = await repo.query_videos(site="xhamster")
        assert xh_total == 1 and xh_rows[0]["viewkey"] == "xhOwQIZ"
        ph_rows, ph_total = await repo.query_videos(site="pornhub")
        assert ph_total == 1 and ph_rows[0]["viewkey"] == "ph111"

        # Remote dir routing uses separate xHamster base dir
        settings = RuntimeSettings(rclone_remote="gd", rclone_remote_base_dir="Pornhub", rclone_xh_remote_base_dir="xHamster")
        assert build_remote_dir(settings, "webtolove", site="pornhub") == "gd:Pornhub/webtolove"
        assert build_remote_dir(settings, "webtolove", site="xhamster") == "gd:xHamster/webtolove"
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 5. TaskManager: xHamster Job Runs WITHOUT PornHub Cookie & Uploads to xHamster/
# ---------------------------------------------------------------------------
async def test_xhamster_manual_job_skips_pornhub_cookie_and_uploads_to_xh_remote_dir(tmp_path: Path, monkeypatch):
    from app.engine import task_manager as tm_mod

    env = EnvSettings(data_dir=tmp_path)
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    # Intentionally leave ph_cookie AND xh_cookie EMPTY!
    await repo.save_settings(
        RuntimeSettings(
            ph_cookie="",
            xh_cookie="",
            uploader_enabled=True,
            rclone_remote="gd",
            rclone_remote_base_dir="Pornhub",
            rclone_xh_remote_base_dir="xHamster",
            min_file_size_bytes=16,
            page_delay_min=0.0,
            page_delay_max=0.0,
        )
    )

    ph_cookie_checked = False

    async def forbid_ph_cookie(*args, **kwargs):
        nonlocal ph_cookie_checked
        ph_cookie_checked = True
        raise AssertionError("PornHub cookie check must NOT run for xHamster-only jobs!")

    monkeypatch.setattr(tm_mod, "check_cookie_valid", forbid_ph_cookie)

    async def fake_scan_list_pages(ctx, initial_url, selectors, **kwargs):
        assert initial_url == "https://xhamster.com/users/webtolove/videos"
        from app.parsers.list_scanner import ListedVideo

        return [
            ListedVideo(
                viewkey="xhOwQIZ",
                url="https://xhamster.com/videos/demo-xhOwQIZ",
                title="XH Demo",
                page_num=1,
                item_num=1,
                total_on_page=1,
                published_at="2026-01-01",
                duration_seconds=300,
            )
        ]

    async def fake_fetch_details(ctx, items, selectors, **kwargs):
        from app.parsers.video import VideoDetail

        return [
            VideoDetail(
                viewkey="xhOwQIZ",
                title="XH Demo",
                page_url="https://xhamster.com/videos/demo-xhOwQIZ",
                referer_url="https://xhamster.com/videos/demo-xhOwQIZ",
                download_url="https://video-nss.xhcdn.com/tok/2160p.h264.mp4.m3u8",
                quality="2160p",
                video_format="hls",
                uploader_username="webtolove",
                uploader_type="users",
                published_at="2026-01-01",
                duration_seconds=300,
            )
        ]

    async def fake_download_hls(**kwargs):
        assert kwargs["segment_concurrency"] == 4
        assert kwargs["max_buffer_bytes"] == 12 * 1024 * 1024
        dl_dir: Path = kwargs["download_dir"]
        dl_dir.mkdir(parents=True, exist_ok=True)
        out = dl_dir / "XH Demo [xhOwQIZ].mp4"
        out.write_bytes(mp4_payload())
        return hls_mod.DownloadOutcome(
            success=True,
            local_path=out,
            file_size=out.stat().st_size,
            quality="2160p",
            download_url=kwargs["playlist_url"],
        )

    upload_sites: List[str] = []

    async def fake_upload_file_batch(
        download_dir: Path,
        uploader_folder: str,
        files_by_viewkey: Dict[str, Path],
        settings: RuntimeSettings,
        cancel_event=None,
        **kwargs,
    ):
        site = kwargs.get("site", "pornhub")
        upload_sites.append(site)
        remote_dir = build_remote_dir(settings, uploader_folder, site=site)
        return UploadBatchResult(
            uploaded_viewkeys=list(files_by_viewkey.keys()),
            remote_dir=remote_dir,
            verified_sources={vk: file_snapshot(p) for vk, p in files_by_viewkey.items()},
            remote_paths={vk: f"{remote_dir}/{p.name}" for vk, p in files_by_viewkey.items()},
        )

    monkeypatch.setattr(tm_mod, "scan_list_pages", fake_scan_list_pages)
    monkeypatch.setattr(tm_mod, "fetch_video_details_batch", fake_fetch_details)
    monkeypatch.setattr(tm_mod, "download_hls_to_mp4", fake_download_hls)
    monkeypatch.setattr(tm_mod, "upload_file_batch", fake_upload_file_batch)

    tm = TaskManager(env, repo, events)
    tm.start()
    try:
        job = await tm.enqueue_job(
            kind="manual_target",
            label="[xHamster] 下载目标: webtolove",
            params={
                "site": "xhamster",
                "usernames": ["webtolove"],
                "target_type": "users",
                "auto_upload": True,
            },
        )
        for _ in range(80):
            cur = await repo.get_job(job["id"])
            if cur and cur["status"] in {"completed", "failed", "partial_failed", "cancelled"}:
                break
            await asyncio.sleep(0.05)

        final_job = await repo.get_job(job["id"])
        assert final_job is not None and final_job["status"] == "completed"
        assert ph_cookie_checked is False
        assert upload_sites == ["xhamster"]

        video = await repo.get_video("xhOwQIZ")
        assert video is not None
        assert video["site"] == "xhamster"
        assert video["status"] == "uploaded"
        assert video["remote_path"].startswith("gd:xHamster/webtolove/")
    finally:
        await tm.stop()
        await db.close()


# ---------------------------------------------------------------------------
# 6. API E2E: xHamster Target CRUD, Cookie Import, Subscriptions & Video Filter
# ---------------------------------------------------------------------------
def test_xhamster_api_endpoints_e2e(tmp_path: Path, monkeypatch):
    from app.api import overview as overview_api
    from app.api import settings as settings_api
    from app.parsers.xhamster import XHamsterSubscriptionItem

    async def fake_check_xh_cookie(cookie_str: str, **kwargs):
        return True, "Cookie 有效（当前账号: OrderlyPaw690）", "OrderlyPaw690"

    async def fake_xh_subs(ctx, timeout=20.0):
        return [
            XHamsterSubscriptionItem(
                "creator",
                "fragile-tempt",
                "Fragile Tempt",
                "https://ic-nss.xhcdn.com/ft.jpg",
                "webtolove",
                "webtolove",
            )
        ]

    monkeypatch.setattr(settings_api, "check_xhamster_cookie_valid", fake_check_xh_cookie)
    monkeypatch.setattr(overview_api, "fetch_xhamster_subscriptions_detailed", fake_xh_subs)

    env = EnvSettings(data_dir=tmp_path, admin_password="secret", secret_key="k" * 40)
    app = create_app(env)
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"password": "secret"}).status_code == 200

        # Import xHamster cookie
        r_imp = client.post("/api/settings/xh-cookie/import", json={"cookie": "xh_sess=ok"})
        assert r_imp.status_code == 200
        assert r_imp.json()["valid"] is True
        assert r_imp.json()["logged_in_username"] == "OrderlyPaw690"

        # Verify redaction + codec & remote base dir settings persistence
        r_put = client.put(
            "/api/settings",
            json={"xh_preferred_codec": "av1", "rclone_xh_remote_base_dir": "XH_Archive"},
        )
        assert r_put.status_code == 200
        s = r_put.json()["settings"]
        assert s["xh_preferred_codec"] == "av1"
        assert s["rclone_xh_remote_base_dir"] == "XH_Archive"
        assert s["has_xh_cookie"] is True
        assert s["xh_cookie"] == "********"

        # Fetch xHamster subscriptions
        r_subs = client.get("/api/subscriptions?site=xhamster")
        assert r_subs.status_code == 200
        assert r_subs.json()["site"] == "xhamster"
        assert r_subs.json()["subscriptions"][0]["username"] == "fragile-tempt"
        assert r_subs.json()["subscriptions"][0]["bound_username"] == "webtolove"
        assert r_subs.json()["subscriptions"][0]["bound_display_name"] == "webtolove"

        # Create xHamster creator & favorites targets
        r_t1 = client.post(
            "/api/targets",
            json={"site": "xhamster", "username": "fragile-tempt", "target_type": "creator"},
        )
        assert r_t1.status_code == 200
        assert r_t1.json()["target"]["site"] == "xhamster"

        r_t2 = client.post(
            "/api/targets",
            json={"site": "xhamster", "username": "", "target_type": "favorites"},
        )
        assert r_t2.status_code == 200
        assert r_t2.json()["target"]["username"] == "_currentUser_"
        assert r_t2.json()["target"]["target_type"] == "favorites"


# ---------------------------------------------------------------------------
# 7. xHamster Auto-Login Service, Refresh Endpoint & Engine Auto-Renewal
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_login_xhamster_and_get_cookies_success_and_error(monkeypatch):
    from types import SimpleNamespace
    from app.services import cookie as cookie_svc
    from app.parsers import xhamster as xh_parser

    captured_calls = []

    class FakeLoginCtx:
        def __init__(self, cookie_str="", **kwargs):
            self.cookie_str = cookie_str
            self.cancel_event = asyncio.Event()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def cookie_string(self):
            return self.cookie_str

        async def fetch_with_pow(self, url, method="GET", data=None, headers=None, **kwargs):
            captured_calls.append((method, url, data, headers or {}))
            if method == "GET":
                return SimpleNamespace(status_code=200, text="<html>login</html>", final_url=url), None
            body = json.loads(data)
            if body["password"] == "good-pass":
                self.cookie_str = "x_csrf_token=1; _id=fake_session_token; _cfg=abc"
                return SimpleNamespace(
                    status_code=200,
                    text=json.dumps({"extras": {"result": True, "userId": 411169334}, "requestData": []}),
                    final_url=url,
                ), None
            return SimpleNamespace(
                status_code=200,
                text=json.dumps({"extras": {"result": False, "error": {"username": "Invalid login or password"}}, "requestData": []}),
                final_url=url,
            ), None

    async def fake_verify(cookie_str: str, **kwargs):
        assert "_id=fake_session_token" in cookie_str
        return True, "Cookie 有效（当前账号: NativePea222）", "NativePea222"

    monkeypatch.setattr(cookie_svc, "TaskHttpContext", FakeLoginCtx)
    monkeypatch.setattr(xh_parser, "check_xhamster_cookie_valid", fake_verify)

    # Empty creds
    empty = await cookie_svc.login_xhamster_and_get_cookies("", "")
    assert empty.success is False

    # Successful login verifies double-submit CSRF header + JSON payload
    ok = await cookie_svc.login_xhamster_and_get_cookies("NativePea222", "good-pass")
    assert ok.success is True
    assert ok.logged_in_username == "NativePea222"
    assert "_id=fake_session_token" in ok.cookie_str
    post_call = [c for c in captured_calls if c[0] == "POST"][0]
    assert post_call[1] == "https://xhamster.com/api/front/user/login"
    assert post_call[3].get("X-CSRF-Token") == "1"
    assert json.loads(post_call[2])["remember"] == 1

    # Wrong password returns friendly Chinese error
    bad = await cookie_svc.login_xhamster_and_get_cookies("NativePea222", "bad-pass")
    assert bad.success is False
    assert "账号或密码错误" in bad.message


def test_xhamster_login_url_encodes_captcha_token():
    from app.services.cookie import _xhamster_login_url

    url = _xhamster_login_url(
        "https://xhamster.com/api/front/user/login",
        captcha_token="a+b/c=",
        captcha_version="v2",
    )
    assert "g-recaptcha-response=a%2Bb%2Fc%3D" in url
    assert "g-recaptcha-version=v2" in url


XH_LOGIN_HTML_WITH_CAPTCHA_KEYS = (
    '<script>window.initials={"recaptchaEnterpriseKeyV3":"page-ent-key",'
    '"recaptchaKeyV2":"page-v2-key"};</script>'
)


def _xh_captcha_ctx():
    """Build a fake TaskHttpContext that returns scripted login responses."""

    class FakeLoginCtx:
        instances = []

        def __init__(self, cookie_str="", **kwargs):
            self.cookie_str = cookie_str
            self.cancel_event = asyncio.Event()
            self.post_payloads: List[Dict[str, Any]] = []
            self.post_urls: List[str] = []
            self.responses: List[tuple] = []
            FakeLoginCtx.instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def cookie_string(self):
            return self.cookie_str

        async def fetch_with_pow(self, url, method="GET", data=None, headers=None, **kwargs):
            if method == "GET":
                return SimpleNamespace(
                    status_code=200,
                    text=XH_LOGIN_HTML_WITH_CAPTCHA_KEYS,
                    final_url=url,
                ), None

            self.post_payloads.append(json.loads(data))
            self.post_urls.append(url)
            status, body = self.responses.pop(0)
            if status == 200:
                self.cookie_str = "x_csrf_token=1; _id=fake_after_captcha"
            return SimpleNamespace(
                status_code=status,
                text=json.dumps(body) if isinstance(body, dict) else str(body),
                final_url=url,
            ), None

    return FakeLoginCtx


def _xh_show_captcha() -> Dict[str, Any]:
    return {
        "extras": {
            "result": False,
            "showCaptcha": True,
            "captchaActionName": "customLoginAction",
        },
        "requestData": [],
    }


@pytest.mark.parametrize("task_type", ["NoCaptchaTaskProxyless", "RecaptchaV2TaskProxyless"])
@pytest.mark.asyncio
async def test_login_xhamster_uses_selected_v2_route(monkeypatch, task_type):
    from app.services import cookie as cookie_svc
    from app.services.captcha import CaptchaSolution
    from app.parsers import xhamster as xh_parser

    FakeLoginCtx = _xh_captcha_ctx()
    solver_calls: List[Dict[str, Any]] = []

    async def fake_solver(client_key, website_url, website_key, **kwargs):
        solver_calls.append(
            {
                "client_key": client_key,
                "website_url": website_url,
                "website_key": website_key,
                **kwargs,
            }
        )
        return CaptchaSolution(token="v2-token", task_id="task-v2", task_type="type")

    async def fake_verify(cookie_str: str, **kwargs):
        assert "_id=fake_after_captcha" in cookie_str
        return True, "Cookie 有效（当前账号: NativePea222）", "NativePea222"

    def make_ctx(*args, **kwargs):
        ctx = FakeLoginCtx(*args, **kwargs)
        ctx.responses = [
            (403, _xh_show_captcha()),
            (200, {"extras": {"result": True, "userId": 1}, "requestData": []}),
        ]
        return ctx

    monkeypatch.setattr(cookie_svc, "TaskHttpContext", make_ctx)
    monkeypatch.setattr(cookie_svc, "solve_recaptcha", fake_solver)
    monkeypatch.setattr(xh_parser, "check_xhamster_cookie_valid", fake_verify)

    result = await cookie_svc.login_xhamster_and_get_cookies(
        "NativePea222",
        "good-pass",
        yescaptcha_enabled=True,
        yescaptcha_client_key="client-key",
        yescaptcha_task_type=task_type,
        yescaptcha_timeout_seconds=60,
    )

    assert result.success is True
    assert "_id=fake_after_captcha" in result.cookie_str

    ctx = FakeLoginCtx.instances[-1]
    assert len(ctx.post_urls) == 2
    assert "g-recaptcha-response" not in ctx.post_urls[0]
    assert "g-recaptcha-response=v2-token" in ctx.post_urls[1]
    assert "g-recaptcha-version=v2" in ctx.post_urls[1]
    assert all("g-recaptcha-response" not in payload for payload in ctx.post_payloads)

    assert len(solver_calls) == 1
    assert solver_calls[0]["task_type"] == task_type
    assert solver_calls[0]["website_key"] == "page-v2-key"
    assert solver_calls[0]["timeout"] == 60
    assert "page_action" not in solver_calls[0]


@pytest.mark.asyncio
async def test_login_xhamster_rejected_v2_token_does_not_fallback_to_v3(monkeypatch):
    from app.services import cookie as cookie_svc
    from app.services.captcha import CaptchaSolution
    from app.parsers import xhamster as xh_parser

    FakeLoginCtx = _xh_captcha_ctx()
    solver_calls: List[Dict[str, Any]] = []

    async def fake_solver(client_key, website_url, website_key, **kwargs):
        solver_calls.append(kwargs)
        return CaptchaSolution(token="v2-token", task_id="task-v2", task_type="type")

    def make_ctx(*args, **kwargs):
        ctx = FakeLoginCtx(*args, **kwargs)
        ctx.responses = [
            (403, _xh_show_captcha()),
            (400, {"extras": {"result": False, "errorMessage": "Recaptcha failure. Try again"}, "requestData": []}),
        ]
        return ctx

    monkeypatch.setattr(cookie_svc, "TaskHttpContext", make_ctx)
    monkeypatch.setattr(cookie_svc, "solve_recaptcha", fake_solver)
    monkeypatch.setattr(xh_parser, "check_xhamster_cookie_valid", lambda *a, **k: None)

    result = await cookie_svc.login_xhamster_and_get_cookies(
        "NativePea222",
        "good-pass",
        yescaptcha_enabled=True,
        yescaptcha_client_key="client-key",
        yescaptcha_timeout_seconds=60,
    )

    assert result.success is False
    assert "切换另一个标准 v2 线路" in result.message
    assert len(solver_calls) == 1

    ctx = FakeLoginCtx.instances[-1]
    assert len(ctx.post_urls) == 2


@pytest.mark.asyncio
async def test_login_xhamster_requires_yescaptcha_when_challenged(monkeypatch):
    from app.services import cookie as cookie_svc
    from app.parsers import xhamster as xh_parser

    solver_calls = []

    class FakeLoginCtx:
        def __init__(self, cookie_str="", **kwargs):
            self.cookie_str = cookie_str
            self.cancel_event = asyncio.Event()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def cookie_string(self):
            return self.cookie_str

        async def fetch_with_pow(self, url, method="GET", data=None, headers=None, **kwargs):
            if method == "GET":
                return SimpleNamespace(
                    status_code=200,
                    text='<script>{"recaptchaEnterpriseKeyV3":"ent","recaptchaKeyV2":"v2"}</script>',
                    final_url=url,
                ), None
            return SimpleNamespace(
                status_code=403,
                text=json.dumps(
                    {
                        "extras": {
                            "result": False,
                            "showCaptcha": True,
                            "captchaActionName": "authorizedUserModel",
                        },
                        "requestData": [],
                    }
                ),
                final_url=url,
            ), None

    async def fake_solver(*args, **kwargs):
        solver_calls.append((args, kwargs))
        raise AssertionError("solver must not be called when YesCaptcha is disabled")

    monkeypatch.setattr(cookie_svc, "TaskHttpContext", FakeLoginCtx)
    monkeypatch.setattr(cookie_svc, "solve_recaptcha", fake_solver)
    monkeypatch.setattr(xh_parser, "check_xhamster_cookie_valid", lambda *a, **k: None)

    result = await cookie_svc.login_xhamster_and_get_cookies("NativePea222", "good-pass")

    assert result.success is False
    assert "YesCaptcha" in result.message
    assert solver_calls == []


def test_xhamster_refresh_endpoint_background_polling(tmp_path: Path, monkeypatch):
    from app.api import settings as settings_api
    from app.services.cookie import CookieLoginResult

    calls = {"count": 0}

    async def fake_xh_login(username: str, password: str, **kwargs):
        calls["count"] += 1
        await asyncio.sleep(0.05)
        return CookieLoginResult(
            success=True,
            cookie_str="x_csrf_token=1; _id=background_id_token",
            logged_in_username="NativePea222",
            message="xHamster 登录并更新 Cookie 成功（当前账号: NativePea222）",
        )

    monkeypatch.setattr(settings_api, "login_xhamster_and_get_cookies", fake_xh_login)

    env = EnvSettings(data_dir=tmp_path, admin_password="secret", secret_key="k" * 40)
    app = create_app(env)
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"password": "secret"}).status_code == 200
        client.put(
            "/api/settings",
            json={"xh_username": "NativePea222", "xh_password": "secret-pass"},
        )

        first = client.post("/api/settings/xh-cookie/refresh").json()
        assert first["started"] is True
        assert first["running"] is True

        second = client.post("/api/settings/xh-cookie/refresh").json()
        assert second["started"] is False
        assert second["running"] is True

        status = {}
        for _ in range(100):
            status = client.get("/api/settings/xh-cookie/refresh/status").json()
            if not status["running"]:
                break
            time.sleep(0.02)

        assert status["running"] is False
        assert status["success"] is True
        assert status["logged_in_username"] == "NativePea222"
        assert calls["count"] == 1


def test_xhamster_refresh_endpoint_and_password_redaction(tmp_path: Path, monkeypatch):
    from app.api import settings as settings_api
    from app.services.cookie import CookieLoginResult

    async def fake_xh_login(username: str, password: str, **kwargs):
        if username == "NativePea222" and password == "secret-pass":
            return CookieLoginResult(
                success=True,
                cookie_str="x_csrf_token=1; _id=refreshed_id_token",
                logged_in_username="NativePea222",
                message="xHamster 登录并更新 Cookie 成功（当前账号: NativePea222）",
            )
        return CookieLoginResult(success=False, message="xHamster 登录被拒绝: 账号或密码错误")

    monkeypatch.setattr(settings_api, "login_xhamster_and_get_cookies", fake_xh_login)

    env = EnvSettings(data_dir=tmp_path, admin_password="secret", secret_key="k" * 40)
    app = create_app(env)
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"password": "secret"}).status_code == 200

        r_save = client.put(
            "/api/settings",
            json={"xh_username": "NativePea222", "xh_password": "secret-pass"},
        )
        assert r_save.status_code == 200
        saved = r_save.json()["settings"]
        assert saved["xh_username"] == "NativePea222"
        assert saved["xh_password"] == "********"

        r_ref = client.post("/api/settings/xh-cookie/refresh?wait=1")
        assert r_ref.status_code == 200
        body = r_ref.json()
        assert body["success"] is True
        assert body["logged_in_username"] == "NativePea222"

        r_status = client.get("/api/settings/xh-cookie/refresh/status")
        assert r_status.status_code == 200
        assert r_status.json()["running"] is False
        assert r_status.json()["success"] is True

        r_get = client.get("/api/settings")
        assert r_get.status_code == 200
        s = r_get.json()["settings"]
        assert s["has_xh_cookie"] is True
        assert s["xh_cookie"] == "********"
        assert s["xh_logged_in_username"] == "NativePea222"
        assert r_get.json()["xh_cookie_status"]["valid"] is True


@pytest.mark.asyncio
async def test_task_manager_auto_renews_expired_xhamster_cookie(tmp_path: Path, monkeypatch):
    from app.engine import task_manager as tm_mod
    from app.services.cookie import CookieLoginResult

    env = EnvSettings(data_dir=tmp_path, admin_password="secret", secret_key="k" * 40)
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    await repo.update_settings_patch(
        {
            "xh_username": "NativePea222",
            "xh_password": "secret-pass",
            "xh_cookie": "x_csrf_token=1; _id=expired",
        }
    )
    events = EventBus()
    tm = TaskManager(env, repo, events)

    async def fake_check_xh(cookie_str: str, **kwargs):
        if "_id=fresh" in cookie_str:
            return True, "Cookie 有效（当前账号: NativePea222）", "NativePea222"
        return False, "xHamster Cookie 已失效（重定向到登录页）", ""

    async def fake_login_xh(username: str, password: str, **kwargs):
        assert username == "NativePea222" and password == "secret-pass"
        return CookieLoginResult(
            success=True,
            cookie_str="x_csrf_token=1; _id=fresh",
            logged_in_username="NativePea222",
            message="xHamster 登录并更新 Cookie 成功（当前账号: NativePea222）",
        )

    monkeypatch.setattr(tm_mod, "check_xhamster_cookie_valid", fake_check_xh)
    monkeypatch.setattr(tm_mod, "login_xhamster_and_get_cookies", fake_login_xh)

    try:
        settings = await repo.get_settings()
        refreshed = await tm._ensure_valid_xh_cookie(settings, job_id=1)
        assert refreshed is not None
        assert "_id=fresh" in refreshed.xh_cookie
        assert refreshed.xh_logged_in_username == "NativePea222"
        assert tm.xh_cookie_status["valid"] is True
    finally:
        await db.close()


def test_xhamster_uploader_prefers_landing_creator_and_channel_over_author():
    from app.parsers.xhamster import _extract_xhamster_uploader

    creator_model = {
        "author": {"name": "SweetHot69", "pageURL": "https://xhamster.com/users/sweethot69"},
        "landing": {"type": "person", "name": "SweetGirl69", "link": "https://xhamster.com/creators/sweetgirl69"},
    }
    assert _extract_xhamster_uploader(creator_model, None) == ("sweetgirl69", "creator")

    channel_model = {
        "author": {"name": "BLACKED", "pageURL": "https://xhamster.com/users/blacked"},
        "landing": {"type": "channel", "name": "VIXEN", "link": "https://xhamster.com/channels/vixen"},
        "channelModel": {"id": 42846, "channelURL": "https://xhamster.com/channels/vixen"},
    }
    assert _extract_xhamster_uploader(channel_model, None) == ("vixen", "channel")

    plain_user_model = {
        "author": {"name": "PlainUser", "pageURL": "https://xhamster.com/users/plainuser"},
        "landing": None,
    }
    assert _extract_xhamster_uploader(plain_user_model, None) == ("plainuser", "users")


@pytest.mark.asyncio
async def test_xhamster_subscribe_action_extraction_and_xapi_execution(monkeypatch):
    from app.parsers.target import subscribe_from_action
    from app.parsers.xhamster import extract_xhamster_subscribe_action

    creator_action = extract_xhamster_subscribe_action(
        {
            "infoComponent": {
                "subscribeButtonsProps": {
                    "subscribeButtonProps": {
                        "modelName": "pornstarModel",
                        "id": 227768,
                        "subscribed": False,
                    }
                }
            }
        }
    )
    assert creator_action is not None
    assert creator_action.site == "xhamster"
    assert creator_action.entity_model == "pornstarModel"
    assert creator_action.entity_id == 227768
    assert creator_action.already_subscribed is False

    channel_action = extract_xhamster_subscribe_action(
        {
            "layoutPage": {
                "channelLandingInfoProps": {
                    "subscribeButtonsProps": {
                        "subscribeButtonProps": {"modelName": "channelModel", "id": 42846, "subscribed": True}
                    }
                }
            }
        }
    )
    assert channel_action is not None
    assert channel_action.entity_model == "channelModel"
    assert channel_action.entity_id == 42846
    assert channel_action.already_subscribed is True

    user_action = extract_xhamster_subscribe_action({"profile": {"modelName": "userModel", "id": 78837540}})
    assert user_action is not None
    assert user_action.entity_model == "userModel"
    assert user_action.entity_id == 78837540

    # 1. Missing cookie cleanly returns login_required without hitting network
    ctx_anon = TaskHttpContext(cookie_str="", concurrency=1)
    res_anon = await subscribe_from_action(ctx_anon, creator_action)
    assert res_anon.ok is False and res_anon.status == "login_required"

    # 2. Already subscribed skips network call
    res_already = await subscribe_from_action(ctx_anon, channel_action)
    assert res_already.ok is True and res_already.status == "already_subscribed"

    # 3. Authenticated context invokes POST /x-api with entitySubscriptionModelSync
    ctx_auth = TaskHttpContext(cookie_str="UID=411169334; _id=tok", concurrency=1)
    captured: List[Dict[str, Any]] = []

    async def fake_post(url: str, **kwargs):
        captured.append({"url": url, **kwargs})
        body = json.dumps(
            [
                {
                    "name": "entitySubscriptionModelSync",
                    "extras": {"result": True, "message": "Subscribed!", "userId": 411169334, "userType": "user"},
                }
            ]
        )
        return PowFetchResult(body, url, 200, 0, False), None

    monkeypatch.setattr(ctx_auth, "fetch_with_pow", fake_post)
    res_ok = await subscribe_from_action(
        ctx_auth,
        creator_action,
        referer_url="https://xhamster.com/creators/sweetgirl69/newest",
        label="SweetGirl69",
    )
    assert res_ok.ok is True and res_ok.status == "subscribed"
    assert len(captured) == 1
    assert captured[0]["url"] == "https://xhamster.com/x-api"
    assert captured[0]["method"] == "POST"
    sent_payload = json.loads(captured[0]["data"])
    assert sent_payload[0]["name"] == "entitySubscriptionModelSync"
    assert sent_payload[0]["requestData"]["model"]["entityModel"] == "pornstarModel"
    assert sent_payload[0]["requestData"]["model"]["entityID"] == 227768
    assert sent_payload[0]["requestData"]["model"]["state"] is True


@pytest.mark.asyncio
async def test_task_manager_xhamster_auto_subscribe_in_target_and_url_modes(tmp_path: Path, monkeypatch):
    env = EnvSettings(data_dir=tmp_path, admin_password="secret", secret_key="k" * 40)
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    await repo.update_settings_patch(
        {
            "schedule_enabled": False,
            "ph_cookie": "ph_sess=only-ph",
            "xh_cookie": "UID=411169334; _id=only-xh",
            "page_delay_min": 0.0,
            "page_delay_max": 0.0,
        }
    )
    events = EventBus()
    tm = TaskManager(env, repo, events)

    master_url = "https://video-nss-h.xhcdn.com/tok/master.m3u8"
    list_html = (
        "<script>window.initials="
        + json.dumps(
            {
                "infoComponent": {
                    "pageTitle": "SweetGirl69 Porn Videos",
                    "subscribeButtonsProps": {
                        "subscribeButtonProps": {"modelName": "pornstarModel", "id": 227768, "subscribed": False}
                    },
                },
                "paginationComponent": {"lastPageNumber": 1},
                "trendingVideoSectionComponent": {
                    "videoListProps": {
                        "videoThumbProps": [
                            {
                                "id": 1,
                                "title": "Sweet Clip",
                                "pageURL": "https://xhamster.com/videos/sweet-clip-xhAAA11",
                                "duration": 120,
                                "created": 1767225600,
                            }
                        ]
                    }
                },
            }
        )
        + ";</script>"
    )
    video_html = (
        "<script>window.initials="
        + json.dumps(
            {
                "videoModel": {
                    "id": 1,
                    "idHashSlug": "xhAAA11",
                    "title": "Sweet Clip",
                    "duration": 120,
                    "created": 1767225600,
                    "author": {"name": "SweetHot69", "pageURL": "https://xhamster.com/users/sweethot69"},
                    "landing": {
                        "type": "person",
                        "name": "SweetGirl69",
                        "logo": "https://ic-ut-nss.xhcdn.com/a/sweet/avatar_80.jpg",
                        "link": "https://xhamster.com/creators/sweetgirl69",
                    },
                },
                "subscriptionComponent": {
                    "subscribeButtonsProps": {
                        "subscribeButtonProps": {"modelName": "pornstarModel", "id": 227768, "subscribed": False}
                    }
                },
                "xplayerSettings": {
                    "sources": {"hls": {"h264": {"url": _encode_xhamster_hex(master_url, 2, 1234)}}}
                },
            }
        )
        + ";</script>"
    )
    master_m3u8 = (
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,CODECS="mp4a.40.2,avc1.4d4015"\n'
        "1080p.h264.mp4.m3u8\n"
    )

    subscribe_calls: List[Dict[str, Any]] = []

    async def fake_fetch(self_ctx: TaskHttpContext, url: str, **kwargs):
        if url == "https://xhamster.com/x-api":
            subscribe_calls.append({"cookie": self_ctx.cookie_string(), "data": json.loads(kwargs["data"])})
            body = json.dumps(
                [
                    {
                        "name": "entitySubscriptionModelSync",
                        "extras": {"result": True, "message": "Subscribed!", "userId": 411169334, "userType": "user"},
                    }
                ]
            )
            return PowFetchResult(body, url, 200, 0, False), None
        if "/creators/sweetgirl69/newest" in url:
            return PowFetchResult(list_html, url, 200, 0, False), None
        if "/videos/sweet-clip-xhAAA11" in url:
            return PowFetchResult(video_html, url, 200, 0, False), None
        if url == master_url:
            return PowFetchResult(master_m3u8, url, 200, 0, False), None
        return PowFetchResult("", url, 404, 0, False), None

    monkeypatch.setattr(TaskHttpContext, "fetch_with_pow", fake_fetch)

    try:
        # 1. manual_target mode with subscribe_creator=True
        j1 = await repo.create_job(
            "manual_target",
            "xh sub target",
            {
                "site": "xhamster",
                "usernames": ["sweetgirl69"],
                "target_type": "creator",
                "export_only": True,
                "subscribe_creator": True,
            },
        )
        await tm._run_job(j1["id"])
        assert len(subscribe_calls) == 1
        assert "_id=only-xh" in subscribe_calls[0]["cookie"]
        assert "ph_sess" not in subscribe_calls[0]["cookie"]
        assert subscribe_calls[0]["data"][0]["requestData"]["model"]["entityID"] == 227768

        # 2. manual_urls mode with subscribe_creator=True & add_to_auto_track=True
        subscribe_calls.clear()
        j2 = await repo.create_job(
            "manual_urls",
            "xh sub urls",
            {
                "urls": ["https://xhamster.com/videos/sweet-clip-xhAAA11"],
                "export_only": True,
                "subscribe_creator": True,
                "add_to_auto_track": True,
            },
        )
        await tm._run_job(j2["id"])
        assert len(subscribe_calls) == 1
        assert "_id=only-xh" in subscribe_calls[0]["cookie"]
        assert subscribe_calls[0]["data"][0]["requestData"]["model"]["entityModel"] == "pornstarModel"
        assert subscribe_calls[0]["data"][0]["requestData"]["model"]["entityID"] == 227768
        targets = await repo.list_targets()
        matched = next(
            (
                t
                for t in targets
                if t["site"] == "xhamster" and t["username"] == "sweetgirl69" and t["target_type"] == "creator"
            ),
            None,
        )
        assert matched is not None
        assert matched["display_name"] == "SweetGirl69"
        assert matched["avatar_url"] == "https://ic-ut-nss.xhcdn.com/a/sweet/avatar_80.jpg"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_xhamster_subscriptions_detailed_attaches_bound_user_alias(monkeypatch):
    from app.parsers.xhamster import fetch_xhamster_subscriptions_detailed

    ctx = TaskHttpContext(concurrency=1)
    pornstars_html = (
        "<script>window.initials="
        + json.dumps(
            {
                "paginationProps": {"lastPageNumber": 1},
                "layoutPage": {
                    "pornstars": [
                        {
                            "name": "Fragile Tempt",
                            "pageURL": "https://xhamster.com/creators/fragile-tempt",
                            "imageUserAvatarUrl": "https://ic-ut-nss.xhcdn.com/a/xxx/webp/078/837/540/avatar_150.jpg",
                            "subscriptionModel": {"subscribed": True},
                        }
                    ]
                },
            }
        )
        + ";</script>"
    )
    channels_html = "<script>window.initials={\"layoutPage\":{\"channels\":[]}};</script>"
    users_html = (
        "<script>window.initials="
        + json.dumps(
            {
                "subscriptionsUsersPaging": {"maxPages": 1},
                "subscriptionsUsersCollection": [
                    {
                        "id": 78837540,
                        "name": "webtolove",
                        "pageURL": "https://xhamster.com/users/webtolove",
                        "thumbURL": "https://ic-ut-nss.xhcdn.com/a/zzz/webp/078/837/540/avatar_80.jpg",
                    },
                    {
                        "id": 99900011,
                        "name": "SoloUser",
                        "pageURL": "https://xhamster.com/users/solouser",
                        "thumbURL": "https://ic-ut-nss.xhcdn.com/a/aaa/webp/099/900/011/avatar_80.jpg",
                    },
                ],
            }
        )
        + ";</script>"
    )

    async def fake_fetch(url: str, **kwargs):
        if url.endswith("/my/subscriptions/pornstars"):
            return PowFetchResult(pornstars_html, url, 200, 0, False), None
        if url.endswith("/my/subscriptions/channels"):
            return PowFetchResult(channels_html, url, 200, 0, False), None
        if url.endswith("/my/subscriptions/users"):
            return PowFetchResult(users_html, url, 200, 0, False), None
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_fetch)
    items = await fetch_xhamster_subscriptions_detailed(ctx)
    assert len(items) == 2
    assert items[0].target_type == "creator"
    assert items[0].username == "fragile-tempt"
    assert items[0].display_name == "Fragile Tempt"
    assert items[0].bound_username == "webtolove"
    assert items[0].bound_display_name == "webtolove"
    assert items[1].target_type == "users"
    assert items[1].username == "solouser"
    assert items[1].bound_username == ""


@pytest.mark.asyncio
async def test_xhamster_resolve_target_upgrades_bound_user_and_user_scan_unions_creator(monkeypatch):
    from app.parsers.xhamster import resolve_xhamster_target

    ctx = TaskHttpContext(concurrency=1)
    user_profile_html = (
        "<script>window.initials="
        + json.dumps(
            {
                "pornstarSectionComponent": {
                    "pornstar": {
                        "id": 405427,
                        "name": "Fragile Tempt",
                        "type": "creator",
                        "pageURL": "https://xhamster.com/creators/fragile-tempt",
                    }
                }
            }
        )
        + ";</script>"
    )
    user_videos_html = (
        "<script>window.initials="
        + json.dumps(
            {
                "maxVideoPages": 1,
                "videoListComponent": {
                    "videoThumbProps": [
                        {"id": 1, "title": "Own Clip", "pageURL": "https://xhamster.com/videos/own-xhAAA11", "duration": 100, "created": 1767225600},
                    ]
                },
            }
        )
        + ";</script>"
    )
    creator_videos_html = (
        "<script>window.initials="
        + json.dumps(
            {
                "paginationComponent": {"lastPageNumber": 1},
                "trendingVideoSectionComponent": {
                    "videoListProps": {
                        "videoThumbProps": [
                            {"id": 1, "title": "Own Clip", "pageURL": "https://xhamster.com/videos/own-xhAAA11", "duration": 100, "created": 1767225600},
                            {"id": 2, "title": "Collab Clip", "pageURL": "https://xhamster.com/videos/collab-xhBBB22", "duration": 200, "created": 1767225600},
                        ]
                    }
                },
            }
        )
        + ";</script>"
    )
    plain_user_html = "<script>window.initials={\"profile\":{\"modelName\":\"userModel\",\"id\":123}};</script>"

    async def fake_fetch(url: str, **kwargs):
        if url.endswith("/users/webtolove/videos"):
            return PowFetchResult(user_videos_html, url, 200, 0, False), None
        if url.endswith("/users/webtolove"):
            return PowFetchResult(user_profile_html, url, 200, 0, False), None
        if url.endswith("/creators/fragile-tempt/newest"):
            return PowFetchResult(creator_videos_html, url, 200, 0, False), None
        if url.endswith("/users/plainuser/videos"):
            return PowFetchResult(user_videos_html, url, 200, 0, False), None
        if url.endswith("/users/plainuser"):
            return PowFetchResult(plain_user_html, url, 200, 0, False), None
        return PowFetchResult("", url, 404, 0, False), None

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_fetch)

    # 1. Auto-resolving a bound user upgrades to the linked creator target
    assert await resolve_xhamster_target(ctx, "webtolove") == ("creator", "fragile-tempt")
    assert await resolve_xhamster_target(ctx, "plainuser") == ("users", "plainuser")

    # 2. Natural scan of an existing users target unions linked creator videos
    full = await scan_xhamster_list_pages(
        ctx,
        "https://xhamster.com/users/webtolove/videos",
        target_type="users",
        profile_username="webtolove",
        delay_min=0,
        delay_max=0,
    )
    assert [v.viewkey for v in full] == ["xhAAA11", "xhBBB22"]

    # 3. Explicit pages_spec stays strictly scoped to the user page
    scoped = await scan_xhamster_list_pages(
        ctx,
        "https://xhamster.com/users/webtolove/videos",
        target_type="users",
        profile_username="webtolove",
        pages_spec="1",
        delay_min=0,
        delay_max=0,
    )
    assert [v.viewkey for v in scoped] == ["xhAAA11"]


def test_xhamster_profile_meta_extracts_channel_logo_and_creator_pornstar_top():
    from app.parsers.xhamster import _extract_profile_meta_from_initials

    av_ch, dn_ch = _extract_profile_meta_from_initials(
        {
            "layoutPage": {
                "channelLandingInfoProps": {
                    "sponsorChannel": {
                        "channelName": "VIXEN",
                        "siteLogoURL": "https://thumb-v-nss.xhcdn.com/site/000/007/179/logo.jpg",
                    }
                }
            }
        },
        target_type="channel",
    )
    assert dn_ch == "VIXEN"
    assert av_ch == "https://thumb-v-nss.xhcdn.com/site/000/007/179/logo.jpg"

    av_cr, dn_cr = _extract_profile_meta_from_initials(
        {
            "infoComponent": {
                "pornstarTop": {
                    "name": "SweetGirl69",
                    "thumbUrl": "https://ic-tt-nss.xhcdn.com/a/xxx/avatar1.jpg",
                },
                "displayUserModel": {
                    "name": "SweetHot69",
                    "thumbURL": "https://ic-ut-nss.xhcdn.com/a/yyy/avatar_150.jpg",
                },
            }
        },
        target_type="creator",
    )
    assert dn_cr == "SweetGirl69"
    assert av_cr == "https://ic-tt-nss.xhcdn.com/a/xxx/avatar1.jpg"

    from app.parsers.xhamster import parse_xhamster_target_from_url

    assert parse_xhamster_target_from_url("https://xhamster.com/users/profiles/nativepea222") == ("users", "nativepea222")
    assert parse_xhamster_target_from_url("https://xhamster.com/users/profiles") is None
