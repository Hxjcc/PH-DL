"""Core async task queue, download-to-upload streaming pipeline, and auto-update runner."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import random
import shutil
import time
import urllib.parse
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
from ..parsers.target import (
    PH_ORIGIN,
    SubscribeAction,
    SubscribeResult,
    extract_subscribe_action,
    resolve_target_type,
    subscribe_from_action,
)
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
from ..services.filename import (
    format_remote_video_filename,
    is_valid_video_file,
    normalized_quality,
    sanitize_filename,
    video_filename_base,
)
from ..services.notifier import build_auto_update_report, send_serverchan
from ..services.uploader import (
    UploadSessionCache,
    delete_verified_source,
    file_snapshot,
    upload_file_batch,
)
from .events import EventBus
from .disk_scheduler import AdaptiveDiskScheduler, SmartUploadPool, UploadQueuedItem


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


@dataclass
class _PendingParseSpec:
    item_id: int
    page_url: str
    page_num: int
    item_num: int
    default_uploader: str
    default_type: str
    group_by_uploader: bool = False
    uploader_hint: Optional[Tuple[str, str]] = None


async def _invoke_upload_file_batch(
    *,
    download_dir: Path,
    uploader_folder: str,
    files_by_viewkey: Dict[str, Path],
    settings: RuntimeSettings,
    cancel_event: Optional[asyncio.Event],
    session_cache: UploadSessionCache,
    on_progress: Optional[Callable[[str, int, int, float, str], None]],
    remote_names_by_viewkey: Optional[Dict[str, str]] = None,
):
    """Invoke `upload_file_batch`, staying compatible with legacy 5-arg test mocks."""
    params = inspect.signature(upload_file_batch).parameters
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    kwargs: Dict[str, Any] = {
        "download_dir": download_dir,
        "uploader_folder": uploader_folder,
        "files_by_viewkey": files_by_viewkey,
        "settings": settings,
        "cancel_event": cancel_event,
    }
    if has_var_kw or "session_cache" in params:
        kwargs["session_cache"] = session_cache
    if has_var_kw or "on_progress" in params:
        kwargs["on_progress"] = on_progress
    if remote_names_by_viewkey and (has_var_kw or "remote_names_by_viewkey" in params):
        kwargs["remote_names_by_viewkey"] = remote_names_by_viewkey
    return await upload_file_batch(**kwargs)


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
    # Pipeline: Streaming Pre-Parse -> Adaptive Download -> Multi-Worker Upload
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
        parse_specs: Optional[Dict[int, _PendingParseSpec]] = None,
        include_keywords: Optional[Set[str]] = None,
        exclude_keywords: Optional[Set[str]] = None,
        on_detail_parsed: Optional[Callable[[VideoDetail], Awaitable[None]]] = None,
    ) -> Tuple[Dict[str, int], Dict[str, List[str]]]:
        """Stream pre-parse, byte-adaptive downloads, and multi-worker zero-wait uploads."""
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

        disk_budget = DownloadDiskBudget(self.env.temp_dir, int(settings.min_free_disk_gb * 1024 ** 3))
        upload_worker_count = max(1, int(getattr(settings, "upload_workers", 2))) if auto_upload else 0
        scheduler = AdaptiveDiskScheduler(
            self.env.resolved_data_dir,
            disk_budget,
            max_downloads=max(1, settings.max_concurrent_downloads),
            max_uploads=max(1, upload_worker_count),
            min_free_disk_gb=settings.min_free_disk_gb,
            max_pending_upload_files=settings.max_pending_upload_files,
            disk_usage_fn=lambda p: shutil.disk_usage(p),
            on_status=lambda snap: self.events.emit_scheduler_status(job_id, snap),
        )
        upload_pool = SmartUploadPool(
            scheduler,
            batch_trigger_size=max(1, int(settings.upload_batch_size)),
            chunk_size=max(1, int(settings.rclone_transfers)),
            allow_small_micro_batch=True,
        )
        upload_session_cache = UploadSessionCache()
        scheduler.emit_status(force=True)

        async def _mark_entries_cancelled(entries: List[UploadQueuedItem]) -> None:
            if not entries:
                return
            for entry in entries:
                self.events.clear_item_progress(entry.item_id)
                it = await self.repo.update_job_item(
                    entry.item_id,
                    status="cancelled",
                    error_message="上传已取消，保留本地文件",
                )
                if it:
                    self.events.broadcast("item_updated", it)
                self.events.emit_log(
                    "WARNING",
                    f"[上传失败] {entry.local_path.name}: 上传已取消，保留本地文件",
                    job_id=job_id,
                )
            await scheduler.on_upload_discarded(len(entries), sum(e.file_size for e in entries))
            refreshed = await self.repo.recalculate_job_counts(job_id)
            if refreshed:
                self.events.broadcast("job_updated", refreshed)

        async def _upload_chunk(chunk: List[UploadQueuedItem]) -> None:
            if not chunk:
                return
            if ctx.cancel_event.is_set():
                await _mark_entries_cancelled(chunk)
                return

            uploader_folder = chunk[0].uploader_folder
            download_dir = chunk[0].download_dir
            remote_target = (
                f"{settings.rclone_remote}:{settings.rclone_remote_base_dir}/{uploader_folder}"
                if settings.rclone_remote_base_dir
                else f"{settings.rclone_remote}:{uploader_folder}"
            )
            vk_to_entry = {e.viewkey: e for e in chunk}
            chunk_bytes = sum(e.file_size for e in chunk)

            for entry in chunk:
                it = await self.repo.update_job_item(entry.item_id, status="uploading")
                if it:
                    self.events.broadcast("item_updated", it)
                self.events.emit_item_progress(
                    job_id,
                    entry.item_id,
                    0,
                    entry.file_size,
                    0.0,
                    phase="uploading",
                )

            files_map = {e.viewkey: e.local_path for e in chunk}
            remote_names_map: Dict[str, str] = {}
            for entry in chunk:
                dated_name = format_remote_video_filename(entry.local_path.name, entry.published_at)
                if dated_name and dated_name != entry.local_path.name:
                    remote_names_map[entry.viewkey] = dated_name

            if len(chunk) == 1:
                size_mb = chunk[0].file_size / (1024 * 1024)
                size_tag = f" ({size_mb:.1f} MB)" if chunk[0].file_size > 0 else ""
                remote_label = remote_names_map.get(chunk[0].viewkey) or chunk[0].local_path.name
                self.events.emit_log(
                    "INFO",
                    f"[上传] 开始上传至 {remote_target}: {remote_label}{size_tag}",
                    job_id=job_id,
                )
            else:
                self.events.emit_log(
                    "INFO",
                    f"[上传] 开始向 {remote_target} 并行上传 {len(chunk)} 个视频...",
                    job_id=job_id,
                )

            def _on_upload_progress(vk: str, sent: int, total: int, speed: float, phase: str) -> None:
                entry = vk_to_entry.get(vk)
                if entry is not None:
                    self.events.emit_item_progress(
                        job_id,
                        entry.item_id,
                        sent,
                        total if total > 0 else entry.file_size,
                        speed,
                        phase=phase,
                    )

            await scheduler.on_upload_started()
            upload_started = time.monotonic()
            try:
                up_res = await _invoke_upload_file_batch(
                    download_dir=download_dir,
                    uploader_folder=uploader_folder,
                    files_by_viewkey=files_map,
                    settings=settings,
                    cancel_event=ctx.cancel_event,
                    session_cache=upload_session_cache,
                    on_progress=_on_upload_progress,
                    remote_names_by_viewkey=remote_names_map or None,
                )
                elapsed = max(0.0, time.monotonic() - upload_started)
                uploaded_set = set(up_res.uploaded_viewkeys)

                for entry in chunk:
                    self.events.clear_item_progress(entry.item_id)
                    snapshot = up_res.verified_sources.get(entry.viewkey)
                    local_rel = entry.local_path.relative_to(download_dir).as_posix()
                    custom_remote_name = remote_names_map.get(entry.viewkey)
                    if custom_remote_name:
                        parent_rel = Path(local_rel).parent.as_posix()
                        fallback_remote_rel = f"{parent_rel}/{custom_remote_name}" if parent_rel not in {"", "."} else custom_remote_name
                    else:
                        fallback_remote_rel = local_rel
                    verified_remote_path = (
                        (getattr(up_res, "remote_paths", None) or {}).get(entry.viewkey)
                        or f"{up_res.remote_dir}/{fallback_remote_rel}"
                    )
                    if (
                        entry.viewkey in uploaded_set and snapshot is not None
                        and not ctx.cancel_event.is_set() and file_snapshot(entry.local_path) == snapshot
                        and await self.repo.record_verified_upload(
                            entry.viewkey,
                            str(entry.local_path),
                            verified_remote_path,
                        )
                    ):
                        local_path = str(entry.local_path)
                        removed_local = False
                        if not ctx.cancel_event.is_set() and delete_verified_source(entry.local_path, snapshot):
                            await self.repo.clear_uploaded_local_path(entry.viewkey, local_path)
                            local_path = ""
                            removed_local = True
                        elif not ctx.cancel_event.is_set():
                            self.events.emit_log(
                                "WARNING",
                                f"[上传] 本地副本未移除: {entry.local_path.name}",
                                job_id=job_id,
                            )
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
                        cleanup_note = "，已删除本地文件" if removed_local else "，保留本地副本"
                        speed_note = ""
                        if elapsed > 0.1 and entry.file_size > 0 and len(chunk) == 1:
                            speed_mb_s = (entry.file_size / (1024 * 1024)) / elapsed
                            speed_note = f", {speed_mb_s:.1f} MB/s"
                        remote_display_name = custom_remote_name or entry.local_path.name
                        self.events.emit_log(
                            "INFO",
                            f"[上传成功] {remote_display_name} -> {up_res.remote_dir} "
                            f"(耗时 {elapsed:.1f}s{speed_note}{cleanup_note})",
                            job_id=job_id,
                        )
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
            finally:
                for entry in chunk:
                    self.events.clear_item_progress(entry.item_id)
                # Release disk backpressure immediately once local files are deleted.
                await scheduler.on_upload_finished(len(chunk), chunk_bytes)

            refreshed = await self.repo.recalculate_job_counts(job_id)
            if refreshed:
                self.events.broadcast("job_updated", refreshed)

        async def _upload_worker() -> None:
            while True:
                chunk = await upload_pool.take_next_chunk(ctx.cancel_event)
                if chunk is None:
                    return
                await _upload_chunk(chunk)
                if ctx.cancel_event.is_set():
                    return

        async def _enqueue_for_upload(
            *,
            item_id: int,
            viewkey: str,
            uploader_folder: str,
            download_dir: Path,
            local_path: Path,
            page_url: str,
            file_size: int,
            published_at: str = "",
        ) -> None:
            size = int(file_size or 0)
            if size <= 0:
                with contextlib.suppress(OSError):
                    size = local_path.stat().st_size
            await scheduler.on_upload_queued(size)
            await upload_pool.put(
                UploadQueuedItem(
                    item_id=item_id,
                    viewkey=viewkey,
                    uploader_folder=uploader_folder,
                    download_dir=download_dir,
                    local_path=local_path,
                    page_url=page_url,
                    file_size=size,
                    published_at=(published_at or "").strip(),
                )
            )

        upload_tasks: List[asyncio.Task] = []
        if auto_upload:
            upload_tasks = [
                asyncio.create_task(_upload_worker(), name=f"phdl-upload-{job_id}-{idx + 1}")
                for idx in range(upload_worker_count)
            ]

        failed_item_ids_pass1: List[int] = []

        async def _download_items_pass(
            target_item_ids: List[int],
            pass_num: int,
            pass_parse_specs: Optional[Dict[int, _PendingParseSpec]] = None,
        ) -> List[int]:
            pass_failed: List[int] = []
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

            timeout = aiohttp.ClientTimeout(total=None, connect=20.0, sock_connect=20.0, sock_read=45.0)
            connector = aiohttp.TCPConnector(limit=64, limit_per_host=16)

            async with aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                read_bufsize=2 * 1024 * 1024,
                trust_env=True,
            ) as dl_session:
                async def _dl_one(item_id: int) -> None:
                    if ctx.cancel_event.is_set():
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
                        cleaned_stale = False
                        if partial_path is not None and partial_path.name.endswith(".mp4.tmp") and partial_path.is_file():
                            with contextlib.suppress(OSError):
                                partial_path.unlink(missing_ok=True)
                                remove_download_checkpoint(partial_path)
                                cleaned_stale = True
                        it = await self.repo.update_job_item(
                            item_id,
                            status="skipped",
                            temp_path="" if cleaned_stale else item.get("temp_path") or "",
                            error_message="已在云端上传记录中，跳过（已清理残留临时文件）" if cleaned_stale else "已在云端上传记录中，跳过",
                        )
                        if it:
                            self.events.broadcast("item_updated", it)
                        if cleaned_stale:
                            self.events.emit_log(
                                "INFO",
                                f"已在云端上传记录中，已自动清理残留临时文件: {partial_path.name}",
                                job_id=job_id,
                            )
                            self.events.broadcast("overview_changed", {})
                        await _refresh_job_progress()
                        return

                    if ledger and ledger["status"] == "downloaded" and ledger["local_path"] and quality_matches:
                        existing_local = Path(ledger["local_path"])
                        if existing_local.resolve().is_relative_to(self.env.downloads_dir.resolve()) and await asyncio.to_thread(
                            is_valid_video_file, existing_local, settings.min_file_size_bytes, ledger["file_size"] or None,
                        ):
                            existing_size = existing_local.stat().st_size
                            pub_date = str(item.get("published_at") or ledger.get("published_at") or "").strip()
                            dur_sec = int(item.get("duration_seconds") or ledger.get("duration_seconds") or 0)
                            await self.repo.upsert_video(
                                vkey,
                                status="downloaded",
                                local_path=str(existing_local),
                                file_size=existing_size,
                                upload_requested=will_upload,
                                published_at=pub_date,
                                duration_seconds=dur_sec,
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
                                await _enqueue_for_upload(
                                    item_id=item_id,
                                    viewkey=vkey,
                                    uploader_folder=uploader_folder,
                                    download_dir=existing_local.parent,
                                    local_path=existing_local,
                                    page_url=item["page_url"],
                                    file_size=existing_size,
                                    published_at=pub_date,
                                )
                            await _refresh_job_progress()
                            return

                    required_bytes = 0
                    disk_wait_rounds = 0
                    max_disk_wait_rounds = 60
                    outcome = None

                    while True:
                        ok_slot, slot_err = await scheduler.acquire_download_slot(
                            will_upload=will_upload,
                            required_bytes=required_bytes,
                            cancel_event=ctx.cancel_event,
                            on_wait=lambda msg: self.events.emit_log("INFO", msg, job_id=job_id),
                            on_request_flush=upload_pool.request_flush_nowait if auto_upload else None,
                        )
                        if not ok_slot:
                            cancelled = ctx.cancel_event.is_set()
                            it = await self.repo.update_job_item(
                                item_id,
                                status="cancelled" if cancelled else "failed",
                                error_message="已取消" if cancelled else (slot_err or "磁盘空间不足"),
                            )
                            if it:
                                self.events.broadcast("item_updated", it)
                            if not cancelled:
                                if slot_err:
                                    self.events.emit_log("ERROR", slot_err, job_id=job_id)
                                _record_failed_url(uploader_folder, item["page_url"])
                            await _refresh_job_progress()
                            return

                        try:
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
                                self.events.emit_item_progress(
                                    job_id, item_id, dl_bytes, tot_bytes, speed_bps, phase="downloading",
                                )
                                scheduler.emit_status()

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
                        finally:
                            self.events.clear_item_progress(item_id)
                            await scheduler.release_download_slot()

                        if (
                            outcome is not None
                            and not outcome.success
                            and not outcome.cancelled
                            and getattr(outcome, "disk_wait_bytes", 0) > 0
                            and will_upload
                            and not ctx.cancel_event.is_set()
                            and disk_wait_rounds < max_disk_wait_rounds
                        ):
                            disk_wait_rounds += 1
                            required_bytes = int(outcome.disk_wait_bytes)
                            scheduler.record_video_size(required_bytes)
                            it = await self.repo.update_job_item(
                                item_id,
                                status="pending",
                                temp_path=str(outcome.temp_path or ""),
                                bytes_downloaded=outcome.file_size,
                                quality=outcome.quality or item["quality"],
                                download_url=outcome.download_url or item["download_url"],
                                error_message="",
                            )
                            if it:
                                self.events.broadcast("item_updated", it)
                            if auto_upload:
                                await upload_pool.request_flush()
                            refreshed_item = await self.repo.get_job_item(item_id)
                            if refreshed_item:
                                item = refreshed_item
                                if item.get("temp_path"):
                                    partial_path = Path(item["temp_path"])
                            continue
                        break

                    if outcome is None:
                        return

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
                        if not (outcome.error or "").startswith(("磁盘", "写入", "单文件")):
                            pass_failed.append(item_id)
                        else:
                            _record_failed_url(uploader_folder, item["page_url"])
                        self.events.emit_log("WARNING", f"下载失败: {item['title']} ({outcome.error})", job_id=job_id)
                        await _refresh_job_progress()
                        return

                    scheduler.record_video_size(outcome.file_size)
                    pub_date = str(item.get("published_at") or "").strip()
                    dur_sec = int(item.get("duration_seconds") or 0)
                    if not pub_date or dur_sec <= 0:
                        existing_video = ledger or (await self.repo.get_video(vkey) if vkey else None)
                        if existing_video:
                            pub_date = pub_date or str(existing_video.get("published_at") or "").strip()
                            if dur_sec <= 0:
                                dur_sec = int(existing_video.get("duration_seconds") or 0)
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
                        published_at=pub_date,
                        duration_seconds=dur_sec,
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
                        await _enqueue_for_upload(
                            item_id=item_id,
                            viewkey=vkey,
                            uploader_folder=uploader_folder,
                            download_dir=dl_dir,
                            local_path=outcome.local_path,
                            page_url=item["page_url"],
                            file_size=outcome.file_size,
                            published_at=pub_date,
                        )

                    await _refresh_job_progress()

                worker_count = min(len(target_item_ids), max(1, settings.max_concurrent_downloads))
                if worker_count > 0:
                    ready_queue: asyncio.Queue[Optional[int]] = asyncio.Queue()
                    in_window_ids: Set[int] = set()
                    window_sem: Optional[asyncio.Semaphore] = None
                    producer_task: Optional[asyncio.Task] = None

                    if pass_parse_specs:
                        prefetch_window = max(8, max(1, settings.max_concurrent_downloads) * 3)
                        parse_concurrency = max(1, min(settings.max_concurrent_video_details, 6))
                        window_sem = asyncio.Semaphore(prefetch_window)

                        async def _preparse_producer() -> None:
                            parse_input: asyncio.Queue[Optional[int]] = asyncio.Queue()
                            for iid in target_item_ids:
                                parse_input.put_nowait(iid)
                            for _ in range(parse_concurrency):
                                parse_input.put_nowait(None)

                            async def _parse_worker() -> None:
                                assert window_sem is not None
                                while True:
                                    iid = await parse_input.get()
                                    try:
                                        if iid is None:
                                            return
                                        spec = pass_parse_specs.get(iid)
                                        if spec is None:
                                            await ready_queue.put(iid)
                                            continue

                                        if ctx.cancel_event.is_set():
                                            continue

                                        try:
                                            await until_stopped(window_sem.acquire(), ctx.cancel_event)
                                        except StopRequested:
                                            continue

                                        handed_off = False
                                        try:
                                            if ctx.cancel_event.is_set():
                                                continue

                                            details = await fetch_video_details_batch(
                                                ctx,
                                                [(spec.page_url, spec.page_num, spec.item_num)],
                                                settings.selectors,
                                                concurrency=1,
                                                preferred_qualities=_resolve_item_qualities(iid),
                                                include_keywords=include_keywords,
                                                exclude_keywords=exclude_keywords,
                                                uploader_hint=spec.uploader_hint,
                                                timeout=settings.request_timeout,
                                            )
                                            detail = details[0] if details else VideoDetail(
                                                viewkey=extract_viewkey_from_url(spec.page_url) or "",
                                                title="",
                                                page_url=spec.page_url,
                                                referer_url=spec.page_url,
                                                error="未解析到视频详情",
                                            )

                                            if ctx.cancel_event.is_set() or detail.error == "操作已取消":
                                                it = await self.repo.update_job_item(iid, status="cancelled", error_message="已取消")
                                                if it:
                                                    self.events.broadcast("item_updated", it)
                                                continue

                                            if on_detail_parsed is not None and (not detail.error or detail.filtered_out):
                                                try:
                                                    await on_detail_parsed(detail)
                                                except Exception as link_err:
                                                    self.events.emit_log(
                                                        "WARNING",
                                                        f"博主联动处理失败: {link_err}",
                                                        job_id=job_id,
                                                    )

                                            if detail.filtered_out:
                                                it = await self.repo.update_job_item(
                                                    iid,
                                                    status="skipped",
                                                    error_message="不符合画质或关键词筛选，跳过",
                                                )
                                                if it:
                                                    self.events.broadcast("item_updated", it)
                                                await _refresh_job_progress()
                                                continue

                                            err_msg = detail.error or (
                                                "未解析到视频标识或 MP4 直链"
                                                if not detail.download_url or not detail.viewkey
                                                else ""
                                            )
                                            if err_msg:
                                                it = await self.repo.update_job_item(
                                                    iid,
                                                    status="failed",
                                                    error_message=err_msg,
                                                )
                                                if it:
                                                    self.events.broadcast("item_updated", it)
                                                self.events.emit_log(
                                                    "WARNING",
                                                    f"视频解析失败 [{detail.viewkey or spec.page_url}]: {err_msg}",
                                                    job_id=job_id,
                                                )
                                                folder = _target_folder_name(spec.default_uploader, spec.default_type)
                                                _record_failed_url(folder, spec.page_url)
                                                await _refresh_job_progress()
                                                continue

                                            resolved_uploader = (
                                                (detail.uploader_username or spec.default_uploader)
                                                if spec.group_by_uploader
                                                else spec.default_uploader
                                            )
                                            resolved_type = (
                                                (detail.uploader_type or spec.default_type)
                                                if spec.group_by_uploader
                                                else spec.default_type
                                            )
                                            patch: Dict[str, Any] = {
                                                "viewkey": detail.viewkey,
                                                "uploader": resolved_uploader,
                                                "target_type": resolved_type,
                                                "download_url": detail.download_url,
                                                "referer_url": detail.referer_url,
                                                "quality": detail.quality,
                                            }
                                            if detail.title:
                                                patch["title"] = detail.title
                                            if detail.published_at:
                                                patch["published_at"] = detail.published_at
                                            if detail.duration_seconds > 0:
                                                patch["duration_seconds"] = detail.duration_seconds
                                            it = await self.repo.update_job_item(iid, **patch)
                                            if it:
                                                self.events.broadcast("item_updated", it)

                                            in_window_ids.add(iid)
                                            await ready_queue.put(iid)
                                            handed_off = True
                                        finally:
                                            if not handed_off:
                                                window_sem.release()
                                    finally:
                                        parse_input.task_done()

                            pworkers = [asyncio.create_task(_parse_worker()) for _ in range(parse_concurrency)]
                            try:
                                await asyncio.gather(*pworkers)
                            finally:
                                for pw in pworkers:
                                    if not pw.done():
                                        pw.cancel()
                                await asyncio.gather(*pworkers, return_exceptions=True)
                                for _ in range(worker_count):
                                    await ready_queue.put(None)

                        producer_task = asyncio.create_task(_preparse_producer(), name=f"phdl-preparse-{job_id}")
                    else:
                        for iid in target_item_ids:
                            ready_queue.put_nowait(iid)
                        for _ in range(worker_count):
                            ready_queue.put_nowait(None)

                    async def _worker() -> None:
                        while True:
                            iid = await ready_queue.get()
                            try:
                                if iid is None:
                                    return
                                if window_sem is not None and iid in in_window_ids:
                                    in_window_ids.discard(iid)
                                    window_sem.release()
                                await _dl_one(iid)
                            finally:
                                ready_queue.task_done()

                    workers = [asyncio.create_task(_worker()) for _ in range(worker_count)]
                    try:
                        if producer_task is not None:
                            await asyncio.gather(producer_task, *workers)
                        else:
                            await asyncio.gather(*workers)
                    except BaseException:
                        if producer_task is not None and not producer_task.done():
                            producer_task.cancel()
                        for worker in workers:
                            worker.cancel()
                        tasks_to_wait = [*workers, *([producer_task] if producer_task is not None else [])]
                        await asyncio.gather(*tasks_to_wait, return_exceptions=True)
                        raise
                if ctx.cancel_event.is_set():
                    await self.repo.bulk_cancel_pending_job_items(job_id, error_message="已取消")
                    self.events.broadcast("job_items_changed", {"job_id": job_id})
                await _refresh_job_progress(force=True)

            return pass_failed

        try:
            failed_item_ids_pass1 = await _download_items_pass(item_ids, pass_num=1, pass_parse_specs=parse_specs)

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
                            if d.published_at:
                                patch["published_at"] = d.published_at
                            if d.duration_seconds > 0:
                                patch["duration_seconds"] = d.duration_seconds
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
            if upload_tasks:
                await upload_pool.mark_downloads_done()
                if ctx.cancel_event.is_set():
                    await upload_pool.notify_cancel()
                await asyncio.gather(*upload_tasks, return_exceptions=True)
                leftovers = await upload_pool.drain_all()
                if leftovers:
                    await _mark_entries_cancelled(leftovers)
            self.events.clear_scheduler_status(job_id)

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
                    "published_at": detail.published_at,
                    "duration_seconds": detail.duration_seconds,
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
        on_subscribe_action: Optional[Callable[[SubscribeAction, str], Awaitable[None]]] = None,
        on_page_progress: Optional[Callable[[int, int, int], None]] = None,
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
            scan_kwargs: Dict[str, Any] = {
                "target_type": target_type,
                "pages_spec": pages_spec,
                "videos_spec": videos_spec,
                "catch_up": catch_up,
                "skip_vkeys": skip_vkeys,
                "delay_min": settings.page_delay_min,
                "delay_max": settings.page_delay_max,
                "page_concurrency": settings.max_concurrent_list_pages,
                "timeout": settings.request_timeout,
                "profile_username": uploader_hint[0] if uploader_hint else None,
                "on_profile": on_profile,
                "on_page": _on_page,
                "on_error": on_scan_error,
            }
            if on_subscribe_action is not None:
                scan_kwargs["on_subscribe_action"] = on_subscribe_action
            if on_page_progress is not None:
                scan_kwargs["on_page_progress"] = on_page_progress
            listed = await scan_list_pages(
                ctx,
                list_url,
                settings.selectors,
                **scan_kwargs,
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

    async def _maybe_track_creator(
        self,
        job_id: int,
        username: str,
        target_type: str,
        *,
        display_name: str = "",
        avatar_url: str = "",
        tracked_keys: Set[Tuple[str, str]],
    ) -> None:
        uname = (username or "").strip()
        utype = (target_type or "model").strip().lower()
        if not uname or uname == "_currentUser_":
            return
        if utype not in {"model", "pornstar"}:
            dedup = (utype, uname.lower())
            if dedup not in tracked_keys and utype == "channel":
                tracked_keys.add(dedup)
                self.events.emit_log("INFO", f"跳过加入自动追更 '{uname}'：暂不支持 Channel 类型追更。", job_id=job_id)
            return
        dedup = (utype, uname.lower())
        if dedup in tracked_keys:
            return
        tracked_keys.add(dedup)
        target, created = await self.repo.ensure_target(
            uname,
            utype,
            avatar_url=avatar_url,
            display_name=display_name,
        )
        shown = (target or {}).get("display_name") or display_name or uname
        if created:
            self.events.emit_log("INFO", f"已自动加入追更目标: {shown} ({utype})", job_id=job_id)
            self.events.broadcast("targets_changed", {})
            self.events.broadcast("overview_changed", {})
        else:
            self.events.emit_log("INFO", f"已在自动追更列表中: {shown} ({utype})", job_id=job_id)

    async def _maybe_subscribe_creator(
        self,
        job_id: int,
        ctx: TaskHttpContext,
        action: Optional[SubscribeAction],
        *,
        referer_url: str,
        creator_label: str,
        dedup_key: str,
        subscribed_keys: Set[str],
        timeout: float,
    ) -> Optional[SubscribeResult]:
        if ctx.cancel_event.is_set():
            return None
        key = (action.creator_id if action and action.creator_id else dedup_key).strip().lower()
        if not key or key in subscribed_keys:
            return None
        subscribed_keys.add(key)

        res = await subscribe_from_action(
            ctx,
            action,
            referer_url=referer_url,
            label=creator_label,
            timeout=timeout,
        )
        if res.status == "already_subscribed":
            self.events.emit_log("INFO", f"已订阅博主 '{creator_label}'，跳过重复订阅。", job_id=job_id)
        elif res.ok:
            self.events.emit_log("INFO", f"已自动订阅博主: {creator_label}", job_id=job_id)
            self.events.broadcast("subscriptions_changed", {})
        else:
            self.events.emit_log("WARNING", f"自动订阅博主 '{creator_label}' 失败: {res.message}", job_id=job_id)
        return res

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
        add_to_auto_track = bool(params.get("add_to_auto_track", False))
        subscribe_creator = bool(params.get("subscribe_creator", False))

        targets: List[Tuple[str, str]] = []
        if raw_type == "liked":
            targets.append(("_currentUser_", "liked"))
        else:
            if not usernames and raw_type == "favorites" and settings.ph_logged_in_username:
                usernames = [settings.ph_logged_in_username]
            seen_users: Set[str] = set()
            for uname in usernames:
                if uname.casefold() in seen_users:
                    continue
                seen_users.add(uname.casefold())
                if raw_type in {"model", "pornstar", "favorites"}:
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
        scoped_listed: Dict[str, Tuple[ListedVideo, str, str]] = {}
        scan_errors: List[str] = []
        profile_avatars: Dict[str, str] = dict(params.get("profile_avatars") or {})
        profile_names: Dict[str, str] = dict(params.get("profile_names") or {})
        tracked_keys: Set[Tuple[str, str]] = set()
        subscribed_keys: Set[str] = set()

        for target_idx, (uname, utype) in enumerate(targets):
            if ctx.cancel_event.is_set():
                break
            list_url = build_target_list_url(uname if uname != "_currentUser_" else None, utype)
            target_display = "Liked Videos" if utype == "liked" else (profile_names.get(uname) or uname)

            def _emit_manual_page_progress(
                page_num: int,
                _page_total: int,
                found_count: int,
                t_idx: int = target_idx,
                t_name: str = target_display,
            ) -> None:
                self.events.emit_scan_status(
                    job_id,
                    {
                        "mode": "pages",
                        "target_index": t_idx + 1,
                        "total_targets": len(targets),
                        "current_target": t_name,
                        "page": int(page_num),
                        "found_videos": int(found_count),
                    },
                )

            _emit_manual_page_progress(1, 0, len(scoped_listed))

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

            can_link_creator = utype in {"model", "pornstar"} and uname != "_currentUser_"
            subscribe_attempted = False

            async def _handle_list_subscribe(
                action: SubscribeAction,
                referer_url: str,
                target_user: str = uname,
                target_type: str = utype,
            ) -> None:
                nonlocal subscribe_attempted
                subscribe_attempted = True
                label = profile_names.get(target_user) or target_user
                sub_res = await self._maybe_subscribe_creator(
                    job_id,
                    ctx,
                    action,
                    referer_url=referer_url,
                    creator_label=label,
                    dedup_key=f"{target_type}:{target_user.lower()}",
                    subscribed_keys=subscribed_keys,
                    timeout=settings.request_timeout,
                )
                if sub_res and sub_res.avatar_url and not profile_avatars.get(target_user):
                    await _save_target_profile(sub_res.avatar_url, profile_names.get(target_user) or "")

            on_sub_cb = _handle_list_subscribe if (subscribe_creator and can_link_creator) else None
            page_errors: List[str] = []
            resolved_user = uname if uname != "_currentUser_" else "My_Liked"
            if export_only:
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
                    uploader_hint=(resolved_user, utype),
                    on_profile=_save_target_profile,
                    on_subscribe_action=on_sub_cb,
                    on_page_progress=_emit_manual_page_progress,
                    on_scan_error=page_errors.append,
                )
                scan_errors.extend(f"{uname}: {error}" for error in page_errors)
            else:
                scan_kwargs: Dict[str, Any] = {
                    "target_type": utype,
                    "pages_spec": pages_spec,
                    "videos_spec": videos_spec,
                    "catch_up": False,
                    "skip_vkeys": known_vkeys,
                    "delay_min": settings.page_delay_min,
                    "delay_max": settings.page_delay_max,
                    "page_concurrency": settings.max_concurrent_list_pages,
                    "timeout": settings.request_timeout,
                    "profile_username": resolved_user,
                    "on_profile": _save_target_profile,
                    "on_page_progress": _emit_manual_page_progress,
                    "on_error": page_errors.append,
                }
                if on_sub_cb is not None:
                    scan_kwargs["on_subscribe_action"] = on_sub_cb
                listed = await scan_list_pages(
                    ctx,
                    list_url,
                    settings.selectors,
                    **scan_kwargs,
                )
                details = []
                scan_errors.extend(f"{uname}: {error}" for error in page_errors)

            if subscribe_creator and can_link_creator and not subscribe_attempted and not ctx.cancel_event.is_set():
                # Fallback to profile homepage only if list pages yielded no subscribe button.
                profile_url = f"{PH_ORIGIN}/{utype}/{urllib.parse.quote(uname, safe='')}"
                prof_res, _ = await ctx.fetch_with_pow(
                    profile_url,
                    timeout=max(5.0, float(settings.request_timeout)),
                    max_retries=1,
                    log_label=f"获取博主主页 {uname}",
                )
                fallback_action = (
                    extract_subscribe_action(prof_res.text)
                    if prof_res is not None and prof_res.status_code < 400
                    else None
                )
                await _handle_list_subscribe(
                    fallback_action or SubscribeAction(),
                    (prof_res.final_url if prof_res is not None else profile_url),
                )

            if add_to_auto_track and can_link_creator and not ctx.cancel_event.is_set():
                await self._maybe_track_creator(
                    job_id,
                    uname,
                    utype,
                    display_name=profile_names.get(uname) or "",
                    avatar_url=profile_avatars.get(uname) or "",
                    tracked_keys=tracked_keys,
                )

            if not listed:
                if not page_errors and not ctx.cancel_event.is_set():
                    self.events.emit_log("INFO", f"目标 '{uname}' ({utype}) 未发现待处理的视频。", job_id=job_id)
                continue

            if export_only:
                all_details.extend(details)
            else:
                for lv in listed:
                    key = lv.viewkey or lv.url
                    if key not in scoped_listed:
                        scoped_listed[key] = (lv, resolved_user, utype)

        if scan_errors:
            await self.repo.update_job(job_id, error_message=f"{len(scan_errors)} 页扫描未完成：{scan_errors[0]}")
        if export_only:
            await self._record_export_result(job_id, all_details)
            return

        if not scoped_listed or ctx.cancel_event.is_set():
            return

        listed_entries = list(scoped_listed.values())
        item_specs = [
            {
                "viewkey": lv.viewkey,
                "title": lv.title or lv.viewkey or lv.url,
                "uploader": uname,
                "target_type": utype,
                "page_url": lv.url,
                "download_url": "",
                "referer_url": lv.url,
                "quality": "",
                "status": "pending",
                "error_message": "",
                "upload_requested": auto_upload,
                "published_at": lv.published_at,
                "duration_seconds": lv.duration_seconds,
            }
            for lv, uname, utype in listed_entries
        ]
        rows = await self.repo.add_job_items_batch(job_id, item_specs)
        all_item_ids: List[int] = []
        parse_specs: Dict[int, _PendingParseSpec] = {}
        for row, (lv, uname, utype) in zip(rows, listed_entries):
            iid = int(row["id"])
            all_item_ids.append(iid)
            parse_specs[iid] = _PendingParseSpec(
                item_id=iid,
                page_url=lv.url,
                page_num=lv.page_num,
                item_num=lv.item_num,
                default_uploader=uname,
                default_type=utype,
                group_by_uploader=False,
                uploader_hint=(uname, utype),
            )
            self.events.broadcast("item_updated", row)
        updated_job = await self.repo.recalculate_job_counts(job_id)
        if updated_job:
            self.events.broadcast("job_updated", updated_job)
        self.events.emit_log(
            "INFO",
            f"列表扫描完成：共 {len(all_item_ids)} 个视频，启动流式按需解析、动态下载与实时上传...",
            job_id=job_id,
        )
        if all_item_ids and not ctx.cancel_event.is_set():
            self.events.clear_scan_status(job_id)
            await self._run_download_upload_pipeline(
                job_id,
                all_item_ids,
                settings,
                ctx,
                auto_upload=auto_upload,
                redownload=redownload,
                preferred_qualities=qualities,
                parse_specs=parse_specs,
                include_keywords=include_kw,
                exclude_keywords=exclude_kw,
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
        add_to_auto_track = bool(params.get("add_to_auto_track", False))
        subscribe_creator = bool(params.get("subscribe_creator", False))
        batch_folder = sanitize_filename(params.get("batch_name") or "url_batch")

        creator_link_lock = asyncio.Lock()
        tracked_keys: Set[Tuple[str, str]] = set()
        subscribed_keys: Set[str] = set()

        async def _handle_url_creator_linkage(detail: VideoDetail) -> None:
            if not (add_to_auto_track or subscribe_creator) or ctx.cancel_event.is_set():
                return
            async with creator_link_lock:
                sub_res: Optional[SubscribeResult] = None
                uploader_name = (detail.uploader_username or "").strip()
                uploader_type = (detail.uploader_type or "model").strip().lower()
                creator_label = uploader_name or (detail.subscribe_action.creator_id if detail.subscribe_action else "") or detail.viewkey
                if subscribe_creator:
                    dedup_key = f"{uploader_type}:{uploader_name.lower()}" if uploader_name else (detail.viewkey or detail.page_url)
                    sub_res = await self._maybe_subscribe_creator(
                        job_id,
                        ctx,
                        detail.subscribe_action,
                        referer_url=detail.referer_url or detail.page_url,
                        creator_label=creator_label,
                        dedup_key=dedup_key,
                        subscribed_keys=subscribed_keys,
                        timeout=settings.request_timeout,
                    )
                if add_to_auto_track and uploader_name:
                    await self._maybe_track_creator(
                        job_id,
                        uploader_name,
                        uploader_type,
                        avatar_url=sub_res.avatar_url if sub_res else "",
                        tracked_keys=tracked_keys,
                    )

        if export_only:
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
            if add_to_auto_track or subscribe_creator:
                for d in details:
                    if not d.error or d.filtered_out:
                        await _handle_url_creator_linkage(d)
            await self._record_export_result(job_id, details)
            return

        item_specs = [
            {
                "viewkey": extract_viewkey_from_url(u) or "",
                "title": extract_viewkey_from_url(u) or u,
                "uploader": batch_folder,
                "target_type": "model",
                "page_url": u,
                "download_url": "",
                "referer_url": u,
                "quality": "",
                "status": "pending",
                "error_message": "",
                "upload_requested": auto_upload,
            }
            for u in video_urls
        ]
        rows = await self.repo.add_job_items_batch(job_id, item_specs)
        item_ids: List[int] = []
        parse_specs: Dict[int, _PendingParseSpec] = {}
        for idx, (row, u) in enumerate(zip(rows, video_urls), start=1):
            iid = int(row["id"])
            item_ids.append(iid)
            parse_specs[iid] = _PendingParseSpec(
                item_id=iid,
                page_url=u,
                page_num=1,
                item_num=idx,
                default_uploader=batch_folder,
                default_type="model",
                group_by_uploader=group_by_uploader,
            )
            self.events.broadcast("item_updated", row)
        updated_job = await self.repo.recalculate_job_counts(job_id)
        if updated_job:
            self.events.broadcast("job_updated", updated_job)
        self.events.emit_log(
            "INFO",
            f"已入队 {len(item_ids)} 条链接，启动流式按需解析、动态下载与实时上传...",
            job_id=job_id,
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
                parse_specs=parse_specs,
                include_keywords=include_kw,
                exclude_keywords=exclude_kw,
                on_detail_parsed=_handle_url_creator_linkage if (add_to_auto_track or subscribe_creator) else None,
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
                    "published_at": str(video.get("published_at") or "").strip(),
                    "duration_seconds": int(video.get("duration_seconds") or 0),
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

        target_concurrency = max(1, min(6, int(getattr(settings, "update_target_concurrency", 2) or 1)))
        raw_scan_mode = str(params.get("scan_mode") or getattr(settings, "update_scan_mode", "catch_up") or "catch_up").strip().lower()
        full_scan = raw_scan_mode == "full"
        if targets:
            mode_label = "全量翻页" if full_scan else "智能衔接"
            self.events.emit_log(
                "INFO",
                f"开始检查 {len(targets)} 个追更目标的更新（{mode_label}，目标并发 {target_concurrency}）...",
                job_id=job_id,
            )

        completed_targets = 0
        target_sem = asyncio.Semaphore(target_concurrency)
        # Preserve original target ordering when emitting check notes / creating job items.
        ordered_outcomes: List[Optional[Dict[str, Any]]] = [None] * len(targets)

        async def _scan_one_target(target_idx: int, target: Dict[str, Any]) -> None:
            nonlocal completed_targets, target_error_count
            async with target_sem:
                if ctx.cancel_event.is_set():
                    return
                if target_idx > 0 and settings.page_delay_max > 0:
                    delay_lo = max(0.0, float(settings.page_delay_min))
                    delay_hi = max(delay_lo, float(settings.page_delay_max))
                    try:
                        await until_stopped(
                            asyncio.sleep(random.uniform(delay_lo, delay_hi)),
                            ctx.cancel_event,
                        )
                    except StopRequested:
                        return
                tid = int(target["id"])
                uname = target["username"]
                utype = target["target_type"]
                display = "Liked Videos" if utype == "liked" else uname
                display_label = "Liked Videos" if utype == "liked" else ((target.get("display_name") or "").strip() or uname)
                qual_set = _quality_set(target.get("preferred_quality") or settings.preferred_quality)

                self.events.emit_scan_status(
                    job_id,
                    {
                        "mode": "targets",
                        "scanned": completed_targets,
                        "total": len(targets),
                        "current_target": display_label,
                        "found_new": sum(new_by_target.values()),
                    },
                )

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
                # However, if page 1 itself failed with 0 new videos saved (e.g. a transient
                # 403), there is no gap behind a saved head — keep catch_up enabled so we
                # don't trigger a full historical rescan across every affected target.
                only_page1_failed = (
                    previous_error.startswith("列表第 1 页:")
                    and "；" not in previous_error
                    and int(target.get("last_new_count") or 0) == 0
                )
                needs_rescan = (
                    bool(previous_error)
                    and "基线" not in previous_error
                    and not only_page1_failed
                )
                try:
                    listed, details = await self._scan_target_with_details(
                        job_id,
                        ctx=ctx,
                        list_url=list_url,
                        target_type=utype,
                        settings=settings,
                        catch_up=(not full_scan and not needs_rescan),
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
                    completed_targets += 1
                    self.events.emit_scan_status(
                        job_id,
                        {
                            "mode": "targets",
                            "scanned": completed_targets,
                            "total": len(targets),
                            "current_target": display_label,
                            "found_new": sum(new_by_target.values()),
                        },
                    )
                    return

                if ctx.cancel_event.is_set():
                    return
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

                folder = _target_folder_name(uname, utype)
                if valid_new:
                    new_by_target[folder] = new_by_target.get(folder, 0) + len(valid_new)
                    for d in valid_new:
                        known_vkeys.add(d.viewkey)

                completed_targets += 1
                self.events.emit_scan_status(
                    job_id,
                    {
                        "mode": "targets",
                        "scanned": completed_targets,
                        "total": len(targets),
                        "current_target": display_label,
                        "found_new": sum(new_by_target.values()),
                    },
                )

                if valid_new or detail_failures:
                    ordered_outcomes[target_idx] = {
                        "uname": uname,
                        "utype": utype,
                        "display": display,
                        "qual_set": qual_set,
                        "candidates": candidates,
                        "valid_new": valid_new,
                    }

        if targets:
            if target_concurrency == 1:
                for target_idx, target in enumerate(targets):
                    if ctx.cancel_event.is_set():
                        break
                    await _scan_one_target(target_idx, target)
            else:
                await asyncio.gather(*(_scan_one_target(idx, t) for idx, t in enumerate(targets)))

        for outcome in ordered_outcomes:
            if outcome is None or ctx.cancel_event.is_set():
                continue
            uname = outcome["uname"]
            utype = outcome["utype"]
            display = outcome["display"]
            qual_set = outcome["qual_set"]
            candidates = outcome["candidates"]
            valid_new = outcome["valid_new"]
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
                    qual_tag = f" ({d.quality})" if d.quality else ""
                    lines.append(f"{i}. {d.title}{qual_tag} - {d.page_url}")
                    self.events.emit_log(
                        "INFO",
                        f"[发现更新] {display} #{i}: {d.title}{qual_tag} - {d.page_url}",
                        job_id=job_id,
                    )
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
                self.events.clear_scan_status(job_id)
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
            if total_found > 0:
                self.events.emit_log(
                    "INFO",
                    f"仅检查更新完成：共发现 {total_found} 个新视频（未下载，如需下载请在「自动追更」执行全量追更）。",
                    job_id=job_id,
                )
            elif target_error_count == 0:
                self.events.emit_log(
                    "INFO",
                    f"仅检查更新完成：共检查 {len(targets)} 个目标，暂未发现新视频。",
                    job_id=job_id,
                )
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

        target_label = "Liked Videos" if utype == "liked" else ((target.get("display_name") or "").strip() or uname)

        def _emit_baseline_page_progress(page_num: int, _page_total: int, found_count: int) -> None:
            self.events.emit_scan_status(
                job_id,
                {
                    "mode": "baseline",
                    "current_target": target_label,
                    "page": int(page_num),
                    "found_videos": int(found_count),
                },
            )

        _emit_baseline_page_progress(1, 0, 0)

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
            on_page_progress=_emit_baseline_page_progress,
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
                    "uploader": "My_Liked" if (utype == "liked" or uname == "_currentUser_") else uname,
                    "target_type": utype,
                    "video_page_url": lv.url,
                    "status": "baseline",
                    "published_at": lv.published_at,
                    "duration_seconds": lv.duration_seconds,
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

        created_job = await self.repo.update_job(job_id, total_items=len(candidates), status="running")
        if created_job:
            self.events.broadcast("job_updated", created_job)

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
                try:
                    size = await until_stopped(
                        probe_download_size(
                            size_session,
                            detail.download_url or "",
                            referer_url=detail.referer_url or detail.page_url,
                            cookie_str=cookie_str,
                            proxy_url=settings.normalized_proxy,
                            timeout=settings.request_timeout,
                        ),
                        ctx.cancel_event,
                    )
                except StopRequested:
                    return candidate, detail, None, True
                return candidate, detail, size, False

        chunk_size = max(20, min(60, int(settings.max_concurrent_video_details) * 2))
        detail_failures = 0
        size_failures = 0
        completed_with_size = 0

        timeout = aiohttp.ClientTimeout(total=25.0, connect=10.0, sock_read=20.0)
        connector = aiohttp.TCPConnector(limit=size_limit + 8, limit_per_host=16)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as size_session:
            for start in range(0, len(candidates), chunk_size):
                if ctx.cancel_event.is_set():
                    break
                batch = candidates[start : start + chunk_size]
                details = await fetch_video_details_batch(
                    ctx,
                    [
                        (candidate["video_page_url"], 0, start + idx + 1)
                        for idx, candidate in enumerate(batch)
                    ],
                    settings.selectors,
                    concurrency=settings.max_concurrent_video_details,
                    skip_dom=True,
                    timeout=settings.request_timeout,
                )
                if ctx.cancel_event.is_set():
                    break
                detail_by_viewkey = {
                    detail.viewkey: detail for detail in details if detail.viewkey
                }

                ready: List[Tuple[Dict[str, Any], VideoDetail]] = []
                for candidate in batch:
                    detail = detail_by_viewkey.get(candidate["viewkey"])
                    if detail is not None and detail.error == "操作已取消":
                        continue
                    if detail is None or not detail.download_url:
                        detail_failures += 1
                        error = detail.error if detail else "详情页未返回结果"
                        await self.repo.update_job_item(
                            item_ids[candidate["viewkey"]],
                            status="failed",
                            error_message=error or "没有可用 MP4",
                        )
                        continue
                    ready.append((candidate, detail))

                if ctx.cancel_event.is_set():
                    break

                if ready:
                    probe_results = await asyncio.gather(
                        *[
                            _probe(size_session, candidate, detail)
                            for candidate, detail in ready
                        ]
                    )
                    for candidate, detail, file_size, cancelled in probe_results:
                        if cancelled or ctx.cancel_event.is_set():
                            continue
                        if file_size:
                            completed_with_size += 1
                        else:
                            size_failures += 1
                        await self.repo.update_video_metadata(
                            candidate["viewkey"],
                            quality=detail.quality or "",
                            file_size=int(file_size or 0),
                            published_at=detail.published_at or "",
                            duration_seconds=int(detail.duration_seconds or 0),
                        )
                        await self.repo.update_job_item(
                            item_ids[candidate["viewkey"]],
                            status="done" if file_size else "failed",
                            quality=detail.quality or "",
                            bytes_total=int(file_size or 0),
                            error_message="" if file_size else "画质已保存，但无法确认媒体文件大小",
                        )

                if ctx.cancel_event.is_set():
                    break

                refreshed = await self.repo.recalculate_job_counts(job_id)
                if refreshed:
                    self.events.broadcast("job_updated", refreshed)
                self.events.broadcast("job_items_changed", {"job_id": job_id})

        if ctx.cancel_event.is_set():
            await self.repo.bulk_cancel_pending_job_items(job_id, error_message="已取消")
            self.events.broadcast("job_items_changed", {"job_id": job_id})
            self.events.emit_log(
                "WARNING",
                f"元数据补全已取消：已完成 {completed_with_size} 条，其余条目已批量取消。",
                job_id=job_id,
            )
            return

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
        target_qual_by_pair: Dict[Tuple[str, str], Optional[Set[str]]] = {}
        target_qual_by_user: Dict[str, Optional[Set[str]]] = {}
        if quality_option is None:
            for t in await self.repo.list_targets():
                q_set = _quality_set(t.get("preferred_quality"))
                if not q_set:
                    continue
                u_low = str(t.get("username") or "").strip().lower()
                t_low = str(t.get("target_type") or "model").strip().lower()
                if t_low == "liked":
                    target_qual_by_pair[("my_liked", "liked")] = q_set
                    target_qual_by_pair[("_currentuser_", "liked")] = q_set
                else:
                    target_qual_by_pair[(u_low, t_low)] = q_set
                    if t_low not in {"favorites", "liked"} and u_low not in target_qual_by_user:
                        target_qual_by_user[u_low] = q_set

        def _item_default_qualities(old_item: Dict[str, Any]) -> Optional[Set[str]]:
            if quality_option is not None:
                return qualities
            u_low = str(old_item.get("uploader") or "").strip().lower()
            t_low = str(old_item.get("target_type") or "model").strip().lower()
            if (u_low, t_low) in target_qual_by_pair:
                return target_qual_by_pair[(u_low, t_low)]
            if t_low not in {"favorites", "liked"} and u_low in target_qual_by_user:
                return target_qual_by_user[u_low]
            return qualities

        include_kw = _split_csv_set(params.get("keywords"), lower=True)
        exclude_kw = _split_csv_set(params.get("exclude_keywords"), lower=True)
        lookup_groups: Dict[Optional[Tuple[str, ...]], List[str]] = defaultdict(list)
        lookup_urls_set: Set[str] = set()
        for _, _, old, _ in annotated:
            if not old.get("download_url") and not old.get("local_path") and old.get("page_url"):
                purl = old["page_url"]
                if purl in lookup_urls_set:
                    continue
                lookup_urls_set.add(purl)
                q_set = _item_default_qualities(old)
                q_key = tuple(sorted(q_set)) if q_set else None
                lookup_groups[q_key].append(purl)
        resolved: Dict[str, VideoDetail] = {}
        for q_key, urls_in_group in lookup_groups.items():
            details = await fetch_video_details_batch(
                ctx,
                [(url, 0, idx + 1) for idx, url in enumerate(urls_in_group)],
                settings.selectors,
                concurrency=settings.max_concurrent_video_details,
                preferred_qualities=set(q_key) if q_key else None,
                include_keywords=include_kw,
                exclude_keywords=exclude_kw,
                timeout=settings.request_timeout,
            )
            for detail in details:
                resolved[detail.page_url] = detail
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
                old = {
                    **old,
                    "viewkey": detail.viewkey,
                    "title": detail.title,
                    "download_url": detail.download_url,
                    "referer_url": detail.referer_url,
                    "quality": detail.quality,
                    "published_at": detail.published_at or old.get("published_at") or "",
                    "duration_seconds": detail.duration_seconds or int(old.get("duration_seconds") or 0),
                }
            elif old["page_url"] in lookup_urls_set:
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
                published_at=str(old.get("published_at") or ""),
                duration_seconds=int(old.get("duration_seconds") or 0),
            )
            if status == "pending":
                new_item_ids.append(int(row["id"]))
            previous_quality = normalized_quality(old["quality"])
            item_q = _item_default_qualities(old)
            retry_qualities[int(row["id"])] = item_q or ({previous_quality} if previous_quality else None)
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
                published_at=str(prior.get("published_at") or ""),
                duration_seconds=int(prior.get("duration_seconds") or 0),
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
