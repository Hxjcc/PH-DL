"""Core async task queue, download-to-upload streaming pipeline, and auto-update runner."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

import aiohttp

from ..cancel import StopRequested, until_stopped
from ..config import EnvSettings, RuntimeSettings
from ..db.repo import Repository, utc_now_iso
from ..parsers.http_session import TaskHttpContext
from ..parsers.list_scanner import ListedVideo, build_target_list_url, scan_list_pages
from ..parsers.target import resolve_target_type
from ..parsers.video import (
    VideoDetail,
    collect_video_urls,
    extract_viewkey_from_url,
    fetch_video_details_batch,
)
from ..services.cookie import check_cookie_valid, login_and_get_cookies
from ..services.downloader import (
    DownloadDiskBudget,
    download_mp4_with_resume,
    probe_download_size,
    read_download_checkpoint,
    remove_download_checkpoint,
)
from ..services.filename import is_valid_video_file, normalized_quality, sanitize_filename, video_filename_base
from ..services.notifier import build_auto_update_report, send_serverchan
from ..services.uploader import delete_verified_source, file_snapshot, upload_file_batch
from .events import EventBus


def _split_csv_set(raw: Optional[str], lower: bool = False) -> Optional[Set[str]]:
    if not raw or not str(raw).strip():
        return None
    parts = [p.strip() for p in str(raw).replace("，", ",").split(",") if p.strip()]
    if not parts:
        return None
    return {p.lower() for p in parts} if lower else set(parts)


def _quality_set(raw: Optional[str]) -> Optional[Set[str]]:
    return {q for part in (_split_csv_set(raw) or ()) if (q := normalized_quality(part))} or None


def _target_folder_name(username: Optional[str], target_type: str) -> str:
    t = (target_type or "model").strip().lower()
    if t == "liked":
        return "My_Liked_Videos"
    clean = sanitize_filename(username or "Ungrouped")
    if t == "favorites":
        return f"{clean}_favorites"
    return clean


_FLUSH_SENTINEL = object()


@dataclass
class _UploadQueuedItem:
    item_id: int
    viewkey: str
    uploader_folder: str
    download_dir: Path
    local_path: Path
    page_url: str = ""


class TaskManager:
    """In-process async job orchestrator replacing CLI subprocess execution."""

    def __init__(self, env: EnvSettings, repo: Repository, events: EventBus) -> None:
        self.env = env
        self.repo = repo
        self.events = events

        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._current_job_id: Optional[int] = None
        self._current_cancel_event: Optional[asyncio.Event] = None
        self._cancelled_queued_ids: Set[int] = set()
        self._last_cookie_valid: Optional[bool] = None
        self._last_cookie_message: str = "未检测"

    def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop(), name="phdl-task-worker")

    async def restore_queued_jobs(self) -> int:
        job_ids = await self.repo.list_queued_job_ids()
        for job_id in job_ids:
            await self._queue.put(job_id)
        return len(job_ids)

    async def stop(self) -> None:
        if self._current_cancel_event is not None:
            self._current_cancel_event.set()
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        self._worker_task = None

    @property
    def current_job_id(self) -> Optional[int]:
        return self._current_job_id

    @property
    def queued_count(self) -> int:
        return self._queue.qsize()

    @property
    def cookie_status(self) -> Dict[str, Any]:
        return {"valid": self._last_cookie_valid, "message": self._last_cookie_message}

    def set_cookie_status(self, valid: Optional[bool], message: str) -> None:
        self._last_cookie_valid = valid
        self._last_cookie_message = message
        self.events.broadcast("overview_changed", {})

    async def enqueue_job(
        self,
        kind: str,
        label: str,
        params: Dict[str, Any],
        trigger: str = "manual",
    ) -> Dict[str, Any]:
        job = await self.repo.create_job(kind=kind, label=label, params=params, trigger=trigger)
        job_id = int(job["id"])
        await self._queue.put(job_id)
        self.events.broadcast("job_updated", job)
        self.events.broadcast("overview_changed", {})
        self.events.emit_log("INFO", f"任务已入队: {label}", job_id=job_id)
        return job

    async def cancel_job(self, job_id: int) -> bool:
        job = await self.repo.get_job(job_id)
        if not job:
            return False
        if job["status"] not in {"queued", "scanning", "running"}:
            return False

        if self._current_job_id == job_id and self._current_cancel_event is not None:
            self._current_cancel_event.set()
            self.events.emit_log("WARNING", "收到停止指令，正在安全中止当前任务...", job_id=job_id)
            return True

        updated = await self.repo.cancel_queued_job(job_id)
        if updated:
            self._cancelled_queued_ids.add(job_id)
            self.events.broadcast("job_updated", updated)
            self.events.broadcast("overview_changed", {})
            self.events.emit_log("WARNING", "已取消排队中的任务", job_id=job_id)
            return True
        if self._current_job_id == job_id and self._current_cancel_event is not None:
            self._current_cancel_event.set()
            return True
        return False

    async def _worker_loop(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                if job_id in self._cancelled_queued_ids:
                    self._cancelled_queued_ids.discard(job_id)
                    continue
                await self._run_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.events.emit_log("ERROR", f"任务执行发生未捕获异常: {err}", job_id=job_id)
                updated = await self.repo.update_job(
                    job_id,
                    status="failed",
                    error_message=str(err),
                    finished_at=utc_now_iso(),
                )
                if updated:
                    self.events.broadcast("job_updated", updated)
            finally:
                self._current_job_id = None
                self._current_cancel_event = None
                self.events.clear_job_progress(job_id)
                with contextlib.suppress(Exception):
                    await self.repo.prune_old_jobs(200)
                self.events.broadcast("overview_changed", {})
                self._queue.task_done()

    async def _ensure_valid_cookie(
        self,
        settings: RuntimeSettings,
        job_id: int,
        *,
        notify_on_failure: bool = False,
    ) -> Optional[RuntimeSettings]:
        self.events.emit_log("INFO", "正在验证 Cookie 有效性...", job_id=job_id)
        if settings.ph_cookie.strip():
            check = await check_cookie_valid(
                settings.ph_cookie,
                proxy_url=settings.normalized_proxy,
                timeout=settings.request_timeout,
            )
            if check.valid:
                self.set_cookie_status(True, check.message)
                self.events.emit_log("INFO", "Cookie 有效。", job_id=job_id)
                return settings
            self.events.emit_log("WARNING", f"当前 Cookie 无效 ({check.message})，尝试自动登录刷新...", job_id=job_id)
        else:
            self.events.emit_log("WARNING", "未配置 Cookie，尝试使用账号密码自动登录...", job_id=job_id)

        if settings.ph_username.strip() and settings.ph_password.strip():
            login_res = await login_and_get_cookies(
                settings.ph_username,
                settings.ph_password,
                proxy_url=settings.normalized_proxy,
                timeout=settings.request_timeout,
                yescaptcha_enabled=settings.yescaptcha_enabled,
                yescaptcha_client_key=settings.yescaptcha_client_key,
                yescaptcha_api_base=settings.yescaptcha_api_base,
                yescaptcha_task_type=settings.yescaptcha_task_type,
                yescaptcha_timeout_seconds=settings.yescaptcha_timeout_seconds,
                on_log=lambda level, message: self.events.emit_log(level, message, job_id=job_id),
            )
            if login_res.success and login_res.cookie_str:
                patch: Dict[str, Any] = {"ph_cookie": login_res.cookie_str}
                if login_res.logged_in_username:
                    patch["ph_logged_in_username"] = login_res.logged_in_username
                updated_settings = await self.repo.update_settings_patch(patch)
                self.set_cookie_status(True, "自动登录刷新成功")
                self.events.emit_log("INFO", "自动登录并更新 Cookie 成功。", job_id=job_id)
                return updated_settings
            err_msg = login_res.message
        else:
            err_msg = "未配置有效 Cookie，且未填写 PornHub 账号密码"

        self.set_cookie_status(False, err_msg)
        self.events.emit_log("ERROR", f"Cookie 准备失败: {err_msg}", job_id=job_id)
        if notify_on_failure:
            await send_serverchan(
                "PH-DL 告警：Cookie 失效",
                f"定时追更因 Cookie 失效中止：{err_msg}\n请打开 Web 面板在【系统设置】中手动粘贴浏览器 Cookie。",
                settings,
            )
        return None

    async def _run_job(self, job_id: int) -> None:
        job = await self.repo.get_job(job_id)
        if not job or job["status"] != "queued":
            return

        cancel_event = asyncio.Event()
        self._current_job_id = job_id
        self._current_cancel_event = cancel_event

        started = await self.repo.claim_queued_job(job_id)
        if not started:
            return
        self.events.broadcast("job_updated", started)
        self.events.broadcast("overview_changed", {})

        settings = await self.repo.get_settings()
        kind = job["kind"]
        params = job.get("params") or {}

        # Validate cookie before any network work
        try:
            checked_settings = await until_stopped(
                self._ensure_valid_cookie(
                    settings, job_id,
                    notify_on_failure=(kind == "auto_update" and job.get("trigger") == "schedule"),
                ), cancel_event,
            )
        except StopRequested:
            stopped = await self.repo.update_job(job_id, status="cancelled", error_message="已取消", finished_at=utc_now_iso())
            if stopped:
                self.events.broadcast("job_updated", stopped)
            return
        if checked_settings is None:
            failed = await self.repo.update_job(
                job_id,
                status="failed",
                error_message=self._last_cookie_message,
                finished_at=utc_now_iso(),
            )
            if failed:
                self.events.broadcast("job_updated", failed)
            return
        settings = checked_settings

        async with TaskHttpContext(
            cookie_str=settings.ph_cookie,
            proxy_url=settings.normalized_proxy,
            concurrency=settings.max_concurrent_video_details,
            timeout=settings.request_timeout,
            max_retries=settings.max_retries_page_fetch,
            retry_delay_base=settings.retry_delay_base,
            retry_delay_max=settings.retry_delay_max,
            cancel_event=cancel_event,
            on_log=lambda lvl, msg: self.events.emit_log(lvl, msg, job_id=job_id),
        ) as ctx:
            if kind == "manual_target":
                await self._exec_manual_target(job_id, params, settings, ctx)
            elif kind == "manual_urls":
                await self._exec_manual_urls(job_id, params, settings, ctx)
            elif kind in {"auto_update", "check_updates"}:
                await self._exec_update_check(
                    job_id,
                    params,
                    settings,
                    ctx,
                    auto_download=(kind == "auto_update"),
                )
            elif kind == "baseline_sync":
                await self._exec_baseline_sync(job_id, params, settings, ctx)
            elif kind == "enrich_metadata":
                await self._exec_enrich_metadata(job_id, params, settings, ctx)
            elif kind == "retry_items":
                await self._exec_retry_items(job_id, params, settings, ctx)
            elif kind == "resume_temp":
                await self._exec_resume_temp(job_id, params, settings, ctx)
            else:
                await self.repo.update_job(
                    job_id,
                    status="failed",
                    error_message=f"未知任务类型: {kind}",
                    finished_at=utc_now_iso(),
                )
                return

        uses_job_items = kind not in {"baseline_sync", "check_updates"} and not bool(params.get("export_only", False))
        final_job = (
            await self.repo.recalculate_job_counts(job_id)
            if uses_job_items
            else await self.repo.get_job(job_id)
        )
        if final_job is None:
            return
        if final_job["status"] in {"scanning", "running"}:
            if cancel_event.is_set():
                final_status = "cancelled"
            elif int(final_job.get("failed_items") or 0) > 0 or str(final_job.get("error_message") or "").strip():
                final_status = "partial_failed"
            else:
                final_status = "completed"
            final_job = await self.repo.update_job(
                job_id,
                status=final_status,
                finished_at=utc_now_iso(),
            )
        if final_job:
            self.events.broadcast("job_updated", final_job)
            self.events.emit_log(
                "INFO",
                f"任务结束 [{final_job['status']}]: 总计 {final_job['total_items']}，"
                f"完成 {final_job['done_items']}，已上传 {final_job['uploaded_items']}，"
                f"跳过 {final_job['skipped_items']}，失败 {final_job['failed_items']}",
                job_id=job_id,
            )

    # ------------------------------------------------------------------
    # Pipeline: Concurrent Download -> Streaming rclone Upload + Backpressure
    # ------------------------------------------------------------------
    async def _run_download_upload_pipeline(
        self,
        job_id: int,
        item_ids: List[int],
        settings: RuntimeSettings,
        ctx: TaskHttpContext,
        *,
        auto_upload: bool,
        redownload: bool = False,
        preferred_qualities: Optional[Set[str]] = None,
        item_preferred_qualities: Optional[Dict[int, Optional[Set[str]]]] = None,
        allow_reparse_retry: bool = True,
    ) -> Tuple[Dict[str, int], Dict[str, List[str]]]:
        """Download items concurrently and stream completed files into rclone upload batches."""
        uploaded_by_uploader: Dict[str, int] = defaultdict(int)
        failed_urls_by_uploader: Dict[str, List[str]] = defaultdict(list)

        def _record_failed_url(folder: str, page_url: str) -> None:
            if page_url and page_url not in failed_urls_by_uploader[folder]:
                failed_urls_by_uploader[folder].append(page_url)

        def _resolve_item_qualities(iid: int) -> Optional[Set[str]]:
            if item_preferred_qualities is not None and iid in item_preferred_qualities:
                return item_preferred_qualities[iid]
            return preferred_qualities

        if not item_ids:
            return dict(uploaded_by_uploader), dict(failed_urls_by_uploader)

        running_job = await self.repo.update_job(job_id, status="running")
        if running_job:
            self.events.broadcast("job_updated", running_job)

        upload_queue: asyncio.Queue[Any] = asyncio.Queue()
        disk_budget = DownloadDiskBudget(self.env.temp_dir, int(settings.min_free_disk_gb * 1024 ** 3))
        pending_local_count = 0
        flush_pending = False
        pending_lock = asyncio.Lock()
        space_freed_event = asyncio.Event()
        space_freed_event.set()

        async def _flush_uploader_group(
            uploader_folder: str,
            download_dir: Path,
            batch_entries: List[_UploadQueuedItem],
        ) -> None:
            nonlocal pending_local_count
            if not batch_entries:
                return
            for entry in batch_entries:
                it = await self.repo.update_job_item(entry.item_id, status="uploading")
                if it:
                    self.events.broadcast("item_updated", it)

            files_map = {e.viewkey: e.local_path for e in batch_entries}
            self.events.emit_log(
                "INFO",
                f"[上传] 开始向 {settings.rclone_remote}:{settings.rclone_remote_base_dir}/{uploader_folder} "
                f"上传 {len(batch_entries)} 个视频...",
                job_id=job_id,
            )
            up_res = await upload_file_batch(
                download_dir=download_dir,
                uploader_folder=uploader_folder,
                files_by_viewkey=files_map,
                settings=settings,
                cancel_event=ctx.cancel_event,
            )
            uploaded_set = set(up_res.uploaded_viewkeys)
            verified_count = 0
            deleted_local = 0
            for entry in batch_entries:
                snapshot = up_res.verified_sources.get(entry.viewkey)
                if (
                    entry.viewkey in uploaded_set and snapshot is not None
                    and not ctx.cancel_event.is_set() and file_snapshot(entry.local_path) == snapshot
                    and await self.repo.record_verified_upload(
                        entry.viewkey,
                        str(entry.local_path),
                        f"{up_res.remote_dir}/{entry.local_path.relative_to(download_dir).as_posix()}",
                    )
                ):
                    local_path = str(entry.local_path)
                    if not ctx.cancel_event.is_set() and delete_verified_source(entry.local_path, snapshot):
                        await self.repo.clear_uploaded_local_path(entry.viewkey, local_path)
                        local_path = ""
                        deleted_local += 1
                    elif not ctx.cancel_event.is_set():
                        self.events.emit_log("WARNING", f"[上传] 本地副本未移除: {entry.local_path.name}", job_id=job_id)
                    it = await self.repo.update_job_item(
                        entry.item_id,
                        status="done",
                        local_path=local_path,
                        upload_requested=0,
                        error_message="",
                    )
                    if it:
                        self.events.broadcast("item_updated", it)
                    uploaded_by_uploader[uploader_folder] += 1
                    verified_count += 1
                else:
                    cancelled = ctx.cancel_event.is_set()
                    err_txt = "上传已取消，保留本地文件" if cancelled else up_res.error or "未能确认上传结果"
                    it = await self.repo.update_job_item(
                        entry.item_id,
                        status="cancelled" if cancelled else "failed",
                        error_message=err_txt if cancelled else f"上传失败: {err_txt}",
                    )
                    if it:
                        self.events.broadcast("item_updated", it)
                    if not cancelled:
                        _record_failed_url(uploader_folder, entry.page_url)
                    self.events.emit_log("WARNING", f"[上传失败] {entry.local_path.name}: {err_txt}", job_id=job_id)

            async with pending_lock:
                # All entries in this batch have exited the upload queue (uploaded or marked failed)
                pending_local_count = max(0, pending_local_count - len(batch_entries))
                space_freed_event.set()

            refreshed = await self.repo.recalculate_job_counts(job_id)
            if refreshed:
                self.events.broadcast("job_updated", refreshed)

            self.events.emit_log(
                "INFO",
                f"[上传完成] {uploader_folder}: 成功 {verified_count}，删除本地 {deleted_local}，"
                f"未完成 {len(batch_entries) - verified_count}",
                job_id=job_id,
            )

        async def _upload_worker() -> None:
            nonlocal flush_pending, pending_local_count
            buffers: Dict[Tuple[str, Path], List[_UploadQueuedItem]] = defaultdict(list)

            async def _flush_all() -> None:
                for (folder, dl_dir), entries in list(buffers.items()):
                    if entries and not ctx.cancel_event.is_set():
                        buffers[(folder, dl_dir)] = []
                        await _flush_uploader_group(folder, dl_dir, entries)

            try:
                while True:
                    item = await upload_queue.get()
                    try:
                        if item is None:
                            await _flush_all()
                            return
                        if item is _FLUSH_SENTINEL:
                            async with pending_lock:
                                flush_pending = False
                            await _flush_all()
                            async with pending_lock:
                                space_freed_event.set()
                            continue
                        key = (item.uploader_folder, item.download_dir)
                        buffers[key].append(item)
                        if len(buffers[key]) >= settings.upload_batch_size:
                            batch = buffers[key]
                            buffers[key] = []
                            await _flush_uploader_group(item.uploader_folder, item.download_dir, batch)
                    finally:
                        upload_queue.task_done()
            finally:
                async with pending_lock:
                    flush_pending = False
                    pending_local_count = 0
                    space_freed_event.set()

        async def _wait_for_disk_and_queue_headroom(will_upload: bool) -> bool:
            """Apply VPS disk & pending-upload backpressure before starting a download."""
            nonlocal flush_pending
            if not will_upload:
                # Even when uploads are disabled, never let manual downloads
                # consume the last reserved bytes of the VPS disk.
                if settings.min_free_disk_gb <= 0:
                    return True
                try:
                    free_gb = shutil.disk_usage(self.env.resolved_data_dir).free / (1024 ** 3)
                except OSError:
                    return True
                if free_gb < settings.min_free_disk_gb:
                    self.events.emit_log(
                        "ERROR",
                        f"VPS 磁盘可用空间不足（{free_gb:.2f} GB < {settings.min_free_disk_gb:.2f} GB），"
                        "已停止继续下载。",
                        job_id=job_id,
                    )
                    return False
                return True
            logged_wait = False
            while not ctx.cancel_event.is_set():
                low_disk = False
                if settings.min_free_disk_gb > 0:
                    try:
                        free_gb = shutil.disk_usage(self.env.resolved_data_dir).free / (1024 ** 3)
                        low_disk = free_gb < settings.min_free_disk_gb
                    except OSError:
                        low_disk = False

                async with pending_lock:
                    cur_pending = pending_local_count
                    too_many_pending = cur_pending >= settings.max_pending_upload_files
                    if not too_many_pending and not low_disk:
                        return True
                    if cur_pending == 0 and low_disk:
                        self.events.emit_log("ERROR", "VPS 磁盘可用空间不足，无法继续下载。", job_id=job_id)
                        return False
                    space_freed_event.clear()
                    if not flush_pending:
                        flush_pending = True
                        await upload_queue.put(_FLUSH_SENTINEL)

                if not logged_wait:
                    logged_wait = True
                    self.events.emit_log(
                        "INFO",
                        "VPS 磁盘或待上传文件达到背压阈值，正在触发上传释放空间...",
                        job_id=job_id,
                    )

                wait_space = asyncio.create_task(space_freed_event.wait())
                wait_cancel = asyncio.create_task(ctx.cancel_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        {wait_space, wait_cancel},
                        timeout=2.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                except asyncio.CancelledError:
                    wait_space.cancel()
                    wait_cancel.cancel()
                    raise
            return False

        upload_task: Optional[asyncio.Task] = None
        if auto_upload:
            upload_task = asyncio.create_task(_upload_worker(), name=f"phdl-upload-{job_id}")

        failed_item_ids_pass1: List[int] = []

        async def _download_items_pass(target_item_ids: List[int], pass_num: int) -> List[int]:
            nonlocal pending_local_count
            pass_failed: List[int] = []
            sem = asyncio.Semaphore(max(1, settings.max_concurrent_downloads))
            refresh_lock = asyncio.Lock()
            last_refresh_at = 0.0

            async def _refresh_job_progress(force: bool = False) -> None:
                nonlocal last_refresh_at
                async with refresh_lock:
                    now = time.monotonic()
                    if not force and (now - last_refresh_at) < 0.25:
                        return
                    last_refresh_at = now
                    refreshed = await self.repo.recalculate_job_counts(job_id)
                    if refreshed:
                        self.events.broadcast("job_updated", refreshed)

            timeout = aiohttp.ClientTimeout(total=None, connect=30.0, sock_read=300.0)
            connector = aiohttp.TCPConnector(limit=64, limit_per_host=16)

            async with aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                read_bufsize=2 * 1024 * 1024,
                trust_env=True,
            ) as dl_session:
                async def _dl_one(item_id: int) -> None:
                    nonlocal pending_local_count
                    async with sem:
                        if ctx.cancel_event.is_set():
                            it = await self.repo.update_job_item(item_id, status="cancelled", error_message="已取消")
                            if it:
                                self.events.broadcast("item_updated", it)
                            return

                        item = await self.repo.get_job_item(item_id)
                        if not item:
                            return

                        vkey = item["viewkey"]
                        uploader_folder = _target_folder_name(item["uploader"], item["target_type"])
                        dl_dir = self.env.downloads_dir / uploader_folder
                        tmp_dir = self.env.temp_dir / uploader_folder
                        partial_path = Path(item["temp_path"]) if item.get("temp_path") else None
                        if partial_path is not None and partial_path.resolve().is_relative_to(self.env.temp_dir.resolve()):
                            tmp_dir = partial_path.parent
                        else:
                            partial_path = None
                        will_upload = auto_upload and item.get("upload_requested") != 0
                        requested = _resolve_item_qualities(item_id)
                        ledger = await self.repo.get_video(vkey) if vkey and not redownload else None
                        quality = normalized_quality(item["quality"])
                        saved_quality = normalized_quality(ledger["quality"]) if ledger else ""
                        quality_matches = (
                            saved_quality == quality if quality else not requested or saved_quality in requested
                        )

                        if ledger and ledger["status"] == "uploaded" and (not requested or quality_matches):
                            it = await self.repo.update_job_item(
                                item_id,
                                status="skipped",
                                error_message="已在云端上传记录中，跳过",
                            )
                            if it:
                                self.events.broadcast("item_updated", it)
                            await _refresh_job_progress()
                            return

                        if ledger and ledger["status"] == "downloaded" and ledger["local_path"] and quality_matches:
                            existing_local = Path(ledger["local_path"])
                            if existing_local.resolve().is_relative_to(self.env.downloads_dir.resolve()) and await asyncio.to_thread(
                                is_valid_video_file, existing_local, settings.min_file_size_bytes, ledger["file_size"] or None,
                            ):
                                existing_size = existing_local.stat().st_size
                                await self.repo.upsert_video(
                                    vkey,
                                    status="downloaded",
                                    local_path=str(existing_local),
                                    file_size=existing_size,
                                    upload_requested=will_upload,
                                )
                                it = await self.repo.update_job_item(
                                    item_id,
                                    status="downloaded" if will_upload else "skipped",
                                    local_path=str(existing_local),
                                    quality=ledger["quality"],
                                    bytes_downloaded=existing_size,
                                    bytes_total=existing_size,
                                    upload_requested=int(will_upload),
                                    error_message="" if will_upload else "本地已有符合画质的完整文件，跳过",
                                )
                                if it:
                                    self.events.broadcast("item_updated", it)
                                if will_upload:
                                    async with pending_lock:
                                        pending_local_count += 1
                                    await upload_queue.put(
                                        _UploadQueuedItem(
                                            item_id=item_id,
                                            viewkey=vkey,
                                            uploader_folder=uploader_folder,
                                            download_dir=existing_local.parent,
                                            local_path=existing_local,
                                            page_url=item["page_url"],
                                        )
                                    )
                                await _refresh_job_progress()
                                return

                        if not await _wait_for_disk_and_queue_headroom(will_upload):
                            cancelled = ctx.cancel_event.is_set()
                            it = await self.repo.update_job_item(
                                item_id, status="cancelled" if cancelled else "failed",
                                error_message="已取消" if cancelled else "磁盘空间不足",
                            )
                            if it:
                                self.events.broadcast("item_updated", it)
                            return

                        it = await self.repo.update_job_item(
                            item_id,
                            status="downloading",
                            retry_count=pass_num - 1,
                            upload_requested=int(will_upload),
                            error_message="",
                        )
                        if it:
                            self.events.broadcast("item_updated", it)

                        def _on_progress(dl_bytes: int, tot_bytes: int, speed_bps: float) -> None:
                            self.events.emit_item_progress(job_id, item_id, dl_bytes, tot_bytes, speed_bps)

                        outcome = await download_mp4_with_resume(
                            session=dl_session,
                            parse_ctx=ctx,
                            selectors=settings.selectors,
                            viewkey=vkey,
                            title=item["title"],
                            download_url=item["download_url"],
                            page_url=item["page_url"],
                            referer_url=item["referer_url"],
                            quality=item["quality"],
                            download_dir=dl_dir,
                            temp_dir=tmp_dir,
                            partial_path=partial_path,
                            replace_existing=redownload,
                            disk_budget=disk_budget,
                            cookie_str=settings.ph_cookie,
                            proxy_url=settings.normalized_proxy,
                            preferred_qualities=requested,
                            max_attempts=settings.download_attempts,
                            retry_delay_base=settings.retry_delay_base,
                            retry_delay_max=settings.retry_delay_max,
                            request_timeout=settings.request_timeout,
                            min_file_size_bytes=settings.min_file_size_bytes,
                            cancel_event=ctx.cancel_event,
                            on_progress=_on_progress,
                            on_log=lambda lvl, msg: self.events.emit_log(lvl, msg, job_id=job_id),
                        )
                        self.events.clear_item_progress(item_id)

                        if outcome.cancelled:
                            it = await self.repo.update_job_item(
                                item_id,
                                status="cancelled",
                                temp_path=str(outcome.temp_path or ""),
                                bytes_downloaded=outcome.file_size,
                                quality=outcome.quality or item["quality"],
                                download_url=outcome.download_url or item["download_url"],
                                error_message="已取消（断点已保留）" if outcome.temp_path else "已取消",
                            )
                            if it:
                                self.events.broadcast("item_updated", it)
                            return

                        if not outcome.success or outcome.local_path is None:
                            it = await self.repo.update_job_item(
                                item_id,
                                status="failed",
                                temp_path=str(outcome.temp_path or ""),
                                bytes_downloaded=outcome.file_size,
                                quality=outcome.quality or item["quality"],
                                download_url=outcome.download_url or item["download_url"],
                                error_message=outcome.error or "下载失败",
                            )
                            if it:
                                self.events.broadcast("item_updated", it)
                            if not (outcome.error or "").startswith(("磁盘", "写入")):
                                pass_failed.append(item_id)
                            else:
                                _record_failed_url(uploader_folder, item["page_url"])
                            self.events.emit_log("WARNING", f"下载失败: {item['title']} ({outcome.error})", job_id=job_id)
                            await _refresh_job_progress()
                            return

                        await self.repo.upsert_video(
                            vkey,
                            title=item["title"],
                            uploader=item["uploader"],
                            target_type=item["target_type"],
                            quality=outcome.quality or item["quality"],
                            video_page_url=item["page_url"],
                            status="downloaded",
                            local_path=str(outcome.local_path),
                            file_size=outcome.file_size,
                            downloaded_at=utc_now_iso(),
                            force_status=True,
                            upload_requested=will_upload,
                        )
                        it = await self.repo.update_job_item(
                            item_id,
                            status="downloaded",
                            quality=outcome.quality or item["quality"],
                            download_url=outcome.download_url or item["download_url"],
                            local_path=str(outcome.local_path),
                            temp_path="",
                            bytes_downloaded=outcome.file_size,
                            bytes_total=outcome.file_size,
                            error_message="",
                        )
                        if it:
                            self.events.broadcast("item_updated", it)

                        self.events.emit_log(
                            "INFO",
                            f"下载成功: {item['title']} ({outcome.quality or item['quality']}, "
                            f"{outcome.file_size / (1024 * 1024):.1f} MB)",
                            job_id=job_id,
                        )

                        if will_upload:
                            async with pending_lock:
                                pending_local_count += 1
                            await upload_queue.put(
                                _UploadQueuedItem(
                                    item_id=item_id,
                                    viewkey=vkey,
                                    uploader_folder=uploader_folder,
                                    download_dir=dl_dir,
                                    local_path=outcome.local_path,
                                    page_url=item["page_url"],
                                )
                            )

                        await _refresh_job_progress()

                worker_count = min(len(target_item_ids), max(1, settings.max_concurrent_downloads))
                if worker_count > 0:
                    item_queue: asyncio.Queue[Optional[int]] = asyncio.Queue()
                    for iid in target_item_ids:
                        item_queue.put_nowait(iid)
                    for _ in range(worker_count):
                        item_queue.put_nowait(None)

                    async def _worker() -> None:
                        while True:
                            iid = await item_queue.get()
                            try:
                                if iid is None:
                                    return
                                await _dl_one(iid)
                            finally:
                                item_queue.task_done()

                    workers = [asyncio.create_task(_worker()) for _ in range(worker_count)]
                    try:
                        await asyncio.gather(*workers)
                    except BaseException:
                        for worker in workers:
                            worker.cancel()
                        await asyncio.gather(*workers, return_exceptions=True)
                        raise
                await _refresh_job_progress(force=True)

            return pass_failed

        try:
            failed_item_ids_pass1 = await _download_items_pass(item_ids, pass_num=1)

            # One automatic re-parse & retry pass for failed downloads
            final_failed_ids = list(failed_item_ids_pass1)
            if allow_reparse_retry and failed_item_ids_pass1 and not ctx.cancel_event.is_set():
                self.events.emit_log(
                    "INFO",
                    f"首轮有 {len(failed_item_ids_pass1)} 个下载失败，自动重新解析直链并重试...",
                    job_id=job_id,
                )
                retry_candidates: List[Tuple[int, str]] = []
                for fid in failed_item_ids_pass1:
                    row = await self.repo.get_job_item(fid)
                    if row and row.get("page_url"):
                        retry_candidates.append((fid, row["page_url"]))

                if retry_candidates:
                    grouped_candidates: Dict[Optional[Tuple[str, ...]], List[Tuple[int, str]]] = defaultdict(list)
                    for fid, purl in retry_candidates:
                        q_set = _resolve_item_qualities(fid)
                        q_key = tuple(sorted(q_set)) if q_set else None
                        grouped_candidates[q_key].append((fid, purl))

                    by_url: Dict[str, VideoDetail] = {}
                    for q_key, group_rows in grouped_candidates.items():
                        reparsed = await fetch_video_details_batch(
                            ctx,
                            [(u, 0, idx + 1) for idx, (_, u) in enumerate(group_rows)],
                            settings.selectors,
                            concurrency=settings.max_concurrent_video_details,
                            preferred_qualities=set(q_key) if q_key else None,
                            timeout=settings.request_timeout,
                        )
                        for d in reparsed:
                            by_url[d.page_url] = d

                    ready_for_pass2: List[int] = []
                    unresolved_ids: List[int] = []
                    for fid, purl in retry_candidates:
                        d = by_url.get(purl)
                        if d and d.download_url:
                            patch: Dict[str, Any] = {
                                "download_url": d.download_url,
                                "referer_url": d.referer_url,
                                "quality": d.quality,
                            }
                            await self.repo.update_job_item(fid, **patch)
                            ready_for_pass2.append(fid)
                        else:
                            unresolved_ids.append(fid)

                    pass2_failed = await _download_items_pass(ready_for_pass2, pass_num=2) if ready_for_pass2 else []
                    final_failed_ids = unresolved_ids + pass2_failed

            for fid in final_failed_ids:
                row = await self.repo.get_job_item(fid)
                if row and row.get("page_url"):
                    folder = _target_folder_name(row.get("uploader"), row.get("target_type") or "model")
                    _record_failed_url(folder, row["page_url"])
        finally:
            if upload_task is not None:
                await upload_queue.put(None)
                await upload_task

        return dict(uploaded_by_uploader), dict(failed_urls_by_uploader)

    # ------------------------------------------------------------------
    # Job Executors
    # ------------------------------------------------------------------
    async def _create_items_from_details(
        self,
        job_id: int,
        details: List[VideoDetail],
        *,
        default_uploader: str = "Ungrouped",
        default_type: str = "model",
        group_by_uploader: bool = True,
        upload_requested: bool = False,
    ) -> List[int]:
        item_ids: List[int] = []
        selected: Dict[str, VideoDetail] = {}
        for detail in details:
            if detail.filtered_out or detail.error == "操作已取消":
                continue
            key = detail.viewkey or detail.page_url
            old = selected.get(key)
            if old is None or (old.error and not detail.error and detail.download_url):
                selected[key] = detail

        specs: List[Dict[str, Any]] = []
        for detail in selected.values():
            uploader = (detail.uploader_username or default_uploader) if group_by_uploader else default_uploader
            utype = (detail.uploader_type or default_type) if group_by_uploader else default_type
            error = detail.error or (
                "未解析到视频标识或 MP4 直链" if not detail.download_url or not detail.viewkey else ""
            )
            specs.append(
                {
                    "viewkey": detail.viewkey,
                    "title": detail.title,
                    "uploader": uploader,
                    "target_type": utype,
                    "page_url": detail.page_url,
                    "download_url": detail.download_url or "",
                    "referer_url": detail.referer_url,
                    "quality": detail.quality,
                    "status": "failed" if error else "pending",
                    "error_message": error,
                    "upload_requested": upload_requested,
                }
            )
            if error:
                self.events.emit_log("WARNING", f"视频解析失败 [{detail.viewkey}]: {error}", job_id=job_id)

        rows = await self.repo.add_job_items_batch(job_id, specs)
        for row in rows:
            if row.get("status") == "pending":
                item_ids.append(int(row["id"]))
            self.events.broadcast("item_updated", row)

        updated_job = await self.repo.recalculate_job_counts(job_id)
        if updated_job:
            self.events.broadcast("job_updated", updated_job)
        return item_ids

    def _export_urls(self, details: List[VideoDetail]) -> List[str]:
        urls: List[str] = []
        seen_viewkeys: Set[str] = set()
        for detail in details:
            # Parsing checks title keywords and the requested MP4 qualities.
            # Rejected or incomplete details must not become exported links.
            if detail.error or not detail.download_url or not detail.viewkey or detail.viewkey in seen_viewkeys:
                continue
            seen_viewkeys.add(detail.viewkey)
            urls.append(
                detail.page_url
                or f"https://www.pornhub.com/view_video.php?viewkey={detail.viewkey}"
            )
        return urls

    @staticmethod
    def _detail_failures(details: List[VideoDetail]) -> Set[str]:
        valid = {detail.viewkey or detail.page_url for detail in details if detail.download_url and not detail.error}
        return {
            detail.viewkey or detail.page_url for detail in details
            if not detail.filtered_out and detail.error != "操作已取消"
            and (detail.error or not detail.download_url or not detail.viewkey)
        } - valid

    async def _record_export_result(self, job_id: int, details: List[VideoDetail]) -> None:
        urls = self._export_urls(details)
        failures = self._detail_failures(details)
        error_patch = {}
        if failures:
            first = next(
                d for d in details
                if (d.viewkey or d.page_url) in failures and not d.filtered_out and d.error != "操作已取消"
            )
            job = await self.repo.get_job(job_id)
            errors = [(job or {}).get("error_message"), f"{len(failures)} 条解析失败：{first.error or '缺少视频或媒体信息'}"]
            error_patch["error_message"] = "；".join(error for error in errors if error)
        await self.repo.update_job(
            job_id, export_text=("\n".join(urls) + "\n") if urls else "",
            total_items=len(urls) + len(failures), done_items=len(urls), failed_items=len(failures),
            **error_patch,
        )
        self.events.emit_log("INFO", f"已导出 {len(urls)} 条链接，{len(failures)} 条解析失败。", job_id=job_id)

    async def _scan_target_with_details(
        self,
        job_id: int,
        *,
        ctx: TaskHttpContext,
        list_url: str,
        target_type: str,
        settings: RuntimeSettings,
        pages_spec: Optional[str] = None,
        videos_spec: Optional[str] = None,
        catch_up: bool = False,
        skip_vkeys: Optional[Set[str]] = None,
        preferred_qualities: Optional[Set[str]] = None,
        include_keywords: Optional[Set[str]] = None,
        exclude_keywords: Optional[Set[str]] = None,
        uploader_hint: Optional[Tuple[str, str]] = None,
        on_profile: Optional[Callable[[str, str], Awaitable[None]]] = None,
        on_scan_error: Optional[Callable[[str], None]] = None,
    ) -> Tuple[List[ListedVideo], List[VideoDetail]]:
        """Scan list pages with 4-way prefetch and stream details through one concurrency limit."""
        detail_semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_video_details))
        page_detail_tasks: List[asyncio.Task] = []

        async def _parse_page_details(page_items: List[ListedVideo]) -> List[VideoDetail]:
            return await fetch_video_details_batch(
                ctx,
                [(lv.url, lv.page_num, lv.item_num) for lv in page_items],
                settings.selectors,
                concurrency=settings.max_concurrent_video_details,
                preferred_qualities=preferred_qualities,
                include_keywords=include_keywords,
                exclude_keywords=exclude_keywords,
                uploader_hint=uploader_hint,
                semaphore=detail_semaphore,
                timeout=settings.request_timeout,
            )

        async def _on_page(page_items: List[ListedVideo]) -> None:
            page_detail_tasks.append(asyncio.create_task(_parse_page_details(page_items)))

        self.events.emit_log(
            "INFO",
            f"列表预取并发 {settings.max_concurrent_list_pages}，详情并发 "
            f"{settings.max_concurrent_video_details}，已启用边扫描边解析。",
            job_id=job_id,
        )
        try:
            listed = await scan_list_pages(
                ctx,
                list_url,
                settings.selectors,
                target_type=target_type,
                pages_spec=pages_spec,
                videos_spec=videos_spec,
                catch_up=catch_up,
                skip_vkeys=skip_vkeys,
                delay_min=settings.page_delay_min,
                delay_max=settings.page_delay_max,
                page_concurrency=settings.max_concurrent_list_pages,
                timeout=settings.request_timeout,
                profile_username=uploader_hint[0] if uploader_hint else None,
                on_profile=on_profile,
                on_page=_on_page,
                on_error=on_scan_error,
            )
        except BaseException:
            for task in page_detail_tasks:
                task.cancel()
            await asyncio.gather(*page_detail_tasks, return_exceptions=True)
            raise

        if not listed:
            return [], []

        self.events.emit_log(
            "INFO",
            f"列表扫描完成：共 {len(listed)} 个视频，等待 "
            f"{len(page_detail_tasks)} 个详情批次完成...",
            job_id=job_id,
        )
        if page_detail_tasks:
            try:
                detail_groups = await asyncio.gather(*page_detail_tasks)
            except BaseException:
                for task in page_detail_tasks:
                    task.cancel()
                await asyncio.gather(*page_detail_tasks, return_exceptions=True)
                raise
            details = [detail for group in detail_groups for detail in group]
        else:
            # Compatibility path for scanners that do not stream page callbacks.
            details = await _parse_page_details(listed)
        details.sort(key=lambda d: (d.page_num, d.item_num))
        return listed, details

    async def _exec_manual_target(
        self,
        job_id: int,
        params: Dict[str, Any],
        settings: RuntimeSettings,
        ctx: TaskHttpContext,
    ) -> None:
        usernames: List[str] = [str(u).strip() for u in (params.get("usernames") or []) if str(u).strip()]
        raw_type = (params.get("target_type") or "").strip().lower()
        quality_setting = params.get("quality")
        qualities = _quality_set(settings.preferred_quality if quality_setting is None else quality_setting)
        include_kw = _split_csv_set(params.get("keywords"), lower=True)
        exclude_kw = _split_csv_set(params.get("exclude_keywords"), lower=True)
        pages_spec = (params.get("pages") or "").strip() or None
        videos_spec = (params.get("videos") or "").strip() or None
        redownload = bool(params.get("redownload", False))
        export_only = bool(params.get("export_only", False))
        auto_upload = bool(params.get("auto_upload", settings.uploader_enabled))

        targets: List[Tuple[str, str]] = []
        if raw_type == "liked":
            targets.append(("_currentUser_", "liked"))
        else:
            if raw_type == "user":
                raw_type = "users"
            if not usernames and raw_type in {"favorites", "model", "pornstar", "users"} and settings.ph_logged_in_username:
                usernames = [settings.ph_logged_in_username]
            seen_users: Set[str] = set()
            for uname in usernames:
                if uname.casefold() in seen_users:
                    continue
                seen_users.add(uname.casefold())
                if raw_type in {"model", "pornstar", "users", "favorites"}:
                    resolved = raw_type
                else:
                    detected = await resolve_target_type(ctx, uname, timeout=settings.request_timeout)
                    if detected == "channel":
                        self.events.emit_log("WARNING", f"跳过 '{uname}'：识别为 Channel，当前不支持 Channel。", job_id=job_id)
                        continue
                    resolved = detected or "model"
                    self.events.emit_log("INFO", f"自动识别目标 '{uname}' 类型为 '{resolved}'。", job_id=job_id)
                targets.append((uname, resolved))

        if not targets:
            await self.repo.update_job(job_id, error_message="没有有效的下载目标")
            self.events.emit_log("WARNING", "没有有效的下载目标。", job_id=job_id)
            return

        known_vkeys = set() if (redownload or export_only or qualities) else await self.repo.get_uploaded_viewkeys()
        known_vkeys.update(params.get("skip_completed_viewkeys") or ())
        all_details: List[VideoDetail] = []
        scoped: Dict[str, Tuple[VideoDetail, str, str]] = {}
        scan_errors: List[str] = []
        profile_avatars: Dict[str, str] = dict(params.get("profile_avatars") or {})
        profile_names: Dict[str, str] = dict(params.get("profile_names") or {})

        for uname, utype in targets:
            if ctx.cancel_event.is_set():
                break
            list_url = build_target_list_url(uname if uname != "_currentUser_" else None, utype)

            async def _save_target_profile(
                url: str,
                display_name: str,
                target_user: str = uname,
                target_type: str = utype,
            ) -> None:
                await self.repo.update_target_profile_by_username(
                    target_user,
                    target_type,
                    avatar_url=url,
                    display_name=display_name,
                )
                if target_user == "_currentUser_":
                    return
                changed = False
                if url and profile_avatars.get(target_user) != url:
                    profile_avatars[target_user] = url
                    changed = True
                if display_name and profile_names.get(target_user) != display_name:
                    profile_names[target_user] = display_name
                    changed = True
                if changed:
                    updated_job = await self.repo.merge_job_params(
                        job_id,
                        {"profile_avatars": profile_avatars, "profile_names": profile_names},
                    )
                    if updated_job:
                        self.events.broadcast("job_updated", updated_job)

            page_errors: List[str] = []
            listed, details = await self._scan_target_with_details(
                job_id,
                ctx=ctx,
                list_url=list_url,
                target_type=utype,
                settings=settings,
                pages_spec=pages_spec,
                videos_spec=videos_spec,
                skip_vkeys=known_vkeys,
                preferred_qualities=qualities,
                include_keywords=include_kw,
                exclude_keywords=exclude_kw,
                uploader_hint=(uname if uname != "_currentUser_" else "My_Liked", utype),
                on_profile=_save_target_profile,
                on_scan_error=page_errors.append,
            )
            scan_errors.extend(f"{uname}: {error}" for error in page_errors)
            if not listed:
                if not page_errors and not ctx.cancel_event.is_set():
                    self.events.emit_log("INFO", f"目标 '{uname}' ({utype}) 未发现待处理的视频。", job_id=job_id)
                continue

            if export_only:
                all_details.extend(details)
            else:
                for detail in details:
                    if detail.filtered_out or detail.error == "操作已取消":
                        continue
                    key = detail.viewkey or detail.page_url
                    old = scoped.get(key)
                    if old is None or ((old[0].error or not old[0].download_url) and detail.download_url and not detail.error):
                        scoped[key] = (detail, uname if uname != "_currentUser_" else "My_Liked", utype)

        if scan_errors:
            await self.repo.update_job(job_id, error_message=f"{len(scan_errors)} 页扫描未完成：{scan_errors[0]}")
        if export_only:
            await self._record_export_result(job_id, all_details)
            return

        groups: Dict[Tuple[str, str], List[VideoDetail]] = defaultdict(list)
        for detail, uname, utype in scoped.values():
            groups[(uname, utype)].append(detail)
        all_item_ids: List[int] = []
        for (uname, utype), details in groups.items():
            all_item_ids.extend(await self._create_items_from_details(
                job_id, details, default_uploader=uname, default_type=utype,
                group_by_uploader=False, upload_requested=auto_upload,
            ))
        if all_item_ids and not ctx.cancel_event.is_set():
            await self._run_download_upload_pipeline(
                job_id,
                all_item_ids,
                settings,
                ctx,
                auto_upload=auto_upload,
                redownload=redownload,
                preferred_qualities=qualities,
            )

    async def _exec_manual_urls(
        self,
        job_id: int,
        params: Dict[str, Any],
        settings: RuntimeSettings,
        ctx: TaskHttpContext,
    ) -> None:
        video_urls, ignored = collect_video_urls(str(url) for url in (params.get("urls") or []))
        skipped_non_video = ignored + int(params.get("skipped_non_video") or 0)

        if skipped_non_video:
            self.events.emit_log("WARNING", f"已跳过 {skipped_non_video} 条非视频链接。", job_id=job_id)
        if not video_urls:
            await self.repo.update_job(job_id, error_message="未识别到有效的视频链接")
            self.events.emit_log("WARNING", "未识别到任何有效视频链接。", job_id=job_id)
            return

        quality_setting = params.get("quality")
        qualities = _quality_set(settings.preferred_quality if quality_setting is None else quality_setting)
        include_kw = _split_csv_set(params.get("keywords"), lower=True)
        exclude_kw = _split_csv_set(params.get("exclude_keywords"), lower=True)
        group_by_uploader = bool(params.get("group_by_uploader", True))
        redownload = bool(params.get("redownload", False))
        export_only = bool(params.get("export_only", False))
        auto_upload = bool(params.get("auto_upload", settings.uploader_enabled))
        batch_folder = sanitize_filename(params.get("batch_name") or "url_batch")

        self.events.emit_log("INFO", f"正在并发解析 {len(video_urls)} 条视频链接...", job_id=job_id)
        details = await fetch_video_details_batch(
            ctx,
            [(u, 1, idx + 1) for idx, u in enumerate(video_urls)],
            settings.selectors,
            concurrency=settings.max_concurrent_video_details,
            preferred_qualities=qualities,
            include_keywords=include_kw,
            exclude_keywords=exclude_kw,
            timeout=settings.request_timeout,
        )

        if export_only:
            await self._record_export_result(job_id, details)
            return

        item_ids = await self._create_items_from_details(
            job_id,
            details,
            default_uploader=batch_folder,
            default_type="model",
            group_by_uploader=group_by_uploader,
            upload_requested=auto_upload,
        )
        if item_ids and not ctx.cancel_event.is_set():
            await self._run_download_upload_pipeline(
                job_id,
                item_ids,
                settings,
                ctx,
                auto_upload=auto_upload,
                redownload=redownload,
                preferred_qualities=qualities,
            )

    async def _exec_update_check(
        self,
        job_id: int,
        params: Dict[str, Any],
        settings: RuntimeSettings,
        ctx: TaskHttpContext,
        *,
        auto_download: bool,
    ) -> None:
        target_ids = params.get("target_ids")
        if target_ids:
            targets = []
            for tid in target_ids:
                row = await self.repo.get_target(int(tid))
                if row:
                    targets.append(row)
        else:
            targets = await self.repo.list_targets(enabled_only=True)

        pending_upload_ids: List[int] = []
        if auto_download and settings.uploader_enabled and not target_ids:
            # Reconcile files left behind by a previous cancelled/failed upload
            # instead of silently skipping them forever via the dedup ledger.
            cleared_ghosts = 0
            valid_pending: List[Dict[str, Any]] = []
            for _ in range(5):
                pending_videos = await self.repo.list_pending_upload_videos(limit=100)
                missing_vkeys: List[str] = []
                valid_pending = []
                for video in pending_videos:
                    local_path = str(video.get("local_path") or "").strip()
                    if not local_path or not Path(local_path).is_file():
                        if video.get("viewkey"):
                            missing_vkeys.append(str(video["viewkey"]))
                        continue
                    valid_pending.append(video)
                if missing_vkeys:
                    cleared_ghosts += await self.repo.clear_missing_pending_uploads(missing_vkeys)
                if not missing_vkeys or len(pending_videos) < 100:
                    break

            if cleared_ghosts:
                self.events.emit_log(
                    "WARNING",
                    f"检测到 {cleared_ghosts} 条待补传记录的本地文件已不存在，已清理待传标记。",
                    job_id=job_id,
                )

            pending_specs = [
                {
                    "viewkey": str(video.get("viewkey") or ""),
                    "title": str(video.get("title") or ""),
                    "uploader": str(video.get("uploader") or ""),
                    "target_type": str(video.get("target_type") or "model"),
                    "page_url": video.get("video_page_url")
                    or f"https://www.pornhub.com/view_video.php?viewkey={video['viewkey']}",
                    "quality": str(video.get("quality") or ""),
                    "status": "pending",
                    "local_path": str(video.get("local_path") or "").strip(),
                    "upload_requested": True,
                }
                for video in valid_pending
            ]
            if pending_specs:
                inserted_rows = await self.repo.add_job_items_batch(job_id, pending_specs)
                for row in inserted_rows:
                    pending_upload_ids.append(int(row["id"]))
                    self.events.broadcast("item_updated", row)
            if pending_upload_ids:
                refreshed_pending = await self.repo.recalculate_job_counts(job_id)
                if refreshed_pending:
                    self.events.broadcast("job_updated", refreshed_pending)
                self.events.emit_log(
                    "INFO",
                    f"检测到 {len(pending_upload_ids)} 个已下载未上传视频，纳入本次补偿上传。",
                    job_id=job_id,
                )

        if not targets and not pending_upload_ids:
            self.events.emit_log("WARNING", "追更列表为空或没有已启用的目标。", job_id=job_id)
            return

        known_vkeys = await self.repo.get_known_viewkeys()
        new_by_target: Dict[str, int] = {}
        new_item_ids: List[int] = []
        item_qual_map: Dict[int, Optional[Set[str]]] = dict.fromkeys(pending_upload_ids)
        check_notes: List[str] = []
        target_error_count = 0

        if targets:
            self.events.emit_log("INFO", f"开始检查 {len(targets)} 个追更目标的更新...", job_id=job_id)

        for target in targets:
            if ctx.cancel_event.is_set():
                break
            tid = int(target["id"])
            uname = target["username"]
            utype = target["target_type"]
            display = "Liked Videos" if utype == "liked" else uname
            qual_set = _quality_set(target.get("preferred_quality") or settings.preferred_quality)

            async def _save_profile(
                url: str,
                display_name: str,
                target_id: int = tid,
            ) -> None:
                await self.repo.update_target_profile(
                    target_id,
                    avatar_url=url,
                    display_name=display_name,
                )

            list_url = build_target_list_url(uname if uname != "_currentUser_" else None, utype)
            page_errors: List[str] = []
            previous_error = str(target.get("last_error") or "")
            # A prior pass may have saved videos before and after a failed page.
            # That new overlap must not cause us to stop before the gap next time.
            needs_rescan = bool(previous_error) and "基线" not in previous_error
            try:
                listed, details = await self._scan_target_with_details(
                    job_id,
                    ctx=ctx,
                    list_url=list_url,
                    target_type=utype,
                    settings=settings,
                    catch_up=not needs_rescan,
                    skip_vkeys=known_vkeys,
                    preferred_qualities=qual_set,
                    uploader_hint=(uname if uname != "_currentUser_" else "My_Liked", utype),
                    on_profile=_save_profile,
                    on_scan_error=page_errors.append,
                )
            except Exception as err:
                target_error_count += 1
                await self.repo.mark_target_checked(tid, 0, error=str(err))
                self.events.emit_log("WARNING", f"检查 '{display}' 失败: {err}", job_id=job_id)
                continue

            if ctx.cancel_event.is_set():
                break
            if listed:
                self.events.emit_log(
                    "INFO", f"目标 '{display}' 列表发现 {len(listed)} 个候选，详情解析结束。", job_id=job_id,
                )
            candidates = [d for d in details if not d.viewkey or d.viewkey not in known_vkeys]
            valid_new = [d for d in candidates if d.download_url and d.viewkey and not d.error]
            valid_new.sort(key=lambda d: (d.page_num, d.item_num))
            detail_failures = self._detail_failures(candidates)
            errors = list(page_errors)
            if detail_failures:
                errors.append(f"{len(detail_failures)} 个视频解析失败")
            if errors:
                target_error_count += 1
            await self.repo.mark_target_checked(tid, len(valid_new), error="；".join(errors[:3]))

            if not valid_new and not detail_failures:
                continue

            folder = _target_folder_name(uname, utype)
            if valid_new:
                new_by_target[folder] = new_by_target.get(folder, 0) + len(valid_new)
            for d in valid_new:
                known_vkeys.add(d.viewkey)

            if auto_download:
                created = await self._create_items_from_details(
                    job_id,
                    candidates,
                    default_uploader=uname if uname != "_currentUser_" else "My_Liked",
                    default_type=utype,
                    group_by_uploader=False,
                    upload_requested=settings.uploader_enabled,
                )
                new_item_ids.extend(created)
                for cid in created:
                    item_qual_map[cid] = qual_set
            elif valid_new:
                lines = [f"**{display}** 发现 {len(valid_new)} 个新视频："]
                for i, d in enumerate(valid_new, start=1):
                    lines.append(f"{i}. {d.title} ({d.quality}) - {d.page_url}")
                check_notes.append("\n".join(lines))

        self.events.broadcast("overview_changed", {})

        if target_error_count > 0:
            message = f"{target_error_count} 个目标检查失败，可查看目标列表或实时日志"
            await self.repo.update_job(job_id, error_message=message)
            self.events.emit_log("WARNING", message, job_id=job_id)

        if ctx.cancel_event.is_set():
            return

        pipeline_item_ids = [*pending_upload_ids, *new_item_ids]
        if auto_download:
            if pipeline_item_ids:
                uploaded_by, failed_urls_by = await self._run_download_upload_pipeline(
                    job_id,
                    pipeline_item_ids,
                    settings,
                    ctx,
                    auto_upload=settings.uploader_enabled,
                    redownload=False,
                    preferred_qualities=_quality_set(settings.preferred_quality),
                    item_preferred_qualities=item_qual_map,
                )
                if not ctx.cancel_event.is_set():
                    if pending_upload_ids and not new_item_ids:
                        self.events.emit_log(
                            "INFO",
                            f"已补偿上传 {len(pending_upload_ids)} 个历史本地文件。",
                            job_id=job_id,
                        )
                    title, body = build_auto_update_report(new_by_target, uploaded_by, failed_urls_by)
                    ok, msg = await send_serverchan(title, body, settings)
                    if settings.serverchan_enabled:
                        self.events.emit_log("INFO" if ok else "WARNING", f"ServerChan 通知: {msg}", job_id=job_id)
            else:
                if target_error_count == 0:
                    self.events.emit_log("INFO", "本次自动追更未发现新视频。", job_id=job_id)
                if settings.notify_if_no_updates and target_error_count == 0:
                    await send_serverchan(
                        "PH-DL 自动追更：无新内容",
                        f"本次共检查 {len(targets)} 个目标，均无更新。",
                        settings,
                    )
        else:
            total_found = sum(new_by_target.values())
            await self.repo.update_job(job_id, total_items=total_found, done_items=total_found)
            if check_notes:
                await send_serverchan("PH-DL 更新检查汇总", "\n\n---\n\n".join(check_notes), settings)
            elif settings.notify_if_no_updates and target_error_count == 0:
                await send_serverchan(
                    "PH-DL 更新检查：无新内容",
                    f"本次共检查 {len(targets)} 个目标，均无更新。",
                    settings,
                )

    async def _exec_baseline_sync(
        self,
        job_id: int,
        params: Dict[str, Any],
        settings: RuntimeSettings,
        ctx: TaskHttpContext,
    ) -> None:
        """Scan a target's videos and record viewkeys as baseline without downloading."""
        target_id = params.get("target_id")
        target = await self.repo.get_target(int(target_id)) if target_id else None
        if not target:
            await self.repo.update_job(job_id, error_message="基线初始化失败：目标不存在")
            self.events.emit_log("ERROR", "基线初始化失败：目标不存在。", job_id=job_id)
            return

        uname = target["username"]
        utype = target["target_type"]
        pages_spec = (params.get("pages") or "").strip() or None
        list_url = build_target_list_url(uname if uname != "_currentUser_" else None, utype)

        async def _save_profile(url: str, display_name: str) -> None:
            await self.repo.update_target_profile(
                int(target["id"]),
                avatar_url=url,
                display_name=display_name,
            )

        page_errors: List[str] = []
        listed = await scan_list_pages(
            ctx,
            list_url,
            settings.selectors,
            target_type=utype,
            pages_spec=pages_spec,
            delay_min=settings.page_delay_min,
            delay_max=settings.page_delay_max,
            page_concurrency=settings.max_concurrent_list_pages,
            timeout=settings.request_timeout,
            profile_username=uname,
            on_profile=_save_profile,
            on_error=page_errors.append,
        )

        batch_records = []
        for lv in listed:
            if ctx.cancel_event.is_set():
                break
            batch_records.append(
                {
                    "viewkey": lv.viewkey,
                    "title": lv.title,
                    "uploader": "" if uname == "_currentUser_" else uname,
                    "target_type": utype,
                    "video_page_url": lv.url,
                    "status": "baseline",
                }
            )
        count = await self.repo.upsert_videos_batch(batch_records)

        error = f"{len(page_errors)} 页基线未扫描完成：{page_errors[0]}" if page_errors else ""
        if not ctx.cancel_event.is_set():
            await self.repo.mark_target_checked(int(target["id"]), 0, error=error)
        await self.repo.update_job(job_id, total_items=count, done_items=count, error_message=error)
        self.events.emit_log("WARNING" if error else "INFO", f"目标 '{uname}' 已登记 {count} 个历史视频。{error}", job_id=job_id)

    async def _exec_enrich_metadata(
        self,
        job_id: int,
        params: Dict[str, Any],
        settings: RuntimeSettings,
        ctx: TaskHttpContext,
    ) -> None:
        limit = int(params.get("limit") or 0)
        candidates = await self.repo.list_baseline_metadata_candidates(limit=limit)
        if not candidates:
            self.events.emit_log("INFO", "没有需要补全元数据的基线视频。", job_id=job_id)
            return

        self.events.emit_log(
            "INFO",
            f"开始补全 {len(candidates)} 条基线视频的画质和文件大小...",
            job_id=job_id,
        )
        item_specs = [
            {
                "viewkey": candidate["viewkey"],
                "title": candidate.get("title") or "",
                "uploader": candidate.get("uploader") or "",
                "target_type": candidate.get("target_type") or "model",
                "page_url": candidate.get("video_page_url") or "",
                "quality": candidate.get("quality") or "",
                "status": "pending",
            }
            for candidate in candidates
        ]
        inserted_items = await self.repo.add_job_items_batch(job_id, item_specs)
        item_ids: Dict[str, int] = {
            row["viewkey"]: int(row["id"]) for row in inserted_items
        }

        created_job = await self.repo.update_job(job_id, total_items=len(candidates))
        if created_job:
            self.events.broadcast("job_updated", created_job)

        details = await fetch_video_details_batch(
            ctx,
            [
                (candidate["video_page_url"], 0, idx + 1)
                for idx, candidate in enumerate(candidates)
            ],
            settings.selectors,
            concurrency=settings.max_concurrent_video_details,
            skip_dom=True,
            timeout=settings.request_timeout,
        )
        detail_by_viewkey = {
            detail.viewkey: detail for detail in details if detail.viewkey
        }

        ready: List[Tuple[Dict[str, Any], VideoDetail]] = []
        detail_failures = 0
        for candidate in candidates:
            detail = detail_by_viewkey.get(candidate["viewkey"])
            if detail is None or not detail.download_url:
                detail_failures += 1
                error = detail.error if detail else "详情页未返回结果"
                item = await self.repo.update_job_item(
                    item_ids[candidate["viewkey"]],
                    status="failed",
                    error_message=error or "没有可用 MP4",
                )
                if item:
                    self.events.broadcast("item_updated", item)
                continue
            ready.append((candidate, detail))

        self.events.emit_log(
            "INFO",
            f"详情解析完成：{len(ready)} 条可继续，{detail_failures} 条详情失败；"
            "开始读取媒体文件大小...",
            job_id=job_id,
        )

        size_limit = max(1, min(20, settings.max_concurrent_video_details))
        size_sem = asyncio.Semaphore(size_limit)
        cookie_str = ctx.cookie_string()

        async def _probe(
            size_session: aiohttp.ClientSession,
            candidate: Dict[str, Any],
            detail: VideoDetail,
        ):
            async with size_sem:
                if ctx.cancel_event.is_set():
                    return candidate, detail, None, True
                size = await probe_download_size(
                    size_session,
                    detail.download_url or "",
                    referer_url=detail.referer_url or detail.page_url,
                    cookie_str=cookie_str,
                    proxy_url=settings.normalized_proxy,
                    timeout=settings.request_timeout,
                )
                return candidate, detail, size, False

        size_failures = 0
        processed = 0
        completed_with_size = 0
        if ready:
            timeout = aiohttp.ClientTimeout(total=25.0, connect=10.0, sock_read=20.0)
            connector = aiohttp.TCPConnector(limit=size_limit + 8, limit_per_host=16)
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as size_session:
                probe_results = await asyncio.gather(
                    *[
                        _probe(size_session, candidate, detail)
                        for candidate, detail in ready
                    ]
                )

            for candidate, detail, file_size, cancelled in probe_results:
                if cancelled or ctx.cancel_event.is_set():
                    break
                if file_size:
                    completed_with_size += 1
                else:
                    size_failures += 1
                await self.repo.update_video_metadata(
                    candidate["viewkey"],
                    quality=detail.quality or "",
                    file_size=int(file_size or 0),
                )
                item = await self.repo.update_job_item(
                    item_ids[candidate["viewkey"]],
                    status="done" if file_size else "failed",
                    quality=detail.quality or "",
                    bytes_total=int(file_size or 0),
                    error_message="" if file_size else "画质已保存，但无法确认媒体文件大小",
                )
                if item:
                    self.events.broadcast("item_updated", item)
                processed += 1
                if processed % 25 == 0:
                    refreshed = await self.repo.recalculate_job_counts(job_id)
                    if refreshed:
                        self.events.broadcast("job_updated", refreshed)

        self.events.emit_log(
            "INFO",
            f"元数据补全完成：画质/大小均成功 {completed_with_size} 条，"
            f"详情失败 {detail_failures} 条，大小获取失败 {size_failures} 条。",
            job_id=job_id,
        )

    def _existing_temp_size(self, temp_path_str: str) -> int:
        if not temp_path_str:
            return 0
        try:
            p = Path(temp_path_str)
            if p.is_file():
                return p.stat().st_size
        except OSError:
            pass
        return 0

    async def _exec_retry_items(
        self,
        job_id: int,
        params: Dict[str, Any],
        settings: RuntimeSettings,
        ctx: TaskHttpContext,
    ) -> None:
        source_job_id = int(params.get("source_job_id") or 0)
        source_items = await self.repo.list_job_items(source_job_id)
        retryable = [
            it
            for it in source_items
            if it["status"] in {"failed", "cancelled", "pending", "downloaded", "uploading"}
        ]
        if not retryable:
            self.events.emit_log("INFO", f"源任务 #{source_job_id} 没有可重试/续传的条目。", job_id=job_id)
            return

        # Prioritize items that already have a partial .mp4.tmp file on disk while keeping relative order
        annotated = []
        for idx, old in enumerate(retryable):
            tmp_size = self._existing_temp_size(old.get("temp_path") or "")
            if tmp_size == 0 and old.get("viewkey"):
                folder = _target_folder_name(old.get("uploader"), old.get("target_type") or "model")
                candidate = self.env.temp_dir / folder / f"{video_filename_base(old.get('title') or '', old['viewkey'])}.mp4.tmp"
                tmp_size = self._existing_temp_size(str(candidate))
                if tmp_size > 0:
                    old = {**old, "temp_path": str(candidate)}
            annotated.append((0 if tmp_size > 0 else 1, idx, old, tmp_size))
        annotated.sort(key=lambda x: (x[0], x[1]))

        original_opt_in = params.get("auto_upload")
        auto_upload = original_opt_in is True or (original_opt_in is None and settings.uploader_enabled)
        quality_option = params.get("quality")
        qualities = _quality_set(settings.preferred_quality if quality_option is None else quality_option)
        include_kw = _split_csv_set(params.get("keywords"), lower=True)
        exclude_kw = _split_csv_set(params.get("exclude_keywords"), lower=True)
        lookup_urls = list(dict.fromkeys(
            old["page_url"] for _, _, old, _ in annotated
            if not old.get("download_url") and not old.get("local_path") and old.get("page_url")
        ))
        resolved: Dict[str, VideoDetail] = {}
        if lookup_urls:
            details = await fetch_video_details_batch(
                ctx, [(url, 0, idx + 1) for idx, url in enumerate(lookup_urls)], settings.selectors,
                concurrency=settings.max_concurrent_video_details, preferred_qualities=qualities,
                include_keywords=include_kw, exclude_keywords=exclude_kw, timeout=settings.request_timeout,
            )
            resolved = {detail.page_url: detail for detail in details}
        retry_qualities: Dict[int, Optional[Set[str]]] = {}
        seen: Set[str] = set()
        new_item_ids: List[int] = []
        for _, _, old, tmp_size in annotated:
            key = old["viewkey"] or old["page_url"]
            if key in seen:
                continue
            seen.add(key)
            opt_in = old.get("upload_requested")
            wanted_upload = auto_upload and (bool(opt_in) if opt_in is not None else original_opt_in is True)
            detail = resolved.get(old["page_url"])
            status, error = "pending", ""
            if ctx.cancel_event.is_set():
                status, error = "cancelled", "已取消"
            elif detail and detail.filtered_out:
                status, error = "skipped", detail.error or "不符合原任务的筛选条件"
            elif detail and (not detail.download_url or detail.error):
                status, error = "failed", detail.error or "重新解析视频失败"
            elif detail:
                old = {**old, "viewkey": detail.viewkey, "title": detail.title,
                       "download_url": detail.download_url, "referer_url": detail.referer_url,
                       "quality": detail.quality}
            elif old["page_url"] in lookup_urls:
                status, error = "failed", "重新解析视频没有返回结果"
            row = await self.repo.add_job_item(
                job_id,
                viewkey=old["viewkey"],
                title=old["title"],
                uploader=old["uploader"],
                target_type=old["target_type"],
                page_url=old["page_url"],
                download_url=old["download_url"],
                referer_url=old["referer_url"],
                quality=old["quality"],
                temp_path=old.get("temp_path") or "",
                local_path=old.get("local_path") or "",
                bytes_downloaded=tmp_size,
                upload_requested=wanted_upload,
                status=status,
                error_message=error,
            )
            if status == "pending":
                new_item_ids.append(int(row["id"]))
            previous_quality = normalized_quality(old["quality"])
            retry_qualities[int(row["id"])] = qualities or ({previous_quality} if previous_quality else None)
            self.events.broadcast("item_updated", row)

        updated_job = await self.repo.recalculate_job_counts(job_id)
        if updated_job:
            self.events.broadcast("job_updated", updated_job)

        await self._run_download_upload_pipeline(
            job_id,
            new_item_ids,
            settings,
            ctx,
            auto_upload=auto_upload,
            redownload=bool(params.get("redownload", False)),
            preferred_qualities=qualities,
            item_preferred_qualities=retry_qualities,
        )

    async def _exec_resume_temp(
        self,
        job_id: int,
        params: Dict[str, Any],
        settings: RuntimeSettings,
        ctx: TaskHttpContext,
    ) -> None:
        temp_files = self.list_temp_files()
        if not temp_files:
            self.events.emit_log("INFO", "没有找到可续传的 .mp4.tmp 临时文件。", job_id=job_id)
            return

        auto_upload = bool(params.get("auto_upload", settings.uploader_enabled)) and settings.uploader_enabled
        new_item_ids: List[int] = []
        preferred_by_item: Dict[int, Optional[Set[str]]] = {}
        handled: Set[str] = set()
        # Prefer a checkpoint with an identity record over an older orphan for the same video.
        temp_files.sort(key=lambda tf: (not tf["verified_checkpoint"], -tf["size_bytes"]))
        for tf in temp_files:
            vkey = tf["viewkey"]
            if not vkey or vkey in handled:
                continue
            handled.add(vkey)
            prior = await self.repo.find_temp_job_item(vkey, tf["path"]) or {}
            original_opt_in = prior.get("upload_requested")
            if original_opt_in is None:
                try:
                    original_opt_in = json.loads(prior.get("params_json") or "{}").get("auto_upload") is True
                except (ValueError, AttributeError):
                    original_opt_in = False
            folder = tf["folder"]
            utype = "favorites" if folder.lower().endswith("_favorites") else ("liked" if folder.lower() == "my_liked_videos" else "model")
            uploader = folder[:-len("_favorites")] if utype == "favorites" else folder
            title = prior.get("title") or tf["name"].rsplit("[", 1)[0].strip() or vkey
            page_url = prior.get("page_url") or f"https://www.pornhub.com/view_video.php?viewkey={vkey}"
            quality = tf["quality"] or prior.get("quality") or ""
            row = await self.repo.add_job_item(
                job_id,
                viewkey=vkey,
                title=title,
                uploader=prior.get("uploader") or uploader,
                target_type=prior.get("target_type") or utype,
                page_url=page_url,
                download_url=prior.get("download_url") or "",
                referer_url=prior.get("referer_url") or page_url,
                quality=quality,
                temp_path=tf["path"],
                bytes_downloaded=int(tf.get("size_bytes") or 0),
                upload_requested=auto_upload and bool(original_opt_in),
            )
            new_item_ids.append(int(row["id"]))
            wanted_quality = normalized_quality(quality)
            preferred_by_item[int(row["id"])] = {wanted_quality} if wanted_quality else _quality_set(settings.preferred_quality)
            self.events.broadcast("item_updated", row)

        updated_job = await self.repo.recalculate_job_counts(job_id)
        if updated_job:
            self.events.broadcast("job_updated", updated_job)
        if new_item_ids:
            await self._run_download_upload_pipeline(
                job_id,
                new_item_ids,
                settings,
                ctx,
                auto_upload=auto_upload,
                redownload=False,
                item_preferred_qualities=preferred_by_item,
            )

    # ------------------------------------------------------------------
    # Disk / Residual temp file inspection
    # ------------------------------------------------------------------
    def list_temp_files(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        if not self.env.temp_dir.exists():
            return results
        for tmp in sorted(self.env.temp_dir.rglob("*.mp4.tmp")):
            try:
                if tmp.is_symlink() or not tmp.is_file():
                    continue
                st = tmp.stat()
                if st.st_size <= 0:
                    continue
                vkey = extract_viewkey_from_url(tmp.name) or ""
                if not vkey and "[" in tmp.stem and "]" in tmp.stem:
                    vkey = tmp.stem.rsplit("[", 1)[1].split("]", 1)[0]
                checkpoint = read_download_checkpoint(tmp)
                if checkpoint and checkpoint.viewkey != vkey:
                    checkpoint = None
                results.append(
                    {
                        "name": tmp.name,
                        "folder": tmp.parent.name,
                        "viewkey": vkey,
                        "size_bytes": st.st_size,
                        "path": str(tmp),
                        "quality": checkpoint.quality if checkpoint else "",
                        "verified_checkpoint": bool(checkpoint and checkpoint.etag and st.st_size <= checkpoint.total),
                    }
                )
            except OSError:
                continue
        return results

    def clear_temp_files(self) -> int:
        if not self.env.temp_dir.exists():
            return 0
        removed = 0
        for tmp in list(self.env.temp_dir.rglob("*.mp4.tmp")):
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
                remove_download_checkpoint(tmp)
                removed += 1
        return removed
