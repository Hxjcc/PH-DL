"""Streaming MP4 downloader with true HTTP Range resume and progress callbacks."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional, Set

import aiohttp

from ..cancel import StopRequested, until_stopped
from ..config import SelectorSettings
from ..parsers.http_session import TaskHttpContext
from ..parsers.video import fetch_single_video_detail
from .filename import is_valid_video_file, normalized_quality, video_filename_base
from .http_proxy import effective_request_proxy

DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}

ProgressCallback = Callable[[int, int, float], None]  # (downloaded_bytes, total_bytes, speed_bps)
LogCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class DownloadOutcome:
    success: bool
    cancelled: bool = False
    local_path: Optional[Path] = None
    temp_path: Optional[Path] = None
    file_size: int = 0
    quality: str = ""
    download_url: str = ""
    error: str = ""
    disk_wait_bytes: int = 0


class _RangeMismatch(Exception):
    pass


class _DiskSpaceError(Exception):
    def __init__(self, message: str, *, required_bytes: int = 0, during_write: bool = False) -> None:
        super().__init__(message)
        self.required_bytes = max(0, int(required_bytes))
        self.during_write = during_write


class DownloadDiskBudget:
    """Reserve known response sizes across downloads and enforce the floor on writes."""

    _CHECK_INTERVAL_BYTES = 4 * 1024 * 1024
    _STRICT_NEAR_FLOOR_BYTES = 64 * 1024 * 1024

    def __init__(self, path: Path, keep_free_bytes: int):
        self.path = path
        self.keep_free_bytes = max(0, int(keep_free_bytes))
        self._reserved = 0
        self._lock = threading.Lock()

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return self._reserved

    def free_bytes(self) -> int:
        return int(shutil.disk_usage(self.path).free)

    @contextlib.contextmanager
    def reserve(self, expected_bytes: int) -> Iterator[Callable[[int], None]]:
        if not self.keep_free_bytes:
            yield lambda _size: None
            return

        remaining = max(0, expected_bytes)
        with self._lock:
            initial_free = shutil.disk_usage(self.path).free
            if initial_free < self.keep_free_bytes + self._reserved + remaining:
                raise _DiskSpaceError("磁盘余量不足，无法为此下载预留空间", required_bytes=remaining)
            self._reserved += remaining
        last_free = initial_free
        written_since_check = 0
        try:
            def before_write(size: int) -> None:
                nonlocal last_free, written_since_check
                with self._lock:
                    others = self._reserved - remaining
                    projected_free = last_free - written_since_check - size
                    need_check = (
                        written_since_check + size >= self._CHECK_INTERVAL_BYTES
                        or projected_free < self.keep_free_bytes + others + self._STRICT_NEAR_FLOOR_BYTES
                    )
                    if need_check:
                        last_free = shutil.disk_usage(self.path).free
                        written_since_check = 0
                        if last_free < self.keep_free_bytes + others + size:
                            raise _DiskSpaceError(
                                "写入会低于磁盘保留空间，已保留下载断点",
                                required_bytes=max(size, remaining),
                                during_write=True,
                            )
                    written_since_check += size

            def consume(size: int) -> None:
                nonlocal remaining
                before_write(size)
                with self._lock:
                    used = min(size, remaining)
                    remaining -= used
                    self._reserved -= used

            yield consume
        finally:
            with self._lock:
                self._reserved -= remaining


@dataclass(frozen=True)
class DownloadCheckpoint:
    viewkey: str
    quality: str
    request_fingerprint: str
    response_fingerprint: str
    etag: str
    total: int


_SATISFIED_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_UNSATISFIED_RANGE = re.compile(r"bytes \*/(\d+)")


def _fingerprint(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _strong_etag(value: str) -> str:
    candidate = value.strip()
    return candidate if re.fullmatch(r'"[^"\x00-\x20\x7f]*"', candidate) else ""


def _checkpoint_path(path: Path) -> Path:
    return path.with_name(path.name + ".json")


def read_download_checkpoint(path: Path) -> Optional[DownloadCheckpoint]:
    try:
        data = json.loads(_checkpoint_path(path).read_text(encoding="utf-8"))
        if data.pop("version") != 1:
            return None
        checkpoint = DownloadCheckpoint(**data)
        if (
            type(checkpoint.total) is int
            and checkpoint.total >= 0
            and all(isinstance(value, str) for name, value in data.items() if name != "total")
        ):
            return checkpoint
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return None


def remove_download_checkpoint(path: Path) -> None:
    _checkpoint_path(path).unlink(missing_ok=True)


def _write_checkpoint(path: Path, checkpoint: DownloadCheckpoint) -> None:
    sidecar = _checkpoint_path(path)
    draft = sidecar.with_suffix(".json.new")
    draft.write_text(json.dumps({"version": 1, **asdict(checkpoint)}), encoding="utf-8")
    os.replace(draft, sidecar)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


async def probe_download_size(
    session: aiohttp.ClientSession,
    download_url: str,
    *,
    referer_url: str,
    cookie_str: str = "",
    proxy_url: Optional[str] = None,
    timeout: float = 20.0,
) -> Optional[int]:
    """Read total media size with a one-byte Range request without downloading the file."""
    if not download_url:
        return None
    if ".m3u8" in download_url.lower():
        return None
    headers = dict(DOWNLOAD_HEADERS)
    headers["Referer"] = referer_url or "https://www.pornhub.com/"
    headers["Range"] = "bytes=0-0"
    if cookie_str:
        headers["Cookie"] = cookie_str
    try:
        async with session.get(
            download_url,
            headers=headers,
            allow_redirects=True,
            proxy=effective_request_proxy(session, proxy_url),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if response.status == 206:
                content_range = _SATISFIED_RANGE.fullmatch(response.headers.get("Content-Range", "").strip())
                if content_range and content_range.group(1, 2) == ("0", "0"):
                    return int(content_range.group(3)) or None
                return None
            if response.status == 200:
                content_length = response.headers.get("Content-Length", "")
                return (
                    int(content_length)
                    if content_length.isdigit() and int(content_length) > 0
                    else None
                )
            return None
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
        return None


async def download_mp4_with_resume(
    *,
    session: aiohttp.ClientSession,
    parse_ctx: TaskHttpContext,
    selectors: SelectorSettings,
    viewkey: str,
    title: str,
    download_url: str,
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
    retry_delay_base: float = 2.0,
    retry_delay_max: float = 10.0,
    request_timeout: Optional[float] = None,
    min_file_size_bytes: int = 10240,
    cancel_event: Optional[asyncio.Event] = None,
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
) -> DownloadOutcome:
    """Only append ranges proven to belong to the previous HTTP representation."""
    stop = cancel_event if cancel_event is not None else parse_ctx.cancel_event
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", viewkey or ""):
        return DownloadOutcome(success=False, error="缺少或无效的 viewkey")

    base_name = video_filename_base(title, viewkey)
    default_part = temp_dir / f"{base_name}.mp4.tmp"
    part = default_part
    if partial_path is not None and partial_path.resolve().is_relative_to(temp_dir.resolve()):
        if partial_path.name.endswith(".mp4.tmp") and partial_path.is_file():
            part = partial_path

    url = (download_url or "").strip()
    referer = (referer_url or page_url or "https://www.pornhub.com/").strip()
    current_quality = (quality or "").strip()
    initial_checkpoint = read_download_checkpoint(part)
    if not current_quality and initial_checkpoint and initial_checkpoint.viewkey == viewkey:
        current_quality = initial_checkpoint.quality
    disk = disk_budget or DownloadDiskBudget(temp_dir, 0)
    owned_parts: Set[Path] = set()
    superseded_parts: Set[Path] = set()
    force_fresh = False

    def emit(level: str, message: str) -> None:
        if on_log:
            on_log(level, message)

    def incomplete(error: str, *, cancelled: bool = False, disk_wait_bytes: int = 0) -> DownloadOutcome:
        try:
            size = _size(part)
        except OSError:
            size = 0
        if not size and part in owned_parts:
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
                remove_download_checkpoint(part)
        return DownloadOutcome(
            success=False,
            cancelled=cancelled,
            temp_path=part if size else None,
            file_size=size,
            quality=current_quality,
            download_url=url,
            error=error,
            disk_wait_bytes=max(0, int(disk_wait_bytes)),
        )

    def new_part() -> Path:
        # Never truncate an earlier download while restarting with a different/unknown source.
        if not default_part.exists():
            return default_part
        # Keep the random suffix after the viewkey, so legacy file listings can still identify it.
        fd, name = tempfile.mkstemp(prefix=f"{base_name}.", suffix=".mp4.tmp", dir=temp_dir)
        os.close(fd)
        return Path(name)

    async def refresh_link() -> bool:
        nonlocal url, referer, current_quality
        target_page = page_url or referer
        locked = normalized_quality(current_quality) if _size(part) else ""
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

    async def receive() -> int:
        nonlocal part, force_fresh
        offset = _size(part)
        checkpoint = read_download_checkpoint(part) if offset else None
        if not (
            checkpoint and not force_fresh and checkpoint.viewkey == viewkey
            and normalized_quality(checkpoint.quality) == normalized_quality(current_quality)
            and checkpoint.request_fingerprint == _fingerprint(url)
            and _strong_etag(checkpoint.etag) and 0 < offset <= checkpoint.total
        ):
            checkpoint = None

        headers = {**DOWNLOAD_HEADERS, "Referer": referer}
        if cookie_str:
            headers["Cookie"] = cookie_str
        if checkpoint:
            headers["Range"] = f"bytes={offset}-"
            headers["If-Range"] = checkpoint.etag

        async with session.get(url, headers=headers, allow_redirects=True, proxy=effective_request_proxy(session, proxy_url),
                               auto_decompress=False) as response:
            if response.status not in {200, 206, 416}:
                response.raise_for_status()
                raise _RangeMismatch(f"意外的下载响应 HTTP {response.status}")
            etag = _strong_etag(response.headers.get("ETag", ""))
            resolved = _fingerprint(str(getattr(response, "url", url)))
            same_entity = bool(checkpoint and etag == checkpoint.etag and resolved == checkpoint.response_fingerprint)
            range_header = response.headers.get("Content-Range", "").strip()
            if response.status == 416:
                span = _UNSATISFIED_RANGE.fullmatch(range_header)
                if same_entity and span and int(span[1]) == offset == checkpoint.total:
                    return offset
                raise _RangeMismatch("HTTP 416 无法确认断点对应同一完整文件")

            encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
            if encoding not in {"", "identity"}:
                raise _RangeMismatch("媒体响应经过内容编码，不能校验原始字节范围")
            length = response.headers.get("Content-Length", "").strip()
            expected = int(length) if length.isdigit() else 0

            if response.status == 206:
                span = _SATISFIED_RANGE.fullmatch(range_header)
                if not same_entity or not span:
                    raise _RangeMismatch("HTTP 206 缺少有效的范围或原文件校验标记")
                start, end, total = map(int, span.groups())
                if start != offset or not start <= end < total or total != checkpoint.total:
                    raise _RangeMismatch("HTTP 206 返回的字节区间或总大小与断点不符")
                span_bytes = end - start + 1
                if length and expected != span_bytes:
                    raise _RangeMismatch("HTTP 206 响应体长度与字节区间不符")
                expected = span_bytes
                mode = "ab"
            else:
                start = 0
                total = expected
                mode = "wb"

            with disk.reserve(expected) as prepare_write:
                if mode == "wb":
                    if _size(part) and part not in owned_parts:
                        prev_cp = read_download_checkpoint(part)
                        if (
                            prev_cp is not None
                            and prev_cp.viewkey == viewkey
                            and normalized_quality(prev_cp.quality) == normalized_quality(current_quality)
                        ):
                            # Same video & quality written by an earlier pass/attempt: keep intact
                            # during retry, then remove automatically once the new file validates.
                            superseded_parts.add(part)
                        part = new_part()
                        emit("INFO", f"无法安全续接，保留原断点并重新下载: {title}")
                    owned_parts.add(part)
                    _write_checkpoint(part, DownloadCheckpoint(
                        viewkey, current_quality, _fingerprint(url), resolved, etag, total,
                    ))
                    force_fresh = False

                downloaded = start
                bytes_since_tick = 0
                last_tick = time.monotonic()
                if on_progress:
                    on_progress(downloaded, total, 0.0)
                # Unbuffered writes keep the disk free-space check aligned with written bytes;
                # offload large chunks to a worker thread so 1000 Mbps disk I/O never stalls the loop.
                with open(part, mode, buffering=0) as fp:
                    async for chunk in response.content.iter_chunked(2 * 1024 * 1024):
                        if stop.is_set():
                            raise StopRequested
                        if expected and downloaded - start + len(chunk) > expected:
                            raise _RangeMismatch("下载响应超过声明的字节范围")
                        prepare_write(len(chunk))
                        if len(chunk) >= 512 * 1024:
                            write_task = asyncio.create_task(asyncio.to_thread(fp.write, chunk))
                            try:
                                written = await write_task
                            except BaseException:
                                with contextlib.suppress(Exception):
                                    await write_task
                                raise
                        else:
                            written = fp.write(chunk)
                        if written != len(chunk):
                            raise OSError("未能写入全部下载数据")
                        downloaded += len(chunk)
                        bytes_since_tick += len(chunk)
                        now = time.monotonic()
                        if on_progress and now - last_tick >= 0.35:
                            on_progress(downloaded, total, bytes_since_tick / (now - last_tick))
                            last_tick, bytes_since_tick = now, 0
                if on_progress:
                    on_progress(downloaded, total, 0.0)
                if total and downloaded != total:
                    raise aiohttp.ClientPayloadError(f"下载不完整 ({downloaded}/{total})")
                return downloaded

    attempts = max(1, int(max_attempts))
    last_error = "下载失败"
    try:
        if stop.is_set():
            raise StopRequested
        download_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        for attempt in range(attempts):
            if stop.is_set():
                raise StopRequested
            try:
                if not url and not await until_stopped(refresh_link(), stop):
                    last_error = "无法解析视频直链"
                else:
                    size = await until_stopped(receive(), stop)
                    if await asyncio.to_thread(is_valid_video_file, part, min_file_size_bytes, size):
                        if stop.is_set():
                            raise StopRequested
                        final = download_dir / f"{base_name}.mp4"
                        if final.exists() and not replace_existing:
                            # The other file may be a different quality or have unknown provenance.
                            tag = normalized_quality(current_quality)
                            stem = f"{base_name}.{tag + 'p' if tag else 'download'}"
                            final = download_dir / f"{stem}.mp4"
                            suffix = 2
                            while final.exists():
                                final = download_dir / f"{stem}.{suffix}.mp4"
                                suffix += 1
                        os.replace(part, final)
                        with contextlib.suppress(OSError):
                            remove_download_checkpoint(part)
                        for stale in (owned_parts | superseded_parts) - {part}:
                            with contextlib.suppress(OSError):
                                stale.unlink(missing_ok=True)
                                remove_download_checkpoint(stale)
                        return DownloadOutcome(success=True, local_path=final, file_size=size,
                                               quality=current_quality, download_url=url)
                    last_error = "文件未通过完整性校验（大小或 MP4 结构不完整）"
                    if part in owned_parts:
                        part.unlink(missing_ok=True)
                        remove_download_checkpoint(part)
                    force_fresh = True
            except _RangeMismatch as err:
                last_error = str(err)
                force_fresh = True
            except aiohttp.ClientResponseError as err:
                last_error = f"HTTP {err.status}"
                if attempt < attempts - 1 and err.status in {403, 410, 472}:
                    emit("INFO", f"{title}: 直链疑似过期，重新解析直链")
                    await until_stopped(refresh_link(), stop)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                last_error = f"下载连接中断: {type(err).__name__}"
            except ValueError as err:
                last_error = f"代理或下载参数无效: {err}"
            except _DiskSpaceError as err:
                return incomplete(str(err), disk_wait_bytes=err.required_bytes)

            if attempt < attempts - 1:
                emit("WARNING", f"{title}: {last_error}，准备重试")
                await until_stopped(asyncio.sleep(min(retry_delay_base * (2 ** attempt), retry_delay_max)), stop)
    except StopRequested:
        return incomplete("已取消", cancelled=True)
    except OSError as err:
        return incomplete(f"文件读写失败: {err.strerror or type(err).__name__}")
    return incomplete(last_error)
