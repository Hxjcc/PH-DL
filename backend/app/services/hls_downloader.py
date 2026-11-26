"""yt-dlp library-mode HLS downloader with fragment resume, disk backpressure, and lossless MP4 remuxing."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import inspect
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import aiohttp
import yt_dlp
from yt_dlp.utils import DownloadCancelled, DownloadError

from ..cancel import StopRequested, until_stopped
from ..config import SelectorSettings
from ..parsers.http_session import TaskHttpContext
from ..parsers.video import fetch_single_video_detail
from .downloader import (
    DOWNLOAD_HEADERS,
    DownloadDiskBudget,
    DownloadOutcome,
    LogCallback,
    ProgressCallback,
    _DiskSpaceError,
)
from .filename import _looks_like_ts, is_valid_video_file, normalized_quality, video_filename_base

_EXT_X_MAP_URI_RE = re.compile(r'URI\s*=\s*"([^"]+)"', re.IGNORECASE)
_EXT_X_KEY_METHOD_RE = re.compile(r"METHOD\s*=\s*([A-Z0-9-]+)", re.IGNORECASE)
_EXTINF_RE = re.compile(r"#EXTINF:\s*([0-9.]+)", re.IGNORECASE)
_HTTP_EXPIRED_RE = re.compile(r"(?:HTTP\s+Error|status\s+code)\s*(403|410|472)\b|\b(403|410|472)\b", re.IGNORECASE)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_HLS_ENGINE_NAME = "yt-dlp"
_HLS_MANIFEST_VERSION = 2
_META_FILENAMES = frozenset({"manifest.json"})


def is_hls_url(url: Optional[str]) -> bool:
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        path = urllib.parse.urlparse(raw).path.lower()
    except ValueError:
        return False
    return path.endswith(".m3u8") or ".m3u8" in path


@dataclass(frozen=True)
class HlsMediaPlaylist:
    init_url: Optional[str]
    segment_urls: Tuple[str, ...]
    total_duration: float
    is_fmp4: bool
    encrypted_method: str = ""


def parse_hls_media_playlist(m3u8_text: str, base_url: str) -> HlsMediaPlaylist:
    """Parse an HLS media playlist (MPEG-TS or fMP4 with ``#EXT-X-MAP``)."""
    init_url: Optional[str] = None
    segments: List[str] = []
    total_duration = 0.0
    encrypted_method = ""

    for raw_line in (m3u8_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP:"):
            m_uri = _EXT_X_MAP_URI_RE.search(line)
            if m_uri:
                init_url = urllib.parse.urljoin(base_url, m_uri.group(1).strip())
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
                    total_duration += float(m_dur.group(1))
            continue
        if line.startswith("#"):
            continue
        segments.append(urllib.parse.urljoin(base_url, line))

    is_fmp4 = bool(init_url) or any(
        urllib.parse.urlparse(u).path.lower().endswith((".m4s", ".mp4", ".cmfv", ".cmfa"))
        for u in segments[:3]
    )
    return HlsMediaPlaylist(
        init_url=init_url,
        segment_urls=tuple(segments),
        total_duration=total_duration,
        is_fmp4=is_fmp4,
        encrypted_method=encrypted_method,
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
            if entry.name in _META_FILENAMES or entry.name.endswith(".ytdl"):
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
        self.warnings: List[str] = []
        self.errors: List[str] = []
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
            self.warnings.append(cleaned)

    def error(self, msg: Any) -> None:
        cleaned = self._clean(msg)
        if not cleaned:
            return
        with self._lock:
            self.errors.append(cleaned)


class _YtDlpDiskLimitStop(DownloadCancelled):
    """Signal raised inside a yt-dlp progress hook when DownloadDiskBudget floor is hit."""

    def __init__(self, disk_error: _DiskSpaceError) -> None:
        super().__init__(str(disk_error))
        self.disk_error = disk_error


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


def _find_downloaded_stream(hls_dir: Path) -> Optional[Path]:
    """Locate the completed stream file produced by yt-dlp inside ``hls_dir``."""
    preferred = hls_dir / "stream.mp4"
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
            if name.startswith("stream.") and entry.stat().st_size > 0:
                candidates.append(entry)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0]


async def _terminate_proc(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=3.0)
        return
    except asyncio.TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=3.0)


async def _run_ffmpeg(
    cmd: List[str],
    *,
    cancel_event: asyncio.Event,
    timeout: float = 600.0,
) -> Tuple[int, str]:
    if cancel_event.is_set():
        return 130, "已取消"
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "未找到 ffmpeg 可执行文件"
    except Exception as err:
        return 1, f"启动 ffmpeg 失败: {err}"

    comm_task = asyncio.create_task(proc.communicate())
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {comm_task, cancel_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if comm_task in done:
            cancel_task.cancel()
            with contextlib.suppress(BaseException):
                await cancel_task
            _, stderr_b = comm_task.result()
            return int(proc.returncode or 0), (stderr_b or b"").decode(errors="replace").strip()

        cancelled = cancel_task in done
        await _terminate_proc(proc)
        for task in (comm_task, cancel_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(comm_task, cancel_task, return_exceptions=True)
        return (130 if cancelled else 124), ("已取消" if cancelled else "ffmpeg 封装超时")
    except asyncio.CancelledError:
        await _terminate_proc(proc)
        raise


async def _remux_stream_to_mp4(
    *,
    ffmpeg_bin: str,
    input_path: Path,
    output_path: Path,
    is_ts: bool,
    cancel_event: asyncio.Event,
) -> Tuple[bool, str]:
    """Losslessly remux yt-dlp's downloaded MPEG-TS or fMP4 HLS stream into a faststart MP4."""
    if is_ts:
        cmd_primary = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        rc, stderr = await _run_ffmpeg(cmd_primary, cancel_event=cancel_event)
        if rc == 0 and output_path.is_file():
            return True, ""
        if rc == 130 or cancel_event.is_set():
            return False, "已取消"

        # Fallback without aac_adtstoasc bitstream filter in case audio is non-ADTS
        cmd_fallback = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        rc2, stderr2 = await _run_ffmpeg(cmd_fallback, cancel_event=cancel_event)
        if rc2 == 0 and output_path.is_file():
            return True, ""
        return False, stderr2 or stderr or f"ffmpeg TS 封装失败 (exit={rc2})"

    cmd_mp4 = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    rc, stderr = await _run_ffmpeg(cmd_mp4, cancel_event=cancel_event)
    if rc == 0 and output_path.is_file():
        return True, ""
    if rc == 130 or cancel_event.is_set():
        return False, "已取消"
    return False, stderr or f"ffmpeg fMP4 封装失败 (exit={rc})"


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
) -> Tuple[bool, List[str], Optional[BaseException]]:
    """Run yt-dlp's native concurrent HLS downloader in a worker thread."""
    capture = _YtDlpCaptureLogger()
    hook_lock = threading.Lock()
    best_dl_bytes = _media_dir_size(hls_dir)
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

        raw_dl = int(info.get("downloaded_bytes") or 0)
        raw_tot = int(info.get("total_bytes") or info.get("total_bytes_estimate") or 0)
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
        "socket_timeout": max(10.0, float(request_timeout)),
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

    caught: Optional[BaseException] = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([playlist_url])
        return True, capture.errors, None
    except _YtDlpDiskLimitStop as exc:
        caught = exc.disk_error
        exc.__traceback__ = None
    except DownloadCancelled as exc:
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
        return False, capture.errors, disk_error_holder[0]
    return False, capture.errors, caught


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
    segment_concurrency: int = 8,
    retry_delay_base: float = 2.0,
    retry_delay_max: float = 10.0,
    request_timeout: Optional[float] = None,
    min_file_size_bytes: int = 10240,
    cancel_event: Optional[asyncio.Event] = None,
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
) -> DownloadOutcome:
    """Download an HLS stream via yt-dlp with fragment resume and lossless ffmpeg MP4 remuxing."""
    _ = session  # Retained in signature for caller compatibility; yt-dlp manages its own HTTP pool.
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
        and partial_path.resolve().is_relative_to(temp_dir.resolve())
        and partial_path.name.endswith(".hls")
        and partial_path.is_dir()
    ):
        hls_dir = partial_path

    url = (playlist_url or "").strip()
    referer = (referer_url or page_url or "https://xhamster.com/").strip()
    current_quality = (quality or "").strip()
    disk = disk_budget or DownloadDiskBudget(temp_dir, 0)
    req_timeout = max(10.0, float(request_timeout or 30.0))
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
        if size <= 0 and hls_dir.is_dir():
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

    def _prepare_hls_dir() -> None:
        manifest_path = hls_dir / "manifest.json"
        expected_meta = {
            "engine": _HLS_ENGINE_NAME,
            "version": _HLS_MANIFEST_VERSION,
            "viewkey": viewkey,
            "quality": normalized_quality(current_quality),
        }
        if hls_dir.is_dir():
            reuse = False
            try:
                old_meta = json.loads(manifest_path.read_text(encoding="utf-8"))
                reuse = (
                    isinstance(old_meta, dict)
                    and old_meta.get("engine") == _HLS_ENGINE_NAME
                    and old_meta.get("viewkey") == expected_meta["viewkey"]
                    and old_meta.get("quality") == expected_meta["quality"]
                )
            except (OSError, ValueError, TypeError):
                reuse = False
            if not reuse:
                shutil.rmtree(hls_dir, ignore_errors=True)
        hls_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(expected_meta), encoding="utf-8")

    attempts = max(1, int(max_attempts))
    last_error = "HLS 下载失败"

    try:
        if stop.is_set():
            raise StopRequested
        download_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)

        for attempt in range(attempts):
            if stop.is_set():
                raise StopRequested
            should_retry = False
            try:
                if not url and not await until_stopped(refresh_playlist_url(), stop):
                    last_error = "无法解析 HLS 播放清单"
                    should_retry = True
                else:
                    _prepare_hls_dir()
                    raw_stream = _find_downloaded_stream(hls_dir)

                    if raw_stream is None:
                        initial_bytes = _media_dir_size(hls_dir)
                        if initial_bytes > 0:
                            emit_progress(initial_bytes, initial_bytes + 1, 0.0, "downloading")

                        thread_stop = threading.Event()
                        if stop.is_set():
                            thread_stop.set()

                        async def _bridge_cancel() -> None:
                            await stop.wait()
                            thread_stop.set()

                        cancel_bridge = asyncio.create_task(_bridge_cancel())
                        try:
                            with disk.reserve(0) as prepare_write:
                                ok_dl, ydl_errors, ydl_exc = await asyncio.to_thread(
                                    _run_ytdlp_hls_sync,
                                    playlist_url=url,
                                    hls_dir=hls_dir,
                                    referer=referer,
                                    cookie_str=cookie_str,
                                    proxy_url=proxy_url,
                                    segment_concurrency=segment_concurrency,
                                    request_timeout=req_timeout,
                                    ffmpeg_bin=ffmpeg_bin,
                                    thread_stop=thread_stop,
                                    prepare_write=prepare_write,
                                    on_tick=lambda d, t, s: emit_progress_threadsafe(d, t, s, "downloading"),
                                )
                        finally:
                            cancel_bridge.cancel()
                            with contextlib.suppress(BaseException):
                                await cancel_bridge

                        if stop.is_set() or (isinstance(ydl_exc, DownloadCancelled) and not isinstance(ydl_exc, _DiskSpaceError)):
                            raise StopRequested
                        if isinstance(ydl_exc, _DiskSpaceError):
                            raise ydl_exc
                        if not ok_dl:
                            err_summary, expired_code, non_retryable = _classify_ytdlp_error(ydl_exc, ydl_errors)
                            last_error = err_summary
                            if non_retryable:
                                return incomplete(last_error)
                            if expired_code is not None and attempt < attempts - 1:
                                emit("INFO", f"{title}: HLS 签名链接疑似过期（HTTP {expired_code}），重新解析链接并续传剩余分片")
                                await until_stopped(refresh_playlist_url(), stop)
                            should_retry = True
                        else:
                            raw_stream = _find_downloaded_stream(hls_dir)
                            if raw_stream is None:
                                last_error = "yt-dlp 下载完成后未找到视频流文件"
                                should_retry = True

                if not should_retry and raw_stream is not None:
                    if stop.is_set():
                        raise StopRequested

                    raw_size = raw_stream.stat().st_size
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
