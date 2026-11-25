"""Byte-level adaptive disk scheduler and priority upload queue for high-throughput pipelines."""

from __future__ import annotations

import asyncio
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..services.downloader import DownloadDiskBudget

GIB = 1024 ** 3
MIB = 1024 ** 2


@dataclass
class UploadQueuedItem:
    item_id: int
    viewkey: str
    uploader_folder: str
    download_dir: Path
    local_path: Path
    page_url: str = ""
    file_size: int = 0
    enqueued_seq: int = 0
    published_at: str = ""


class AdaptiveDiskScheduler:
    """Dynamically regulates download concurrency and upload priority based on effective free disk bytes."""

    MIN_EST_VIDEO_BYTES = 256 * MIB
    MAX_EST_VIDEO_BYTES = 3 * GIB
    DEFAULT_EST_VIDEO_BYTES = 1 * GIB
    GREEN_MIN_BYTES = 8 * GIB
    YELLOW_MIN_BYTES = 3 * GIB

    def __init__(
        self,
        data_dir: Path,
        disk_budget: DownloadDiskBudget,
        *,
        max_downloads: int,
        max_uploads: int,
        min_free_disk_gb: float,
        max_pending_upload_files: int = 20,
        disk_usage_fn: Optional[Callable[[Path], Any]] = None,
        on_status: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.data_dir = data_dir
        self.disk_budget = disk_budget
        self.max_downloads = max(1, int(max_downloads))
        self.max_uploads = max(1, int(max_uploads))
        self.min_free_disk_gb = max(0.0, float(min_free_disk_gb))
        self.keep_free_bytes = int(self.min_free_disk_gb * GIB)
        self.max_pending_upload_files = max(1, int(max_pending_upload_files))
        self._disk_usage_fn = disk_usage_fn or shutil.disk_usage
        self._on_status = on_status

        self._cond = asyncio.Condition()
        self.active_downloads = 0
        self.active_uploads = 0
        self.pending_upload_count = 0
        self.pending_upload_bytes = 0
        self.avg_video_bytes = self.DEFAULT_EST_VIDEO_BYTES
        self._waiting_large: List[int] = []
        self._last_emit_at = 0.0
        self._last_signature: Optional[Tuple[Any, ...]] = None

    def free_bytes(self) -> int:
        try:
            return max(0, int(self._disk_usage_fn(self.data_dir).free))
        except OSError:
            return 0

    def effective_headroom_bytes(self) -> int:
        free = self.free_bytes()
        if self.keep_free_bytes <= 0:
            return free
        return free - self.keep_free_bytes - self.disk_budget.reserved_bytes

    def compute_waterline(self) -> Tuple[str, int, int, int]:
        """Return (level, allowed_downloads, free_bytes, effective_headroom_bytes)."""
        free = self.free_bytes()
        if self.keep_free_bytes <= 0:
            return "green", self.max_downloads, free, free

        headroom = free - self.keep_free_bytes - self.disk_budget.reserved_bytes
        green_threshold = max(self.GREEN_MIN_BYTES, int(3.5 * self.avg_video_bytes))
        yellow_threshold = max(self.YELLOW_MIN_BYTES, int(1.8 * self.avg_video_bytes))

        if headroom <= 0:
            return "red", 0, free, headroom
        if headroom < yellow_threshold:
            return "orange", 1, free, headroom
        if headroom < green_threshold:
            allowed = min(self.max_downloads, max(2, self.max_downloads // 2))
            return "yellow", allowed, free, headroom
        return "green", self.max_downloads, free, headroom

    @property
    def prefer_largest_upload(self) -> bool:
        if self._waiting_large:
            return True
        level, _, _, _ = self.compute_waterline()
        return level in {"yellow", "orange", "red"}

    def record_video_size(self, size_bytes: int) -> None:
        if size_bytes <= 0:
            return
        clamped = max(self.MIN_EST_VIDEO_BYTES, min(self.MAX_EST_VIDEO_BYTES, int(size_bytes)))
        self.avg_video_bytes = int(self.avg_video_bytes * 0.7 + clamped * 0.3)

    def snapshot(self) -> Dict[str, Any]:
        level, allowed, free, headroom = self.compute_waterline()
        return {
            "level": level,
            "active_downloads": self.active_downloads,
            "allowed_downloads": allowed,
            "max_downloads": self.max_downloads,
            "active_uploads": self.active_uploads,
            "max_uploads": self.max_uploads,
            "pending_uploads": self.pending_upload_count,
            "pending_upload_bytes": self.pending_upload_bytes,
            "free_bytes": free,
            "headroom_bytes": max(0, headroom),
            "reserved_bytes": self.disk_budget.reserved_bytes,
            "waiting_for_space": len(self._waiting_large),
            "prefer_largest_upload": self.prefer_largest_upload,
        }

    @staticmethod
    def _status_signature(snap: Dict[str, Any]) -> Tuple[Any, ...]:
        return (
            snap["level"],
            snap["active_downloads"],
            snap["allowed_downloads"],
            snap["active_uploads"],
            snap["pending_uploads"],
            snap["waiting_for_space"],
        )

    def emit_status(self, *, force: bool = False) -> None:
        if self._on_status is None:
            return
        now = time.monotonic()
        snap = self.snapshot()
        sig = self._status_signature(snap)
        state_changed = sig != self._last_signature
        if not force and not state_changed and (now - self._last_emit_at) < 0.8:
            return
        self._last_emit_at = now
        self._last_signature = sig
        self._on_status(snap)

    async def notify_changed(self, *, force_emit: bool = False) -> None:
        async with self._cond:
            self._cond.notify_all()
        self.emit_status(force=force_emit)

    async def acquire_download_slot(
        self,
        *,
        will_upload: bool,
        required_bytes: int = 0,
        cancel_event: asyncio.Event,
        on_wait: Optional[Callable[[str], None]] = None,
        on_request_flush: Optional[Callable[[], None]] = None,
    ) -> Tuple[bool, str]:
        """Acquire a download slot, dynamically throttling or suspending until uploads free space."""
        req = max(0, int(required_bytes))
        if not will_upload:
            if self.keep_free_bytes > 0:
                free = self.free_bytes()
                headroom = free - self.keep_free_bytes - self.disk_budget.reserved_bytes
                if free < self.keep_free_bytes or (req > 0 and headroom < req):
                    return (
                        False,
                        f"VPS 磁盘可用空间不足（{free / GIB:.2f} GB < {self.min_free_disk_gb:.2f} GB），已停止继续下载。",
                    )
            async with self._cond:
                while not cancel_event.is_set() and self.active_downloads >= self.max_downloads:
                    await self._wait_with_cancel(cancel_event, timeout=1.0)
                if cancel_event.is_set():
                    return False, "已取消"
                self.active_downloads += 1
            self.emit_status()
            return True, ""

        logged_wait = False
        registered_large = False
        try:
            async with self._cond:
                if req > 0:
                    self._waiting_large.append(req)
                    registered_large = True

                while not cancel_event.is_set():
                    level, allowed, free, headroom = self.compute_waterline()
                    too_many_pending = self.pending_upload_count >= self.max_pending_upload_files

                    # Large-file fairness lock: don't let new unknown-size downloads starve a waiting large file.
                    blocked_by_large_waiter = bool(self._waiting_large) and req == 0

                    if req > 0:
                        fits = (
                            (self.keep_free_bytes <= 0 or headroom >= req)
                            and self.active_downloads < max(1, allowed)
                            and not too_many_pending
                        )
                    else:
                        fits = (
                            not blocked_by_large_waiter
                            and (self.keep_free_bytes <= 0 or headroom > 0)
                            and self.active_downloads < allowed
                            and not too_many_pending
                        )

                    if fits:
                        if registered_large:
                            self._waiting_large.remove(req)
                            registered_large = False
                        self.active_downloads += 1
                        self.emit_status()
                        return True, ""

                    # True physical disk limit: nothing in flight or queued for upload can free space.
                    no_releasable_work = self.pending_upload_count == 0 and self.active_downloads == 0
                    if no_releasable_work and self.keep_free_bytes > 0:
                        if req > 0 and headroom < req:
                            return (
                                False,
                                f"单文件体积 ({req / GIB:.2f} GB) 超过 VPS 可用空间"
                                f"（当前剩余 {free / GIB:.2f} GB，保留红线 {self.min_free_disk_gb:.1f} GB）",
                            )
                        if headroom <= 0:
                            return (
                                False,
                                f"VPS 磁盘可用空间不足（剩余 {free / GIB:.2f} GB < 保留红线 {self.min_free_disk_gb:.1f} GB）",
                            )

                    if on_request_flush is not None:
                        on_request_flush()

                    if not logged_wait:
                        logged_wait = True
                        if req > 0:
                            msg = (
                                f"VPS 剩余空间紧凑（余量 {max(0, headroom) / GIB:.2f} GB < 需求 {req / GIB:.2f} GB），"
                                "暂停该下载并优先上传大文件释放空间..."
                            )
                        elif too_many_pending:
                            msg = "待上传本地文件达到背压上限，正在等待上传释放队列..."
                        elif blocked_by_large_waiter:
                            msg = "正在为等待中的大视频腾出磁盘空间，暂缓开启新下载..."
                        else:
                            msg = (
                                f"VPS 磁盘水位进入 {level} 区（余量 {max(0, headroom) / GIB:.2f} GB），"
                                f"动态调整下载并发为 {allowed}/{self.max_downloads}，等待上传释放空间..."
                            )
                        if on_wait is not None:
                            on_wait(msg)
                        self.emit_status(force=True)

                    await self._wait_with_cancel(cancel_event, timeout=1.0)
            return False, "已取消"
        finally:
            if registered_large:
                async with self._cond:
                    if req in self._waiting_large:
                        self._waiting_large.remove(req)
                    self._cond.notify_all()
                self.emit_status()

    async def release_download_slot(self) -> None:
        async with self._cond:
            self.active_downloads = max(0, self.active_downloads - 1)
            self._cond.notify_all()
        self.emit_status()

    async def on_upload_queued(self, size_bytes: int) -> None:
        size = max(0, int(size_bytes))
        self.record_video_size(size)
        async with self._cond:
            self.pending_upload_count += 1
            self.pending_upload_bytes += size
            self._cond.notify_all()
        self.emit_status()

    async def on_upload_started(self) -> None:
        async with self._cond:
            self.active_uploads += 1
        self.emit_status()

    async def on_upload_finished(self, count: int, total_bytes: int) -> None:
        async with self._cond:
            self.active_uploads = max(0, self.active_uploads - 1)
            self.pending_upload_count = max(0, self.pending_upload_count - max(0, count))
            self.pending_upload_bytes = max(0, self.pending_upload_bytes - max(0, int(total_bytes)))
            self._cond.notify_all()
        self.emit_status(force=True)

    async def on_upload_discarded(self, count: int, total_bytes: int) -> None:
        async with self._cond:
            self.pending_upload_count = max(0, self.pending_upload_count - max(0, count))
            self.pending_upload_bytes = max(0, self.pending_upload_bytes - max(0, int(total_bytes)))
            self._cond.notify_all()
        self.emit_status()

    async def _wait_with_cancel(self, cancel_event: asyncio.Event, timeout: float = 1.0) -> None:
        """Must be called while holding self._cond."""
        if cancel_event.is_set():
            return
        cond_wait = asyncio.create_task(self._cond.wait())
        cancel_wait = asyncio.create_task(cancel_event.wait())
        try:
            done, pending = await asyncio.wait(
                {cond_wait, cancel_wait},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        except BaseException:
            cond_wait.cancel()
            cancel_wait.cancel()
            raise


class SmartUploadPool:
    """Thread-safe asyncio upload queue supporting zero-wait streaming, FIFO / Largest-First priority, and small-file batching."""

    SMALL_FILE_THRESHOLD_BYTES = 150 * MIB
    MAX_SMALL_BATCH = 3

    def __init__(
        self,
        scheduler: AdaptiveDiskScheduler,
        *,
        batch_trigger_size: int = 1,
        chunk_size: int = 1,
        allow_small_micro_batch: bool = True,
    ) -> None:
        self.scheduler = scheduler
        self.batch_trigger_size = max(1, int(batch_trigger_size))
        self.chunk_size = max(1, int(chunk_size))
        self.allow_small_micro_batch = allow_small_micro_batch

        self._cond = asyncio.Condition()
        self._items: List[UploadQueuedItem] = []
        self._seq = 0
        self._downloads_done = False
        self._flush_requested = False

    async def put(self, item: UploadQueuedItem) -> None:
        async with self._cond:
            self._seq += 1
            item.enqueued_seq = self._seq
            self._items.append(item)
            self._cond.notify_all()

    async def request_flush(self) -> None:
        async with self._cond:
            if not self._flush_requested:
                self._flush_requested = True
                self._cond.notify_all()

    def request_flush_nowait(self) -> None:
        asyncio.create_task(self.request_flush())

    async def mark_downloads_done(self) -> None:
        async with self._cond:
            self._downloads_done = True
            self._cond.notify_all()

    async def notify_cancel(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    async def drain_all(self) -> List[UploadQueuedItem]:
        async with self._cond:
            drained = list(self._items)
            self._items.clear()
            self._flush_requested = False
            self._cond.notify_all()
            return drained

    def _ready_keys(self) -> set[Tuple[str, Path]]:
        if self._downloads_done or self._flush_requested or self.batch_trigger_size <= 1:
            return {(it.uploader_folder, it.download_dir) for it in self._items}
        counts: Dict[Tuple[str, Path], int] = defaultdict(int)
        for it in self._items:
            counts[(it.uploader_folder, it.download_dir)] += 1
        return {k for k, c in counts.items() if c >= self.batch_trigger_size}

    async def take_next_chunk(self, cancel_event: asyncio.Event) -> Optional[List[UploadQueuedItem]]:
        """Block until a ready chunk is available, or return None when finished/cancelled."""
        async with self._cond:
            while True:
                if cancel_event.is_set():
                    return None
                ready_keys = self._ready_keys()
                candidates = [it for it in self._items if (it.uploader_folder, it.download_dir) in ready_keys]
                if candidates:
                    prefer_largest = self.scheduler.prefer_largest_upload
                    if prefer_largest:
                        anchor = max(candidates, key=lambda x: (x.file_size, -x.enqueued_seq))
                    else:
                        anchor = min(candidates, key=lambda x: x.enqueued_seq)

                    group_key = (anchor.uploader_folder, anchor.download_dir)
                    same_group = [it for it in candidates if (it.uploader_folder, it.download_dir) == group_key]
                    if prefer_largest:
                        same_group.sort(key=lambda x: (-x.file_size, x.enqueued_seq))
                    else:
                        same_group.sort(key=lambda x: x.enqueued_seq)

                    if self.chunk_size > 1:
                        selected = same_group[: self.chunk_size]
                    elif (
                        self.allow_small_micro_batch
                        and not prefer_largest
                        and self.scheduler.keep_free_bytes > 0
                        and 0 < anchor.file_size < self.SMALL_FILE_THRESHOLD_BYTES
                    ):
                        small_peers = [
                            it for it in same_group
                            if 0 < it.file_size < self.SMALL_FILE_THRESHOLD_BYTES
                        ]
                        selected = small_peers[: self.MAX_SMALL_BATCH]
                    else:
                        selected = [anchor]

                    selected_ids = {it.item_id for it in selected}
                    self._items = [it for it in self._items if it.item_id not in selected_ids]
                    if not self._items:
                        self._flush_requested = False
                    return selected

                if self._downloads_done:
                    return None

                cond_wait = asyncio.create_task(self._cond.wait())
                cancel_wait = asyncio.create_task(cancel_event.wait())
                try:
                    _, pending = await asyncio.wait(
                        {cond_wait, cancel_wait},
                        timeout=1.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                except BaseException:
                    cond_wait.cancel()
                    cancel_wait.cancel()
                    raise
