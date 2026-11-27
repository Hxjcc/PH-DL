"""High-speed async aiohttp HLS downloader with yt-dlp fallback, fragment resume, and lossless MP4 remuxing."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import gc
import hashlib
import inspect
import json
import os
import re
import shutil
import stat
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

import aiohttp

from ..cancel import StopRequested, until_stopped
from ..config import SelectorSettings
from ..parsers.http_session import TaskHttpContext
from ..parsers.urls import is_cookie_origin, is_media_url
from ..parsers.video import fetch_single_video_detail
from ..parsers.xhamster import HlsVariant, parse_hls_master_variants
from .downloader import (
    DOWNLOAD_HEADERS,
    DownloadDiskBudget,
    DownloadOutcome,
    LogCallback,
    ProgressCallback,
    _DiskSpaceError,
    _await_file_io,
    _disk_write,
    _media_resource,
    _write_media_chunk,
    write_atomic_json,
)
from .filename import _looks_like_ts, is_valid_video_file, is_within_directory, normalized_quality, video_filename_base
from .http_proxy import MediaPolicyError, effective_request_proxy, open_media_response
from .subprocesses import run_bounded_process

_EXT_X_MAP_URI_RE = re.compile(r'URI\s*=\s*"([^"]+)"', re.IGNORECASE)
_EXT_X_MAP_RANGE_RE = re.compile(r"(?:^|,)\s*BYTERANGE\s*=", re.IGNORECASE)
_EXT_X_KEY_METHOD_RE = re.compile(r"METHOD\s*=\s*([A-Z0-9-]+)", re.IGNORECASE)
_EXTINF_RE = re.compile(r"#EXTINF:\s*([0-9.]+)", re.IGNORECASE)
_HTTP_EXPIRED_RE = re.compile(r"(?:HTTP\s+Error|status\s+code)\s*(403|410|472)\b|\b(403|410|472)\b", re.IGNORECASE)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_HLS_ENGINE_NAME = "yt-dlp"
_HLS_MANIFEST_VERSION = 3
_AIOHTTP_ENGINE_NAME = "aiohttp"
_AIOHTTP_MANIFEST_VERSION = 5
_AIOHTTP_CHUNK_BYTES = 256 * 1024
_HLS_SPILL_COPY_BYTES = 512 * 1024
HLS_JOB_BUFFER_BYTES = 48 * 1024 * 1024
_AIOHTTP_MAX_SEGMENT_BYTES = 64 * 1024 * 1024
_MAX_PLAYLIST_BYTES = 4 * 1024 * 1024
_MAX_MANIFEST_BYTES = 16_384
_AIOHTTP_MANIFEST_FLUSH_SEGMENTS = 16
_AIOHTTP_MANIFEST_FLUSH_SECONDS = 4.0
_META_FILENAMES = frozenset({"manifest.json", "manifest.json.tmp"})
_YTDLP_CANCEL_GRACE_SECONDS = 2.0
_active_ytdlp_workers: Dict[Path, asyncio.Task] = {}


def ytdlp_hls_dir_in_use(hls_dir: Path) -> bool:
    worker = _active_ytdlp_workers.get(hls_dir.resolve())
    return worker is not None and not worker.done()


def _playlist_resource(url: str) -> str:
    resource = _media_resource(url)
    return urllib.parse.urlunsplit(("", "", resource.path, resource.query, ""))


def is_hls_url(url: Optional[str]) -> bool:
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        path = urllib.parse.urlparse(raw).path.lower()
    except ValueError:
        return False
    return path.endswith(".m3u8") or ".m3u8" in path


async def _write_in_thread(func: Callable, *args: Any, **kwargs: Any) -> None:
    await _await_file_io(asyncio.create_task(asyncio.to_thread(func, *args, **kwargs)))


@contextlib.asynccontextmanager
async def _reserve_fallback_connections(semaphore: Optional[asyncio.Semaphore], workers: int):
    acquired = 0
    try:
        if semaphore is not None:
            for _ in range(workers):
                await semaphore.acquire()
                acquired += 1
        yield
    finally:
        if semaphore is not None:
            for _ in range(acquired):
                semaphore.release()


class HlsMemoryBudget:
    """Account for resident segment payloads across one event loop's downloads."""

    def __init__(self, max_bytes: int = HLS_JOB_BUFFER_BYTES, *, http_slots: int = 0, writers: int = 0):
        self.max_bytes = max(1, int(max_bytes))
        # Keep space for each active response chunk and each ordered spill copy.
        self._payload_limit = max(0, self.max_bytes - http_slots * _AIOHTTP_CHUNK_BYTES - writers * _HLS_SPILL_COPY_BYTES)
        self.used_bytes = 0
        self.peak_bytes = 0

    def try_reserve(self, size: int) -> bool:
        if self.used_bytes + size > self._payload_limit:
            return False
        self.used_bytes += size
        self.peak_bytes = max(self.peak_bytes, self.used_bytes)
        return True

    def release(self, size: int) -> None:
        self.used_bytes -= size


@dataclass
class _HlsSegment:
    size: int
    data: Optional[bytearray] = None
    spill: Optional[Path] = None


@dataclass(frozen=True)
class HlsMediaPlaylist:
    init_url: Optional[str]
    segment_urls: Tuple[str, ...]
    total_duration: float
    is_fmp4: bool
    segment_durations: Tuple[float, ...] = ()
    encrypted_method: str = ""
    is_master: bool = False
    has_byterange: bool = False
    has_separate_media: bool = False
    has_multiple_maps: bool = False
    has_map_byterange: bool = False
    is_endlist: bool = False


