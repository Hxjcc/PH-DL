"""Tests for video published date/duration extraction, dated remote upload, and cloud rename backfill."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, List

import pytest

from app.config import EnvSettings, RuntimeSettings, SelectorSettings
from app.db.database import Database
from app.db.repo import Repository
from app.engine.events import EventBus
from app.engine.task_manager import TaskManager
from app.parsers.http_session import PowFetchResult, TaskHttpContext
from app.parsers.list_scanner import ListedVideo, extract_video_cards_from_list_page
from app.parsers.video import extract_detail_published_and_duration, parse_duration_seconds
from app.services import uploader
from app.services.filename import (
    build_dated_remote_path,
    extract_date_from_cdn_url,
    format_remote_video_filename,
    has_date_prefix,
    normalize_published_date,
)

VALID_MP4 = (
    (16).to_bytes(4, "big") + b"ftypisom\x00\x00\x02\x00"
    + (12).to_bytes(4, "big") + b"moov\x00\x00\x00\x00"
    + (16).to_bytes(4, "big") + b"mdat" + b"01234567"
)


def test_date_normalization_and_remote_filename_helpers():
    assert normalize_published_date("2026-10-05T10:35:49+00:00") == "2026-10-05"
    assert normalize_published_date("2026-10-05") == "2026-10-05"
    assert normalize_published_date("10/05/2026") == "2026-10-05"
    assert normalize_published_date("invalid") == ""
    assert normalize_published_date("2026-02-31") == ""

    assert extract_date_from_cdn_url(
        "https://ew.phncdn.com/c6541/videos/202611/18/69678175/180P_225K_69678175.webm"
    ) == "2026-11-18"
    assert extract_date_from_cdn_url("https://example.com/no-date.jpg") == ""

    assert not has_date_prefix("Clip Title [6ac37ac96f248].mp4")
    assert has_date_prefix("2026-10-05_Clip Title [6ac37ac96f248].mp4")
    assert has_date_prefix("pikpak:Pornhub/alice/2026-10-05_Clip Title [6ac37ac96f248].mp4")
    assert has_date_prefix("[2026-10-05] Clip Title [6ac37ac96f248].mp4")

    assert (
        format_remote_video_filename("Clip Title [vk1].mp4", "2026-10-05T10:35:49+00:00")
        == "2026-10-05_Clip Title [vk1].mp4"
    )
    # Idempotent: already prefixed filename is never double-prefixed
    assert (
        format_remote_video_filename("2026-10-05_Clip Title [vk1].mp4", "2026-10-05")
        == "2026-10-05_Clip Title [vk1].mp4"
    )
    # Long UTF-8 title stays within byte cap while keeping date prefix, [viewkey], and extension
    long_name = ("超长标题" * 25) + " [vk_long].mp4"
    capped = format_remote_video_filename(long_name, "2026-10-05", max_bytes=120)
    assert capped.startswith("2026-10-05_")
    assert capped.endswith(" [vk_long].mp4")
    assert len(capped.encode("utf-8")) <= 120

    assert (
        build_dated_remote_path("pikpak:Pornhub/alice/Clip [vk1].mp4", "2026-10-05")
        == "pikpak:Pornhub/alice/2026-10-05_Clip [vk1].mp4"
    )
    assert (
        build_dated_remote_path("pikpak:Pornhub/alice/2026-10-05_Clip [vk1].mp4", "2026-10-05")
        == "pikpak:Pornhub/alice/2026-10-05_Clip [vk1].mp4"
    )


def test_list_card_and_detail_html_extract_date_and_duration():
    assert parse_duration_seconds("12:39") == 759
    assert parse_duration_seconds("1:05:20") == 3920
    assert parse_duration_seconds("PT00H20M15S") == 1215
    assert parse_duration_seconds("PT1H02M05S") == 3725
    assert parse_duration_seconds("900") == 900
    assert parse_duration_seconds("") == 0

    list_html = """
    <ul id="mostRecentVideosSection" class="videos">
      <li class="pcVideoListItem videoBox" data-video-vkey="vkMedia1">
        <a href="/view_video.php?viewkey=vkMedia1" title="Mediabook Priority">
          <img
            src="https://pix-cdn77.phncdn.com/c6531/videos/202611/25/999/thumb.jpg"
            data-mediabook="https://ew.phncdn.com/c6541/videos/202610/05/64738365/180P_225K.webm"
          />
          <var class="duration">20:15</var>
        </a>
      </li>
      <li class="pcVideoListItem videoBox" data-video-vkey="vkImg2">
        <a href="/view_video.php?viewkey=vkImg2" title="Image Fallback">
          <img src="https://ei.phncdn.com/videos/202311/05/442477291/original/0.jpg" />
          <var class="duration">1:02:03</var>
        </a>
      </li>
    </ul>
    """
    cards = extract_video_cards_from_list_page(list_html, SelectorSettings())
    assert cards == [
        ("vkMedia1", "https://www.pornhub.com/view_video.php?viewkey=vkMedia1", "Mediabook Priority", "2026-10-05", 1215),
        ("vkImg2", "https://www.pornhub.com/view_video.php?viewkey=vkImg2", "Image Fallback", "2023-11-05", 3723),
    ]

    detail_html_ld = """
    <html><head>
      <script type="application/ld+json">
        {"@type":"VideoObject","uploadDate":"2026-10-05T10:35:49+00:00","duration":"PT00H20M15S"}
      </script>
    </head><body></body></html>
    """
    assert extract_detail_published_and_duration(detail_html_ld) == ("2026-10-05", 1215)

    detail_html_fallback = '<span class="trustSignalsDate">Submitted: 11/18/2026</span>'
    assert extract_detail_published_and_duration(detail_html_fallback, {"video_duration": "633"}) == ("2026-11-18", 633)


async def test_upload_file_batch_uses_copyto_for_dated_remote_name_and_rename_remote_file(tmp_path: Path, monkeypatch):
    dl_dir = tmp_path / "alice"
    dl_dir.mkdir()
    local_file = dl_dir / "Clip One [vk1].mp4"
    local_file.write_bytes(VALID_MP4)
    digest = hashlib.sha256(VALID_MP4).hexdigest()

    recorded_cmds: List[List[str]] = []

    async def fake_run_cmd(cmd: List[str], **kwargs):
        recorded_cmds.append(cmd)
        action = cmd[1]
        if action == "listremotes":
            return 0, "pikpak: pikpak\n", ""
        if action == "mkdir":
            return 0, "", ""
        if action == "copyto":
            assert cmd[2] == str(local_file)
            assert cmd[3] == "pikpak:Pornhub/alice/2026-10-05_Clip One [vk1].mp4"
            return 0, "", ""
        if action == "lsjson":
            target = next(arg for arg in cmd[2:] if arg.startswith("pikpak:"))
            if target == "pikpak:Pornhub/alice/2026-10-05_Clip One [vk1].mp4":
                return 0, json.dumps({"IsDir": False, "Size": len(VALID_MP4), "Hashes": {"sha256": digest}}), ""
            return 3, "", "not found"
        if action == "moveto":
            return 0, "", ""
        return 1, "", f"unexpected {cmd}"

    monkeypatch.setattr(uploader, "_run_cmd", fake_run_cmd)
    settings = RuntimeSettings(rclone_remote="pikpak", rclone_remote_base_dir="Pornhub", min_file_size_bytes=10)

    res = await uploader.upload_file_batch(
        dl_dir,
        "alice",
        {"vk1": local_file},
        settings,
        remote_names_by_viewkey={"vk1": "2026-10-05_Clip One [vk1].mp4"},
    )
    assert res.uploaded_viewkeys == ["vk1"]
    assert res.remote_paths == {"vk1": "pikpak:Pornhub/alice/2026-10-05_Clip One [vk1].mp4"}
    assert any(cmd[1] == "copyto" for cmd in recorded_cmds)

    ok, err, already = await uploader.rename_remote_file(
        "pikpak:Pornhub/alice/Old [vk2].mp4",
        "pikpak:Pornhub/alice/2026-10-05_Old [vk2].mp4",
        settings,
    )
    assert (ok, err, already) == (True, "", False)


async def test_remote_rename_session_uses_rcd_and_handles_idempotent_stat():
    settings = RuntimeSettings(rclone_remote="gd", rclone_remote_base_dir="Pornhub", min_file_size_bytes=10)
    session = uploader.RemoteRenameSession(settings)
    session.using_rcd = True
    session._session = object()  # type: ignore[assignment]

    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_rc_post(endpoint: str, payload: dict[str, Any]):
        calls.append((endpoint, payload))
        if endpoint == "operations/movefile" and payload["srcRemote"].endswith("ok.mp4"):
            return 200, {}
        if endpoint == "operations/movefile":
            return 500, {"error": "object not found"}
        if endpoint == "operations/stat" and payload["remote"].endswith("2026-10-05_already.mp4"):
            return 200, {"item": {"IsDir": False, "Size": 1024}}
        if endpoint == "operations/stat":
            return 200, {"item": None}
        return 500, {"error": "unexpected"}

    session._rc_post = fake_rc_post  # type: ignore[method-assign]

    ok1, err1, already1 = await session.rename("gd:Pornhub/alice/ok.mp4", "gd:Pornhub/alice/2026-10-05_ok.mp4")
    assert (ok1, err1, already1) == (True, "", False)
    assert calls[0] == (
        "operations/movefile",
        {
            "srcFs": "gd:",
            "srcRemote": "Pornhub/alice/ok.mp4",
            "dstFs": "gd:",
            "dstRemote": "Pornhub/alice/2026-10-05_ok.mp4",
        },
    )

    ok2, err2, already2 = await session.rename(
        "gd:Pornhub/alice/already.mp4",
        "gd:Pornhub/alice/2026-10-05_already.mp4",
    )
    assert ok2 is True and already2 is True and "已存在" in err2


async def test_rename_remote_dates_job_backfills_from_list_and_detail_then_renames(tmp_path: Path, monkeypatch):
    env = EnvSettings(data_dir=tmp_path)
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    try:
        repo = Repository(db)
        await repo.update_settings_patch({"rclone_remote": "pikpak", "rclone_remote_base_dir": "Pornhub", "ph_cookie": "ok=1"})
        await repo.upsert_target("alice", "model", display_name="Alice")

        # Uploaded video from monitored creator (missing published_at -> filled by list scan)
        await repo.upsert_video(
            "vk_list",
            title="Alice Video",
            uploader="alice",
            target_type="model",
            status="uploaded",
            remote_path="pikpak:Pornhub/alice/Alice Video [vk_list].mp4",
            file_size=50000,
        )
        # Baseline video of same creator also gets free date+duration backfill during list scan
        await repo.upsert_video(
            "vk_base",
            title="Alice Baseline",
            uploader="alice",
            target_type="model",
            status="baseline",
        )
        # Uploaded standalone URL video (missing published_at -> filled by detail fallback)
        await repo.upsert_video(
            "vk_url",
            title="Standalone Clip",
            uploader="url_batch",
            target_type="model",
            status="uploaded",
            remote_path="pikpak:Pornhub/url_batch/Standalone Clip [vk_url].mp4",
            file_size=40000,
        )
        # Already dated remote video -> must NOT be counted or touched
        await repo.upsert_video(
            "vk_done",
            title="Already Dated",
            uploader="alice",
            target_type="model",
            status="uploaded",
            remote_path="pikpak:Pornhub/alice/2026-09-01_Already Dated [vk_done].mp4",
            published_at="2026-09-01",
            file_size=30000,
        )

        stats = await repo.get_video_stats()
        assert stats["pending_remote_date_rename"] == 2

        events = EventBus()
        tm = TaskManager(env, repo, events)

        async def fake_scan_list_pages(ctx, initial_url, selectors, **kwargs):
            on_page = kwargs.get("on_page")
            items = [
                ListedVideo("vk_list", "https://www.pornhub.com/view_video.php?viewkey=vk_list", "Alice Video", 1, 1, 2, "2026-10-05", 759),
                ListedVideo("vk_base", "https://www.pornhub.com/view_video.php?viewkey=vk_base", "Alice Baseline", 1, 2, 2, "2026-08-20", 420),
            ]
            if on_page:
                await on_page(items)
            return items

        async def fake_fetch_pub_meta(ctx, page_url, **kwargs):
            if "vk_url" in page_url:
                return "2026-07-14", 600, None
            return "", 0, "404"

        renamed_calls: list[tuple[str, str]] = []

        async def fake_rename_remote(old_p, new_p, settings, **kwargs):
            renamed_calls.append((old_p, new_p))
            return True, "", False

        import app.engine.task_manager as tm_mod

        monkeypatch.setattr(tm_mod, "scan_list_pages", fake_scan_list_pages)
        monkeypatch.setattr(tm_mod, "fetch_video_published_meta", fake_fetch_pub_meta)
        monkeypatch.setattr(tm_mod, "rename_remote_file", fake_rename_remote)

        job = await repo.create_job("rename_remote_dates", "网盘补全日期", {})
        settings = await repo.get_settings()
        ctx = TaskHttpContext()
        await tm._exec_rename_remote_dates(int(job["id"]), {}, settings, ctx)

        assert set(renamed_calls) == {
            (
                "pikpak:Pornhub/alice/Alice Video [vk_list].mp4",
                "pikpak:Pornhub/alice/2026-10-05_Alice Video [vk_list].mp4",
            ),
            (
                "pikpak:Pornhub/url_batch/Standalone Clip [vk_url].mp4",
                "pikpak:Pornhub/url_batch/2026-07-14_Standalone Clip [vk_url].mp4",
            ),
        }

        v_list = await repo.get_video("vk_list")
        v_base = await repo.get_video("vk_base")
        v_url = await repo.get_video("vk_url")
        assert v_list and v_list["published_at"] == "2026-10-05" and v_list["duration_seconds"] == 759
        assert v_list["remote_path"] == "pikpak:Pornhub/alice/2026-10-05_Alice Video [vk_list].mp4"
        assert v_base and v_base["published_at"] == "2026-08-20" and v_base["duration_seconds"] == 420
        assert v_url and v_url["published_at"] == "2026-07-14" and v_url["duration_seconds"] == 600
        assert v_url["remote_path"] == "pikpak:Pornhub/url_batch/2026-07-14_Standalone Clip [vk_url].mp4"

        stats_after = await repo.get_video_stats()
        assert stats_after["pending_remote_date_rename"] == 0

        # Verify published sort order (newest published_at first)
        ordered, total = await repo.query_videos(sort_by="published")
        assert total == 4
        assert [r["viewkey"] for r in ordered] == ["vk_list", "vk_done", "vk_base", "vk_url"]
    finally:
        await db.close()
