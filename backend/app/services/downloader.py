"""Streaming MP4 downloader with true HTTP Range resume and progress callbacks."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, replace
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterator, Optional, Set, TypeVar
from urllib.parse import unquote_plus, urlsplit

import aiohttp

from ..cancel import StopRequested, until_stopped
from ..config import SelectorSettings
from ..parsers.http_session import TaskHttpContext
from ..parsers.video import fetch_single_video_detail
from .filename import is_valid_video_file, is_within_directory, normalized_quality, video_filename_base
from .http_proxy import effective_request_proxy, open_media_response

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
_T = TypeVar("_T")
_MIB = 1024 * 1024
_RANGE_BYTES = 4 * _MIB
_RANGE_READ_BYTES = 512 * 1024
_RANGE_READ_BUFSIZE = 512 * 1024
_MAX_CHECKPOINT_BYTES = 16_384
_MAX_CHECKPOINT_RANGES = 128
_MAX_FILE_BYTES = (1 << 63) - 1
_CHECKPOINT_WRITE_INTERVAL = 3.0
_EXPIRED_MEDIA_CODES = {403, 410, 472}
_SIGNED_QUERY_KEYS = frozenset({"token", "sig", "signature", "expires", "validfrom", "validto", "hash", "hdnea"})


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


class _RetrySingleStream(_RangeMismatch):
    """A response established that parallel requests cannot be combined safely."""


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
            @contextlib.contextmanager
            def writing(size: int) -> Iterator[None]:
                nonlocal last_free, written_since_check, remaining
                with self._lock:
                    others = self._reserved - remaining
                    projected_free = last_free - written_since_check - size
                    if (
                        written_since_check + size >= self._CHECK_INTERVAL_BYTES
                        or projected_free < self.keep_free_bytes + others + self._STRICT_NEAR_FLOOR_BYTES
                    ):
                        last_free = shutil.disk_usage(self.path).free
                        written_since_check = 0
                        if last_free < self.keep_free_bytes + others + size:
                            raise _DiskSpaceError(
                                "写入会低于磁盘保留空间，已保留下载断点",
                                required_bytes=max(size, remaining),
                                during_write=True,
                            )
                    written_since_check += size
                    reserved_for_write = min(size, remaining)
                    remaining -= reserved_for_write
                    self._reserved += size - reserved_for_write
                try:
                    yield
                finally:
                    with self._lock:
                        self._reserved -= size

            yield _DiskWriteGuard(writing)
        finally:
            with self._lock:
                self._reserved -= remaining


@dataclass(frozen=True)
class _DiskWriteGuard:
    writing: Callable[[int], contextlib.AbstractContextManager[None]]

    def __call__(self, size: int) -> None:
        # Legacy observers such as yt-dlp report the bytes after their own write.
        with self.writing(size):
            pass


def _disk_write(prepare_write: Callable[[int], None], size: int) -> contextlib.AbstractContextManager[None]:
    if isinstance(prepare_write, _DiskWriteGuard):
        return prepare_write.writing(size)
    prepare_write(size)
    return contextlib.nullcontext()


def _write_media_chunk(fp, chunk: bytes, prepare_write: Callable[[int], None]) -> None:
    with _disk_write(prepare_write, len(chunk)):
        if fp.write(chunk) != len(chunk):
            raise OSError("未能写入全部下载数据")


async def _await_file_io(task: asyncio.Task[_T]) -> _T:
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancelling an await never stops a running thread. Keep the file open
        # until that thread has finished, including on repeated cancellation.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


@dataclass(frozen=True)
class DownloadCheckpoint:
    viewkey: str
    quality: str
    request_fingerprint: str
    response_fingerprint: str
    etag: str
    total: int
    completed_ranges: Optional[list[list[int]]] = None

    def saved_bytes(self, file_size: int) -> int:
        if file_size > self.total:
            return 0
        if self.completed_ranges is None:
            return file_size
        if self.completed_ranges and self.completed_ranges[-1][1] > file_size:
            return 0
        return sum(end - start for start, end in self.completed_ranges)


_SATISFIED_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_UNSATISFIED_RANGE = re.compile(r"bytes \*/(\d+)")


def _fingerprint(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _media_resource(url: str):
    """Compare origins, paths, and non-signing query parameters during explicit URL renewal."""
    parsed = urlsplit(url)
    query = "&".join(part for part in parsed.query.split("&")
                     if unquote_plus(part.partition("=")[0]).lower() not in _SIGNED_QUERY_KEYS)
    return parsed._replace(query=query)


def _strong_etag(value: str) -> str:
    candidate = value.strip()
    return candidate if re.fullmatch(r'"[^"\x00-\x20\x7f]*"', candidate) else ""


def _http_date_validator(value: str) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > 64 or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        return ""
    try:
        dt = parsedate_to_datetime(candidate)
    except (TypeError, ValueError, IndexError, OverflowError):
        return ""
    return candidate if dt is not None else ""


def _valid_validator(value: str) -> str:
    """Validate an RFC 9110 ``If-Range`` entity validator (strong ETag or HTTP-date)."""
    return _strong_etag(value) or _http_date_validator(value)


def _response_validator(response) -> tuple[str, bool]:
    """Return ``(validator, rejected_weak_etag)`` for Range / ``If-Range`` verification.

    Prefers a strong ETag, falls back to ``Last-Modified`` (valid RFC 9110 ``If-Range``
    HTTP-date used by CDNs such as PornHub's). Only flags ``rejected_weak_etag=True`` when
    an explicit weak/malformed ETag is returned without a valid ``Last-Modified`` fallback.
    """
    raw_etag = response.headers.get("ETag", "").strip()
    strong = _strong_etag(raw_etag)
    if strong:
        return strong, False
    last_modified = _http_date_validator(response.headers.get("Last-Modified", ""))
    if last_modified:
        return last_modified, False
    return "", bool(raw_etag)


def _checkpoint_path(path: Path) -> Path:
    return path.with_name(path.name + ".json")


def read_download_checkpoint(path: Path) -> Optional[DownloadCheckpoint]:
    try:
        sidecar = _checkpoint_path(path)
        if sidecar.is_symlink() or sidecar.stat().st_size > _MAX_CHECKPOINT_BYTES:
            return None
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        if data.pop("version") != 1:
            return None
        checkpoint = DownloadCheckpoint(**data)
        if type(checkpoint.total) is not int or not 0 <= checkpoint.total <= _MAX_FILE_BYTES or not all(
            isinstance(value, str) for name, value in data.items() if name not in {"total", "completed_ranges"}
        ):
            return None
        ranges = checkpoint.completed_ranges
        if ranges is not None:
            if not isinstance(ranges, list) or len(ranges) > _MAX_CHECKPOINT_RANGES:
                return None
            previous_end = -1
            for interval in ranges:
                if (not isinstance(interval, list) or len(interval) != 2
                    or any(type(bound) is not int for bound in interval)
                    or not previous_end < interval[0] < interval[1] <= checkpoint.total):
                    return None
                previous_end = interval[1]
        return checkpoint
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return None


def remove_download_checkpoint(path: Path) -> None:
    _checkpoint_path(path).unlink(missing_ok=True)


def write_atomic_json(path: Path, data: dict) -> None:
    draft = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".checkpoint-", delete=False) as handle:
            draft = Path(handle.name)
            json.dump(data, handle)
        os.replace(draft, path)
    finally:
        if draft is not None:
            draft.unlink(missing_ok=True)


def _write_checkpoint(path: Path, checkpoint: DownloadCheckpoint) -> None:
    data = asdict(checkpoint)
    if checkpoint.completed_ranges is None:
        data.pop("completed_ranges")
    else:
        data["completed_ranges"] = data["completed_ranges"][:_MAX_CHECKPOINT_RANGES]
    write_atomic_json(_checkpoint_path(path), {"version": 1, **data})


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _range_chunk_size(total: int) -> int:
    if total >= 256 * _MIB:
        return 12 * _MIB
    if total >= 64 * _MIB:
        return 8 * _MIB
    return _RANGE_BYTES


def _missing_ranges(total: int, completed: list[list[int]]) -> Iterator[tuple[int, int]]:
    chunk = _range_chunk_size(total)
    start = 0
    for left, right in [*completed, [total, total]]:
        for offset in range(start, left, chunk):
            yield offset, min(offset + chunk, left)
        start = right


def _add_completed_range(completed: list[list[int]], start: int, end: int) -> None:
    left = 0
    while left < len(completed) and completed[left][1] < start:
        left += 1
    right = left
    while right < len(completed) and completed[right][0] <= end:
        start = min(start, completed[right][0])
        end = max(end, completed[right][1])
        right += 1
    completed[left:right] = [[start, end]]


def _content_length(response) -> Optional[int]:
    length = response.headers.get("Content-Length", "").strip()
    return int(length) if length.isdigit() else None


def _identity_encoding(response) -> bool:
    return response.headers.get("Content-Encoding", "identity").strip().lower() in {"", "identity"}


def _response_range(response) -> tuple[int, int, int]:
    span = _SATISFIED_RANGE.fullmatch(response.headers.get("Content-Range", "").strip())
    if not span:
        raise _RangeMismatch("HTTP 206 缺少有效的字节范围")
    start, last, total = map(int, span.groups())
    end = last + 1
    if not start < end <= total <= _MAX_FILE_BYTES:
        raise _RangeMismatch("HTTP 206 返回了无效的字节区间")
    length = _content_length(response)
    if "Content-Length" in response.headers and length != end - start:
        raise _RangeMismatch("HTTP 206 响应体长度与字节区间不符")
    return start, end, total


def _progress_tracker(callback: Optional[ProgressCallback], downloaded: int, total: int):
    window: collections.deque[tuple[float, int]] = collections.deque()
    window_bytes = 0
    last_tick = 0.0
    if callback:
        callback(downloaded, total, 0.0)

    def advance(count: int) -> None:
        nonlocal downloaded, window_bytes, last_tick
        downloaded += count
        if not callback:
            return
        now = time.monotonic()
        if count > 0:
            if window and now - window[-1][0] < 0.1:
                tick, acc = window[-1]
                window[-1] = (tick, acc + count)
            else:
                window.append((now, count))
            window_bytes += count
        cutoff = now - 2.0
        while window and window[0][0] < cutoff:
            _, old = window.popleft()
            window_bytes -= old
        if last_tick > 0.0 and (now - last_tick) < 0.25:
            return
        span = max(0.25, now - window[0][0]) if window else 0.25
        speed = max(0.0, window_bytes / span) if window else 0.0
        last_tick = now
        callback(downloaded, total, speed)

    return advance


async def _stream_mp4_body(
    response,
    fp,
    expected: Optional[int],
    prepare_write: Callable[[int], None],
    advance: Callable[[int], None],
    stop: asyncio.Event,
    *,
    overlap: bool,
    chunk_size: Optional[int] = None,
) -> int:
    received = 0
    pending: Optional[asyncio.Task[None]] = None
    read_size = chunk_size if chunk_size is not None else (_MIB if overlap else _RANGE_READ_BYTES)

    async def write_chunk(chunk: bytes) -> None:
        await asyncio.to_thread(_write_media_chunk, fp, chunk, prepare_write)
        advance(len(chunk))

    try:
        async for chunk in response.content.iter_chunked(read_size):
            if pending is not None:
                previous, pending = pending, None
                await _await_file_io(previous)
            if stop.is_set():
                raise StopRequested
            if expected is not None and received + len(chunk) > expected:
                raise _RangeMismatch("下载响应超过声明的字节范围")
            received += len(chunk)
            pending = asyncio.create_task(write_chunk(chunk))
            if not overlap:
                previous, pending = pending, None
                await _await_file_io(previous)
            del chunk
    finally:
        if pending is not None:
            await _await_file_io(pending)
    if expected is not None and received != expected:
        raise aiohttp.ClientPayloadError(f"下载不完整 ({received}/{expected})")
    return received


async def probe_download_size(
    session: aiohttp.ClientSession,
    download_url: str,
    *,
    referer_url: str,
    cookie_str: str = "",
    proxy_url: Optional[str] = None,
    timeout: float = 20.0,
    site: Optional[str] = None,
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
        async with open_media_response(
            session,
            download_url,
            headers=headers,
            site=site,
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
    range_connections: int = 1,
    range_semaphore: Optional[asyncio.Semaphore] = None,
    min_multi_range_bytes: int = 24 * _MIB,
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
    if partial_path is not None and not partial_path.is_symlink() and is_within_directory(partial_path, temp_dir):
        if partial_path.name.endswith(".mp4.tmp") and partial_path.is_file():
            part = partial_path

    url = (download_url or "").strip()
    referer = (referer_url or page_url or "https://www.pornhub.com/").strip()
    current_quality = (quality or "").strip()
    initial_checkpoint = None if part.is_symlink() else read_download_checkpoint(part)
    if not current_quality and initial_checkpoint and initial_checkpoint.viewkey == viewkey:
        current_quality = initial_checkpoint.quality
    disk = disk_budget or DownloadDiskBudget(temp_dir, 0)
    owned_parts: Set[Path] = set()
    superseded_parts: Set[Path] = set()
    force_fresh = False
    connections = max(1, min(4, int(range_connections)))
    parallel_enabled = connections > 1
    validated_response_url = ""
    renewed_from: Optional[tuple[str, str]] = None

    def emit(level: str, message: str) -> None:
        if on_log:
            on_log(level, message)

    def incomplete(error: str, *, cancelled: bool = False, disk_wait_bytes: int = 0) -> DownloadOutcome:
        try:
            size = _size(part)
            checkpoint = read_download_checkpoint(part) if size else None
            if checkpoint and checkpoint.completed_ranges is not None:
                size = checkpoint.saved_bytes(size)
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
        if not default_part.exists() and not default_part.is_symlink():
            return default_part
        # Keep the random suffix after the viewkey, so legacy file listings can still identify it.
        fd, name = tempfile.mkstemp(prefix=f"{base_name}.", suffix=".mp4.tmp", dir=temp_dir)
        os.close(fd)
        return Path(name)

    async def refresh_link() -> bool:
        nonlocal url, referer, current_quality, renewed_from
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
        renewed_from = (url, validated_response_url or url) if (
            url and locked and detail.viewkey == viewkey
            and normalized_quality(detail.quality or current_quality) == locked
        ) else None
        url = detail.download_url
        referer = detail.referer_url or referer
        current_quality = detail.quality or current_quality
        return True

    def fresh_part() -> None:
        nonlocal part, force_fresh
        if part.is_symlink():
            part = new_part()
        elif _size(part):
            previous = read_download_checkpoint(part)
            if part not in owned_parts or (previous and previous.completed_ranges is not None):
                if (previous and previous.viewkey == viewkey
                    and normalized_quality(previous.quality) == normalized_quality(current_quality)):
                    superseded_parts.add(part)
                part = new_part()
                emit("INFO", f"无法安全续接，保留原断点并重新下载: {title}")
        owned_parts.add(part)
        force_fresh = False

    def media_request(start: Optional[int], end: Optional[int], etag: str, *, pooled: bool):
        headers = {**DOWNLOAD_HEADERS, "Referer": referer}
        if cookie_str:
            headers["Cookie"] = cookie_str
        if start is not None:
            headers["Range"] = f"bytes={start}-{'' if end is None else end - 1}"
            if etag:
                headers["If-Range"] = etag
        return open_media_response(
            session, url, headers=headers, site=getattr(parse_ctx, "site", None),
            proxy=effective_request_proxy(session, proxy_url), auto_decompress=False,
            **({"read_bufsize": _RANGE_READ_BUFSIZE} if pooled else {}),
        )

    async def receive_remaining_ranges(
        checkpoint: DownloadCheckpoint,
        completed: list[list[int]],
        prepare_write: Callable[[int], None],
        advance: Callable[[int], None],
    ) -> None:
        requests = _missing_ranges(checkpoint.total, completed.copy())
        last_checkpoint = time.monotonic()

        async def worker() -> None:
            nonlocal last_checkpoint
            with open(part, "r+b", buffering=0) as fp:
                for start, end in requests:
                    async with range_semaphore or contextlib.nullcontext():
                        async with media_request(start, end, checkpoint.etag, pooled=True) as response:
                            if response.status not in {200, 206, 412, 416}:
                                response.raise_for_status()
                            try:
                                resp_validator, resp_weak = _response_validator(response)
                                if (
                                    response.status != 206
                                    or not _identity_encoding(response)
                                    or resp_weak
                                    or resp_validator != checkpoint.etag
                                    or _fingerprint(str(getattr(response, "url", url))) != checkpoint.response_fingerprint
                                    or _response_range(response) != (start, end, checkpoint.total)
                                ):
                                    raise _RangeMismatch("并行下载的 HTTP 字节区间或校验标记已变化")
                                fp.seek(start)
                                await _stream_mp4_body(
                                    response, fp, end - start, prepare_write, advance, stop,
                                    overlap=True, chunk_size=_RANGE_READ_BYTES,
                                )
                            except _RangeMismatch as err:
                                raise _RetrySingleStream(str(err)) from err
                    _add_completed_range(completed, start, end)
                    now = time.monotonic()
                    if now - last_checkpoint >= _CHECKPOINT_WRITE_INTERVAL:
                        _write_checkpoint(part, checkpoint)
                        last_checkpoint = now

        if completed == [[0, checkpoint.total]]:
            return
        workers = [asyncio.create_task(worker()) for _ in range(connections)]
        try:
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            _write_checkpoint(part, checkpoint)
        if completed != [[0, checkpoint.total]]:
            raise aiohttp.ClientPayloadError("并行下载仍有未完成的字节区间")

    async def receive(*, allow_probe: bool = True) -> int:
        nonlocal validated_response_url, parallel_enabled
        offset = 0 if part.is_symlink() else _size(part)
        checkpoint = read_download_checkpoint(part) if offset and not force_fresh else None
        request_fingerprint = _fingerprint(url)
        renewed = bool(
            renewed_from and checkpoint and checkpoint.completed_ranges is not None
            and checkpoint.request_fingerprint == _fingerprint(renewed_from[0])
            and checkpoint.response_fingerprint == _fingerprint(renewed_from[1])
            and _media_resource(renewed_from[0]) == _media_resource(url)
        )
        if not (
            checkpoint and checkpoint.viewkey == viewkey
            and normalized_quality(checkpoint.quality) == normalized_quality(current_quality)
            and (checkpoint.request_fingerprint == request_fingerprint or renewed)
            and _valid_validator(checkpoint.etag) and 0 < offset <= checkpoint.total
            and (not checkpoint.completed_ranges or checkpoint.completed_ranges[-1][1] <= offset)
        ):
            checkpoint = None
            offset = 0

        sparse = checkpoint is not None and checkpoint.completed_ranges is not None
        completed = checkpoint.completed_ranges if sparse else ([[0, offset]] if offset else [])
        bounded = sparse or (
            parallel_enabled and allow_probe and not force_fresh
            and (checkpoint is None or min_multi_range_bytes <= checkpoint.total)
        )
        if bounded:
            first = next(_missing_ranges(checkpoint.total, completed), None) if checkpoint else (0, _RANGE_BYTES)
            start, end = first if first is not None else (checkpoint.total, None)
        else:
            start, end = (offset if checkpoint else None), None

        ranged_checkpoint = None
        with contextlib.ExitStack() as reservations:
            async with range_semaphore if bounded and range_semaphore is not None else contextlib.nullcontext():
                async with media_request(start, end, checkpoint.etag if checkpoint else "", pooled=sparse) as response:
                    if response.status == 412 and bounded:
                        raise _RetrySingleStream("媒体服务拒绝了并行续传校验")
                    if response.status not in {200, 206, 416}:
                        response.raise_for_status()
                        raise _RangeMismatch(f"意外的下载响应 HTTP {response.status}")

                    etag, rejected_weak_etag = _response_validator(response)
                    response_url = str(getattr(response, "url", url))
                    resolved = _fingerprint(response_url)
                    same_entity = bool(checkpoint and etag and etag == checkpoint.etag and (
                        resolved == checkpoint.response_fingerprint
                        or (renewed and renewed_from and _media_resource(renewed_from[1]) == _media_resource(response_url))
                    ))
                    mismatch = _RetrySingleStream if bounded else _RangeMismatch
                    if response.status == 416:
                        span = _UNSATISFIED_RANGE.fullmatch(response.headers.get("Content-Range", "").strip())
                        if same_entity and span and int(span[1]) == checkpoint.total and completed == [[0, checkpoint.total]]:
                            return checkpoint.total
                        raise mismatch("HTTP 416 无法确认断点对应同一完整文件")

                    if not _identity_encoding(response):
                        raise mismatch("媒体响应经过内容编码，不能校验原始字节范围")

                    if response.status == 200:
                        expected = _content_length(response)
                        total = expected or 0
                        write_from = 0
                        # Use this body directly when a server ignored Range.
                        if bounded:
                            parallel_enabled = False
                    else:
                        try:
                            write_from, write_end, total = _response_range(response)
                        except _RangeMismatch as err:
                            raise mismatch(str(err)) from err
                        if start is None or write_from != start or (
                            checkpoint and (not same_entity or total != checkpoint.total)
                        ) or (end is not None and write_end != min(end, total)):
                            raise mismatch("HTTP 206 返回的字节区间或校验标记与断点不符")
                        if write_end < total and (rejected_weak_etag or (not etag and total < min_multi_range_bytes)):
                            # A weak/invalid ETag without Last-Modified cannot be trusted across Range requests,
                            # and small validator-less files should switch straight to one sequential GET.
                            raise mismatch("HTTP 206 缺少可用于续接的原文件校验标记")
                        expected = write_end - write_from
                        if sparse or (bounded and write_end < total and total >= min_multi_range_bytes):
                            ranged_checkpoint = DownloadCheckpoint(
                                viewkey, current_quality, request_fingerprint, resolved, etag, total, completed,
                            )

                    saved = sum(right - left for left, right in completed) if ranged_checkpoint else write_from
                    reserve_bytes = total - saved if ranged_checkpoint else expected or 0
                    prepare_write = reservations.enter_context(disk.reserve(reserve_bytes))
                    if response.status == 200 or checkpoint is None:
                        fresh_part()
                        mode = "wb"
                    else:
                        mode = "r+b"
                    with open(part, mode, buffering=0) as fp:
                        # Truncate any previous entity before publishing the new validator.
                        if ranged_checkpoint or mode == "wb" or renewed:
                            _write_checkpoint(part, ranged_checkpoint or DownloadCheckpoint(
                                viewkey, current_quality, request_fingerprint, resolved, etag, total,
                            ))
                        validated_response_url = response_url
                        advance = _progress_tracker(on_progress, saved, total)
                        fp.seek(write_from)
                        try:
                            received = await _stream_mp4_body(
                                response, fp, expected, prepare_write, advance, stop, overlap=ranged_checkpoint is None,
                            )
                        except _RangeMismatch as err:
                            if bounded:
                                raise _RetrySingleStream(str(err)) from err
                            raise
                    size = write_from + received
                    if ranged_checkpoint:
                        _add_completed_range(completed, write_from, size)
                        _write_checkpoint(part, ranged_checkpoint)

            if ranged_checkpoint is not None:
                await receive_remaining_ranges(ranged_checkpoint, completed, prepare_write, advance)
                size = total

        if total and size != total:
            if bounded and allow_probe:
                # The initial bounded response supplied real bytes for a small file. Continue it sequentially.
                return await receive(allow_probe=False)
            raise aiohttp.ClientPayloadError(f"下载不完整 ({size}/{total})")
        if on_progress:
            on_progress(size, total, 0.0)
        return size

    async def receive_with_fallback() -> int:
        nonlocal force_fresh, parallel_enabled
        try:
            return await receive()
        except _RetrySingleStream as err:
            emit("INFO", f"{title}: {err}，切换为单连接下载")
            force_fresh = True
            parallel_enabled = False
            return await receive(allow_probe=False)

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
                    size = await until_stopped(receive_with_fallback(), stop)
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
                if attempt < attempts - 1 and err.status in _EXPIRED_MEDIA_CODES:
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