def parse_hls_media_playlist(m3u8_text: str, base_url: str) -> HlsMediaPlaylist:
    """Parse an HLS media playlist (MPEG-TS or fMP4 with ``#EXT-X-MAP``)."""
    init_url: Optional[str] = None
    segments: List[str] = []
    durations: List[float] = []
    pending_duration: Optional[float] = None
    total_duration = 0.0
    encrypted_method = ""
    is_master = False
    has_byterange = False
    has_separate_media = False
    has_multiple_maps = has_map_byterange = is_endlist = False

    for raw_line in (m3u8_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            is_master = True
            continue
        if line.startswith("#EXT-X-BYTERANGE:"):
            has_byterange = True
            continue
        if line.startswith("#EXT-X-MEDIA:"):
            has_separate_media = True
            continue
        if line.startswith("#EXT-X-MAP:"):
            attrs = line.partition(":")[2]
            has_map_byterange |= bool(_EXT_X_MAP_RANGE_RE.search(attrs))
            m_uri = _EXT_X_MAP_URI_RE.search(attrs)
            if m_uri:
                next_init = urllib.parse.urljoin(base_url, m_uri.group(1).strip())
                has_multiple_maps |= init_url is not None and init_url != next_init
                init_url = next_init
            continue
        if line == "#EXT-X-ENDLIST":
            is_endlist = True
            continue
        if line.startswith("#EXT-X-KEY:"):
            m_meth = _EXT_X_KEY_METHOD_RE.search(line)
            if m_meth:
                method = m_meth.group(1).strip().upper()
                if method and method != "NONE":
                    encrypted_method = method
            continue
        if line.startswith("#EXTINF:"):
            m_dur = _EXTINF_RE.match(line)
            if m_dur:
                with contextlib.suppress(ValueError):
                    dur = max(0.0, float(m_dur.group(1)))
                    pending_duration = dur
                    total_duration += dur
            continue
        if line.startswith("#"):
            continue
        segments.append(urllib.parse.urljoin(base_url, line))
        durations.append(pending_duration if pending_duration is not None else 0.0)
        pending_duration = None

    is_fmp4 = bool(init_url) or any(
        urllib.parse.urlparse(u).path.lower().endswith((".m4s", ".mp4", ".cmfv", ".cmfa"))
        for u in segments[:3]
    )
    return HlsMediaPlaylist(
        init_url=init_url,
        segment_urls=tuple(segments),
        total_duration=total_duration,
        is_fmp4=is_fmp4,
        segment_durations=tuple(durations),
        encrypted_method=encrypted_method,
        is_master=is_master,
        has_byterange=has_byterange,
        has_separate_media=has_separate_media,
        has_multiple_maps=has_multiple_maps,
        has_map_byterange=has_map_byterange,
        is_endlist=is_endlist,
    )


def _media_dir_size(path: Path) -> int:
    """Return total media bytes stored inside a ``.hls`` work directory (excluding tiny metadata files)."""
    total = 0
    if not path.is_dir():
        return 0
    with contextlib.suppress(OSError):
        for entry in path.iterdir():
            if not entry.is_file():
                continue
            if entry.name in _META_FILENAMES or entry.name.endswith(".ytdl") or entry.name.startswith(".checkpoint-"):
                continue
            with contextlib.suppress(OSError):
                total += entry.stat().st_size
    return total


def _callback_accepts_phase(cb: Optional[Callable[..., Any]]) -> bool:
    if cb is None:
        return False
    try:
        sig = inspect.signature(cb)
    except (TypeError, ValueError):
        return False
    positional = 0
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            return True
        if param.name == "phase" and param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
        if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            positional += 1
    return positional >= 4


class _YtDlpCaptureLogger:
    """Silent yt-dlp logger that captures warnings/errors for structured retry classification."""

    def __init__(self) -> None:
        self.warnings: Deque[str] = collections.deque(maxlen=32)
        self.errors: Deque[str] = collections.deque(maxlen=32)
        self._lock = threading.Lock()

    @staticmethod
    def _clean(msg: Any) -> str:
        text = _ANSI_ESCAPE_RE.sub("", str(msg or "")).replace("\r", " ").strip()
        return re.sub(r"\s+", " ", text)

    def debug(self, msg: Any) -> None:
        return

    def info(self, msg: Any) -> None:
        return

    def warning(self, msg: Any) -> None:
        cleaned = self._clean(msg)
        if not cleaned or "Passing cookies as a header" in cleaned:
            return
        with self._lock:
            self.warnings.append(cleaned[:4096])

    def error(self, msg: Any) -> None:
        cleaned = self._clean(msg)
        if not cleaned:
            return
        with self._lock:
            self.errors.append(cleaned[:4096])


def _classify_ytdlp_error(exc: Optional[BaseException], logger_errors: List[str]) -> Tuple[str, Optional[int], bool]:
    """Return ``(summary_message, expired_http_code, non_retryable)`` from yt-dlp errors."""
    candidates: List[str] = []
    for raw in reversed(logger_errors):
        cleaned = _YtDlpCaptureLogger._clean(raw)
        if cleaned:
            candidates.append(cleaned)
    if exc is not None:
        exc_text = _YtDlpCaptureLogger._clean(exc)
        if exc_text and "write to closed file" not in exc_text.lower():
            candidates.append(exc_text)

    combined = " | ".join(candidates)
    if "DRM protected" in combined or "DRM" in combined:
        return "暂不支持受 DRM 保护的 HLS 流", None, True

    m_http = _HTTP_EXPIRED_RE.search(combined)
    if m_http:
        code = int(m_http.group(1) or m_http.group(2))
        return f"HTTP {code}", code, False

    if candidates:
        primary = candidates[0]
        primary = re.sub(r"^ERROR:\s*", "", primary, flags=re.IGNORECASE)
        primary = re.sub(r"^\[download\]\s*Got error:\s*", "", primary, flags=re.IGNORECASE)
        if len(primary) > 160:
            primary = primary[:157] + "..."
        return f"yt-dlp HLS 下载失败: {primary}", None, False
    return "yt-dlp HLS 下载失败", None, False


def _find_downloaded_stream(hls_dir: Path, prefix: str = "stream") -> Optional[Path]:
    """Locate the completed stream file produced by yt-dlp inside ``hls_dir``."""
    preferred = hls_dir / f"{prefix}.mp4"
    if preferred.is_file() and preferred.stat().st_size > 0:
        return preferred
    candidates: List[Path] = []
    with contextlib.suppress(OSError):
        for entry in hls_dir.iterdir():
            if not entry.is_file():
                continue
            name = entry.name
            if (
                name in _META_FILENAMES
                or name == "remuxed.mp4"
                or name.endswith((".part", ".ytdl", ".tmp"))
                or ".part-Frag" in name
            ):
                continue
            if name.startswith(f"{prefix}.") and entry.stat().st_size > 0:
                candidates.append(entry)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0]


async def _run_ffmpeg(
    cmd: List[str],
    *,
    cancel_event: asyncio.Event,
    timeout: float = 600.0,
) -> Tuple[int, str]:
    try:
        rc, _, stderr = await run_bounded_process(
            cmd, timeout=timeout, cancel_event=cancel_event, capture_stdout=False,
        )
    except FileNotFoundError:
        return 127, "未找到 ffmpeg 可执行文件"
    except Exception as err:
        return 1, f"启动 ffmpeg 失败: {err}"
    if rc == 130:
        return rc, "已取消"
    if rc == 124:
        return rc, "ffmpeg 封装超时"
    return rc, stderr


