"""Comprehensive tests for xHamster parsing, decryption, HLS downloading, DB v9 migration, and multi-site jobs."""

from __future__ import annotations

import asyncio
import json
import sqlite3
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

    # 1. Default h264 picks 2160p H.264 HLS and exposes 720p MP4 for playback
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

    # 2. AV1 preference picks 2160p AV1 HLS
    d_av1 = await fetch_single_video_detail(
        ctx, vid_url, SelectorSettings(), xh_preferred_codec="av1"
    )
    assert d_av1.error is None
    assert d_av1.quality == "2160p"
    assert d_av1.download_url == "https://video-nss.xhcdn.com/tok/2160p.av1.mp4.m3u8"

    # 3. Requesting 720p with H.264 uses direct progressive MP4 instead of HLS
    d_720 = await fetch_single_video_detail(
        ctx, vid_url, SelectorSettings(), preferred_qualities={"720"}, xh_preferred_codec="h264"
    )
    assert d_720.error is None
    assert d_720.quality == "720p"
    assert d_720.video_format == "mp4"
    assert d_720.download_url == mp4_720_url


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
                            "name": "Fragile Tempt",
                            "pageURL": "https://xhamster.com/creators/fragile-tempt",
                            "imageUserAvatarUrl": "https://ic-nss.xhcdn.com/ft.jpg",
                            "subscriptionModel": {"subscribed": True},
                        }
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
    feed_html = (
        "<script>window.initials="
        + json.dumps(
            {
                "seenVideoListComponent": {
                    "videoThumbProps": [
                        {
                            "landing": {
                                "type": "user",
                                "name": "WebToLove",
                                "link": "https://xhamster.com/users/webtolove",
                                "logo": "https://ic-nss.xhcdn.com/wtl.jpg",
                            }
                        }
                    ]
                }
            }
        )
        + ";</script>"
    )

    async def fake_fetch(url: str, **kwargs):
        if url.endswith("/my/subscriptions/pornstars"):
            return PowFetchResult(pornstars_html, url, 200, 0, False), None
        if url.endswith("/my/subscriptions/channels"):
            return PowFetchResult(channels_html, url, 200, 0, False), None
        if url.endswith("/my/subscriptions"):
            return PowFetchResult(feed_html, url, 200, 0, False), None
        return PowFetchResult("", url, 404, 0, False), None

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_fetch)
    subs = await fetch_xhamster_subscriptions(ctx)
    assert subs == [
        ("creator", "fragile-tempt", "Fragile Tempt", "https://ic-nss.xhcdn.com/ft.jpg"),
        ("channel", "sub-ch", "Subscribed Channel", "https://ic-nss.xhcdn.com/ch.jpg"),
        ("users", "webtolove", "WebToLove", "https://ic-nss.xhcdn.com/wtl.jpg"),
    ]


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


async def test_download_hls_to_mp4_resumes_completed_segments_and_remuxes(tmp_path: Path, monkeypatch):
    playlist_url = "https://video-nss.xhcdn.com/tok/2160p.av1.mp4.m3u8"
    playlist_body = (
        "#EXTM3U\n#EXT-X-VERSION:6\n"
        '#EXT-X-MAP:URI="init.mp4"\n'
        "#EXTINF:4.000,\nseg1.m4s\n"
        "#EXTINF:4.000,\nseg2.m4s\n"
    )
    fetched_urls: List[str] = []

    class FakeResp:
        def __init__(self, url: str, data: bytes, status: int = 200):
            self.url = url
            self._data = data
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def raise_for_status(self):
            if self.status >= 400:
                raise RuntimeError(f"HTTP {self.status}")

        async def text(self, errors: str = "replace"):
            return self._data.decode("utf-8", errors=errors)

        async def read(self):
            return self._data

    class FakeSession:
        def get(self, url: str, **kwargs):
            fetched_urls.append(url)
            if url.endswith(".m3u8"):
                return FakeResp(url, playlist_body.encode())
            if url.endswith("init.mp4"):
                return FakeResp(url, b"INITDATA")
            if url.endswith("seg1.m4s"):
                return FakeResp(url, b"SEG1DATA")
            if url.endswith("seg2.m4s"):
                return FakeResp(url, b"SEG2DATA")
            return FakeResp(url, b"", status=404)

    dl_dir = tmp_path / "downloads" / "xHamster" / "creator1"
    tmp_dir = tmp_path / "temp" / "xHamster" / "creator1"
    hls_dir = tmp_dir / "Demo Clip [xhOwQIZ].hls"
    hls_dir.mkdir(parents=True)
    # Pre-seed manifest + seg_00000 to verify segment-level resume skips already completed segments!
    (hls_dir / "manifest.json").write_text(
        json.dumps({"viewkey": "xhOwQIZ", "quality": "2160", "segment_count": 2, "is_fmp4": True}),
        encoding="utf-8",
    )
    (hls_dir / "seg_00000.bin").write_bytes(b"SEG1DATA")

    monkeypatch.setattr(hls_mod.shutil, "which", lambda name: "/usr/local/bin/ffmpeg")
    remux_calls: List[List[str]] = []

    async def fake_run_ffmpeg(cmd: List[str], *, cancel_event: asyncio.Event, timeout: float = 600.0):
        remux_calls.append(cmd)
        out_path = Path(cmd[-1])
        out_path.write_bytes(mp4_payload())
        return 0, ""

    monkeypatch.setattr(hls_mod, "_run_ffmpeg", fake_run_ffmpeg)

    ctx = TaskHttpContext(concurrency=1)
    outcome = await hls_mod.download_hls_to_mp4(
        session=FakeSession(),
        parse_ctx=ctx,
        selectors=SelectorSettings(),
        viewkey="xhOwQIZ",
        title="Demo Clip",
        playlist_url=playlist_url,
        page_url="https://xhamster.com/videos/demo-clip-xhOwQIZ",
        referer_url="https://xhamster.com/videos/demo-clip-xhOwQIZ",
        quality="2160p",
        download_dir=dl_dir,
        temp_dir=tmp_dir,
        min_file_size_bytes=16,
    )

    assert outcome.success is True
    assert outcome.local_path is not None and outcome.local_path.is_file()
    assert not hls_dir.exists()  # cleaned up after remux
    assert "https://video-nss.xhcdn.com/tok/seg1.m4s" not in fetched_urls  # resumed!
    assert "https://video-nss.xhcdn.com/tok/init.mp4" in fetched_urls
    assert "https://video-nss.xhcdn.com/tok/seg2.m4s" in fetched_urls
    assert len(remux_calls) == 1
    assert "-movflags" in remux_calls[0] and "+faststart" in remux_calls[0]


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

    async def fake_check_xh_cookie(cookie_str: str, **kwargs):
        return True, "Cookie 有效（当前账号: OrderlyPaw690）", "OrderlyPaw690"

    async def fake_xh_subs(ctx, timeout=20.0):
        return [("creator", "fragile-tempt", "Fragile Tempt", "https://ic-nss.xhcdn.com/ft.jpg")]

    monkeypatch.setattr(settings_api, "check_xhamster_cookie_valid", fake_check_xh_cookie)
    monkeypatch.setattr(overview_api, "fetch_xhamster_subscriptions", fake_xh_subs)

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

        r_ref = client.post("/api/settings/xh-cookie/refresh")
        assert r_ref.status_code == 200
        body = r_ref.json()
        assert body["success"] is True
        assert body["logged_in_username"] == "NativePea222"

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
