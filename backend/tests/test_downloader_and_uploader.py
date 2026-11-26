"""Tests for range validation, content integrity, and verified uploads."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import aiohttp
import pytest

from app.config import RuntimeSettings, SelectorSettings
from app.parsers.http_session import TaskHttpContext
from app.parsers.video import VideoDetail
from app.services import downloader, uploader
from app.services.filename import is_valid_video_file, video_filename_base
from tests.media_fixtures import mp4_payload

VALID_MP4 = mp4_payload()


class _FakeContent:
    def __init__(self, chunks: List[bytes]):
        self._chunks = chunks

    def iter_chunked(self, _size: int):
        async def _gen():
            for c in self._chunks:
                yield c
        return _gen()


class _FakeResponse:
    def __init__(self, status: int, headers: Dict[str, str], chunks: List[bytes]):
        self.status = status
        self.headers = headers
        self.content = _FakeContent(chunks)

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=self.status,
                message="error",
                headers=None,
            )


class _FakeCtxMgr:
    def __init__(self, resp: _FakeResponse):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, responses: List[_FakeResponse]):
        self._responses = list(responses)
        self.recorded_calls: List[tuple[str, Dict[str, Any]]] = []

    def get(self, url: str, **kwargs):
        self.recorded_calls.append((url, kwargs))
        return _FakeCtxMgr(self._responses.pop(0))


def test_video_file_integrity_validation(tmp_path: Path):
    good = tmp_path / "good.mp4"
    good.write_bytes(VALID_MP4)
    assert is_valid_video_file(good, min_size_bytes=64) is True

    bad_magic = tmp_path / "bad.mp4"
    bad_magic.write_bytes(b"<html>not an mp4 file at all</html>" * 10)
    assert is_valid_video_file(bad_magic, min_size_bytes=64) is False

    # Critical regression: .mp4.tmp files MUST still run MP4 ftyp header check!
    good_tmp = tmp_path / "good.mp4.tmp"
    good_tmp.write_bytes(VALID_MP4)
    assert is_valid_video_file(good_tmp, min_size_bytes=64) is True

    bad_tmp = tmp_path / "bad.mp4.tmp"
    bad_tmp.write_bytes(b"<html>error page disguised as mp4 temp</html>" * 20)
    assert is_valid_video_file(bad_tmp, min_size_bytes=64) is False
    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes(VALID_MP4[:-32])
    assert not is_valid_video_file(truncated, min_size_bytes=64)


async def test_downloader_rejects_html_error_body_written_to_mp4_tmp(tmp_path: Path):
    dl_dir = tmp_path / "dl"
    tmp_dir = tmp_path / "tmp"
    html_error = b"<html><body>502 Bad Gateway</body></html>" * 20

    session = _FakeSession(
        [
            _FakeResponse(
                status=200,
                headers={"Content-Length": str(len(html_error))},
                chunks=[html_error],
            )
        ]
    )
    ctx = TaskHttpContext(cookie_str="sess=ok")
    outcome = await downloader.download_mp4_with_resume(
        session=session,  # type: ignore[arg-type]
        parse_ctx=ctx,
        selectors=SelectorSettings(),
        viewkey="vkhtml1",
        title="HTML Error Video",
        download_url="https://cdn.example/video.mp4",
        page_url="https://www.pornhub.com/view_video.php?viewkey=vkhtml1",
        referer_url="https://www.pornhub.com/view_video.php?viewkey=vkhtml1",
        quality="1080p",
        download_dir=dl_dir,
        temp_dir=tmp_dir,
        max_attempts=1,
        min_file_size_bytes=64,
    )
    assert outcome.success is False
    assert "校验" in outcome.error
    assert not (dl_dir / f"{video_filename_base('HTML Error Video', 'vkhtml1')}.mp4").exists()


async def test_downloader_sends_true_range_header_on_resume(tmp_path: Path):
    dl_dir = tmp_path / "dl"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(parents=True)

    title = "Resume Test"
    viewkey = "vkresume1"
    partial_path = tmp_dir / f"{video_filename_base(title, viewkey)}.mp4.tmp"
    first_part = VALID_MP4[:64]
    rest_part = VALID_MP4[64:]
    session = _FakeSession(
        [
            _FakeResponse(
                status=200,
                headers={"Content-Length": str(len(VALID_MP4)), "ETag": '"fixture-v1"'},
                chunks=[first_part],
            ),
            _FakeResponse(
                status=206,
                headers={"Content-Range": f"bytes 64-{len(VALID_MP4) - 1}/{len(VALID_MP4)}", "ETag": '"fixture-v1"'},
                chunks=[rest_part],
            )
        ]
    )
    ctx = TaskHttpContext(cookie_str="sess=ok")
    progress_ticks = []

    outcome = await downloader.download_mp4_with_resume(
        session=session,  # type: ignore[arg-type]
        parse_ctx=ctx,
        selectors=SelectorSettings(),
        viewkey=viewkey,
        title=title,
        download_url="https://cdn.example/video.mp4",
        page_url="https://www.pornhub.com/view_video.php?viewkey=vkresume1",
        referer_url="https://www.pornhub.com/view_video.php?viewkey=vkresume1",
        quality="1080p",
        download_dir=dl_dir,
        temp_dir=tmp_dir,
        cookie_str="sess=ok",
        max_attempts=2,
        retry_delay_base=0,
        min_file_size_bytes=64,
        on_progress=lambda d, t, s: progress_ticks.append((d, t, s)),
    )

    assert outcome.success is True
    assert outcome.local_path is not None
    assert outcome.local_path.read_bytes() == VALID_MP4
    assert not partial_path.exists()
    # Critical regression assertion: Range header MUST actually be passed per request!
    assert len(session.recorded_calls) == 2
    assert "Range" not in session.recorded_calls[0][1]["headers"]
    sent_headers = session.recorded_calls[1][1]["headers"]
    assert sent_headers["Range"] == "bytes=64-"
    assert sent_headers["If-Range"] == '"fixture-v1"'
    assert sent_headers["Cookie"] == "sess=ok"
    assert len(progress_ticks) >= 1


async def test_downloader_refreshes_expired_403_link_and_applies_new_referer(tmp_path: Path, monkeypatch):
    dl_dir = tmp_path / "dl"
    tmp_dir = tmp_path / "tmp"

    session = _FakeSession(
        [
            _FakeResponse(status=403, headers={}, chunks=[]),
            _FakeResponse(
                status=200,
                headers={"Content-Length": str(len(VALID_MP4))},
                chunks=[VALID_MP4],
            ),
        ]
    )
    ctx = TaskHttpContext(cookie_str="sess=ok")

    async def fake_fetch_single(*args, **kwargs):
        return VideoDetail(
            viewkey="vk403",
            title="Expired Link Video",
            page_url="https://www.pornhub.com/view_video.php?viewkey=vk403",
            referer_url="https://www.pornhub.com/refreshed-referer",
            download_url="https://cdn.example/fresh.mp4",
            quality="1080p",
        )

    monkeypatch.setattr(downloader, "fetch_single_video_detail", fake_fetch_single)

    outcome = await downloader.download_mp4_with_resume(
        session=session,  # type: ignore[arg-type]
        parse_ctx=ctx,
        selectors=SelectorSettings(),
        viewkey="vk403",
        title="Expired Link Video",
        download_url="https://cdn.example/expired.mp4",
        page_url="https://www.pornhub.com/view_video.php?viewkey=vk403",
        referer_url="https://www.pornhub.com/old-referer",
        quality="1080p",
        download_dir=dl_dir,
        temp_dir=tmp_dir,
        retry_delay_base=0.0,
        retry_delay_max=0.0,
        min_file_size_bytes=64,
    )

    assert outcome.success is True
    assert session.recorded_calls[0][0] == "https://cdn.example/expired.mp4"
    assert session.recorded_calls[1][0] == "https://cdn.example/fresh.mp4"
    assert session.recorded_calls[1][1]["headers"]["Referer"] == "https://www.pornhub.com/refreshed-referer"


async def test_uploader_copies_and_verifies_without_downloading_by_default(tmp_path: Path, monkeypatch):
    dl_dir = tmp_path / "alice"
    dl_dir.mkdir()
    f1 = dl_dir / "Video One [vk1].mp4"
    f2 = dl_dir / "Video Two [vk2].mp4"
    f3 = dl_dir / "Video Three [vk3].mp4"
    f4 = dl_dir / "Video Four [vk4].mp4"
    f5 = dl_dir / "Video Five [vk5].mp4"
    f6 = dl_dir / "Video Six [vk6].mp4"
    for f in (f1, f2, f3, f4, f5, f6):
        f.write_bytes(VALID_MP4)

    cmds_seen = []
    run_options = []

    async def fake_run_cmd(cmd: List[str], **kwargs):
        cmds_seen.append(cmd)
        run_options.append(kwargs)
        action = cmd[1]
        if action == "listremotes":
            return 0, "pikpak:              pikpak\ngdrive:              drive\n", ""
        if action == "mkdir":
            return 0, "", ""
        if action == "copy":
            return 0, "", ""
        if action == "lsjson":
            target = cmd[5]
            meta = {"Size": len(VALID_MP4), "IsDir": False}
            if "vk1" in target:
                meta["Hashes"] = {"md5": hashlib.md5(VALID_MP4).hexdigest()}
            elif "vk3" in target:
                meta.update(Name=f3.name, Size=-1)
            elif "vk4" in target:
                meta["Size"] = 12
            elif "vk5" in target:
                meta["Hashes"] = {"md5": hashlib.md5(mp4_payload(b"b")).hexdigest()}
            elif "vk6" in target:
                return 1, "", "object not found"
            return 0, json.dumps(meta), ""
        if action == "check":
            raise AssertionError("Default verification must not re-download files via rclone check --download")
        return 1, "", "not found"

    monkeypatch.setattr(uploader, "_run_cmd", fake_run_cmd)
    settings = RuntimeSettings(
        rclone_remote="pikpak",
        rclone_remote_base_dir="Pornhub",
        min_file_size_bytes=64,
    )

    res = await uploader.upload_file_batch(
        download_dir=dl_dir,
        uploader_folder="alice",
        files_by_viewkey={"vk1": f1, "vk2": f2, "vk3": f3, "vk4": f4, "vk5": f5, "vk6": f6},
        settings=settings,
    )

    # vk1 (matching md5) and vk2 (exact Size, empty hash like PikPak) both verify without --download.
    # vk3 (Size=-1), vk4 (Size mismatch), vk5 (md5 mismatch), vk6 (lsjson missing) all fail safely.
    assert set(res.uploaded_viewkeys) == {"vk1", "vk2"}
    assert set(res.failed_viewkeys) == {"vk3", "vk4", "vk5", "vk6"}
    assert set(res.verified_sources) == {"vk1", "vk2"}
    assert all(path.read_bytes() == VALID_MP4 for path in (f1, f2, f3, f4, f5, f6))

    mkdir_cmd = next(c for c in cmds_seen if c[1] == "mkdir")
    copy_cmd = next(c for c in cmds_seen if c[1] == "copy")
    assert "--transfers=1" not in mkdir_cmd
    assert "--size-only" in copy_cmd
    assert "--ignore-times" not in copy_cmd
    assert "--ignore-checksum" in copy_cmd
    assert "--ignore-size" not in copy_cmd
    assert "--pikpak-upload-concurrency=4" in copy_cmd
    copy_index = cmds_seen.index(copy_cmd)
    assert run_options[copy_index]["timeout"] == settings.rclone_timeout_seconds
    assert run_options[copy_index]["cancel_event"] is None

    # Non-PikPak remote (Google Drive) should receive GD flags and NOT PikPak flags
    gd_settings = RuntimeSettings(rclone_remote="gd", rclone_remote_base_dir="Pornhub")
    gd_opts = uploader.build_rclone_copy_opts(gd_settings, remote_type="drive")
    assert not any(o.startswith("--pikpak-") for o in gd_opts)
    assert "--drive-chunk-size=64Mi" in gd_opts
    assert "--drive-stop-on-upload-limit" in gd_opts

    gd_no_stop = RuntimeSettings(
        rclone_remote="gd",
        rclone_drive_chunk_size="128Mi",
        rclone_drive_stop_on_upload_limit=False,
    )
    gd_no_stop_opts = uploader.build_rclone_copy_opts(gd_no_stop, remote_type="drive")
    assert "--drive-chunk-size=128Mi" in gd_no_stop_opts
    assert "--drive-stop-on-upload-limit" not in gd_no_stop_opts
    assert not any(o.startswith("--drive-") for o in copy_cmd)

    # Safe extra flags support arbitrary future backends while blocking unsafe flags
    extra_parsed = uploader.parse_safe_rclone_extra_flags(
        "--s3-chunk-size=32Mi --onedrive-chunk-size 60M --config=/etc/shadow --password-command echo positional"
    )
    assert extra_parsed == ["--s3-chunk-size=32Mi", "--onedrive-chunk-size", "60M"]
    s3_settings = RuntimeSettings(rclone_remote="r2", rclone_extra_flags="--s3-chunk-size=32Mi --dry-run")
    s3_opts = uploader.build_rclone_copy_opts(s3_settings, remote_type="s3")
    assert "--s3-chunk-size=32Mi" in s3_opts
    assert "--dry-run" not in s3_opts

    remotes = await uploader.list_rclone_remotes(settings)
    assert remotes == [{"name": "pikpak", "type": "pikpak"}, {"name": "gdrive", "type": "drive"}]


async def test_uploader_strict_download_verify_opt_in(tmp_path: Path, monkeypatch):
    dl_dir = tmp_path / "alice"
    dl_dir.mkdir()
    f_ok = dl_dir / "Match [vkok].mp4"
    f_bad = dl_dir / "Mismatch [vkbad].mp4"
    f_ok.write_bytes(VALID_MP4)
    f_bad.write_bytes(VALID_MP4)

    check_calls = []

    async def fake_run_cmd(cmd: List[str], **kwargs):
        action = cmd[1]
        if action == "listremotes":
            return 0, "pikpak: pikpak\n", ""
        if action in {"mkdir", "copy"}:
            return 0, "", ""
        if action == "lsjson":
            return 0, json.dumps({"Size": len(VALID_MP4), "IsDir": False}), ""
        if action == "check":
            name = Path(cmd[cmd.index("--files-from-raw") + 1]).read_text(encoding="utf-8").strip()
            check_calls.append(name)
            assert "--download" in cmd
            return 0, name + "\n" if name == f_ok.name else "", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(uploader, "_run_cmd", fake_run_cmd)
    settings = RuntimeSettings(
        rclone_remote="pikpak",
        rclone_remote_base_dir="Pornhub",
        min_file_size_bytes=64,
        rclone_verify_download=True,
    )
    res = await uploader.upload_file_batch(
        download_dir=dl_dir,
        uploader_folder="alice",
        files_by_viewkey={"vkok": f_ok, "vkbad": f_bad},
        settings=settings,
    )
    assert res.uploaded_viewkeys == ["vkok"]
    assert res.failed_viewkeys == ["vkbad"]
    assert set(check_calls) == {f_ok.name, f_bad.name}


async def test_failed_copy_keeps_source_and_never_trusts_an_old_remote_object(tmp_path, monkeypatch):
    local = tmp_path / "original.mp4"
    local.write_bytes(VALID_MP4)

    async def fake_run_cmd(cmd, **kwargs):
        if cmd[1] == "listremotes":
            return 0, "pikpak: pikpak\n", ""
        if cmd[1] == "mkdir":
            return 0, "", ""
        if cmd[1] == "copy":
            return 1, "", "transfer failed"
        raise AssertionError("A failed copy must not enter the local deletion path")

    monkeypatch.setattr(uploader, "_run_cmd", fake_run_cmd)
    result = await uploader.upload_file_batch(tmp_path, "fixture", {"vk": local},
                                              RuntimeSettings(min_file_size_bytes=64))
    assert result.failed_viewkeys == ["vk"] and not result.verified_sources
    assert local.read_bytes() == VALID_MP4


async def test_run_cmd_timeout_terminates_subprocess():
    started = time.monotonic()
    rc, _, stderr = await uploader._run_cmd(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        timeout=0.2,
    )
    elapsed = time.monotonic() - started

    assert rc == 124
    assert "超时" in stderr
    assert elapsed < 5.0


async def test_run_cmd_cancel_event_terminates_subprocess():
    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        uploader._run_cmd(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=30.0,
            cancel_event=cancel_event,
        )
    )
    await asyncio.sleep(0.2)
    cancel_event.set()

    rc, _, stderr = await asyncio.wait_for(task, timeout=5.0)
    assert rc == 130
    assert "取消" in stderr


async def test_probe_download_size_reads_range_total_without_body(tmp_path: Path):
    session = _FakeSession(
        [
            _FakeResponse(
                status=206,
                headers={"Content-Range": "bytes 0-0/123456789"},
                chunks=[],
            )
        ]
    )
    size = await downloader.probe_download_size(
        session,  # type: ignore[arg-type]
        "https://cdn.example/video.mp4",
        referer_url="https://www.pornhub.com/view_video.php?viewkey=vk_size",
        cookie_str="sess=abc",
    )

    assert size == 123456789
    _, kwargs = session.recorded_calls[0]
    assert kwargs["headers"]["Range"] == "bytes=0-0"
    assert kwargs["headers"]["Cookie"] == "sess=abc"
    assert kwargs["headers"]["Referer"].endswith("viewkey=vk_size")


async def test_uploader_streams_json_progress_and_caches_remote_session(tmp_path: Path, monkeypatch):
    dl_dir = tmp_path / "alice"
    dl_dir.mkdir()
    f1 = dl_dir / "Clip One [vk1].mp4"
    f2 = dl_dir / "Clip Two [vk2].mp4"
    f1.write_bytes(VALID_MP4)
    f2.write_bytes(VALID_MP4)

    # First exercise real streaming _run_cmd(on_stderr_line=...) on a subprocess
    streamed_lines: List[str] = []
    rc, _, _ = await uploader._run_cmd(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; "
                "sys.stderr.write(json.dumps({'level':'notice','stats':{'bytes':50,'speed':100}}) + '\\n'); "
                "sys.stderr.flush()"
            ),
        ],
        timeout=5.0,
        on_stderr_line=streamed_lines.append,
    )
    assert rc == 0 and len(streamed_lines) == 1
    assert uploader.parse_rclone_json_line(streamed_lines[0])["stats"]["bytes"] == 50

    # Now verify upload_file_batch emits uploading/verifying progress and reuses UploadSessionCache
    cmd_actions: List[str] = []
    progress_events: List[tuple[str, int, int, float, str]] = []

    async def fake_run_cmd(cmd: List[str], **kwargs):
        action = cmd[1]
        cmd_actions.append(action)
        if action == "listremotes":
            return 0, "pikpak: pikpak\n", ""
        if action == "mkdir":
            return 0, "", ""
        if action == "copy":
            assert "--use-json-log" in cmd
            assert "--stats=300ms" in cmd
            assert "--log-level=NOTICE" in cmd
            assert "--stats-log-level=NOTICE" in cmd
            cb = kwargs.get("on_stderr_line")
            assert cb is not None
            list_file = Path(cmd[cmd.index("--files-from-raw") + 1])
            rel_name = list_file.read_text(encoding="utf-8").strip().splitlines()[0]
            cb(
                json.dumps(
                    {
                        "level": "notice",
                        "stats": {
                            "speed": 125_000_000,
                            "transferring": [
                                {
                                    "name": rel_name,
                                    "bytes": len(VALID_MP4) // 2,
                                    "size": len(VALID_MP4),
                                    "speed": 125_000_000,
                                }
                            ],
                        },
                    }
                )
            )
            return 0, "", ""
        if action == "lsjson":
            return 0, json.dumps({"Size": len(VALID_MP4), "IsDir": False}), ""
        return 1, "", "unexpected"

    monkeypatch.setattr(uploader, "_run_cmd", fake_run_cmd)
    settings = RuntimeSettings(rclone_remote="pikpak", rclone_remote_base_dir="Pornhub", min_file_size_bytes=64)
    cache = uploader.UploadSessionCache()

    def on_progress(vk: str, sent: int, total: int, speed: float, phase: str) -> None:
        progress_events.append((vk, sent, total, speed, phase))

    res1 = await uploader.upload_file_batch(
        download_dir=dl_dir,
        uploader_folder="alice",
        files_by_viewkey={"vk1": f1},
        settings=settings,
        session_cache=cache,
        on_progress=on_progress,
    )
    res2 = await uploader.upload_file_batch(
        download_dir=dl_dir,
        uploader_folder="alice",
        files_by_viewkey={"vk2": f2},
        settings=settings,
        session_cache=cache,
        on_progress=on_progress,
    )
    assert res1.uploaded_viewkeys == ["vk1"]
    assert res2.uploaded_viewkeys == ["vk2"]
    # Session cache prevents repeating listremotes and mkdir on the second upload to the same remote dir!
    assert cmd_actions.count("listremotes") == 1
    assert cmd_actions.count("mkdir") == 1
    assert cmd_actions.count("copy") == 2
    assert any(vk == "vk1" and phase == "uploading" and speed == 125_000_000 for vk, _, _, speed, phase in progress_events)
    assert any(vk == "vk1" and phase == "verifying" and sent == len(VALID_MP4) for vk, sent, _, _, phase in progress_events)
    assert any(vk == "vk2" and phase == "uploading" for vk, _, _, _, phase in progress_events)


async def test_rclone_error_summary_strips_generic_fatal_tail_and_flags_drive_quota(tmp_path: Path, monkeypatch):
    # 1. Google Drive daily 750GB upload limit hidden above generic Fatal error tail
    err, fatal = uploader.summarize_rclone_error(
        [
            "Failed to copyto: googleapi: Error 403: User rate limit exceeded., userRateLimitExceeded",
            "Cancelling sync due to fatal error: googleapi: Error 403: User rate limit exceeded., userRateLimitExceeded",
            "Fatal error received - not attempting retries",
        ],
        rc=7,
        remote_type="drive",
        action="copyto",
    )
    assert "750GB" in err and fatal == err

    # 2. Storage quota exceeded
    err_q, fatal_q = uploader.summarize_rclone_error(
        [
            "googleapi: Error 403: The user's Drive storage quota has been exceeded., storageQuotaExceeded",
            "Fatal error received - not attempting retries",
        ],
        rc=7,
        remote_type="drive",
        action="copyto",
    )
    assert "存储空间已满" in err_q and fatal_q == err_q

    # 3. Google Drive rc=7 even when only generic fatal line was emitted
    err_rc7, fatal_rc7 = uploader.summarize_rclone_error(
        ["Fatal error received - not attempting retries"],
        rc=7,
        remote_type="drive",
        action="copyto",
    )
    assert "750GB" in err_rc7 and fatal_rc7 == err_rc7

    # 4. Non-quota error strips generic tail and does NOT flag fatal_limit_error
    err_net, fatal_net = uploader.summarize_rclone_error(
        [
            "Failed to copyto: Post https://www.googleapis.com/upload/drive/v3/files: connection reset by peer",
            "Fatal error received - not attempting retries",
        ],
        rc=1,
        remote_type="drive",
        action="copyto",
    )
    assert "connection reset by peer" in err_net
    assert "Fatal error received" not in err_net
    assert fatal_net == ""

    # 5. End-to-end streaming JSON stderr in upload_file_batch captures both msg & error fields
    dl_dir = tmp_path / "alice"
    dl_dir.mkdir()
    local_file = dl_dir / "Clip [vk1].mp4"
    local_file.write_bytes(VALID_MP4)

    async def fake_run_cmd(cmd: List[str], **kwargs):
        action = cmd[1]
        if action == "listremotes":
            return 0, "gd: drive\n", ""
        if action == "mkdir":
            return 0, "", ""
        if action == "copyto":
            cb = kwargs.get("on_stderr_line")
            assert cb is not None
            cb(
                json.dumps(
                    {
                        "level": "error",
                        "msg": "Failed to copyto",
                        "error": "googleapi: Error 403: User rate limit exceeded., userRateLimitExceeded",
                    }
                )
            )
            cb(json.dumps({"level": "error", "msg": "Fatal error received - not attempting retries"}))
            return 7, "", "Fatal error received - not attempting retries"
        return 1, "", "unexpected"

    monkeypatch.setattr(uploader, "_run_cmd", fake_run_cmd)
    settings = RuntimeSettings(rclone_remote="gd", rclone_remote_base_dir="Pornhub", min_file_size_bytes=64)
    res = await uploader.upload_file_batch(
        download_dir=dl_dir,
        uploader_folder="alice",
        files_by_viewkey={"vk1": local_file},
        settings=settings,
        on_progress=lambda *_: None,
        remote_names_by_viewkey={"vk1": "2026-11-26_Clip [vk1].mp4"},
    )
    assert res.failed_viewkeys == ["vk1"]
    assert "750GB" in res.error
    assert res.fatal_limit_error == res.error
    assert local_file.exists()


async def test_multi_remote_chain_session_cache_and_connection_test(tmp_path: Path, monkeypatch):
    # 1. RuntimeSettings.rclone_remote_chain deduplicates and normalizes delimiters/colons
    settings = RuntimeSettings(
        rclone_remote="gd1:",
        rclone_backup_remotes=" gd2:, GD1，gd3;\ngd2 ",
        rclone_remote_base_dir="Pornhub",
        min_file_size_bytes=64,
    )
    assert settings.rclone_remote_chain() == ["gd1", "gd2", "gd3"]

    # 2. UploadSessionCache caches remote_types per remote name independently
    dl_dir = tmp_path / "alice"
    dl_dir.mkdir()
    f1 = dl_dir / "Clip 1 [vk1].mp4"
    f2 = dl_dir / "Clip 2 [vk2].mp4"
    f3 = dl_dir / "Clip 3 [vk3].mp4"
    for fp in (f1, f2, f3):
        fp.write_bytes(VALID_MP4)

    seen_cmds: List[List[str]] = []

    async def fake_run_cmd(cmd: List[str], **kwargs):
        seen_cmds.append(list(cmd))
        action = cmd[1]
        if action == "listremotes":
            return 0, "pikpak: pikpak\ngd2: drive\n", ""
        if action == "mkdir":
            return 0, "", ""
        if action == "copy":
            return 0, "", ""
        if action == "lsjson":
            return 0, json.dumps({"Size": len(VALID_MP4), "IsDir": False}), ""
        return 1, "", "unexpected"

    monkeypatch.setattr(uploader, "_run_cmd", fake_run_cmd)
    cache = uploader.UploadSessionCache()
    s_pikpak = RuntimeSettings(rclone_remote="pikpak", rclone_remote_base_dir="Pornhub", min_file_size_bytes=64)
    s_gd2 = s_pikpak.model_copy(update={"rclone_remote": "gd2"})

    r1 = await uploader.upload_file_batch(dl_dir, "alice", {"vk1": f1}, s_pikpak, session_cache=cache)
    r2 = await uploader.upload_file_batch(dl_dir, "alice", {"vk2": f2}, s_gd2, session_cache=cache)
    r3 = await uploader.upload_file_batch(dl_dir, "alice", {"vk3": f3}, s_gd2, session_cache=cache)

    assert r1.uploaded_viewkeys == ["vk1"]
    assert r2.uploaded_viewkeys == ["vk2"]
    assert r3.uploaded_viewkeys == ["vk3"]
    assert cache.remote_types == {"pikpak": "pikpak", "gd2": "drive"}
    assert sum(1 for c in seen_cmds if c[1] == "listremotes") == 2
    copy_cmds = [c for c in seen_cmds if c[1] == "copy"]
    assert any("--pikpak-chunk-size=32Mi" in c for c in copy_cmds[:1])
    assert any("--drive-stop-on-upload-limit" in c for c in copy_cmds[1:2])

    # 3. test_rclone_remotes tests all configured remotes and allows root fallback for fresh backup
    async def fake_probe_cmd(cmd: List[str], **kwargs):
        action = cmd[1]
        if action == "version":
            return 0, "rclone v1.69.0\n", ""
        if action == "listremotes":
            return 0, "gd1: drive\ngd2: drive\ngd3: drive\n", ""
        if action == "lsd":
            target = cmd[2]
            if target == "gd1:Pornhub":
                return 0, "0 2026-11-26 alice\n", ""
            if target == "gd2:Pornhub":
                return 3, "", "directory not found"
            if target == "gd2:":
                return 0, "", ""
            if target == "gd3:Pornhub":
                return 1, "", "invalid_grant: token expired"
        return 1, "", "unexpected"

    monkeypatch.setattr(uploader, "_run_cmd", fake_probe_cmd)
    ok_all, msg_all, res_all = await uploader.test_rclone_remotes(
        RuntimeSettings(rclone_remote="gd1", rclone_backup_remotes="gd2", rclone_remote_base_dir="Pornhub")
    )
    assert ok_all is True
    assert "全部 2 个远端可用" in msg_all
    assert [r["ok"] for r in res_all] == [True, True]

    ok_part, msg_part, res_part = await uploader.test_rclone_remotes(
        RuntimeSettings(rclone_remote="gd1", rclone_backup_remotes="gd2, gd3", rclone_remote_base_dir="Pornhub")
    )
    assert ok_part is False
    assert "可用 2/3" in msg_part
    assert [r["ok"] for r in res_part] == [True, True, False]