async def _remux_stream_to_mp4(
    *,
    ffmpeg_bin: str,
    input_path: Path,
    output_path: Path,
    is_ts: bool,
    cancel_event: asyncio.Event,
    audio_path: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Losslessly remux yt-dlp's downloaded MPEG-TS or fMP4 HLS stream into a faststart MP4."""
    inputs = ["-protocol_whitelist", "file,pipe", "-i", str(input_path)]
    if audio_path is not None:
        inputs.extend(["-protocol_whitelist", "file,pipe", "-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0"])
    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", *inputs, "-c", "copy"]
    output = ["-movflags", "+faststart", str(output_path)]
    if is_ts:
        rc, stderr = await _run_ffmpeg([*cmd, "-bsf:a", "aac_adtstoasc", *output], cancel_event=cancel_event)
        if rc == 0 and output_path.is_file():
            return True, ""
        if rc == 130 or cancel_event.is_set():
            return False, "已取消"
    rc, stderr = await _run_ffmpeg([*cmd, *output], cancel_event=cancel_event)
    if rc == 0 and output_path.is_file():
        return True, ""
    if rc == 130 or cancel_event.is_set():
        return False, "已取消"
    return False, stderr or f"ffmpeg 封装失败 (exit={rc})"


def _run_ytdlp_hls_sync(
    *,
    playlist_url: str,
    hls_dir: Path,
    referer: str,
    cookie_str: str,
    proxy_url: Optional[str],
    segment_concurrency: int,
    request_timeout: float,
    ffmpeg_bin: str,
    thread_stop: threading.Event,
    prepare_write: Callable[[int], None],
    on_tick: Callable[[int, int, float], None],
    site: Optional[str] = None,
    audio_playlist_url: str = "",
) -> Tuple[bool, List[str], Optional[BaseException]]:
    """Run yt-dlp's native concurrent HLS downloader in a worker thread."""
    import yt_dlp
    from yt_dlp.networking.exceptions import RequestError
    from yt_dlp.utils import DownloadCancelled, DownloadError

    class _YtDlpDiskLimitStop(DownloadCancelled):
        def __init__(self, disk_error: _DiskSpaceError) -> None:
            super().__init__(str(disk_error))
            self.disk_error = disk_error

    capture = _YtDlpCaptureLogger()
    hook_lock = threading.Lock()
    best_dl_bytes = _media_dir_size(hls_dir)
    previous_tracks_bytes = 0
    accounted_bytes = best_dl_bytes
    bytes_since_tick = 0
    started_at = time.monotonic()
    last_tick = 0.0
    disk_error_holder: List[_DiskSpaceError] = []

    def _sleep_or_cancel(n: int = 0, **_kwargs: Any) -> float:
        if thread_stop.is_set():
            raise DownloadCancelled("已取消")
        delay = min(1.0 * (2 ** max(0, int(n))), 5.0)
        if thread_stop.wait(timeout=delay):
            raise DownloadCancelled("已取消")
        return 0.0

    def _progress_hook(info: Dict[str, Any]) -> None:
        nonlocal best_dl_bytes, accounted_bytes, bytes_since_tick, last_tick
        if thread_stop.is_set():
            raise DownloadCancelled("已取消")

        status = str(info.get("status") or "").lower()
        if status == "error":
            return

        raw_dl = previous_tracks_bytes + int(info.get("downloaded_bytes") or 0)
        raw_tot = previous_tracks_bytes + int(info.get("total_bytes") or info.get("total_bytes_estimate") or 0)
        frag_idx = int(info.get("fragment_index") or 0)
        frag_cnt = int(info.get("fragment_count") or 0)
        raw_speed = float(info.get("speed") or 0.0)

        with hook_lock:
            if thread_stop.is_set():
                raise DownloadCancelled("已取消")
            if raw_dl > best_dl_bytes:
                delta = raw_dl - accounted_bytes
                if delta > 0:
                    try:
                        prepare_write(delta)
                    except _DiskSpaceError as derr:
                        disk_error_holder.append(derr)
                        raise _YtDlpDiskLimitStop(derr) from derr
                    accounted_bytes += delta
                    bytes_since_tick += delta
                best_dl_bytes = raw_dl

            if status == "finished":
                return

            est_total = raw_tot
            if frag_cnt > 0 and 0 < frag_idx < frag_cnt and best_dl_bytes > 0:
                frag_est = int((best_dl_bytes / frag_idx) * frag_cnt)
                est_total = max(best_dl_bytes + 1, est_total if est_total > best_dl_bytes else frag_est)
            elif est_total <= best_dl_bytes and best_dl_bytes > 0:
                est_total = best_dl_bytes + 1

            now = time.monotonic()
            if (last_tick == 0.0 and best_dl_bytes > 0) or (now - last_tick >= 0.35):
                dt = max(0.05, now - (last_tick or started_at))
                speed = raw_speed if raw_speed > 0 else (bytes_since_tick / dt)
                on_tick(best_dl_bytes, max(best_dl_bytes, est_total), speed)
                last_tick = now
                bytes_since_tick = 0

    headers: Dict[str, str] = {**DOWNLOAD_HEADERS, "Referer": referer}
    if cookie_str:
        headers["Cookie"] = cookie_str

    ydl_opts: Dict[str, Any] = {
        "outtmpl": str(hls_dir / "stream.%(ext)s"),
        "format": "best",
        "concurrent_fragment_downloads": max(1, min(16, int(segment_concurrency))),
        "buffersize": 256 * 1024,
        "http_chunk_size": 0,
        "retries": 4,
        "fragment_retries": 6,
        "file_access_retries": 5,
        "retry_sleep_functions": {
            "http": _sleep_or_cancel,
            "fragment": _sleep_or_cancel,
            "file_access": _sleep_or_cancel,
        },
        "skip_unavailable_fragments": False,
        "continuedl": True,
        "overwrites": False,
        "nopart": False,
        "fixup": "never",
        "hls_prefer_native": True,
        "ffmpeg_location": str(Path(ffmpeg_bin).parent),
        "socket_timeout": min(float(request_timeout), 15.0),
        "http_headers": headers,
        "logger": capture,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nocolor": True,
        "noplaylist": True,
        "progress_hooks": [_progress_hook],
    }
    if proxy_url:
        ydl_opts["proxy"] = proxy_url

    ydl_class = yt_dlp.YoutubeDL
    if site is not None:
        if not is_media_url(playlist_url, site) or (audio_playlist_url and not is_media_url(audio_playlist_url, site)):
            return False, ["HLS 清单地址不在允许范围内"], None
        from yt_dlp.networking import Request
        from yt_dlp.networking._requests import RequestsRH

        class ScopedRequestsRH(RequestsRH):
            def _create_instance(self, *args, **kwargs):
                session = super()._create_instance(*args, **kwargs)
                session.max_redirects = 5

                def check_redirect(response, *args, **kwargs):
                    if response.is_redirect and not is_media_url(
                        urllib.parse.urljoin(response.url, response.headers["Location"]), site,
                    ):
                        response.close()
                        raise RequestError("HLS 重定向地址不在允许范围内")
                    return response

                session.hooks["response"].append(check_redirect)
                return session

        class ScopedYoutubeDL(yt_dlp.YoutubeDL):
            def build_request_director(self, handlers, preferences=None):
                return super().build_request_director([ScopedRequestsRH])

            def urlopen(self, req):
                if thread_stop.is_set():
                    raise DownloadCancelled("已取消")
                req = Request(req) if isinstance(req, str) else req
                if not is_media_url(req.url, site):
                    raise RequestError("HLS 媒体地址不在允许范围内")
                if cookie_str and is_cookie_origin(req.url, site):
                    req.headers["Cookie"] = cookie_str
                return super().urlopen(req)

        headers.pop("Cookie", None)
        ydl_class = ScopedYoutubeDL
        # FFmpeg's network downloader bypasses the validated request handler.
        # Native HLS handles its own fetching; the local remux runs separately below.
        ydl_opts["external_downloader"] = {"m3u8": "native"}
        ydl_opts["ffmpeg_location"] = str(hls_dir / ".native-hls-only")
        ydl_opts["external_downloader_args"] = {"ffmpeg_i": ["-protocol_whitelist", "file,pipe"]}

    caught: Optional[BaseException] = None
    try:
        with ydl_class(ydl_opts) as ydl:
            ydl.download([playlist_url])
        if audio_playlist_url and not _find_downloaded_stream(hls_dir, "audio"):
            if thread_stop.is_set():
                raise DownloadCancelled("已取消")
            video = _find_downloaded_stream(hls_dir)
            previous_tracks_bytes = video.stat().st_size if video else 0
            with ydl_class({**ydl_opts, "outtmpl": str(hls_dir / "audio.%(ext)s"), "format": "bestaudio/best"}) as ydl:
                ydl.download([audio_playlist_url])
        return True, list(capture.errors), None
    except _YtDlpDiskLimitStop as exc:
        caught = exc.disk_error
        exc.__traceback__ = None
    except DownloadCancelled as exc:
        caught = StopRequested()
        exc.__traceback__ = None
    except RequestError as exc:
        capture.error(str(exc))
        caught = exc
        exc.__traceback__ = None
    except (DownloadError, ValueError, OSError) as exc:
        caught = exc
        exc.__traceback__ = None
    finally:
        if caught is not None:
            # Release FragmentFD's open dest_stream handle immediately on Windows after abrupt abort.
            gc.collect()

    if disk_error_holder:
        return False, list(capture.errors), disk_error_holder[0]
    return False, list(capture.errors), caught


@dataclass(frozen=True)
class _AiohttpHlsOutcome:
    ok: bool = False
    fallback_to_ytdlp: bool = False
    cancelled: bool = False
    expired_code: Optional[int] = None
    error: str = ""
    disk_error: Optional[_DiskSpaceError] = None
    variant: Optional[HlsVariant] = None


def _read_hls_manifest(hls_dir: Path) -> Optional[Dict[str, Any]]:
    manifest_path = hls_dir / "manifest.json"
    try:
        before = manifest_path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_MANIFEST_BYTES:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(manifest_path, flags), "rb") as manifest:
            opened = os.fstat(manifest.fileno())
            if (
                not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened)
                or manifest_path.is_symlink()
            ):
                return None
            raw = manifest.read(_MAX_MANIFEST_BYTES + 1)
        if len(raw) > _MAX_MANIFEST_BYTES:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def _write_hls_manifest_atomic(hls_dir: Path, meta: Dict[str, Any]) -> None:
    write_atomic_json(hls_dir / "manifest.json", meta)


def _write_stream_blocks(fh: Any, blocks: List[bytearray], *, flush: bool = False) -> None:
    try:
        for block in blocks:
            if fh.write(block) != len(block):
                raise OSError("未能写入完整 HLS 分片")
        if flush:
            fh.flush()
    finally:
        blocks.clear()


def _remove_spilled_segments(hls_dir: Path) -> None:
    for path in hls_dir.glob("seg_*.tmp"):
        path.unlink(missing_ok=True)


