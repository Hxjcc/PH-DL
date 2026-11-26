"""Concurrent HLS (MPEG-TS and fMP4/AV1) segment downloader with resume and lossless ffmpeg remuxing."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Set, Tuple

import aiohttp

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
from .filename import is_valid_video_file, normalized_quality, video_filename_base

_EXT_X_MAP_URI_RE = re.compile(r'URI\s*=\s*"([^"]+)"', re.IGNORECASE)
_EXT_X_KEY_METHOD_RE = re.compile(r"METHOD\s*=\s*([A-Z0-9-]+)", re.IGNORECASE)
_EXTINF_RE = re.compile(r"#EXTINF:\s*([0-9.]+)", re.IGNORECASE)


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


def _dir_size(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    with contextlib.suppress(OSError):
        for entry in path.iterdir():
            if entry.is_file():
                with contextlib.suppress(OSError):
                    total += entry.stat().st_size
    return total


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
        done, pending = await asyncio.wait(
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


def _concat_files_sync(paths: List[Path], destination: Path) -> int:
    total = 0
    with open(destination, "wb") as out_fp:
        for src in paths:
            with open(src, "rb") as in_fp:
                while True:
                    buf = in_fp.read(2 * 1024 * 1024)
                    if not buf:
                        break
                    out_fp.write(buf)
                    total += len(buf)
    return total


def _write_concat_list_sync(paths: List[Path], list_path: Path) -> None:
    lines = []
    for p in paths:
        escaped = p.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _remux_hls_segments(
    *,
    ffmpeg_bin: str,
    hls_dir: Path,
    init_path: Optional[Path],
    segment_paths: List[Path],
    is_fmp4: bool,
    output_path: Path,
    cancel_event: asyncio.Event,
) -> Tuple[bool, str]:
    if is_fmp4:
        combined = hls_dir / "combined.fmp4"
        sources = ([init_path] if init_path is not None and init_path.is_file() else []) + segment_paths
        await asyncio.to_thread(_concat_files_sync, sources, combined)
        try:
            cmd = [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(combined),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
            rc, stderr = await _run_ffmpeg(cmd, cancel_event=cancel_event)
            if rc == 0 and output_path.is_file():
                return True, ""
            return False, stderr or f"ffmpeg fMP4 封装失败 (exit={rc})"
        finally:
            with contextlib.suppress(OSError):
                combined.unlink(missing_ok=True)

    # MPEG-TS segments
    concat_txt = hls_dir / "concat.txt"
    await asyncio.to_thread(_write_concat_list_sync, segment_paths, concat_txt)
    cmd_primary = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_txt),
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
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_txt),
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
    segment_concurrency: int = 6,
    retry_delay_base: float = 2.0,
    retry_delay_max: float = 10.0,
    request_timeout: Optional[float] = None,
    min_file_size_bytes: int = 10240,
    cancel_event: Optional[asyncio.Event] = None,
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
) -> DownloadOutcome:
    """Download an HLS variant playlist concurrently with segment resume and remux to MP4 via ffmpeg."""
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

    def emit(level: str, message: str) -> None:
        if on_log:
            on_log(level, message)

    def incomplete(error: str, *, cancelled: bool = False, disk_wait_bytes: int = 0) -> DownloadOutcome:
        size = _dir_size(hls_dir)
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

    async def fetch_playlist() -> HlsMediaPlaylist:
        headers = {**DOWNLOAD_HEADERS, "Referer": referer}
        if cookie_str:
            headers["Cookie"] = cookie_str
        async with session.get(
            url,
            headers=headers,
            allow_redirects=True,
            proxy=proxy_url or None,
            timeout=aiohttp.ClientTimeout(total=req_timeout),
        ) as resp:
            if resp.status >= 400:
                resp.raise_for_status()
            text = await resp.text(errors="replace")
            final_pl_url = str(getattr(resp, "url", url))
        if "#EXTM3U" not in text:
            raise aiohttp.ClientPayloadError("返回的不是有效的 HLS M3U8 清单")
        parsed = parse_hls_media_playlist(text, final_pl_url)
        if parsed.encrypted_method:
            raise ValueError(f"暂不支持加密 HLS 流 ({parsed.encrypted_method})")
        if not parsed.segment_urls:
            raise aiohttp.ClientPayloadError("HLS 清单中未找到视频分片")
        return parsed

    def _prepare_hls_dir(playlist: HlsMediaPlaylist) -> None:
        manifest_path = hls_dir / "manifest.json"
        expected_meta = {
            "viewkey": viewkey,
            "quality": normalized_quality(current_quality),
            "segment_count": len(playlist.segment_urls),
            "is_fmp4": playlist.is_fmp4,
        }
        if hls_dir.is_dir():
            reuse = False
            try:
                old_meta = json.loads(manifest_path.read_text(encoding="utf-8"))
                reuse = (
                    isinstance(old_meta, dict)
                    and old_meta.get("viewkey") == expected_meta["viewkey"]
                    and old_meta.get("quality") == expected_meta["quality"]
                    and old_meta.get("segment_count") == expected_meta["segment_count"]
                    and old_meta.get("is_fmp4") == expected_meta["is_fmp4"]
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
            try:
                if not url and not await until_stopped(refresh_playlist_url(), stop):
                    last_error = "无法解析 HLS 播放清单"
                    continue

                playlist = await until_stopped(fetch_playlist(), stop)
                _prepare_hls_dir(playlist)

                total_segs = len(playlist.segment_urls)
                init_path: Optional[Path] = (hls_dir / "init.bin") if playlist.init_url else None
                seg_paths = [hls_dir / f"seg_{idx:05d}.bin" for idx in range(total_segs)]

                completed_bytes = 0
                completed_segs = 0
                if init_path is not None and init_path.is_file() and init_path.stat().st_size > 0:
                    completed_bytes += init_path.stat().st_size
                for p in seg_paths:
                    if p.is_file() and p.stat().st_size > 0:
                        completed_bytes += p.stat().st_size
                        completed_segs += 1

                bytes_since_tick = 0
                last_tick = time.monotonic()
                progress_lock = asyncio.Lock()

                def _estimate_total(done_bytes: int, done_segs: int) -> int:
                    if done_segs <= 0:
                        return 0
                    return max(done_bytes, int((done_bytes / done_segs) * total_segs))

                if on_progress:
                    on_progress(completed_bytes, _estimate_total(completed_bytes, completed_segs), 0.0)

                headers = {**DOWNLOAD_HEADERS, "Referer": referer}
                if cookie_str:
                    headers["Cookie"] = cookie_str

                sem = asyncio.Semaphore(max(1, min(12, int(segment_concurrency))))
                expired_hit = False

                with disk.reserve(0) as prepare_write:
                    async def _fetch_one(seg_url: str, dest: Path, count_as_seg: bool) -> None:
                        nonlocal completed_bytes, completed_segs, bytes_since_tick, last_tick, expired_hit
                        if dest.is_file() and dest.stat().st_size > 0:
                            return
                        async with sem:
                            if stop.is_set():
                                raise StopRequested
                            part_file = dest.with_suffix(".part")
                            last_seg_err: Optional[Exception] = None
                            for seg_try in range(3):
                                if stop.is_set():
                                    raise StopRequested
                                try:
                                    async with session.get(
                                        seg_url,
                                        headers=headers,
                                        allow_redirects=True,
                                        proxy=proxy_url or None,
                                        timeout=aiohttp.ClientTimeout(total=req_timeout),
                                    ) as resp:
                                        if resp.status in {403, 410, 472}:
                                            expired_hit = True
                                            resp.raise_for_status()
                                        if resp.status >= 400:
                                            resp.raise_for_status()
                                        data = await until_stopped(resp.read(), stop)
                                    if not data:
                                        raise aiohttp.ClientPayloadError("空分片响应")
                                    prepare_write(len(data))
                                    await asyncio.to_thread(part_file.write_bytes, data)
                                    os.replace(part_file, dest)
                                    async with progress_lock:
                                        completed_bytes += len(data)
                                        if count_as_seg:
                                            completed_segs += 1
                                        bytes_since_tick += len(data)
                                        now = time.monotonic()
                                        if on_progress and (now - last_tick >= 0.35 or completed_segs == total_segs):
                                            dt = max(0.05, now - last_tick)
                                            on_progress(
                                                completed_bytes,
                                                _estimate_total(completed_bytes, completed_segs),
                                                bytes_since_tick / dt,
                                            )
                                            last_tick, bytes_since_tick = now, 0
                                    return
                                except aiohttp.ClientResponseError as err:
                                    with contextlib.suppress(OSError):
                                        part_file.unlink(missing_ok=True)
                                    if err.status in {403, 410, 472}:
                                        raise
                                    last_seg_err = err
                                except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                                    with contextlib.suppress(OSError):
                                        part_file.unlink(missing_ok=True)
                                    last_seg_err = err
                                if seg_try < 2:
                                    await until_stopped(asyncio.sleep(0.6 * (seg_try + 1)), stop)
                            if last_seg_err is not None:
                                raise last_seg_err

                    tasks: List[asyncio.Task] = []
                    if playlist.init_url and init_path is not None:
                        tasks.append(asyncio.create_task(_fetch_one(playlist.init_url, init_path, False)))
                    for seg_url, seg_dest in zip(playlist.segment_urls, seg_paths):
                        tasks.append(asyncio.create_task(_fetch_one(seg_url, seg_dest, True)))

                    try:
                        await asyncio.gather(*tasks)
                    except BaseException:
                        for t in tasks:
                            if not t.done():
                                t.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        raise

                if stop.is_set():
                    raise StopRequested
                if on_progress:
                    on_progress(completed_bytes, completed_bytes, 0.0)

                # Reserve space roughly equal to downloaded segments for lossless remux output
                with disk.reserve(completed_bytes):
                    remuxed_mp4 = hls_dir / "remuxed.mp4"
                    with contextlib.suppress(OSError):
                        remuxed_mp4.unlink(missing_ok=True)
                    ok_remux, remux_err = await _remux_hls_segments(
                        ffmpeg_bin=ffmpeg_bin,
                        hls_dir=hls_dir,
                        init_path=init_path,
                        segment_paths=seg_paths,
                        is_fmp4=playlist.is_fmp4,
                        output_path=remuxed_mp4,
                        cancel_event=stop,
                    )
                    if stop.is_set():
                        raise StopRequested
                    if not ok_remux:
                        last_error = f"HLS 封装失败: {remux_err}"
                        continue

                    final_size = remuxed_mp4.stat().st_size if remuxed_mp4.is_file() else 0
                    if not await asyncio.to_thread(is_valid_video_file, remuxed_mp4, min_file_size_bytes, final_size):
                        with contextlib.suppress(OSError):
                            remuxed_mp4.unlink(missing_ok=True)
                        last_error = "封装后的 MP4 未通过完整性校验"
                        continue

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

                shutil.rmtree(hls_dir, ignore_errors=True)
                return DownloadOutcome(
                    success=True,
                    local_path=final,
                    file_size=final_size,
                    quality=current_quality,
                    download_url=url,
                )

            except aiohttp.ClientResponseError as err:
                last_error = f"HTTP {err.status}"
                if attempt < attempts - 1 and err.status in {403, 410, 472}:
                    emit("INFO", f"{title}: HLS 签名链接疑似过期，重新解析链接并续传剩余分片")
                    await until_stopped(refresh_playlist_url(), stop)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                last_error = f"HLS 分片下载中断: {type(err).__name__}"
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
