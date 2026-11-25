"""Tests for video published date/duration extraction, sorting, and dated remote uploads."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import List

from app.config import EnvSettings, RuntimeSettings, SelectorSettings
from app.db.database import Database
from app.db.repo import Repository
from app.parsers.list_scanner import extract_video_cards_from_list_page
from app.parsers.video import extract_detail_published_and_duration, parse_duration_seconds
from app.services import uploader
from app.services.filename import (
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
    assert (
        format_remote_video_filename("2026-10-05_Clip Title [vk1].mp4", "2026-10-05")
        == "2026-10-05_Clip Title [vk1].mp4"
    )
    long_name = ("超长标题" * 25) + " [vk_long].mp4"
    capped = format_remote_video_filename(long_name, "2026-10-05", max_bytes=120)
    assert capped.startswith("2026-10-05_")
    assert capped.endswith(" [vk_long].mp4")
    assert len(capped.encode("utf-8")) <= 120


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


async def test_upload_file_batch_uses_copyto_for_dated_remote_name(tmp_path: Path, monkeypatch):
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


async def test_query_videos_sorts_by_published_at_with_missing_dates_last(tmp_path: Path):
    env = EnvSettings(data_dir=tmp_path)
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    try:
        repo = Repository(db)
        await repo.upsert_video("vkOld", title="Old", status="uploaded", published_at="2024-01-10", duration_seconds=600)
        await repo.upsert_video("vkNew", title="New", status="baseline", published_at="2026-11-20", duration_seconds=1215)
        await repo.upsert_video("vkNone", title="Undated", status="baseline")

        ordered, total = await repo.query_videos(sort_by="published")
        assert total == 3
        assert [r["viewkey"] for r in ordered] == ["vkNew", "vkOld", "vkNone"]
        assert ordered[0]["duration_seconds"] == 1215
    finally:
        await db.close()