def _write_hls_segments(fh, segments: List[_HlsSegment], prepare_write, *, flush: bool) -> None:
    blocks: List[bytearray] = []
    for segment in segments:
        if segment.data is not None:
            blocks.append(segment.data)
            continue
        if blocks:
            with _disk_write(prepare_write, sum(map(len, blocks))):
                _write_stream_blocks(fh, blocks)
        assert segment.spill is not None
        copied = 0
        with segment.spill.open("rb", buffering=0) as source:
            while chunk := source.read(_HLS_SPILL_COPY_BYTES):
                copied += len(chunk)
                if copied > segment.size:
                    break
                _write_media_chunk(fh, chunk, prepare_write)
                del chunk
        if copied != segment.size:
            raise OSError("HLS 临时分片大小已变化")
        segment.spill.unlink()
    if blocks:
        with _disk_write(prepare_write, sum(map(len, blocks))):
            _write_stream_blocks(fh, blocks, flush=flush)
    elif flush:
        fh.flush()


async def _download_hls_aiohttp(
    *,
    session: aiohttp.ClientSession,
    playlist_url: str,
    hls_dir: Path,
    viewkey: str,
    quality: str,
    referer: str,
    cookie_str: str,
    proxy_url: Optional[str],
    segment_concurrency: int,
    request_timeout: float,
    stop: asyncio.Event,
    prepare_write: Callable[[int], None],
    on_tick: Callable[[int, int, float], None],
    max_buffer_bytes: int = 16 * 1024 * 1024,
    site: Optional[str] = None,
    segment_semaphore: Optional[asyncio.Semaphore] = None,
    memory_budget: Optional[HlsMemoryBudget] = None,
    spill_to_disk: bool = False,
) -> _AiohttpHlsOutcome:
    """Download an unencrypted HLS media playlist asynchronously over a pooled aiohttp session.

    Uses a bounded sliding window to fetch segments concurrently over Keep-Alive connections and
    appends them in order to ``stream.part``. With spill enabled, a segment that cannot fit
    in the shared memory budget is streamed to a temporary file until it can be appended.
    Falls back cleanly to yt-dlp for encrypted, master, byte-range, or separate-audio playlists.
    """
    headers: Dict[str, str] = {**DOWNLOAD_HEADERS, "Referer": referer}
    _remove_spilled_segments(hls_dir)
    if cookie_str:
        headers["Cookie"] = cookie_str
    proxy = effective_request_proxy(session, proxy_url)
    req_timeout = max(10.0, float(request_timeout))
    playlist_timeout = aiohttp.ClientTimeout(
        total=req_timeout,
        connect=min(req_timeout, 20.0),
        sock_connect=min(req_timeout, 20.0),
        sock_read=req_timeout,
    )
    seg_timeout = aiohttp.ClientTimeout(
        total=None,
        connect=min(req_timeout, 20.0),
        sock_connect=min(req_timeout, 20.0),
        sock_read=req_timeout,
    )

    # 1. Fetch & parse media playlist
    try:
        async def fetch_playlist() -> tuple[int, str, str]:
            async with open_media_response(
                session, playlist_url, headers=headers, site=site, proxy=proxy, timeout=playlist_timeout,
            ) as resp:
                if resp.status != 200:
                    return resp.status, "", ""
                body = bytearray()
                async for chunk in resp.content.iter_chunked(_AIOHTTP_CHUNK_BYTES):
                    if len(body) + len(chunk) > _MAX_PLAYLIST_BYTES:
                        return resp.status, "", ""
                    body.extend(chunk)
                return resp.status, body.decode("utf-8", errors="replace"), str(resp.url)

        status, m3u8_text, effective_url = await until_stopped(fetch_playlist(), stop)
        if status in (403, 410, 472):
            return _AiohttpHlsOutcome(expired_code=status, error=f"HTTP {status}")
        if status != 200:
            return _AiohttpHlsOutcome(error=f"HLS 清单拉取失败 (HTTP {status})")
        if not effective_url:
            return _AiohttpHlsOutcome(error="HLS 清单体积超出限制")
    except StopRequested:
        return _AiohttpHlsOutcome(cancelled=True)
    except MediaPolicyError as err:
        return _AiohttpHlsOutcome(error=f"HLS 清单请求失败: {err}")
    except ValueError as err:
        return _AiohttpHlsOutcome(fallback_to_ytdlp=True, error=f"HLS 代理不受当前会话支持: {err}")
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
        return _AiohttpHlsOutcome(error=f"HLS 清单请求失败: {err}")

    upper_m3u8 = (m3u8_text or "").upper()
    if "#EXTM3U" not in upper_m3u8 and "#EXTINF" not in upper_m3u8:
        return _AiohttpHlsOutcome(fallback_to_ytdlp=True, error="非标准 HLS 媒体清单")

    playlist = parse_hls_media_playlist(m3u8_text, effective_url)
    if playlist.is_master:
        variants = parse_hls_master_variants(m3u8_text, effective_url)
        if any(v.has_separate_audio for v in variants):
            requested = normalized_quality(quality)
            chosen = next((v for v in variants if not requested or str(v.height) == requested), None)
            if chosen is None:
                return _AiohttpHlsOutcome(error="HLS 主清单没有请求的画质")
            return _AiohttpHlsOutcome(fallback_to_ytdlp=True, variant=chosen)
        return _AiohttpHlsOutcome(fallback_to_ytdlp=True)
    del m3u8_text, upper_m3u8
    if (
        not playlist.segment_urls
        or playlist.encrypted_method
        or playlist.has_byterange
        or playlist.has_separate_media
        or playlist.has_multiple_maps
        or playlist.has_map_byterange
        or not playlist.is_endlist
    ):
        return _AiohttpHlsOutcome(fallback_to_ytdlp=True)

    # 2. Initialize or resume single sequential stream.part
    total_segments = len(playlist.segment_urls)
    # Signatures and CDN hosts can change while the underlying sequence stays intact.
    fingerprint = hashlib.sha256()
    fingerprint.update(_playlist_resource(playlist.init_url or "").encode())
    for url, duration in zip(playlist.segment_urls, playlist.segment_durations, strict=True):
        fingerprint.update(f"\n{_playlist_resource(url)}\t{duration}".encode())
    playlist_id = fingerprint.hexdigest()
    norm_q = normalized_quality(quality)
    stream_part = hls_dir / "stream.part"
    final_stream = hls_dir / "stream.mp4"
    completed_segments = 0
    stream_bytes = 0
    init_written = False
    reused = False

    old_meta = _read_hls_manifest(hls_dir)
    same_playlist = (
        old_meta is not None
        and old_meta.get("engine") == _AIOHTTP_ENGINE_NAME
        and old_meta.get("version") == _AIOHTTP_MANIFEST_VERSION
        and old_meta.get("viewkey") == viewkey
        and old_meta.get("quality") == norm_q
        and old_meta.get("segment_count") == total_segments
        and old_meta.get("playlist_id") == playlist_id
        and bool(old_meta.get("is_fmp4")) == playlist.is_fmp4
    )
    if same_playlist and old_meta is not None:
        try:
            saved_completed = int(old_meta.get("completed_segments") or 0)
            saved_bytes = int(old_meta.get("stream_bytes") or 0)
            if saved_completed == total_segments and saved_bytes > 0 and final_stream.is_file():
                if final_stream.stat().st_size == saved_bytes:
                    return _AiohttpHlsOutcome(ok=True)
            saved_init = bool(old_meta.get("init_written"))
            actual_size = stream_part.stat().st_size
            if (
                0 <= saved_completed <= total_segments
                and 0 <= saved_bytes <= actual_size
                and (not playlist.init_url or saved_init or saved_completed == 0)
            ):
                completed_segments = saved_completed
                stream_bytes = saved_bytes
                init_written = saved_init
                reused = True
        except (OSError, ValueError, TypeError):
            reused = False

    if not reused:
        if hls_dir.is_dir():
            shutil.rmtree(hls_dir, ignore_errors=True)
        hls_dir.mkdir(parents=True, exist_ok=True)
        completed_segments = 0
        stream_bytes = 0
        init_written = False

    def _build_meta() -> Dict[str, Any]:
        return {
            "engine": _AIOHTTP_ENGINE_NAME,
            "version": _AIOHTTP_MANIFEST_VERSION,
            "viewkey": viewkey,
            "quality": norm_q,
            "segment_count": total_segments,
            "playlist_id": playlist_id,
            "playlist_resource": _playlist_resource(playlist_url),
            "is_fmp4": playlist.is_fmp4,
            "init_written": init_written,
            "completed_segments": completed_segments,
            "stream_bytes": stream_bytes,
        }

    _write_hls_manifest_atomic(hls_dir, _build_meta())

    # 3. Real-time progress & rolling 2s speed tracker
    seg_durations = playlist.segment_durations
    has_durations = (
        len(seg_durations) == total_segments
        and playlist.total_duration > 0.0
        and all(d > 0.0 for d in seg_durations)
    )
    prefix_dur: List[float] = [0.0] * (total_segments + 1)
    if has_durations:
        running_dur = 0.0
        for idx, dur in enumerate(seg_durations):
            running_dur += dur
            prefix_dur[idx + 1] = running_dur

    in_flight_bytes = 0
    init_bytes = 0
    best_reported_bytes = stream_bytes
    speed_window: Deque[Tuple[float, int]] = collections.deque()
    speed_window_bytes = 0
    last_tick_at = 0.0
    ready_buffers: Dict[int, _HlsSegment] = {}
    ready_buffered_bytes = 0
    ready_buffered_duration = 0.0
    next_write_idx = completed_segments
    max_buffer_bytes = max(1, int(max_buffer_bytes))
    budget = (memory_budget or HlsMemoryBudget(
        max_buffer_bytes, http_slots=max(1, min(16, int(segment_concurrency))), writers=1,
    )) if spill_to_disk else None
    window_cv = asyncio.Condition()

    def _release_segment(segment: _HlsSegment) -> None:
        if segment.data is not None:
            segment.data = None
            if budget is not None:
                budget.release(segment.size)

    def _record_speed_delta(delta: int, *, force_tick: bool = False) -> None:
        nonlocal speed_window_bytes, best_reported_bytes, last_tick_at
        now = time.monotonic()
        if delta > 0:
            if speed_window and now - speed_window[-1][0] < 0.1:
                tick, accumulated = speed_window[-1]
                speed_window[-1] = (tick, accumulated + delta)
            else:
                speed_window.append((now, delta))
            speed_window_bytes += delta
        cutoff = now - 2.0
        while speed_window and speed_window[0][0] < cutoff:
            _, old_b = speed_window.popleft()
            speed_window_bytes -= old_b

        if not force_tick and last_tick_at > 0.0 and (now - last_tick_at) < 0.25:
            return

        reported_dl = max(best_reported_bytes, stream_bytes + max(0, in_flight_bytes))
        best_reported_bytes = reported_dl
        done_count = next_write_idx + len(ready_buffers)
        known_done_bytes = stream_bytes + ready_buffered_bytes
        if completed_segments >= total_segments:
            est_total = max(reported_dl, stream_bytes)
        elif done_count > 0 and known_done_bytes > 0:
            # Estimate strictly from completed segments (never include partial in-flight bytes
            # in the numerator) and weight by #EXTINF duration when available.
            if has_durations:
                done_dur = prefix_dur[completed_segments] + ready_buffered_duration
                if done_dur > 0.0:
                    media_done_bytes = max(0, known_done_bytes - init_bytes)
                    raw_est = int(init_bytes + (media_done_bytes / done_dur) * playlist.total_duration)
                else:
                    raw_est = int((known_done_bytes / done_count) * total_segments)
            else:
                raw_est = int((known_done_bytes / done_count) * total_segments)
            est_total = max(reported_dl + 1, raw_est)
        else:
            est_total = 0

        if speed_window:
            span = max(0.25, now - speed_window[0][0])
            speed_bps = max(0.0, speed_window_bytes / span)
        else:
            speed_bps = 0.0

        last_tick_at = now
        on_tick(reported_dl, est_total, speed_bps)

    abort_event = asyncio.Event()
    if stop.is_set():
        abort_event.set()

    async def _fetch_segment(seg_url: str, seg_idx: int) -> Tuple[Optional[_HlsSegment], Optional[int], str]:
        nonlocal in_flight_bytes
        last_err = ""
        max_seg_retries = 4
        for retry_idx in range(max_seg_retries + 1):
            if stop.is_set() or abort_event.is_set():
                return None, None, ""
            received = reserved = 0
            buf = None
            spill = None
            result = None
            completed = False
            try:
                async with segment_semaphore or contextlib.nullcontext():
                    async with open_media_response(
                        session, seg_url, headers=headers, site=site, proxy=proxy, timeout=seg_timeout,
                        auto_decompress=False,
                    ) as resp:
                        if resp.status in (403, 410, 472):
                            return None, resp.status, f"HTTP {resp.status}"
                        if resp.status != 200:
                            last_err = f"HTTP {resp.status}"
                        else:
                            if getattr(resp, "headers", {}).get("Content-Encoding", "").strip().lower() not in {"", "identity"}:
                                return None, None, "HLS 分片使用了不支持的内容编码"
                            expected = resp.content_length
                            if expected is not None and expected > _AIOHTTP_MAX_SEGMENT_BYTES:
                                return None, None, "HLS 单个分片体积超出限制"
                            if budget is None:
                                buf = bytearray()
                            elif expected and budget.try_reserve(expected):
                                reserved = expected
                                buf = bytearray(expected)
                            else:
                                # Including an oversized head here guarantees that other videos
                                # sharing the budget never have to lend it unaccounted memory.
                                spill = hls_dir / f"seg_{seg_idx:06d}.tmp"
                            with spill.open("xb", buffering=0) if spill else contextlib.nullcontext() as spool:
                                spool_buf = bytearray(_AIOHTTP_CHUNK_BYTES) if spool is not None else None
                                spool_filled = 0

                                async def _flush_spool() -> None:
                                    nonlocal spool_filled
                                    if spool is None or spool_buf is None or not spool_filled:
                                        return
                                    length = spool_filled
                                    spool_filled = 0

                                    def _write_spool() -> None:
                                        with memoryview(spool_buf)[:length] as view:
                                            _write_media_chunk(spool, view, prepare_write)

                                    await _write_in_thread(_write_spool)

                                async for chunk in resp.content.iter_chunked(_AIOHTTP_CHUNK_BYTES):
                                    n = len(chunk)
                                    upper_limit = expected if expected is not None else _AIOHTTP_MAX_SEGMENT_BYTES
                                    if received + n > upper_limit:
                                        last_err = "HLS 分片超出声明大小或单分片限制"
                                        break
                                    if spool is not None:
                                        assert spool_buf is not None
                                        if not spool_filled and n >= _AIOHTTP_CHUNK_BYTES:
                                            await _write_in_thread(_write_media_chunk, spool, chunk, prepare_write)
                                        elif n <= _AIOHTTP_CHUNK_BYTES - spool_filled:
                                            spool_buf[spool_filled:spool_filled + n] = chunk
                                            spool_filled += n
                                            if spool_filled == _AIOHTTP_CHUNK_BYTES:
                                                await _flush_spool()
                                        else:
                                            with memoryview(chunk) as src:
                                                pos = 0
                                                while pos < n:
                                                    take = min(n - pos, _AIOHTTP_CHUNK_BYTES - spool_filled)
                                                    spool_buf[spool_filled:spool_filled + take] = src[pos:pos + take]
                                                    spool_filled += take
                                                    pos += take
                                                    if spool_filled == _AIOHTTP_CHUNK_BYTES:
                                                        await _flush_spool()
                                    elif budget is not None:
                                        buf[received:received + n] = chunk
                                    else:
                                        if seg_idx > completed_segments and in_flight_bytes + n > max_buffer_bytes:
                                            async with window_cv:
                                                await window_cv.wait_for(lambda: (
                                                    stop.is_set() or abort_event.is_set() or seg_idx <= completed_segments
                                                    or in_flight_bytes + n <= max_buffer_bytes
                                                ))
                                        if stop.is_set() or abort_event.is_set():
                                            return None, None, ""
                                        buf.extend(chunk)
                                    received += n
                                    in_flight_bytes += n
                                    _record_speed_delta(n)
                                    del chunk
                                else:
                                    if spool_filled:
                                        await _flush_spool()
                                    if received and (expected is None or received == expected):
                                        result = _HlsSegment(received, buf, spill)
                                    else:
                                        last_err = f"分片大小不完整 ({received}/{expected or 0})"
                if result is not None:
                    completed = True
                    return result, None, ""
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as err:
                last_err = str(err) or type(err).__name__
            finally:
                if not completed:
                    if reserved:
                        buf = None
                        result = None
                        budget.release(reserved)
                    if spill:
                        spill.unlink(missing_ok=True)
                    if received:
                        async with window_cv:
                            in_flight_bytes -= received
                            window_cv.notify_all()

            if retry_idx < max_seg_retries and not stop.is_set() and not abort_event.is_set():
                delay = min(0.5 * (2 ** retry_idx), 4.0)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(abort_event.wait(), timeout=delay)
        return None, None, last_err or "分片下载失败"

    if stream_bytes > 0:
        _record_speed_delta(0, force_tick=True)

    # 4. Open stream.part once for sequential writes.
    fh = open(stream_part, "r+b" if reused else "wb", buffering=0)
    try:
        if reused:
            fh.seek(stream_bytes)
            fh.truncate(stream_bytes)

        # Download fMP4 init segment if required
        if playlist.init_url and not init_written:
            init_fetch = asyncio.create_task(_fetch_segment(playlist.init_url, completed_segments))
            try:
                init_data, init_code, init_err = await until_stopped(init_fetch, stop)
                if init_data is None:
                    return _AiohttpHlsOutcome(expired_code=init_code, error=init_err or "HLS 初始化分片下载失败")
                if stop.is_set():
                    return _AiohttpHlsOutcome(cancelled=True)
                await _write_in_thread(_write_hls_segments, fh, [init_data], prepare_write, flush=True)
            except StopRequested:
                return _AiohttpHlsOutcome(cancelled=True)
            except _DiskSpaceError as derr:
                return _AiohttpHlsOutcome(disk_error=derr, error=str(derr))
            finally:
                if init_fetch.done() and not init_fetch.cancelled() and init_fetch.exception() is None:
                    segment, _, _ = init_fetch.result()
                    if segment is not None:
                        _release_segment(segment)
            init_bytes = init_data.size
            in_flight_bytes = max(0, in_flight_bytes - init_bytes)
            stream_bytes += init_bytes
            init_written = True
            _write_hls_manifest_atomic(hls_dir, _build_meta())

        remaining_segments = total_segments - completed_segments
        if remaining_segments > 0:
            workers = max(1, min(16, int(segment_concurrency), remaining_segments))
            window_size = 1 if workers == 1 else min(max(workers + 4, 10), 20)
            next_fetch_idx = completed_segments
            fatal_outcome: Optional[_AiohttpHlsOutcome] = None
            fetcher_tasks: List[asyncio.Task[None]] = []
            last_manifest_seg = completed_segments
            last_manifest_at = time.monotonic()

            async def _watch_stop() -> None:
                await stop.wait()
                abort_event.set()
                async with window_cv:
                    window_cv.notify_all()
                for ft in fetcher_tasks:
                    if not ft.done():
                        ft.cancel()

            async def _fetcher() -> None:
                nonlocal next_fetch_idx, fatal_outcome, ready_buffered_bytes, ready_buffered_duration, in_flight_bytes
                while not abort_event.is_set() and not stop.is_set():
                    async with window_cv:
                        while (
                            not abort_event.is_set()
                            and not stop.is_set()
                            and next_fetch_idx < total_segments
                            and (
                                next_fetch_idx - completed_segments >= window_size
                                or (budget is None and ready_buffered_bytes >= max_buffer_bytes
                                    and next_fetch_idx > next_write_idx)
                            )
                        ):
                            await window_cv.wait()
                        if abort_event.is_set() or stop.is_set() or next_fetch_idx >= total_segments:
                            return
                        seg_idx = next_fetch_idx
                        next_fetch_idx += 1

                    disk_error = None
                    try:
                        data, err_code, err_msg = await _fetch_segment(playlist.segment_urls[seg_idx], seg_idx)
                    except _DiskSpaceError as err:
                        data, err_code, err_msg = None, None, str(err)
                        disk_error = err
                    except Exception as err:
                        data, err_code, err_msg = None, None, f"HLS 分片处理失败 ({type(err).__name__})"
                    if data is None:
                        if not stop.is_set() and fatal_outcome is None:
                            fatal_outcome = _AiohttpHlsOutcome(
                                expired_code=err_code,
                                error=err_msg or f"分片 {seg_idx + 1} 下载失败",
                                disk_error=disk_error,
                            )
                        abort_event.set()
                        async with window_cv:
                            window_cv.notify_all()
                        return

                    try:
                        async with window_cv:
                            ready_buffers[seg_idx] = data
                            ready_buffered_bytes += data.size
                            if has_durations:
                                ready_buffered_duration += seg_durations[seg_idx]
                            if seg_idx == next_write_idx:
                                window_cv.notify_all()
                    except asyncio.CancelledError:
                        in_flight_bytes -= data.size
                        _release_segment(data)
                        raise
                    del data

            async def _writer() -> None:
                nonlocal next_write_idx, completed_segments, stream_bytes
                nonlocal in_flight_bytes, ready_buffered_bytes, ready_buffered_duration, fatal_outcome
                nonlocal last_manifest_seg, last_manifest_at
                while next_write_idx < total_segments:
                    async with window_cv:
                        while (
                            next_write_idx not in ready_buffers
                            and not abort_event.is_set()
                            and not stop.is_set()
                        ):
                            await window_cv.wait()

                        segments: List[_HlsSegment] = []
                        batch_len = 0
                        batch_duration = 0.0
                        while next_write_idx in ready_buffers:
                            segments.append(ready_buffers.pop(next_write_idx))
                            batch_len += segments[-1].size
                            if has_durations:
                                batch_duration += seg_durations[next_write_idx]
                            next_write_idx += 1

                        should_stop = (not segments) and (abort_event.is_set() or stop.is_set())

                    if should_stop:
                        break
                    if not segments:
                        continue

                    now = time.monotonic()
                    need_manifest = (
                        (next_write_idx - last_manifest_seg) >= _AIOHTTP_MANIFEST_FLUSH_SEGMENTS
                        or (now - last_manifest_at) >= _AIOHTTP_MANIFEST_FLUSH_SECONDS
                        or next_write_idx >= total_segments
                    )
                    try:
                        await _write_in_thread(_write_hls_segments, fh, segments, prepare_write, flush=need_manifest)
                    except _DiskSpaceError as derr:
                        fatal_outcome = _AiohttpHlsOutcome(disk_error=derr, error=str(derr))
                        abort_event.set()
                        async with window_cv:
                            window_cv.notify_all()
                        break
                    finally:
                        for segment in segments:
                            _release_segment(segment)
                    async with window_cv:
                        ready_buffered_bytes -= batch_len
                        ready_buffered_duration = max(0.0, ready_buffered_duration - batch_duration)
                        in_flight_bytes -= batch_len
                        stream_bytes += batch_len
                        completed_segments = next_write_idx
                        window_cv.notify_all()

                    if need_manifest:
                        _write_hls_manifest_atomic(hls_dir, _build_meta())
                        last_manifest_seg = completed_segments
                        last_manifest_at = now

            fetcher_tasks = [asyncio.create_task(_fetcher()) for _ in range(workers)]
            writer_task = asyncio.create_task(_writer())
            stop_watcher = asyncio.create_task(_watch_stop())
            try:
                await writer_task
                if abort_event.is_set() or stop.is_set():
                    for ft in fetcher_tasks:
                        if not ft.done():
                            ft.cancel()
                await asyncio.gather(*fetcher_tasks, return_exceptions=True)
            finally:
                tasks = [stop_watcher, writer_task, *fetcher_tasks]
                for task in tasks:
                    if not task.done() and not task.cancelling():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            await _write_in_thread(fh.flush)
            _write_hls_manifest_atomic(hls_dir, _build_meta())

            if stop.is_set():
                return _AiohttpHlsOutcome(cancelled=True)
            if fatal_outcome is not None:
                return fatal_outcome
            if completed_segments < total_segments:
                return _AiohttpHlsOutcome(error="HLS 分片未能全部完成")

        await _write_in_thread(fh.flush)
        _write_hls_manifest_atomic(hls_dir, _build_meta())
    finally:
        for segment in ready_buffers.values():
            _release_segment(segment)
        ready_buffers.clear()
        with contextlib.suppress(OSError):
            fh.close()
        _remove_spilled_segments(hls_dir)

    if stop.is_set():
        return _AiohttpHlsOutcome(cancelled=True)

    os.replace(stream_part, final_stream)
    _record_speed_delta(0, force_tick=True)
    return _AiohttpHlsOutcome(ok=True)


async def download_hls_to_mp4(
    *,
    session: aiohttp.ClientSession,
    parse_ctx: TaskHttpContext,
    selectors: SelectorSettings,
    viewkey: str,
    title: str,
    playlist_url: str,
    page_url: str,
    referer_url: str,
    quality: str,
    download_dir: Path,
    temp_dir: Path,
    partial_path: Optional[Path] = None,
    replace_existing: bool = False,
    disk_budget: Optional[DownloadDiskBudget] = None,
    cookie_str: str = "",
    proxy_url: Optional[str] = None,
    preferred_qualities: Optional[Set[str]] = None,
    xh_preferred_codec: str = "h264",
    max_attempts: int = 3,
    segment_concurrency: int = 10,
    max_buffer_bytes: int = 16 * 1024 * 1024,
    segment_semaphore: Optional[asyncio.Semaphore] = None,
    memory_budget: Optional[HlsMemoryBudget] = None,
    spill_to_disk: bool = False,
    fallback_segment_concurrency: Optional[int] = None,
    retry_delay_base: float = 2.0,
    retry_delay_max: float = 10.0,
    request_timeout: Optional[float] = None,
    min_file_size_bytes: int = 10240,
    cancel_event: Optional[asyncio.Event] = None,
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
) -> DownloadOutcome:
    """Download an HLS stream via async aiohttp (with yt-dlp fallback), fragment resume, and lossless MP4 remuxing."""
    stop = cancel_event if cancel_event is not None else parse_ctx.cancel_event
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", viewkey or ""):
        return DownloadOutcome(success=False, error="缺少或无效的 viewkey")

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return DownloadOutcome(
            success=False,
            error="未找到 ffmpeg 可执行文件，无法封装 HLS 视频（请安装 ffmpeg 或使用 Docker 部署）",
        )

    base_name = video_filename_base(title, viewkey)
    hls_dir = temp_dir / f"{base_name}.hls"
    if (
        partial_path is not None
        and not partial_path.is_symlink()
        and is_within_directory(partial_path, temp_dir)
        and partial_path.name.endswith(".hls")
        and partial_path.is_dir()
    ):
        hls_dir = partial_path
    if hls_dir.is_symlink() or (hls_dir.is_dir() and any(entry.is_symlink() for entry in hls_dir.iterdir())):
        return DownloadOutcome(success=False, error="HLS 工作目录含不受信任的文件路径")
    worker_key = hls_dir.resolve()
    if ytdlp_hls_dir_in_use(worker_key):
        return DownloadOutcome(success=False, temp_path=hls_dir, error="上一次 HLS 下载仍在停止，请稍后重试")

    url = (playlist_url or "").strip()
    referer = (referer_url or page_url or "https://xhamster.com/").strip()
    current_quality = (quality or "").strip()
    site = getattr(parse_ctx, "site", None)
    disk = disk_budget or DownloadDiskBudget(temp_dir, 0)
    req_timeout = max(10.0, float(request_timeout or 30.0))
    fallback_workers = fallback_segment_concurrency or (1 if segment_semaphore is not None else segment_concurrency)
    accepts_phase = _callback_accepts_phase(on_progress)
    loop = asyncio.get_running_loop()

    def emit(level: str, message: str) -> None:
        if on_log:
            on_log(level, message)

    def emit_progress(dl_bytes: int, tot_bytes: int, speed_bps: float, phase: str = "downloading") -> None:
        if not on_progress:
            return
        if accepts_phase:
            on_progress(dl_bytes, tot_bytes, speed_bps, phase)  # type: ignore[call-arg]
        else:
            on_progress(dl_bytes, tot_bytes, speed_bps)

    def emit_progress_threadsafe(dl_bytes: int, tot_bytes: int, speed_bps: float, phase: str = "downloading") -> None:
        if not on_progress:
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(emit_progress, dl_bytes, tot_bytes, speed_bps, phase)

    def incomplete(error: str, *, cancelled: bool = False, disk_wait_bytes: int = 0) -> DownloadOutcome:
        size = _media_dir_size(hls_dir)
        if size <= 0 and hls_dir.is_dir() and not ytdlp_hls_dir_in_use(worker_key):
            with contextlib.suppress(OSError):
                shutil.rmtree(hls_dir, ignore_errors=True)
        return DownloadOutcome(
            success=False,
            cancelled=cancelled,
            temp_path=hls_dir if size > 0 else None,
            file_size=size,
            quality=current_quality,
            download_url=url,
            error=error,
            disk_wait_bytes=max(0, int(disk_wait_bytes)),
        )

    async def refresh_playlist_url() -> bool:
        nonlocal url, referer, current_quality
        target_page = page_url or referer
        locked = normalized_quality(current_quality)
        filters = {locked} if locked else preferred_qualities
        detail = await fetch_single_video_detail(
            parse_ctx,
            target_page,
            selectors,
            preferred_qualities=filters,
            xh_preferred_codec=xh_preferred_codec,
            timeout=request_timeout,
        )
        if not detail.download_url and filters != preferred_qualities:
            detail = await fetch_single_video_detail(
                parse_ctx,
                target_page,
                selectors,
                preferred_qualities=preferred_qualities,
                xh_preferred_codec=xh_preferred_codec,
                timeout=request_timeout,
            )
        if not detail.download_url:
            return False
        url = detail.download_url
        referer = detail.referer_url or referer
        current_quality = detail.quality or current_quality
        return True

    def _ytdlp_checkpoint(variant: Optional[HlsVariant] = None) -> Dict[str, Any]:
        expected_meta = {
            "engine": _HLS_ENGINE_NAME,
            "version": _HLS_MANIFEST_VERSION,
            "viewkey": viewkey,
            "quality": normalized_quality(current_quality),
            "playlist_resource": _playlist_resource(url),
        }
        if variant:
            expected_meta["track_resources"] = [_playlist_resource(variant.url), _playlist_resource(variant.audio_url)]
        return expected_meta

    def _matches_ytdlp_checkpoint(meta: Optional[Dict[str, Any]]) -> bool:
        # Masters must be fetched again to select the current signed track URLs.
        return meta is not None and meta == _ytdlp_checkpoint()

    def _prepare_ytdlp_hls_dir(variant: Optional[HlsVariant]) -> None:
        expected_meta = _ytdlp_checkpoint(variant)
        if hls_dir.is_dir() and _read_hls_manifest(hls_dir) != expected_meta:
            shutil.rmtree(hls_dir, ignore_errors=True)
        hls_dir.mkdir(parents=True, exist_ok=True)
        _write_hls_manifest_atomic(hls_dir, expected_meta)

    async def _download_with_ytdlp(variant: Optional[HlsVariant]):
        thread_stop = threading.Event()

        async def _run_worker():
            async with _reserve_fallback_connections(segment_semaphore, fallback_workers):
                if thread_stop.is_set():
                    raise StopRequested
                with disk.reserve(0) as prepare_write:
                    return await asyncio.to_thread(
                        _run_ytdlp_hls_sync,
                        playlist_url=variant.url if variant else url,
                        hls_dir=hls_dir,
                        referer=referer,
                        cookie_str=cookie_str,
                        proxy_url=proxy_url,
                        segment_concurrency=fallback_workers,
                        request_timeout=req_timeout,
                        ffmpeg_bin=ffmpeg_bin,
                        thread_stop=thread_stop,
                        prepare_write=prepare_write,
                        on_tick=lambda d, t, s: emit_progress_threadsafe(d, t, s, "downloading"),
                        **({"site": site} if site else {}),
                        **({"audio_playlist_url": variant.audio_url} if variant and variant.audio_url else {}),
                    )

        def _worker_finished(worker: asyncio.Task) -> None:
            if _active_ytdlp_workers.get(worker_key) is worker:
                _active_ytdlp_workers.pop(worker_key)
            if not worker.cancelled():
                worker.exception()

        worker = asyncio.create_task(_run_worker())
        _active_ytdlp_workers[worker_key] = worker
        worker.add_done_callback(_worker_finished)
        try:
            return await until_stopped(asyncio.shield(worker), stop)
        except (StopRequested, asyncio.CancelledError):
            thread_stop.set()
            # A blocked socket can outlast the job. Keep its directory and slots reserved
            # until the worker actually exits so a retry cannot touch its checkpoint.
            await asyncio.wait({worker}, timeout=_YTDLP_CANCEL_GRACE_SECONDS)
            raise

    attempts = max(1, int(max_attempts))
    last_error = "HLS 下载失败"

    try:
        if stop.is_set():
            raise StopRequested
        download_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        _remove_spilled_segments(hls_dir)

        for attempt in range(attempts):
            if stop.is_set():
                raise StopRequested
            should_retry = False
            try:
                if not url and not await until_stopped(refresh_playlist_url(), stop):
                    last_error = "无法解析 HLS 播放清单"
                    should_retry = True
                else:
                    matching_ytdlp = _matches_ytdlp_checkpoint(_read_hls_manifest(hls_dir))
                    raw_stream = _find_downloaded_stream(hls_dir) if matching_ytdlp else None
                    audio_stream = None
                    variant = None

                    if raw_stream is None:
                        use_ytdlp = session is None or (matching_ytdlp and any(hls_dir.glob("stream.*.ytdl")))

                        if not use_ytdlp and session is not None:
                            with disk.reserve(0) as prepare_write:
                                aio_res = await _download_hls_aiohttp(
                                    session=session,
                                    playlist_url=url,
                                    hls_dir=hls_dir,
                                    viewkey=viewkey,
                                    quality=current_quality,
                                    referer=referer,
                                    cookie_str=cookie_str,
                                    proxy_url=proxy_url,
                                    segment_concurrency=segment_concurrency,
                                    max_buffer_bytes=max_buffer_bytes,
                                    segment_semaphore=segment_semaphore,
                                    memory_budget=memory_budget,
                                    spill_to_disk=spill_to_disk,
                                    site=site,
                                    request_timeout=req_timeout,
                                    stop=stop,
                                    prepare_write=prepare_write,
                                    on_tick=lambda d, t, s: emit_progress(d, t, s, "downloading"),
                                )
                            if aio_res.cancelled or stop.is_set():
                                raise StopRequested
                            if aio_res.disk_error is not None:
                                raise aio_res.disk_error
                            if aio_res.fallback_to_ytdlp:
                                use_ytdlp = True
                                variant = aio_res.variant
                            elif aio_res.ok:
                                raw_stream = _find_downloaded_stream(hls_dir)
                                if raw_stream is None:
                                    last_error = "HLS 下载完成后未找到视频流文件"
                                    should_retry = True
                            else:
                                last_error = aio_res.error or "HLS 分片下载失败"
                                if aio_res.expired_code is not None and attempt < attempts - 1:
                                    emit(
                                        "INFO",
                                        f"{title}: HLS 签名链接疑似过期（HTTP {aio_res.expired_code}），"
                                        "重新解析链接并续传剩余分片",
                                    )
                                    await until_stopped(refresh_playlist_url(), stop)
                                should_retry = True

                        if use_ytdlp and not should_retry and raw_stream is None:
                            if ytdlp_hls_dir_in_use(worker_key):
                                return incomplete("上一次 HLS 下载仍在停止，请稍后重试")
                            _prepare_ytdlp_hls_dir(variant)
                            separate_audio = bool(variant and variant.has_separate_audio)
                            raw_stream = _find_downloaded_stream(hls_dir)
                            if separate_audio:
                                audio_stream = _find_downloaded_stream(hls_dir, "audio")

                            if raw_stream is None or (separate_audio and audio_stream is None):
                                initial_bytes = _media_dir_size(hls_dir)
                                if initial_bytes > 0:
                                    emit_progress(initial_bytes, initial_bytes + 1, 0.0, "downloading")

                                ok_dl, ydl_errors, ydl_exc = await _download_with_ytdlp(variant)

                                if stop.is_set() or isinstance(ydl_exc, StopRequested):
                                    raise StopRequested
                                if isinstance(ydl_exc, _DiskSpaceError):
                                    raise ydl_exc
                                if not ok_dl:
                                    err_summary, expired_code, non_retryable = _classify_ytdlp_error(
                                        ydl_exc, ydl_errors
                                    )
                                    last_error = err_summary
                                    if non_retryable:
                                        return incomplete(last_error)
                                    if expired_code is not None and attempt < attempts - 1:
                                        emit(
                                            "INFO",
                                            f"{title}: HLS 签名链接疑似过期（HTTP {expired_code}），"
                                            "重新解析链接并续传剩余分片",
                                        )
                                        await until_stopped(refresh_playlist_url(), stop)
                                    should_retry = True
                                else:
                                    raw_stream = _find_downloaded_stream(hls_dir)
                                    if separate_audio:
                                        audio_stream = _find_downloaded_stream(hls_dir, "audio")
                                    if raw_stream is None or (separate_audio and audio_stream is None):
                                        last_error = "yt-dlp 下载完成后未找到视频流文件"
                                        should_retry = True

                if not should_retry and raw_stream is not None:
                    if stop.is_set():
                        raise StopRequested

                    raw_size = raw_stream.stat().st_size
                    if audio_stream is not None:
                        raw_size += audio_stream.stat().st_size
                    emit_progress(raw_size, raw_size, 0.0, "remuxing")

                    # Reserve disk space roughly equal to downloaded stream for lossless faststart MP4 remux.
                    with disk.reserve(raw_size):
                        remuxed_mp4 = hls_dir / "remuxed.mp4"
                        with contextlib.suppress(OSError):
                            remuxed_mp4.unlink(missing_ok=True)
                        is_ts = await asyncio.to_thread(_looks_like_ts, raw_stream)
                        ok_remux, remux_err = await _remux_stream_to_mp4(
                            ffmpeg_bin=ffmpeg_bin,
                            input_path=raw_stream,
                            output_path=remuxed_mp4,
                            is_ts=is_ts,
                            cancel_event=stop,
                            **({"audio_path": audio_stream} if audio_stream is not None else {}),
                        )
                        if stop.is_set():
                            raise StopRequested
                        if not ok_remux:
                            last_error = f"HLS 封装失败: {remux_err}"
                            should_retry = True
                        else:
                            final_size = remuxed_mp4.stat().st_size if remuxed_mp4.is_file() else 0
                            if not await asyncio.to_thread(is_valid_video_file, remuxed_mp4, min_file_size_bytes, final_size):
                                with contextlib.suppress(OSError):
                                    remuxed_mp4.unlink(missing_ok=True)
                                last_error = "封装后的 MP4 未通过完整性校验"
                                should_retry = True
                            else:
                                final = download_dir / f"{base_name}.mp4"
                                if final.exists() and not replace_existing:
                                    tag = normalized_quality(current_quality)
                                    stem = f"{base_name}.{tag + 'p' if tag else 'download'}"
                                    final = download_dir / f"{stem}.mp4"
                                    suffix = 2
                                    while final.exists():
                                        final = download_dir / f"{stem}.{suffix}.mp4"
                                        suffix += 1
                                os.replace(remuxed_mp4, final)

                    if not should_retry:
                        shutil.rmtree(hls_dir, ignore_errors=True)
                        return DownloadOutcome(
                            success=True,
                            local_path=final,
                            file_size=final_size,
                            quality=current_quality,
                            download_url=url,
                        )

            except _DiskSpaceError as err:
                return incomplete(str(err), disk_wait_bytes=err.required_bytes)
            except ValueError as err:
                return incomplete(str(err))

            if attempt < attempts - 1:
                emit("WARNING", f"{title}: {last_error}，准备重试")
                await until_stopped(
                    asyncio.sleep(min(retry_delay_base * (2 ** attempt), retry_delay_max)),
                    stop,
                )
    except StopRequested:
        return incomplete("已取消", cancelled=True)
    except OSError as err:
        return incomplete(f"文件读写失败: {err.strerror or type(err).__name__}")
    return incomplete(last_error)
