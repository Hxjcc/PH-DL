"""Core async task queue, download-to-upload streaming pipeline, and auto-update runner."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import random
import shutil
import sqlite3
import time
import urllib.parse
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Tuple

import aiohttp

from ..cancel import StopRequested, until_stopped
from ..config import EnvSettings, RuntimeSettings
from ..db.repo import Repository, job_scan_incomplete, job_uses_item_counts, normalize_site, utc_now_iso
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
from ..parsers.xhamster import (
    build_xhamster_profile_url,
    check_xhamster_cookie_valid,
    detect_site_from_url,
    extract_xhamster_subscribe_action_from_html,
    is_xhamster_url,
    resolve_xhamster_target_with_profile,
)
from ..services.cookie import (
    CookieLoginResult,
    check_cookie_valid,
    format_cookie_valid_message,
    login_and_get_cookies,
    login_xhamster_and_get_cookies,
    prefer_cased_username,
)
from ..services.downloader import (
    DownloadDiskBudget,
    download_mp4_with_resume,
    probe_download_size,
    read_download_checkpoint,
    remove_download_checkpoint,
)
from ..services.http_proxy import create_aiohttp_connector
from ..services.hls_downloader import download_hls_to_mp4, is_hls_url
from ..services.filename import (
    format_remote_video_filename,
    is_valid_video_file,
    is_within_directory,
    normalized_quality,
    sanitize_filename,
    video_filename_base,
)
from ..services.notifier import build_auto_update_report, send_serverchan
from ..services.uploader import (
    UploadSessionCache,
    build_remote_dir,
    classify_rclone_limit_kind,
    delete_verified_source,
    file_snapshot,
    upload_file_batch,
)
from .events import EventBus
from .disk_scheduler import AdaptiveDiskScheduler, SmartUploadPool, UploadQueuedItem
from .memory import reclaim_memory


def _split_csv_set(raw: Optional[str], lower: bool = False) -> Optional[Set[str]]:
    if not raw or not str(raw).strip():
        return None
    parts = [p.strip() for p in str(raw).replace("，", ",").split(",") if p.strip()]
    if not parts:
        return None
    return {p.lower() for p in parts} if lower else set(parts)


def _quality_set(raw: Optional[str]) -> Optional[Set[str]]:
    return {q for part in (_split_csv_set(raw) or ()) if (q := normalized_quality(part))} or None


def _effective_auto_upload(params: Dict[str, Any], settings: RuntimeSettings) -> bool:
    opt_in = params.get("auto_upload")
    return opt_in is True or (opt_in is None and settings.uploader_enabled)


def _item_site(item: Dict[str, Any]) -> str:
    return "xhamster" if is_xhamster_url(item.get("page_url")) else normalize_site(item.get("site"))


def _item_key(viewkey: str, page_url: str) -> str:
    return viewkey or extract_viewkey_from_url(page_url) or page_url


def _target_folder_name(username: Optional[str], target_type: str, site: str = "pornhub") -> str:
    t = (target_type or "model").strip().lower()
    s = normalize_site(site)
    if t == "liked":
        return "My_Liked_Videos"
    if s == "xhamster" and t == "favorites":
        return "My_Favorites"
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
    site: str = "pornhub"


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
    site: str = "pornhub",
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
    if has_var_kw or "site" in params:
        kwargs["site"] = normalize_site(site)
    return await upload_file_batch(**kwargs)


REMOTE_UPLOAD_LIMIT_COOLDOWN_SECONDS = 6 * 3600.0
REMOTE_STORAGE_FULL_COOLDOWN_SECONDS = 24 * 3600.0


class TaskManager:
    """In-process async job orchestrator replacing CLI subprocess execution."""

    def __init__(self, env: EnvSettings, repo: Repository, events: EventBus) -> None:
        self.env = env
        self.repo = repo
        self.events = events

        self._queue: asyncio.Queue[Tuple[int, int]] = asyncio.Queue()
        self._pending_tokens: Dict[int, int] = {}
        self._enqueue_seq = 0
        self._queue_lock = asyncio.Lock()
        self._worker_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._current_job_id: Optional[int] = None
        self._current_cancel_event: Optional[asyncio.Event] = None
        self._current_halt_reason: str = ""
        self._last_cookie_valid: Optional[bool] = None
        self._last_cookie_message: str = "未检测"
        self._last_xh_cookie_valid: Optional[bool] = None
        self._last_xh_cookie_message: str = "未检测"
        self._current_xh_ctx: Optional[TaskHttpContext] = None
        self._remote_cooldowns: Dict[str, Tuple[float, str]] = {}
        self._rr_remote_cursor: int = 0

    def clear_remote_cooldowns(self, remotes: Optional[Iterable[str]] = None) -> None:
        """Reset in-memory quota cooldowns for specified remotes (or all when ``remotes`` is None)."""
        if remotes is None:
            self._remote_cooldowns.clear()
            self._rr_remote_cursor = 0
            return
        for remote in remotes:
            key = str(remote or "").strip().rstrip(":").lower()
            if key:
                self._remote_cooldowns.pop(key, None)

    def get_remote_cooldowns_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Return currently active (non-expired) remote cooldowns."""
        now = time.monotonic()
        expired = [k for k, (until, _) in self._remote_cooldowns.items() if until <= now]
        for k in expired:
            self._remote_cooldowns.pop(k, None)
        return {
            k: {
                "remaining_seconds": max(0, int(until - now)),
                "reason": reason,
            }
            for k, (until, reason) in self._remote_cooldowns.items()
        }

    def _effective_rclone_chain(self, settings: RuntimeSettings) -> List[str]:
        chain = settings.rclone_remote_chain()
        if chain:
            return chain
        fallback = (settings.rclone_remote or "pikpak").strip().rstrip(":")
        return [fallback] if fallback else ["pikpak"]

    def _is_remote_on_cooldown(self, remote: str, *, now: Optional[float] = None) -> bool:
        key = str(remote or "").strip().rstrip(":").lower()
        if not key:
            return False
        entry = self._remote_cooldowns.get(key)
        if entry is None:
            return False
        cur = time.monotonic() if now is None else now
        if entry[0] <= cur:
            self._remote_cooldowns.pop(key, None)
            return False
        return True

    def _mark_remote_exhausted(self, remote: str, reason: str) -> bool:
        """Put ``remote`` on cooldown after hitting a hard upload/storage limit.

        Returns ``True`` when the remote newly transitions into cooldown so concurrent
        upload workers emit the switch warning only once.
        """
        key = str(remote or "").strip().rstrip(":").lower()
        if not key:
            return False
        now = time.monotonic()
        existing = self._remote_cooldowns.get(key)
        was_active = existing is not None and existing[0] > now
        kind = classify_rclone_limit_kind(reason)
        duration = (
            REMOTE_STORAGE_FULL_COOLDOWN_SECONDS
            if kind == "storage_full"
            else REMOTE_UPLOAD_LIMIT_COOLDOWN_SECONDS
        )
        self._remote_cooldowns[key] = (now + duration, reason)
        return not was_active

    def _prepare_rclone_chain_for_job(self, settings: RuntimeSettings) -> Tuple[List[str], List[str]]:
        """Return ``(full_chain, skipped_cooldown_remotes)`` for a newly started job.

        If every configured remote is currently cooled down (e.g. single-remote user
        retrying manually later, or all drives were previously exhausted), clear their
        cooldowns so the new job probes fresh rather than failing immediately.
        """
        chain = self._effective_rclone_chain(settings)
        now = time.monotonic()
        skipped = [r for r in chain if self._is_remote_on_cooldown(r, now=now)]
        if len(skipped) >= len(chain):
            self.clear_remote_cooldowns(chain)
            return chain, []
        return chain, skipped

    def _pick_upload_remote(
        self,
        settings: RuntimeSettings,
        *,
        exclude: Optional[Set[str]] = None,
        advance_cursor: bool = True,
    ) -> Optional[str]:
        """Select the next non-exhausted remote according to ``rclone_remote_strategy``."""
        chain = self._effective_rclone_chain(settings)
        excluded = {r.lower() for r in (exclude or set())}
        now = time.monotonic()
        available = [
            r
            for r in chain
            if r.lower() not in excluded and not self._is_remote_on_cooldown(r, now=now)
        ]
        if not available:
            return None
        strategy = getattr(settings, "rclone_remote_strategy", "failover")
        if strategy == "round_robin" and len(available) > 1:
            idx = self._rr_remote_cursor % len(available)
            if advance_cursor:
                self._rr_remote_cursor += 1
            return available[idx]
        return available[0]

    def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._stopping = False
            self._worker_task = asyncio.create_task(self._worker_loop(), name="phdl-task-worker")

    async def restore_queued_jobs(self) -> int:
        restored = 0
        async with self._queue_lock:
            for job_id in await self.repo.list_queued_job_ids():
                if job_id not in self._pending_tokens and job_id != self._current_job_id:
                    self._enqueue(job_id)
                    restored += 1
        return restored

    async def stop(self) -> None:
        self._stopping = True
        worker = self._worker_task
        if worker is None:
            return
        if self._current_cancel_event is not None:
            self._current_cancel_event.set()
            await asyncio.wait({worker}, timeout=5.0)
        if not worker.done():
            worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        self._worker_task = None

    @property
    def current_job_id(self) -> Optional[int]:
        return self._current_job_id

    @property
    def queued_count(self) -> int:
        return len(self._pending_tokens)

    @property
    def cookie_status(self) -> Dict[str, Any]:
        return {"valid": self._last_cookie_valid, "message": self._last_cookie_message}

    def set_cookie_status(self, valid: Optional[bool], message: str) -> None:
        self._last_cookie_valid = valid
        self._last_cookie_message = self.repo.redact_diagnostic(message)
        self.events.broadcast("overview_changed", {})

    def reset_cookie_status(self) -> None:
        self._last_cookie_valid = None
        self._last_cookie_message = "未检测"

    @property
    def xh_cookie_status(self) -> Dict[str, Any]:
        return {"valid": self._last_xh_cookie_valid, "message": self._last_xh_cookie_message}

    def set_xh_cookie_status(self, valid: Optional[bool], message: str) -> None:
        self._last_xh_cookie_valid = valid
        self._last_xh_cookie_message = self.repo.redact_diagnostic(message)

    def reset_xh_cookie_status(self) -> None:
        self._last_xh_cookie_valid = None
        self._last_xh_cookie_message = "未检测"
        self.events.broadcast("overview_changed", {})

    def _ctx_for_site(self, default_ctx: TaskHttpContext, site: str) -> TaskHttpContext:
        if normalize_site(site) == "xhamster" and self._current_xh_ctx is not None:
            return self._current_xh_ctx
        return default_ctx

    async def _release_idle_http_sessions(self, ctx: TaskHttpContext) -> None:
        await ctx.release_idle_session()
        if self._current_xh_ctx is not None:
            await self._current_xh_ctx.release_idle_session()

    def _enqueue(self, job_id: int) -> None:
        self._enqueue_seq += 1
        self._pending_tokens[job_id] = self._enqueue_seq
        self._queue.put_nowait((job_id, self._enqueue_seq))

    async def enqueue_job(
        self,
        kind: str,
        label: str,
        params: Dict[str, Any],
        trigger: str = "manual",
    ) -> Dict[str, Any]:
        async with self._queue_lock:
            job = await self.repo.create_job(kind=kind, label=label, params=params, trigger=trigger)
            job_id = int(job["id"])
            self._enqueue(job_id)
        self.events.broadcast("job_updated", job)
        self.events.broadcast("overview_changed", {})
        self.events.emit_log("INFO", f"任务已入队: {label}", job_id=job_id)
        return job

    async def enqueue_scheduled_job(self, date: str) -> Optional[int]:
        async with self._queue_lock:
            job = await self.repo.create_scheduled_job(date)
            if job is None:
                return None
            self._enqueue(job["id"])
        self.events.broadcast("job_updated", job)
        self.events.broadcast("overview_changed", {})
        return job["id"]

    async def cancel_job(self, job_id: int) -> bool:
        async with self._queue_lock:
            job = await self.repo.get_job(job_id)
            if not job or job["status"] not in {"queued", "scanning", "running"}:
                return False
            if self._current_job_id == job_id and self._current_cancel_event is not None:
                self._current_cancel_event.set()
                self.events.emit_log("WARNING", "收到停止指令，正在安全中止当前任务...", job_id=job_id)
                return True
            updated = await self.repo.cancel_queued_job(job_id)
            if updated:
                self._pending_tokens.pop(job_id, None)
                self._discard_stale_tokens()
        if updated:
            self.events.broadcast("job_updated", updated)
            self.events.broadcast("job_items_changed", {"job_id": job_id})
            self.events.broadcast("overview_changed", {})
            self.events.emit_log("WARNING", "已取消排队中的任务", job_id=job_id)
            return True
        return False

    def _discard_stale_tokens(self) -> None:
        if self._queue.qsize() <= len(self._pending_tokens) + 64:
            return
        for _ in range(self._queue.qsize()):
            job_id, token = self._queue.get_nowait()
            if self._pending_tokens.get(job_id) == token:
                self._queue.put_nowait((job_id, token))
            self._queue.task_done()

    async def resume_job_in_place(self, job_id: int) -> Tuple[Optional[Dict[str, Any]], str, int]:
        active_error = "运行或排队中的任务无法重试，请先停止"
        async with self._queue_lock:
            job = await self.repo.get_job(job_id)
            if job is None:
                return None, "源任务不存在", 404
            if job["status"] in {"queued", "scanning", "running"} or self._current_job_id == job_id:
                return None, active_error, 409

            kind, params = job["kind"], job["params"]
            export_only = bool(params.get("export_only"))
            replayable = job["status"] in {"cancelled", "failed", "partial_failed"}
            item_resets = None
            if kind == "enrich_metadata":
                if await self.repo.count_baseline_metadata_candidates() == 0 and not replayable:
                    return None, "没有仍需补全元数据的基线视频", 400
                mode = "enrich"
            else:
                auto_upload = _effective_auto_upload(params, await self.repo.get_settings())
                retryable = await self.repo.list_retryable_job_items(job_id, auto_upload=auto_upload)
                has_scan_gap = any(marker in job["error_message"] for marker in ("扫描未完成", "目标检查失败"))
                if replayable and (
                    export_only or kind in {"baseline_sync", "check_updates"}
                    or (has_scan_gap and kind != "retry_items")
                    or not retryable
                ):
                    mode = "rescan"
                elif retryable:
                    mode = "items"
                else:
                    return None, "该任务没有失败或未完成的条目", 400
                if mode == "items" or (
                    mode == "rescan" and kind in {"manual_target", "manual_urls", "auto_update", "resume_temp"}
                ):
                    item_resets = await asyncio.to_thread(self._resume_item_resets, retryable)

            requeued = await self.repo.requeue_job_in_place(
                job_id, params_patch={"resume_mode": mode}, clear_export_text=export_only, item_resets=item_resets,
            )
            if requeued is None:
                if await self.repo.get_job(job_id) is None:
                    return None, "源任务不存在", 404
                return None, active_error, 409
            self._enqueue(job_id)

        self.events.broadcast("job_updated", requeued)
        self.events.broadcast("job_items_changed", {"job_id": job_id})
        self.events.broadcast("overview_changed", {})
        self.events.emit_log("INFO", f"原任务已重新入队: {requeued['label']}", job_id=job_id)
        return requeued, "", 200

    async def _worker_loop(self) -> None:
        while not self._stopping:
            job_id, token = await self._queue.get()
            try:
                async with self._queue_lock:
                    if self._pending_tokens.get(job_id) != token:
                        continue
                    if self._stopping:
                        self._queue.put_nowait((job_id, token))
                        return
                    del self._pending_tokens[job_id]
                try:
                    await self._run_job(job_id)
                except Exception as err:
                    failed = await self._fail_job(job_id, err)
                    if failed:
                        self.events.broadcast("job_updated", failed)
                finally:
                    self._current_job_id = None
                    self._current_cancel_event = None
                    self._current_halt_reason = ""
                    self.events.clear_job_progress(job_id)
                    with contextlib.suppress(Exception):
                        await self.repo.prune_old_jobs(200)
                    with contextlib.suppress(Exception):
                        await reclaim_memory(self.repo.db)
                    self.events.broadcast("overview_changed", {})
            except asyncio.CancelledError:
                if self._pending_tokens.get(job_id) == token:
                    self._queue.put_nowait((job_id, token))
                raise
            finally:
                self._queue.task_done()

    async def _fail_job(self, job_id: int, err: Exception) -> Optional[Dict[str, Any]]:
        self.events.emit_log("ERROR", f"任务执行发生未捕获异常: {err}", job_id=job_id)
        job = await self.repo.get_job(job_id)
        message = str(err)
        if job and (job_scan_incomplete(job) or "扫描未完成" in job["error_message"]
                    or "目标检查失败" in job["error_message"]):
            message = f"扫描未完成：{message}"
        return await self.repo.finish_job(job_id, "failed", message)

    async def _cancel_active_job(self, job_id: int, message: str) -> Optional[Dict[str, Any]]:
        job = await self.repo.get_job(job_id)
        if job is None:
            return None
        if job_scan_incomplete(job) or any(marker in job["error_message"] for marker in ("扫描未完成", "目标检查失败")):
            message = f"扫描未完成，{message}"
        return await self.repo.finish_job(job_id, "cancelled", message, cancel_items=True)

    async def _ensure_valid_cookie(
        self,
        settings: RuntimeSettings,
        job_id: int,
        *,
        notify_on_failure: bool = False,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Optional[RuntimeSettings]:
        self.events.emit_log("INFO", "[PornHub Cookie] 正在验证有效性...", job_id=job_id)
        if settings.ph_cookie.strip():
            check = await check_cookie_valid(
                settings.ph_cookie,
                proxy_url=settings.normalized_proxy,
                timeout=settings.request_timeout,
            )
            if check.valid:
                resolved_user = prefer_cased_username(check.logged_in_username, settings.ph_logged_in_username)
                if resolved_user and resolved_user != settings.ph_logged_in_username:
                    settings = await self.repo.update_settings_patch({"ph_logged_in_username": resolved_user})
                status_msg = format_cookie_valid_message(resolved_user)
                self.set_cookie_status(True, status_msg)
                self.events.emit_log("INFO", f"[PornHub Cookie] {status_msg}。", job_id=job_id)
                return settings
            self.events.emit_log(
                "WARNING",
                f"[PornHub Cookie] 校验未通过 ({check.message})，尝试自动登录刷新...",
                job_id=job_id,
            )
        else:
            self.events.emit_log("WARNING", "[PornHub Cookie] 未配置 Cookie，尝试自动登录刷新...", job_id=job_id)

        if settings.ph_username.strip() and settings.ph_password.strip():
            persisted: List[RuntimeSettings] = []

            async def _persist_ph(res: CookieLoginResult) -> None:
                if not (res.success and res.cookie_str):
                    return
                updated = await self.repo.save_refreshed_cookie("ph", settings, res.cookie_str, res.logged_in_username)
                if updated is None:
                    return
                resolved_user = (res.logged_in_username or updated.ph_logged_in_username or "").strip()
                self.set_cookie_status(True, format_cookie_valid_message(resolved_user))
                persisted.append(updated)

            login_res = await login_and_get_cookies(
                settings.ph_username,
                settings.ph_password,
                proxy_url=settings.normalized_proxy,
                timeout=settings.request_timeout,
                yescaptcha_enabled=settings.yescaptcha_enabled,
                yescaptcha_client_key=settings.yescaptcha_client_key,
                yescaptcha_task_type=settings.yescaptcha_ph_task_type,
                yescaptcha_timeout_seconds=settings.yescaptcha_timeout_seconds,
                on_log=lambda level, message: self.events.emit_log(level, message, job_id=job_id),
                cancel_event=cancel_event,
                on_success=_persist_ph,
            )
            if login_res.success and login_res.cookie_str:
                if not persisted:
                    await _persist_ph(login_res)
                if persisted:
                    return persisted[-1]
                err_msg = "登录期间连接配置已变更，请重试"
            else:
                err_msg = login_res.message
        else:
            err_msg = "未配置有效 Cookie，且未填写 PornHub 账号密码"

        self.set_cookie_status(False, err_msg)
        self.events.emit_log("ERROR", f"[PornHub Cookie] 准备失败: {err_msg}", job_id=job_id)
        if notify_on_failure:
            await send_serverchan(
                "PH-DL 告警：Cookie 失效",
                f"定时追更因 Cookie 失效中止：{self._last_cookie_message}\n请打开 Web 面板在【系统设置】中手动粘贴浏览器 Cookie。",
                settings,
            )
        return None

    async def _job_site_requirements(
        self,
        kind: str,
        params: Dict[str, Any],
        *,
        job_id: Optional[int] = None,
    ) -> Tuple[bool, bool, bool]:
        """Return ``(needs_ph_cookie, needs_xh_cookie, needs_xh_ctx)`` for a job."""
        resume_mode = params.get("resume_mode")
        pending_sites: Set[str] = set()
        if job_id is not None and (
            resume_mode == "items" or (resume_mode == "rescan" and kind in {"auto_update", "resume_temp"})
        ):
            pending_sites = {_item_site(item) for item in await self.repo.list_job_items(job_id, status="pending")}
            if resume_mode == "items":
                return "pornhub" in pending_sites, False, "xhamster" in pending_sites

        if kind == "manual_target":
            site = normalize_site(params.get("site"))
            if site == "xhamster":
                utype = str(params.get("target_type") or "").strip().lower()
                return False, utype == "favorites", True
            return True, False, False

        if kind == "manual_urls":
            urls = [str(u).strip() for u in (params.get("urls") or []) if str(u).strip()]
            if not urls:
                return True, False, False
            has_xh = any(is_xhamster_url(u) for u in urls)
            has_ph = any(not is_xhamster_url(u) for u in urls)
            return has_ph, False, has_xh

        if kind == "baseline_sync":
            target_id = params.get("target_id")
            target = await self.repo.get_target(int(target_id)) if target_id else None
            if target and normalize_site(target.get("site")) == "xhamster":
                return False, str(target.get("target_type") or "").lower() == "favorites", True
            return True, False, False

        if kind in {"auto_update", "check_updates"}:
            target_ids = params.get("target_ids")
            if target_ids:
                targets = []
                for tid in target_ids:
                    row = await self.repo.get_target(int(tid))
                    if row:
                        targets.append(row)
            else:
                targets = await self.repo.list_targets(enabled_only=True)
            if not targets and not pending_sites:
                return True, False, False
            sites = pending_sites | {normalize_site(t.get("site")) for t in targets}
            needs_xh_cookie = any(
                normalize_site(t.get("site")) == "xhamster"
                and str(t.get("target_type") or "").lower() == "favorites"
                for t in targets
            )
            return "pornhub" in sites, needs_xh_cookie, "xhamster" in sites

        if kind == "retry_items":
            source_job_id = int(params.get("source_job_id") or 0)
            source_items = await self.repo.list_job_items(source_job_id) if source_job_id else []
            retryable = [
                it for it in source_items
                if it["status"] in {"failed", "cancelled", "pending", "downloaded", "uploading"}
            ]
            if not retryable:
                return True, False, False
            has_xh = any(
                normalize_site(it.get("site")) == "xhamster" or is_xhamster_url(it.get("page_url"))
                for it in retryable
            )
            has_ph = any(
                normalize_site(it.get("site")) == "pornhub" and not is_xhamster_url(it.get("page_url"))
                for it in retryable
            )
            return has_ph, False, has_xh

        if kind == "resume_temp":
            temp_files = await asyncio.to_thread(self.list_temp_files)
            if not temp_files and not pending_sites:
                return True, False, False
            sites = pending_sites | {normalize_site(tf.get("site")) for tf in temp_files}
            return "pornhub" in sites, False, "xhamster" in sites

        if kind == "enrich_metadata":
            limit = int(params.get("limit") or 0)
            resumed = params.get("resume_mode") == "enrich"
            candidates = await self.repo.list_baseline_metadata_candidates(
                limit=limit, resume_job_id=job_id if resumed else None, site_only=True,
            )
            if not candidates:
                return not resumed, False, False
            has_xh = any(
                normalize_site(c.get("site")) == "xhamster"
                for c in candidates
            )
            has_ph = any(
                normalize_site(c.get("site")) == "pornhub"
                for c in candidates
            )
            return has_ph, False, has_xh

        return True, False, False

    async def _ensure_valid_xh_cookie(
        self,
        settings: RuntimeSettings,
        job_id: int,
        *,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Optional[RuntimeSettings]:
        self.events.emit_log("INFO", "[xHamster Cookie] 正在验证有效性...", job_id=job_id)
        if settings.xh_cookie.strip():
            valid, msg, username = await check_xhamster_cookie_valid(
                settings.xh_cookie,
                proxy_url=settings.normalized_proxy,
                timeout=settings.request_timeout,
            )
            if valid:
                resolved_user = prefer_cased_username(username, settings.xh_logged_in_username)
                if resolved_user and resolved_user != settings.xh_logged_in_username:
                    settings = await self.repo.update_settings_patch({"xh_logged_in_username": resolved_user})
                status_msg = format_cookie_valid_message(resolved_user)
                self.set_xh_cookie_status(True, status_msg)
                self.events.emit_log("INFO", f"[xHamster Cookie] {status_msg}。", job_id=job_id)
                return settings
            self.events.emit_log(
                "WARNING",
                f"[xHamster Cookie] 校验未通过 ({msg})，尝试自动登录刷新...",
                job_id=job_id,
            )
        else:
            self.events.emit_log("WARNING", "[xHamster Cookie] 未配置 Cookie，尝试自动登录刷新...", job_id=job_id)

        if settings.xh_username.strip() and settings.xh_password.strip():
            persisted: List[RuntimeSettings] = []

            async def _persist_xh(res: CookieLoginResult) -> None:
                if not (res.success and res.cookie_str):
                    return
                updated = await self.repo.save_refreshed_cookie("xh", settings, res.cookie_str, res.logged_in_username)
                if updated is None:
                    return
                resolved_user = (res.logged_in_username or updated.xh_logged_in_username or "").strip()
                self.set_xh_cookie_status(True, format_cookie_valid_message(resolved_user))
                persisted.append(updated)

            login_res = await login_xhamster_and_get_cookies(
                settings.xh_username,
                settings.xh_password,
                proxy_url=settings.normalized_proxy,
                timeout=settings.request_timeout,
                yescaptcha_enabled=settings.yescaptcha_enabled,
                yescaptcha_client_key=settings.yescaptcha_client_key,
                yescaptcha_task_type=settings.yescaptcha_xh_task_type,
                yescaptcha_timeout_seconds=settings.yescaptcha_timeout_seconds,
                on_log=lambda lvl, message: self.events.emit_log(lvl, message, job_id=job_id),
                cancel_event=cancel_event,
                on_success=_persist_xh,
            )
            if login_res.success and login_res.cookie_str:
                if not persisted:
                    await _persist_xh(login_res)
                if persisted:
                    return persisted[-1]
                err_msg = "登录期间连接配置已变更，请重试"
            else:
                err_msg = login_res.message or "xHamster 自动登录失败"
        elif settings.xh_cookie.strip():
            err_msg = "xHamster Cookie 已失效，且未配置 xHamster 账号密码"
        else:
            err_msg = "xHamster 收藏夹需要登录 xHamster 账号或粘贴有效 Cookie，请在【系统设置 → Cookie 与账号】中配置"

        self.set_xh_cookie_status(False, err_msg)
        self.events.emit_log("ERROR", f"[xHamster Cookie] 准备失败: {err_msg}", job_id=job_id)
        return None

    async def _run_job(self, job_id: int) -> None:
        cancel_event = asyncio.Event()
        self._pending_tokens.pop(job_id, None)
        self._current_job_id = job_id
        self._current_cancel_event = cancel_event
        self._current_halt_reason = ""
        self._current_xh_ctx = None

        try:
            started = await self.repo.claim_queued_job(job_id)
            if not started:
                return
            self.events.broadcast("job_updated", started)
            self.events.broadcast("overview_changed", {})
            try:
                finished = await self._execute_job(job_id, started, cancel_event)
            except StopRequested:
                finished = await self._cancel_active_job(job_id, "已取消")
            except Exception as err:
                finished = await self._fail_job(job_id, err)
            if finished:
                if finished["status"] != "completed":
                    self.events.broadcast("job_items_changed", {"job_id": job_id})
                self.events.broadcast("job_updated", finished)
                self.events.emit_log(
                    "INFO",
                    f"任务结束 [{finished['status']}]: 总计 {finished['total_items']}，"
                    f"完成 {finished['done_items']}，已上传 {finished['uploaded_items']}，"
                    f"跳过 {finished['skipped_items']}，失败 {finished['failed_items']}",
                    job_id=job_id,
                )
        except asyncio.CancelledError:
            finished = await self._cancel_active_job(job_id, "服务停止，可点击续传或重试")
            if finished:
                self.events.broadcast("job_updated", finished)
            raise
        finally:
            self._current_job_id = None
            self._current_cancel_event = None
            self._current_halt_reason = ""

    async def _execute_job(
        self, job_id: int, job: Dict[str, Any], cancel_event: asyncio.Event,
    ) -> Optional[Dict[str, Any]]:
        settings = await self.repo.get_settings()
        kind = job["kind"]
        params = job.get("params") or {}

        resume_mode = params.get("resume_mode")
        needs_ph_cookie, needs_xh_cookie, needs_xh_ctx = await self._job_site_requirements(kind, params, job_id=job_id)
        scan_error_prefix = "扫描未完成：" if resume_mode == "rescan" else ""

        # Validate required site cookie(s) before network work
        try:
            if needs_ph_cookie:
                checked_settings = await until_stopped(
                    self._ensure_valid_cookie(
                        settings, job_id,
                        notify_on_failure=(kind == "auto_update" and job.get("trigger") == "schedule"),
                        cancel_event=cancel_event,
                    ), cancel_event,
                )
                if checked_settings is None:
                    return await self.repo.finish_job(job_id, "failed", scan_error_prefix + self._last_cookie_message)
                settings = checked_settings

            if needs_xh_cookie:
                checked_xh = await until_stopped(
                    self._ensure_valid_xh_cookie(settings, job_id, cancel_event=cancel_event), cancel_event,
                )
                if checked_xh is None:
                    return await self.repo.finish_job(job_id, "failed", scan_error_prefix + self._last_xh_cookie_message)
                settings = checked_xh
            elif (
                needs_xh_ctx
                and bool(params.get("subscribe_creator", False))
                and not settings.xh_cookie.strip()
                and settings.xh_username.strip()
                and settings.xh_password.strip()
            ):
                # Best-effort login refresh for xHamster auto-subscribe; never block public downloads if login fails.
                checked_xh = await until_stopped(
                    self._ensure_valid_xh_cookie(settings, job_id, cancel_event=cancel_event), cancel_event,
                )
                if checked_xh is not None:
                    settings = checked_xh
                else:
                    self.events.emit_log(
                        "WARNING",
                        "[xHamster Cookie] 自动登录未成功，将继续公开下载但跳过自动订阅。",
                        job_id=job_id,
                    )
        except StopRequested:
            return await self._cancel_active_job(job_id, "已取消")

        async with contextlib.AsyncExitStack() as stack:
            ctx = await stack.enter_async_context(
                TaskHttpContext(
                    site="pornhub",
                    cookie_str=settings.ph_cookie,
                    proxy_url=settings.normalized_proxy,
                    concurrency=settings.max_concurrent_video_details,
                    timeout=settings.request_timeout,
                    max_retries=settings.max_retries_page_fetch,
                    retry_delay_base=settings.retry_delay_base,
                    retry_delay_max=settings.retry_delay_max,
                    cancel_event=cancel_event,
                    on_log=lambda lvl, msg: self.events.emit_log(lvl, msg, job_id=job_id),
                )
            )
            if needs_xh_ctx:
                self._current_xh_ctx = await stack.enter_async_context(
                    TaskHttpContext(
                        site="xhamster",
                        cookie_str=settings.xh_cookie,
                        proxy_url=settings.normalized_proxy,
                        concurrency=settings.max_concurrent_video_details,
                        timeout=settings.request_timeout,
                        max_retries=settings.max_retries_page_fetch,
                        retry_delay_base=settings.retry_delay_base,
                        retry_delay_max=settings.retry_delay_max,
                        cancel_event=cancel_event,
                        on_log=lambda lvl, msg: self.events.emit_log(lvl, msg, job_id=job_id),
                    )
                )
            try:
                if resume_mode == "items":
                    await self._exec_resume_job_items(job_id, params, settings, ctx)
                elif kind == "manual_target":
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
                    if resume_mode != "rescan" or not await self.repo.list_job_items(job_id):
                        await self._exec_retry_items(job_id, params, settings, ctx)
                elif kind == "resume_temp":
                    await self._exec_resume_temp(job_id, params, settings, ctx)
                else:
                    return await self.repo.finish_job(job_id, "failed", f"未知任务类型: {kind}")
            finally:
                self._current_xh_ctx = None

        final_job = (
            await self.repo.recalculate_job_counts(job_id)
            if job_uses_item_counts(job)
            else await self.repo.get_job(job_id)
        )
        if final_job is None:
            return None
        if final_job["status"] in {"scanning", "running"}:
            message = final_job["error_message"]
            if self._current_halt_reason:
                final_status = "partial_failed"
                message = message or self._current_halt_reason
            elif cancel_event.is_set():
                final_status = "cancelled"
                if job_scan_incomplete(final_job) and "扫描未完成" not in message:
                    message = f"扫描未完成，{message or '已取消'}"
            elif int(final_job.get("failed_items") or 0) > 0 or message.strip():
                final_status = "partial_failed"
            else:
                final_status = "completed"
            final_job = await self.repo.finish_job(job_id, final_status, message, cancel_items=cancel_event.is_set())
        return final_job

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
        uploader_display_names: Optional[Dict[str, str]] = None,
    ) -> Tuple[Dict[str, int], Dict[str, List[str]]]:
        """Stream pre-parse, byte-adaptive downloads, and multi-worker zero-wait uploads."""
        uploaded_by_uploader: Dict[str, int] = defaultdict(int)
        failed_urls_by_uploader: Dict[str, List[str]] = defaultdict(list)
        item_display_names: Dict[int, str] = {}
        known_display_names: Dict[str, str] = {}
        for raw_k, raw_v in (uploader_display_names or {}).items():
            ck = str(raw_k or "").strip().lower()
            cv = str(raw_v or "").strip()
            if ck and cv:
                known_display_names[ck] = cv

        def _resolve_item_display_name(iid: int, uploader_name: str) -> str:
            return (
                item_display_names.get(iid)
                or known_display_names.get(str(uploader_name or "").strip().lower())
                or ""
            )

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
        configured_remote_chain: List[str] = []
        if auto_upload:
            configured_remote_chain, skipped_remotes = self._prepare_rclone_chain_for_job(settings)
            if skipped_remotes:
                remaining_remotes = [r for r in configured_remote_chain if r not in skipped_remotes]
                self.events.emit_log(
                    "INFO",
                    f"[上传策略] 已跳过冷却中的限额网盘 ({', '.join(skipped_remotes)})，"
                    f"本次优先使用: {', '.join(remaining_remotes)}",
                    job_id=job_id,
                )
        scheduler.emit_status(force=True)

        async def _mark_entries_cancelled(entries: List[UploadQueuedItem]) -> None:
            if not entries:
                return
            cancel_reason = (
                "因触发网盘限额已暂停，保留本地文件"
                if self._current_halt_reason
                else "上传已取消，保留本地文件"
            )
            for entry in entries:
                self.events.clear_item_progress(entry.item_id)
                it = await self.repo.update_job_item(
                    entry.item_id,
                    status="cancelled",
                    error_message=cancel_reason,
                )
                if it:
                    self.events.broadcast("item_updated", it)
                self.events.emit_log(
                    "WARNING",
                    f"[上传失败] {entry.local_path.name}: {cancel_reason}",
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
            chunk_site = normalize_site(chunk[0].site)
            vk_to_entry = {e.viewkey: e for e in chunk}
            chunk_bytes = sum(e.file_size for e in chunk)
            primary_remote = (settings.rclone_remote or "pikpak").strip().rstrip(":")
            remote_chain = configured_remote_chain or self._effective_rclone_chain(settings)

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

            remote_names_map: Dict[str, str] = {}
            for entry in chunk:
                dated_name = format_remote_video_filename(entry.local_path.name, entry.published_at)
                if dated_name and dated_name != entry.local_path.name:
                    remote_names_map[entry.viewkey] = dated_name

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

            async def _halt_for_all_remotes_exhausted(fatal_limit: str) -> None:
                if ctx.cancel_event.is_set():
                    return
                if not self._current_halt_reason:
                    halt_msg = (
                        f"所有配置的网盘（{', '.join(remote_chain)}）均已达到上传/空间限额（{fatal_limit}）"
                        if len(remote_chain) > 1
                        else fatal_limit
                    )
                    self._current_halt_reason = halt_msg
                    self.events.emit_log(
                        "ERROR",
                        f"检测到网盘上传硬性限制（{halt_msg}），已自动暂停当前任务剩余下载与上传，本地已下载文件均已保留。",
                        job_id=job_id,
                    )
                ctx.cancel_event.set()
                await upload_pool.notify_cancel()

            await scheduler.on_upload_started()
            try:
                remaining_entries: List[UploadQueuedItem] = list(chunk)
                tried_remotes: Set[str] = set()

                while remaining_entries and not ctx.cancel_event.is_set():
                    active_remote = self._pick_upload_remote(settings, exclude=tried_remotes)
                    if active_remote is None:
                        fallback_reason = next(
                            (
                                reason
                                for r in remote_chain
                                for (_, reason) in [self._remote_cooldowns.get(r.lower(), (0.0, ""))]
                                if reason
                            ),
                            "已达到网盘上传或空间限额",
                        )
                        await _halt_for_all_remotes_exhausted(fallback_reason)
                        break

                    tried_remotes.add(active_remote.lower())
                    effective_settings = (
                        settings
                        if active_remote.lower() == primary_remote.lower()
                        else settings.model_copy(update={"rclone_remote": active_remote})
                    )
                    remote_target = build_remote_dir(effective_settings, uploader_folder, site=chunk_site)
                    files_map = {e.viewkey: e.local_path for e in remaining_entries}
                    batch_remote_names = {
                        e.viewkey: remote_names_map[e.viewkey]
                        for e in remaining_entries
                        if e.viewkey in remote_names_map
                    }

                    if len(remaining_entries) == 1:
                        one = remaining_entries[0]
                        size_mb = one.file_size / (1024 * 1024)
                        size_tag = f" ({size_mb:.1f} MB)" if one.file_size > 0 else ""
                        remote_label = batch_remote_names.get(one.viewkey) or one.local_path.name
                        self.events.emit_log(
                            "INFO",
                            f"[上传] 开始上传至 {remote_target}: {remote_label}{size_tag}",
                            job_id=job_id,
                        )
                    else:
                        self.events.emit_log(
                            "INFO",
                            f"[上传] 开始向 {remote_target} 并行上传 {len(remaining_entries)} 个视频...",
                            job_id=job_id,
                        )

                    upload_started = time.monotonic()
                    up_res = await _invoke_upload_file_batch(
                        download_dir=download_dir,
                        uploader_folder=uploader_folder,
                        files_by_viewkey=files_map,
                        settings=effective_settings,
                        cancel_event=ctx.cancel_event,
                        session_cache=upload_session_cache,
                        on_progress=_on_upload_progress,
                        remote_names_by_viewkey=batch_remote_names or None,
                        site=chunk_site,
                    )
                    elapsed = max(0.0, time.monotonic() - upload_started)
                    uploaded_set = set(up_res.uploaded_viewkeys)
                    next_remaining: List[UploadQueuedItem] = []

                    for entry in remaining_entries:
                        self.events.clear_item_progress(entry.item_id)
                        snapshot = up_res.verified_sources.get(entry.viewkey)
                        local_rel = entry.local_path.relative_to(download_dir).as_posix()
                        custom_remote_name = batch_remote_names.get(entry.viewkey)
                        if custom_remote_name:
                            parent_rel = Path(local_rel).parent.as_posix()
                            fallback_remote_rel = (
                                f"{parent_rel}/{custom_remote_name}"
                                if parent_rel not in {"", "."}
                                else custom_remote_name
                            )
                        else:
                            fallback_remote_rel = local_rel
                        verified_remote_path = (
                            (getattr(up_res, "remote_paths", None) or {}).get(entry.viewkey)
                            or f"{up_res.remote_dir}/{fallback_remote_rel}"
                        )
                        if (
                            entry.viewkey in uploaded_set
                            and snapshot is not None
                            and not ctx.cancel_event.is_set()
                            and file_snapshot(entry.local_path) == snapshot
                            and await self.repo.record_verified_upload(
                                entry.viewkey,
                                str(entry.local_path),
                                verified_remote_path,
                                item_id=entry.item_id,
                            )
                        ):
                            local_path = str(entry.local_path)
                            removed_local = False
                            if not ctx.cancel_event.is_set() and delete_verified_source(entry.local_path, snapshot):
                                await self.repo.clear_uploaded_local_path(entry.viewkey, local_path)
                                removed_local = True
                            elif not ctx.cancel_event.is_set():
                                self.events.emit_log(
                                    "WARNING",
                                    f"[上传] 本地副本未移除: {entry.local_path.name}",
                                    job_id=job_id,
                                )
                            it = (
                                await self.repo.update_job_item(entry.item_id, local_path="")
                                if removed_local else await self.repo.get_job_item(entry.item_id)
                            )
                            if it:
                                self.events.broadcast("item_updated", it)
                            uploaded_by_uploader[uploader_folder] += 1
                            cleanup_note = "，已删除本地文件" if removed_local else "，保留本地副本"
                            speed_note = ""
                            if elapsed > 0.1 and entry.file_size > 0 and len(remaining_entries) == 1:
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
                            next_remaining.append(entry)

                    if not next_remaining:
                        remaining_entries = []
                        break

                    fatal_limit = str(getattr(up_res, "fatal_limit_error", "") or "").strip()
                    if fatal_limit and not ctx.cancel_event.is_set():
                        newly_exhausted = self._mark_remote_exhausted(active_remote, fatal_limit)
                        next_remote = self._pick_upload_remote(
                            settings,
                            exclude=tried_remotes,
                            advance_cursor=False,
                        )
                        if next_remote is not None:
                            if newly_exhausted:
                                self.events.emit_log(
                                    "WARNING",
                                    f"[上传切换] 网盘 {active_remote} 触发限额（{fatal_limit}），"
                                    f"已自动切换至备用网盘 {next_remote} 继续上传",
                                    job_id=job_id,
                                )
                            else:
                                label = (
                                    next_remaining[0].local_path.name
                                    if len(next_remaining) == 1
                                    else f"{len(next_remaining)} 个视频"
                                )
                                self.events.emit_log(
                                    "INFO",
                                    f"[上传切换] {label} 将改用备用网盘 {next_remote} 重试上传",
                                    job_id=job_id,
                                )
                            for entry in next_remaining:
                                self.events.emit_item_progress(
                                    job_id,
                                    entry.item_id,
                                    0,
                                    entry.file_size,
                                    0.0,
                                    phase="uploading",
                                )
                            remaining_entries = next_remaining
                            continue

                    # No failover target left (or non-quota failure / user cancellation)
                    cancelled = ctx.cancel_event.is_set()
                    err_txt = (
                        ("因触发网盘限额已暂停，保留本地文件" if self._current_halt_reason else "上传已取消，保留本地文件")
                        if cancelled
                        else up_res.error or "未能确认上传结果"
                    )
                    for entry in next_remaining:
                        it = await self.repo.update_job_item(
                            entry.item_id,
                            status="cancelled" if cancelled else "failed",
                            error_message=err_txt if cancelled else f"上传失败: {err_txt}",
                        )
                        if it:
                            self.events.broadcast("item_updated", it)
                        if not cancelled:
                            _record_failed_url(uploader_folder, entry.page_url)
                        self.events.emit_log(
                            "WARNING",
                            f"[上传失败] {entry.local_path.name}: {err_txt}",
                            job_id=job_id,
                        )
                    remaining_entries = []
                    if fatal_limit and not ctx.cancel_event.is_set():
                        await _halt_for_all_remotes_exhausted(fatal_limit)
                    break

                if remaining_entries:
                    cancel_reason = (
                        "因触发网盘限额已暂停，保留本地文件"
                        if self._current_halt_reason
                        else "上传已取消，保留本地文件"
                    )
                    for entry in remaining_entries:
                        it = await self.repo.update_job_item(
                            entry.item_id,
                            status="cancelled",
                            error_message=cancel_reason,
                        )
                        if it:
                            self.events.broadcast("item_updated", it)
                        self.events.emit_log(
                            "WARNING",
                            f"[上传失败] {entry.local_path.name}: {cancel_reason}",
                            job_id=job_id,
                        )
            finally:
                for entry in chunk:
                    self.events.clear_item_progress(entry.item_id)
                # Release disk backpressure immediately once local files are deleted.
                await scheduler.on_upload_finished(len(chunk), chunk_bytes)

            refreshed = await self.repo.recalculate_job_counts(job_id)
            if refreshed:
                self.events.broadcast("job_updated", refreshed)

        async def _upload_worker() -> None:
            try:
                while not ctx.cancel_event.is_set():
                    chunk = await upload_pool.take_next_chunk(ctx.cancel_event)
                    if chunk is None:
                        return
                    await _upload_chunk(chunk)
            except Exception:
                ctx.cancel_event.set()
                await upload_pool.notify_cancel()
                raise

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
            site: str = "pornhub",
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
                    site=normalize_site(site),
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
            dl_slots = max(1, int(settings.max_concurrent_downloads))
            if dl_slots == 1:
                hls_seg_concurrency, hls_max_buffer_bytes = 8, 24 * 1024 * 1024
            elif dl_slots == 2:
                hls_seg_concurrency, hls_max_buffer_bytes = 6, 16 * 1024 * 1024
            else:
                hls_seg_concurrency, hls_max_buffer_bytes = 4, 12 * 1024 * 1024
            conn_per_host = max(12, min(48, dl_slots * hls_seg_concurrency + 4))
            conn_total = max(24, conn_per_host * 2)
            if not pass_parse_specs:
                await self._release_idle_http_sessions(ctx)
            connector, _ = create_aiohttp_connector(
                settings.normalized_proxy,
                limit=conn_total,
                limit_per_host=conn_per_host,
                keepalive_timeout=20.0,
                ttl_dns_cache=300,
            )

            async with aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                read_bufsize=256 * 1024,
                cookie_jar=aiohttp.DummyCookieJar(),
            ) as dl_session:
                async def _dl_one(item_id: int) -> None:
                    if ctx.cancel_event.is_set():
                        return

                    item = await self.repo.get_job_item(item_id)
                    if not item:
                        return

                    vkey = item["viewkey"]
                    item_site = _item_site(item)
                    site_ctx = self._ctx_for_site(ctx, item_site)
                    site_cookie = settings.xh_cookie if item_site == "xhamster" else settings.ph_cookie
                    uploader_folder = _target_folder_name(item["uploader"], item["target_type"], site=item_site)
                    dl_dir = (
                        (self.env.downloads_dir / "xHamster" / uploader_folder)
                        if item_site == "xhamster"
                        else (self.env.downloads_dir / uploader_folder)
                    )
                    tmp_dir = (
                        (self.env.temp_dir / "xHamster" / uploader_folder)
                        if item_site == "xhamster"
                        else (self.env.temp_dir / uploader_folder)
                    )
                    partial_path = Path(item["temp_path"]) if item.get("temp_path") else None
                    if partial_path is not None and not partial_path.is_symlink() and is_within_directory(partial_path, self.env.temp_dir):
                        tmp_dir = partial_path.parent
                    else:
                        partial_path = None
                    if not is_within_directory(dl_dir, self.env.downloads_dir) or not is_within_directory(tmp_dir, self.env.temp_dir):
                        it = await self.repo.update_job_item(item_id, status="failed", error_message="文件目录超出允许范围")
                        if it:
                            self.events.broadcast("item_updated", it)
                        await _refresh_job_progress()
                        return
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
                        if partial_path is not None:
                            if partial_path.name.endswith(".mp4.tmp") and partial_path.is_file():
                                with contextlib.suppress(OSError):
                                    partial_path.unlink(missing_ok=True)
                                    remove_download_checkpoint(partial_path)
                                    cleaned_stale = True
                            elif partial_path.name.endswith(".hls") and partial_path.is_dir():
                                with contextlib.suppress(OSError):
                                    shutil.rmtree(partial_path, ignore_errors=True)
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

                    local_file = None
                    if vkey and item.get("local_path"):
                        local_file = await asyncio.to_thread(
                            self._existing_local_file, item["local_path"], settings.min_file_size_bytes,
                            item.get("bytes_total") or None,
                        )
                    own_local_file = local_file is not None
                    local_quality = item["quality"]
                    if local_file is None and ledger and ledger["status"] == "downloaded" and ledger["local_path"] and quality_matches:
                        local_file = await asyncio.to_thread(
                            self._existing_local_file, ledger["local_path"], settings.min_file_size_bytes,
                            ledger["file_size"] or None,
                        )
                        local_quality = ledger["quality"]

                    if local_file is not None:
                        existing_local, existing_size = local_file
                        pub_date = str(item.get("published_at") or (ledger or {}).get("published_at") or "").strip()
                        dur_sec = int(item.get("duration_seconds") or (ledger or {}).get("duration_seconds") or 0)
                        local_metadata = {
                            "title": item["title"],
                            "uploader": item["uploader"],
                            "uploader_display_name": _resolve_item_display_name(item_id, item["uploader"]),
                            "target_type": item["target_type"],
                            "quality": local_quality,
                            "video_page_url": item["page_url"],
                        } if own_local_file else {}
                        await self.repo.upsert_video(
                            vkey,
                            site=item_site,
                            status="downloaded",
                            local_path=str(existing_local),
                            file_size=existing_size,
                            force_status=own_local_file,
                            upload_requested=will_upload,
                            published_at=pub_date,
                            duration_seconds=dur_sec,
                            **local_metadata,
                        )
                        completed_here = will_upload or own_local_file
                        it = await self.repo.update_job_item(
                            item_id,
                            status="downloaded" if completed_here else "skipped",
                            local_path=str(existing_local),
                            quality=local_quality,
                            bytes_downloaded=existing_size,
                            bytes_total=existing_size,
                            upload_requested=int(will_upload),
                            error_message="" if completed_here else "本地已有符合画质的完整文件，跳过",
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
                                site=item_site,
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

                            use_hls = is_hls_url(item["download_url"]) or (
                                partial_path is not None and partial_path.name.endswith(".hls")
                            )

                            def _on_progress(
                                dl_bytes: int,
                                tot_bytes: int,
                                speed_bps: float,
                                phase: str = "downloading",
                            ) -> None:
                                norm_phase = (phase or "downloading").strip().lower()
                                self.events.emit_item_progress(
                                    job_id,
                                    item_id,
                                    dl_bytes,
                                    tot_bytes,
                                    speed_bps,
                                    phase=norm_phase,
                                    estimated_total=(use_hls and norm_phase == "downloading"),
                                )
                                scheduler.emit_status()

                            if use_hls:
                                outcome = await download_hls_to_mp4(
                                    session=dl_session,
                                    parse_ctx=site_ctx,
                                    selectors=settings.selectors,
                                    viewkey=vkey,
                                    title=item["title"],
                                    playlist_url=item["download_url"],
                                    page_url=item["page_url"],
                                    referer_url=item["referer_url"],
                                    quality=item["quality"],
                                    download_dir=dl_dir,
                                    temp_dir=tmp_dir,
                                    partial_path=partial_path,
                                    replace_existing=redownload,
                                    disk_budget=disk_budget,
                                    cookie_str=site_cookie,
                                    proxy_url=settings.normalized_proxy,
                                    preferred_qualities=requested,
                                    xh_preferred_codec=settings.xh_preferred_codec,
                                    max_attempts=settings.download_attempts,
                                    segment_concurrency=hls_seg_concurrency,
                                    max_buffer_bytes=hls_max_buffer_bytes,
                                    retry_delay_base=settings.retry_delay_base,
                                    retry_delay_max=settings.retry_delay_max,
                                    request_timeout=settings.request_timeout,
                                    min_file_size_bytes=settings.min_file_size_bytes,
                                    cancel_event=ctx.cancel_event,
                                    on_progress=_on_progress,
                                    on_log=lambda lvl, msg: self.events.emit_log(lvl, msg, job_id=job_id),
                                )
                            else:
                                outcome = await download_mp4_with_resume(
                                    session=dl_session,
                                    parse_ctx=site_ctx,
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
                                    cookie_str=site_cookie,
                                    proxy_url=settings.normalized_proxy,
                                    preferred_qualities=requested,
                                    xh_preferred_codec=settings.xh_preferred_codec,
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
                        site=item_site,
                        title=item["title"],
                        uploader=item["uploader"],
                        uploader_display_name=_resolve_item_display_name(item_id, item["uploader"]),
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
                            site=item_site,
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
                                                self._ctx_for_site(ctx, spec.site),
                                                [(spec.page_url, spec.page_num, spec.item_num)],
                                                settings.selectors,
                                                concurrency=1,
                                                preferred_qualities=_resolve_item_qualities(iid),
                                                include_keywords=include_keywords,
                                                exclude_keywords=exclude_keywords,
                                                uploader_hint=spec.uploader_hint,
                                                xh_preferred_codec=settings.xh_preferred_codec,
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
                                                folder = _target_folder_name(spec.default_uploader, spec.default_type, site=spec.site)
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
                                            detail_dn = (detail.uploader_display_name or "").strip()
                                            if detail_dn:
                                                item_display_names[iid] = detail_dn
                                                for alias in (resolved_uploader, detail.uploader_username):
                                                    alias_key = str(alias or "").strip().lower()
                                                    if alias_key and alias_key not in known_display_names:
                                                        known_display_names[alias_key] = detail_dn
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
                                await self._release_idle_http_sessions(ctx)
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
                    pending_cancel_msg = "因触发网盘限额已暂停" if self._current_halt_reason else "已取消"
                    await self.repo.bulk_cancel_pending_job_items(job_id, error_message=pending_cancel_msg)
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
                retry_candidates: List[Tuple[int, str, str]] = []
                for fid in failed_item_ids_pass1:
                    row = await self.repo.get_job_item(fid)
                    if row and row.get("page_url"):
                        r_site = normalize_site(
                            row.get("site") or ("xhamster" if is_xhamster_url(row.get("page_url")) else "pornhub")
                        )
                        retry_candidates.append((fid, row["page_url"], r_site))

                if retry_candidates:
                    grouped_candidates: Dict[Tuple[str, Optional[Tuple[str, ...]]], List[Tuple[int, str]]] = defaultdict(list)
                    for fid, purl, r_site in retry_candidates:
                        q_set = _resolve_item_qualities(fid)
                        q_key = tuple(sorted(q_set)) if q_set else None
                        grouped_candidates[(r_site, q_key)].append((fid, purl))

                    by_url: Dict[str, VideoDetail] = {}
                    for (grp_site, q_key), group_rows in grouped_candidates.items():
                        reparsed = await fetch_video_details_batch(
                            self._ctx_for_site(ctx, grp_site),
                            [(u, 0, idx + 1) for idx, (_, u) in enumerate(group_rows)],
                            settings.selectors,
                            concurrency=settings.max_concurrent_video_details,
                            preferred_qualities=set(q_key) if q_key else None,
                            xh_preferred_codec=settings.xh_preferred_codec,
                            timeout=settings.request_timeout,
                        )
                        for d in reparsed:
                            by_url[d.page_url] = d

                    ready_for_pass2: List[int] = []
                    unresolved_ids: List[int] = []
                    for fid, purl, _ in retry_candidates:
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
                    row_site = normalize_site(
                        row.get("site") or ("xhamster" if is_xhamster_url(row.get("page_url")) else "pornhub")
                    )
                    folder = _target_folder_name(
                        row.get("uploader"),
                        row.get("target_type") or "model",
                        site=row_site,
                    )
                    _record_failed_url(folder, row["page_url"])
        except BaseException:
            ctx.cancel_event.set()
            raise
        finally:
            upload_results = []
            if upload_tasks:
                with contextlib.suppress(Exception):
                    await self._release_idle_http_sessions(ctx)
                await upload_pool.mark_downloads_done()
                if ctx.cancel_event.is_set():
                    await upload_pool.notify_cancel()
                upload_results = await asyncio.gather(*upload_tasks, return_exceptions=True)
                leftovers = await upload_pool.drain_all()
                if leftovers:
                    await _mark_entries_cancelled(leftovers)
            self.events.clear_scheduler_status(job_id)

        for result in upload_results:
            if isinstance(result, Exception):
                raise result

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
        site: str = "pornhub",
        seen_item_keys: Optional[Set[str]] = None,
    ) -> List[int]:
        norm_site = normalize_site(site)
        item_ids: List[int] = []
        selected: Dict[str, VideoDetail] = {}
        for detail in details:
            if detail.filtered_out or detail.error == "操作已取消":
                continue
            key = _item_key(detail.viewkey, detail.page_url)
            if seen_item_keys is not None and key in seen_item_keys:
                continue
            old = selected.get(key)
            if old is None or (old.error and not detail.error and detail.download_url):
                selected[key] = detail

        specs: List[Dict[str, Any]] = []
        for detail in selected.values():
            uploader = (detail.uploader_username or default_uploader) if group_by_uploader else default_uploader
            utype = (detail.uploader_type or default_type) if group_by_uploader else default_type
            item_site = "xhamster" if (norm_site == "xhamster" or is_xhamster_url(detail.page_url)) else "pornhub"
            error = detail.error or (
                "未解析到视频标识或媒体直链" if not detail.download_url or not detail.viewkey else ""
            )
            specs.append(
                {
                    "site": item_site,
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
        if seen_item_keys is not None:
            seen_item_keys.update(selected)
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
        site: str = "pornhub",
        pages_spec: Optional[str] = None,
        videos_spec: Optional[str] = None,
        catch_up: bool = False,
        skip_vkeys: Optional[Set[str]] = None,
        lookup_skipped: Optional[Callable[[List[str]], Awaitable[Set[str]]]] = None,
        preferred_qualities: Optional[Set[str]] = None,
        include_keywords: Optional[Set[str]] = None,
        exclude_keywords: Optional[Set[str]] = None,
        uploader_hint: Optional[Tuple[str, str]] = None,
        on_profile: Optional[Callable[[str, str], Awaitable[None]]] = None,
        on_canonical_target: Optional[Callable[[str, str], Awaitable[None]]] = None,
        on_subscribe_action: Optional[Callable[[SubscribeAction, str], Awaitable[None]]] = None,
        on_page_progress: Optional[Callable[[int, int, int], None]] = None,
        on_scan_error: Optional[Callable[[str], None]] = None,
    ) -> Tuple[List[ListedVideo], List[VideoDetail]]:
        """Scan list pages with 4-way prefetch and stream details through one concurrency limit."""
        site_ctx = self._ctx_for_site(ctx, site)
        detail_semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_video_details))
        page_detail_tasks: deque[asyncio.Task] = deque()
        details: List[VideoDetail] = []
        active_hint: Optional[Tuple[str, str]] = uploader_hint

        async def _handle_canonical(canon_type: str, canon_user: str) -> None:
            nonlocal active_hint
            if active_hint is not None and active_hint[0] not in {"My_Liked", "My_Favorites"}:
                active_hint = (canon_user, canon_type)
            if on_canonical_target is not None:
                await on_canonical_target(canon_type, canon_user)

        async def _parse_page_details(page_items: List[ListedVideo]) -> List[VideoDetail]:
            return await fetch_video_details_batch(
                site_ctx,
                [(lv.url, lv.page_num, lv.item_num) for lv in page_items],
                settings.selectors,
                concurrency=settings.max_concurrent_video_details,
                preferred_qualities=preferred_qualities,
                include_keywords=include_keywords,
                exclude_keywords=exclude_keywords,
                uploader_hint=active_hint,
                semaphore=detail_semaphore,
                xh_preferred_codec=settings.xh_preferred_codec,
                timeout=settings.request_timeout,
            )

        async def _on_page(page_items: List[ListedVideo]) -> None:
            page_detail_tasks.append(asyncio.create_task(_parse_page_details(page_items)))
            if len(page_detail_tasks) >= max(2, 2 * settings.max_concurrent_list_pages):
                details.extend(await page_detail_tasks.popleft())

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
            if lookup_skipped is not None:
                scan_kwargs["lookup_skipped"] = lookup_skipped
            if on_canonical_target is not None:
                scan_kwargs["on_canonical_target"] = _handle_canonical
            if on_subscribe_action is not None:
                scan_kwargs["on_subscribe_action"] = on_subscribe_action
            if on_page_progress is not None:
                scan_kwargs["on_page_progress"] = on_page_progress
            listed = await scan_list_pages(
                site_ctx,
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
            details.extend(detail for group in detail_groups for detail in group)
        elif not details:
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
        site: str = "pornhub",
    ) -> None:
        norm_site = normalize_site(site)
        uname = (username or "").strip()
        utype = (target_type or ("creator" if norm_site == "xhamster" else "model")).strip().lower()
        if not uname or uname == "_currentUser_":
            return
        allowed_types = {"creator", "pornstar", "users", "channel"} if norm_site == "xhamster" else {"model", "pornstar"}
        if utype not in allowed_types:
            dedup = (f"{norm_site}:{utype}", uname.lower())
            if dedup not in tracked_keys and utype == "channel":
                tracked_keys.add(dedup)
                self.events.emit_log("INFO", f"跳过加入自动追更 '{uname}'：暂不支持 Channel 类型追更。", job_id=job_id)
            return
        dedup = (f"{norm_site}:{utype}", uname.lower())
        if dedup in tracked_keys:
            return
        tracked_keys.add(dedup)
        target, created = await self.repo.ensure_target(
            uname,
            utype,
            avatar_url=avatar_url,
            display_name=display_name,
            site=norm_site,
        )
        shown = (target or {}).get("display_name") or display_name or uname
        if created:
            self.events.emit_log("INFO", f"已自动加入追更目标: {shown} ({utype})", job_id=job_id)
            self.events.broadcast("targets_changed", {})
            self.events.broadcast("overview_changed", {})
        else:
            if avatar_url.strip() or display_name.strip():
                self.events.broadcast("targets_changed", {})
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
        site_prefix = (action.site if action and action.site else "").strip().lower()
        raw_key = (action.creator_id if action and action.creator_id else dedup_key).strip().lower()
        key = f"{site_prefix}:{raw_key}" if (site_prefix and raw_key and not raw_key.startswith(f"{site_prefix}:")) else raw_key
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
        site = normalize_site(params.get("site"))
        site_ctx = self._ctx_for_site(ctx, site)
        usernames: List[str] = [str(u).strip() for u in (params.get("usernames") or []) if str(u).strip()]
        raw_type = (params.get("target_type") or "").strip().lower()
        if site == "xhamster" and raw_type == "user":
            raw_type = "users"
        quality_setting = params.get("quality")
        qualities = _quality_set(settings.preferred_quality if quality_setting is None else quality_setting)
        include_kw = _split_csv_set(params.get("keywords"), lower=True)
        exclude_kw = _split_csv_set(params.get("exclude_keywords"), lower=True)
        pages_spec = (params.get("pages") or "").strip() or None
        videos_spec = (params.get("videos") or "").strip() or None
        redownload = bool(params.get("redownload", False))
        export_only = bool(params.get("export_only", False))
        auto_upload = _effective_auto_upload(params, settings)
        add_to_auto_track = bool(params.get("add_to_auto_track", False))
        subscribe_creator = bool(params.get("subscribe_creator", False))
        historical_items = (
            await self.repo.list_job_items(job_id) if not export_only and params.get("resume_mode") == "rescan" else []
        )
        existing_keys = {_item_key(item["viewkey"], item["page_url"]) for item in historical_items}

        targets: List[Tuple[str, str]] = []
        target_aliases: Dict[str, List[str]] = {}
        resolved_profile_names: Dict[str, str] = {}
        resolved_profile_avatars: Dict[str, str] = {}
        if raw_type == "liked":
            targets.append(("_currentUser_", "liked"))
        elif site == "xhamster" and raw_type == "favorites":
            targets.append(("_currentUser_", "favorites"))
        else:
            if not usernames and raw_type == "favorites" and site == "pornhub" and settings.ph_logged_in_username:
                usernames = [settings.ph_logged_in_username]
            seen_users: Set[str] = set()
            explicit_types = (
                {"creator", "pornstar", "users", "channel"}
                if site == "xhamster"
                else {"model", "pornstar", "favorites"}
            )
            for uname in usernames:
                if uname.casefold() in seen_users:
                    continue
                seen_users.add(uname.casefold())
                if raw_type in explicit_types:
                    resolved = raw_type
                    resolved_uname = uname
                elif site == "xhamster":
                    detected = await resolve_xhamster_target_with_profile(
                        site_ctx, uname, timeout=settings.request_timeout
                    )
                    if detected:
                        resolved, resolved_uname, det_name, det_avatar = detected
                        for key in dict.fromkeys((resolved_uname, uname)):
                            if det_name:
                                resolved_profile_names[key] = det_name
                            if det_avatar:
                                resolved_profile_avatars[key] = det_avatar
                    else:
                        resolved, resolved_uname = "creator", uname
                    if resolved_uname.casefold() != uname.casefold():
                        self.events.emit_log(
                            "INFO",
                            f"自动识别 xHamster 目标 '{uname}' 关联创作者主页 '{resolved_uname}' ({resolved})。",
                            job_id=job_id,
                        )
                    else:
                        self.events.emit_log("INFO", f"自动识别 xHamster 目标 '{resolved_uname}' 类型为 '{resolved}'。", job_id=job_id)
                else:
                    detected = await resolve_target_type(site_ctx, uname, timeout=settings.request_timeout)
                    if detected == "channel":
                        self.events.emit_log("WARNING", f"跳过 '{uname}'：识别为 Channel，当前不支持 Channel。", job_id=job_id)
                        continue
                    resolved = detected or "model"
                    resolved_uname = uname
                    self.events.emit_log("INFO", f"自动识别目标 '{uname}' 类型为 '{resolved}'。", job_id=job_id)
                aliases = target_aliases.setdefault(resolved_uname.casefold(), [])
                for cand_alias in (resolved_uname, uname):
                    if cand_alias and cand_alias not in aliases:
                        aliases.append(cand_alias)
                if not any(t_u.casefold() == resolved_uname.casefold() and t_t == resolved for t_u, t_t in targets):
                    targets.append((resolved_uname, resolved))

        if not targets:
            await self.repo.update_job(job_id, error_message="没有有效的下载目标")
            self.events.emit_log("WARNING", "没有有效的下载目标。", job_id=job_id)
            if not historical_items:
                return

        known_vkeys = set(params.get("skip_completed_viewkeys") or ())
        known_vkeys.update(existing_keys)

        async def lookup_uploaded(keys: List[str]) -> Set[str]:
            return await self.repo.get_known_viewkeys(keys, uploaded_only=True)
        all_details: List[VideoDetail] = []
        scoped_listed: Dict[str, Tuple[ListedVideo, str, str]] = {}
        scan_errors: List[str] = []
        persisted_avatars: Dict[str, str] = dict(params.get("profile_avatars") or {})
        persisted_names: Dict[str, str] = dict(params.get("profile_names") or {})
        profile_avatars: Dict[str, str] = {**persisted_avatars, **resolved_profile_avatars}
        profile_names: Dict[str, str] = {**persisted_names, **resolved_profile_names}
        profile_lock = asyncio.Lock()

        async def _persist_manual_profile(
            keys: Iterable[str],
            target_type: str,
            url: str,
            display_name: str,
        ) -> None:
            clean_url = (url or "").strip()
            clean_name = (display_name or "").strip()
            if not clean_url and not clean_name:
                return
            unique_keys = [k for k in dict.fromkeys(str(x or "").strip() for x in keys) if k and k != "_currentUser_"]
            if not unique_keys:
                return
            async with profile_lock:
                for key in unique_keys:
                    await self.repo.update_target_profile_by_username(
                        key,
                        target_type,
                        avatar_url=clean_url,
                        display_name=clean_name,
                        site=site,
                    )
                    if clean_name:
                        await self.repo.update_videos_uploader_display_name(site, key, clean_name)
                changed = False
                for key in unique_keys:
                    if clean_url and persisted_avatars.get(key) != clean_url:
                        profile_avatars[key] = clean_url
                        persisted_avatars[key] = clean_url
                        changed = True
                    if clean_name and persisted_names.get(key) != clean_name:
                        profile_names[key] = clean_name
                        persisted_names[key] = clean_name
                        changed = True
                if changed:
                    updated_job = await self.repo.merge_job_params(
                        job_id,
                        {"profile_avatars": dict(profile_avatars), "profile_names": dict(profile_names)},
                    )
                    if updated_job:
                        self.events.broadcast("job_updated", updated_job)

        if profile_avatars != persisted_avatars or profile_names != persisted_names:
            for t_uname, t_utype in targets:
                if t_uname == "_currentUser_":
                    continue
                t_keys = target_aliases.get(t_uname.casefold(), [t_uname])
                await _persist_manual_profile(
                    t_keys,
                    t_utype,
                    profile_avatars.get(t_uname) or "",
                    profile_names.get(t_uname) or "",
                )

        tracked_keys: Set[Tuple[str, str]] = set()
        subscribed_keys: Set[str] = set()

        for target_idx, (uname, utype) in enumerate(targets):
            if ctx.cancel_event.is_set():
                break
            list_url = build_target_list_url(uname if uname != "_currentUser_" else None, utype, site=site)
            if utype == "liked":
                target_display = "Liked Videos"
            elif site == "xhamster" and utype == "favorites":
                target_display = "xHamster Favorites"
            else:
                target_display = profile_names.get(uname) or uname

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

            active_uname = uname
            active_utype = utype
            if utype == "liked":
                resolved_user = "My_Liked"
            elif site == "xhamster" and utype == "favorites":
                resolved_user = "My_Favorites"
            else:
                resolved_user = uname

            async def _save_target_profile(
                url: str,
                display_name: str,
            ) -> None:
                target_user = active_uname
                target_type = active_utype
                if target_user == "_currentUser_":
                    await self.repo.update_target_profile_by_username(
                        target_user,
                        target_type,
                        avatar_url=url,
                        display_name=display_name,
                        site=site,
                    )
                    return
                keys = [target_user, uname, *target_aliases.get(uname.casefold(), ())]
                await _persist_manual_profile(keys, target_type, url, display_name)

            can_link_creator = (
                utype in ({"creator", "pornstar", "users", "channel"} if site == "xhamster" else {"model", "pornstar"})
                and uname != "_currentUser_"
            )
            subscribe_attempted = False

            async def _handle_list_subscribe(
                action: SubscribeAction,
                referer_url: str,
            ) -> None:
                nonlocal subscribe_attempted
                subscribe_attempted = True
                target_user = active_uname
                target_type = active_utype
                label = profile_names.get(target_user) or target_user
                sub_res = await self._maybe_subscribe_creator(
                    job_id,
                    site_ctx,
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

            async def _handle_manual_canonical(canon_type: str, canon_user: str) -> None:
                nonlocal active_uname, active_utype, resolved_user
                if active_uname == "_currentUser_":
                    return
                prev_uname, prev_utype = active_uname, active_utype
                active_uname, active_utype = canon_user, canon_type
                resolved_user = canon_user
                if prev_uname in profile_avatars and canon_user not in profile_avatars:
                    profile_avatars[canon_user] = profile_avatars[prev_uname]
                if prev_uname in profile_names and canon_user not in profile_names:
                    profile_names[canon_user] = profile_names[prev_uname]
                aliases = target_aliases.setdefault(canon_user.casefold(), [])
                for cand_alias in (canon_user, prev_uname, *target_aliases.get(prev_uname.casefold(), ())):
                    if cand_alias and cand_alias not in aliases:
                        aliases.append(cand_alias)
                target_aliases[prev_uname.casefold()] = aliases
                self.events.emit_log(
                    "INFO",
                    f"目标 '{prev_uname}' ({prev_utype}) 重定向至新标识 '{canon_user}' ({canon_type})。",
                    job_id=job_id,
                )

            if export_only:
                listed, details = await self._scan_target_with_details(
                    job_id,
                    ctx=ctx,
                    list_url=list_url,
                    target_type=utype,
                    settings=settings,
                    site=site,
                    pages_spec=pages_spec,
                    videos_spec=videos_spec,
                    skip_vkeys=known_vkeys,
                    lookup_skipped=None if redownload or export_only or qualities else lookup_uploaded,
                    preferred_qualities=qualities,
                    include_keywords=include_kw,
                    exclude_keywords=exclude_kw,
                    uploader_hint=(resolved_user, utype),
                    on_profile=_save_target_profile,
                    on_canonical_target=_handle_manual_canonical if can_link_creator else None,
                    on_subscribe_action=on_sub_cb,
                    on_page_progress=_emit_manual_page_progress,
                    on_scan_error=page_errors.append,
                )
                scan_errors.extend(f"{active_uname}: {error}" for error in page_errors)
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
                if not (redownload or export_only or qualities):
                    scan_kwargs["lookup_skipped"] = lookup_uploaded
                if can_link_creator:
                    scan_kwargs["on_canonical_target"] = _handle_manual_canonical
                if on_sub_cb is not None:
                    scan_kwargs["on_subscribe_action"] = on_sub_cb
                listed = await scan_list_pages(
                    site_ctx,
                    list_url,
                    settings.selectors,
                    **scan_kwargs,
                )
                details = []
                scan_errors.extend(f"{active_uname}: {error}" for error in page_errors)

            if subscribe_creator and can_link_creator and not subscribe_attempted and not ctx.cancel_event.is_set():
                # Fallback to profile homepage only if list pages yielded no subscribe button.
                if site == "xhamster":
                    profile_url = build_xhamster_profile_url(active_uname, active_utype)
                    profile_headers: Optional[Dict[str, str]] = {"Referer": "https://xhamster.com/"}
                else:
                    profile_url = f"{PH_ORIGIN}/{active_utype}/{urllib.parse.quote(active_uname, safe='')}"
                    profile_headers = None
                prof_res, _ = await site_ctx.fetch_with_pow(
                    profile_url,
                    headers=profile_headers,
                    timeout=max(5.0, float(settings.request_timeout)),
                    max_retries=1,
                    log_label=f"获取博主主页 {active_uname}",
                )
                if prof_res is not None and prof_res.status_code < 400:
                    fallback_action = (
                        extract_xhamster_subscribe_action_from_html(prof_res.text)
                        if site == "xhamster"
                        else extract_subscribe_action(prof_res.text)
                    )
                else:
                    fallback_action = None
                await _handle_list_subscribe(
                    fallback_action or SubscribeAction(site=site),
                    (prof_res.final_url if prof_res is not None else profile_url),
                )
                del prof_res

            if add_to_auto_track and can_link_creator and not ctx.cancel_event.is_set():
                await self._maybe_track_creator(
                    job_id,
                    active_uname,
                    active_utype,
                    display_name=profile_names.get(active_uname) or profile_names.get(uname) or "",
                    avatar_url=profile_avatars.get(active_uname) or profile_avatars.get(uname) or "",
                    tracked_keys=tracked_keys,
                    site=site,
                )

            if not listed:
                if not page_errors and not ctx.cancel_event.is_set():
                    self.events.emit_log("INFO", f"目标 '{active_uname}' ({active_utype}) 未发现待处理的视频。", job_id=job_id)
                continue

            if export_only:
                all_details.extend(details)
            else:
                for lv in listed:
                    key = _item_key(lv.viewkey, lv.url)
                    if key not in existing_keys and key not in scoped_listed:
                        scoped_listed[key] = (lv, resolved_user, active_utype)
            listed = []

        if scan_errors:
            await self.repo.update_job(job_id, error_message=f"{len(scan_errors)} 页扫描未完成：{scan_errors[0]}")
        if export_only:
            await self._record_export_result(job_id, all_details)
            return

        if ctx.cancel_event.is_set() or (not scoped_listed and not historical_items):
            return

        all_item_ids, parse_specs, item_qualities = await self._prepare_resume_items(historical_items, params, settings)
        del historical_items, existing_keys
        listed_entries = list(scoped_listed.values())
        scoped_listed.clear()
        item_specs = [
            {
                "site": site,
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
                site=site,
            )
            self.events.broadcast("item_updated", row)
        del listed_entries, item_specs, rows
        known_vkeys.clear()
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
            await self._release_idle_http_sessions(ctx)
            await reclaim_memory(self.repo.db)

            async def _backfill_target_profile_from_detail(detail: VideoDetail) -> None:
                det_name = (detail.uploader_display_name or "").strip()
                det_avatar = (detail.uploader_avatar_url or "").strip()
                if not det_name and not det_avatar:
                    return
                det_user = (detail.uploader_username or "").strip()
                matched_keys: List[str] = []
                matched_type = (detail.uploader_type or ("creator" if site == "xhamster" else "model")).strip().lower()
                for t_uname, t_utype in targets:
                    if t_uname == "_currentUser_":
                        continue
                    aliases = target_aliases.get(t_uname.casefold(), [t_uname])
                    if not det_user or any(a.casefold() == det_user.casefold() for a in aliases) or len(targets) == 1:
                        matched_keys.extend(aliases)
                        matched_type = t_utype
                        break
                if det_user:
                    matched_keys.append(det_user)
                if matched_keys:
                    # Only fill missing fields from video detail pages so profile-page metadata stays authoritative.
                    existing_avatar = any(persisted_avatars.get(k) for k in matched_keys)
                    existing_name = any(persisted_names.get(k) for k in matched_keys)
                    await _persist_manual_profile(
                        matched_keys,
                        matched_type,
                        "" if existing_avatar else det_avatar,
                        "" if existing_name else det_name,
                    )

            await self._run_download_upload_pipeline(
                job_id,
                all_item_ids,
                settings,
                ctx,
                auto_upload=auto_upload,
                redownload=redownload,
                preferred_qualities=qualities,
                item_preferred_qualities=item_qualities or None,
                parse_specs=parse_specs,
                include_keywords=include_kw,
                exclude_keywords=exclude_kw,
                on_detail_parsed=_backfill_target_profile_from_detail,
                uploader_display_names=profile_names,
            )

    def _url_creator_linkage_handler(
        self, job_id: int, params: Dict[str, Any], settings: RuntimeSettings, ctx: TaskHttpContext,
    ) -> Optional[Callable[[VideoDetail], Awaitable[None]]]:
        add_to_auto_track = bool(params.get("add_to_auto_track", False))
        subscribe_creator = bool(params.get("subscribe_creator", False))
        if not (add_to_auto_track or subscribe_creator):
            return None

        creator_link_lock = asyncio.Lock()
        tracked_keys: Set[Tuple[str, str]] = set()
        subscribed_keys: Set[str] = set()

        async def _handle_url_creator_linkage(detail: VideoDetail) -> None:
            if ctx.cancel_event.is_set():
                return
            async with creator_link_lock:
                detail_site = detect_site_from_url(detail.page_url)
                sub_res: Optional[SubscribeResult] = None
                uploader_name = (detail.uploader_username or "").strip()
                uploader_type = (
                    detail.uploader_type or ("creator" if detail_site == "xhamster" else "model")
                ).strip().lower()
                creator_label = (
                    (detail.uploader_display_name or "").strip()
                    or uploader_name
                    or (detail.subscribe_action.creator_id if detail.subscribe_action else "")
                    or detail.viewkey
                )
                if subscribe_creator:
                    dedup_key = (
                        f"{detail_site}:{uploader_type}:{uploader_name.lower()}"
                        if uploader_name
                        else f"{detail_site}:{detail.viewkey or detail.page_url}"
                    )
                    sub_res = await self._maybe_subscribe_creator(
                        job_id,
                        self._ctx_for_site(ctx, detail_site),
                        detail.subscribe_action or SubscribeAction(site=detail_site),
                        referer_url=detail.referer_url or detail.page_url,
                        creator_label=creator_label,
                        dedup_key=dedup_key,
                        subscribed_keys=subscribed_keys,
                        timeout=settings.request_timeout,
                    )
                if add_to_auto_track and uploader_name:
                    resolved_avatar = (
                        (sub_res.avatar_url if sub_res and sub_res.avatar_url else "")
                        or (detail.uploader_avatar_url or "").strip()
                    )
                    await self._maybe_track_creator(
                        job_id,
                        uploader_name,
                        uploader_type,
                        display_name=(detail.uploader_display_name or "").strip(),
                        avatar_url=resolved_avatar,
                        tracked_keys=tracked_keys,
                        site=detail_site,
                    )

        return _handle_url_creator_linkage

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
        auto_upload = _effective_auto_upload(params, settings)
        batch_folder = sanitize_filename(params.get("batch_name") or "url_batch")

        on_detail_parsed = self._url_creator_linkage_handler(job_id, params, settings, ctx)

        if export_only:
            self.events.emit_log("INFO", f"正在并发解析 {len(video_urls)} 条视频链接...", job_id=job_id)
            by_site_urls: Dict[str, List[Tuple[str, int, int]]] = defaultdict(list)
            for idx, u in enumerate(video_urls, start=1):
                by_site_urls[detect_site_from_url(u)].append((u, 1, idx))
            details: List[VideoDetail] = []
            for url_site, url_entries in by_site_urls.items():
                details.extend(
                    await fetch_video_details_batch(
                        self._ctx_for_site(ctx, url_site),
                        url_entries,
                        settings.selectors,
                        concurrency=settings.max_concurrent_video_details,
                        preferred_qualities=qualities,
                        include_keywords=include_kw,
                        exclude_keywords=exclude_kw,
                        xh_preferred_codec=settings.xh_preferred_codec,
                        timeout=settings.request_timeout,
                    )
                )
            details.sort(key=lambda d: (d.page_num, d.item_num))
            if on_detail_parsed is not None:
                for d in details:
                    if not d.error or d.filtered_out:
                        await on_detail_parsed(d)
            await self._record_export_result(job_id, details)
            return

        historical_items = await self.repo.list_job_items(job_id) if params.get("resume_mode") == "rescan" else []
        existing_keys = {_item_key(item["viewkey"], item["page_url"]) for item in historical_items}
        existing_keys.update(_item_key("", item["page_url"]) for item in historical_items)
        item_ids, parse_specs, item_qualities = await self._prepare_resume_items(historical_items, params, settings)
        del historical_items
        new_urls = [u for u in video_urls if _item_key("", u) not in existing_keys]
        item_specs = [
            {
                "site": detect_site_from_url(u),
                "viewkey": extract_viewkey_from_url(u) or "",
                "title": extract_viewkey_from_url(u) or u,
                "uploader": batch_folder,
                "target_type": "creator" if is_xhamster_url(u) else "model",
                "page_url": u,
                "download_url": "",
                "referer_url": u,
                "quality": "",
                "status": "pending",
                "error_message": "",
                "upload_requested": auto_upload,
            }
            for u in new_urls
        ]
        rows = await self.repo.add_job_items_batch(job_id, item_specs)
        for idx, (row, u) in enumerate(zip(rows, new_urls), start=1):
            iid = int(row["id"])
            item_ids.append(iid)
            url_site = detect_site_from_url(u)
            parse_specs[iid] = _PendingParseSpec(
                item_id=iid,
                page_url=u,
                page_num=1,
                item_num=idx,
                default_uploader=batch_folder,
                default_type="creator" if url_site == "xhamster" else "model",
                group_by_uploader=group_by_uploader,
                site=url_site,
            )
            self.events.broadcast("item_updated", row)
        del item_specs, rows, existing_keys
        updated_job = await self.repo.recalculate_job_counts(job_id)
        if updated_job:
            self.events.broadcast("job_updated", updated_job)
        self.events.emit_log(
            "INFO",
            f"已入队 {len(item_ids)} 条链接，启动流式按需解析、动态下载与实时上传...",
            job_id=job_id,
        )
        url_profile_avatars: Dict[str, str] = dict(params.get("profile_avatars") or {})
        url_profile_names: Dict[str, str] = dict(params.get("profile_names") or {})
        url_profile_lock = asyncio.Lock()

        async def _handle_url_detail_parsed(detail: VideoDetail) -> None:
            if on_detail_parsed is not None:
                await on_detail_parsed(detail)
            uname = (detail.uploader_username or "").strip()
            dname = (detail.uploader_display_name or "").strip()
            avatar = (detail.uploader_avatar_url or "").strip()
            if not uname or (not dname and not avatar):
                return
            detail_site = detect_site_from_url(detail.page_url)
            async with url_profile_lock:
                if dname:
                    await self.repo.update_videos_uploader_display_name(detail_site, uname, dname)
                changed = False
                if dname and url_profile_names.get(uname) != dname:
                    url_profile_names[uname] = dname
                    changed = True
                if avatar and url_profile_avatars.get(uname) != avatar:
                    url_profile_avatars[uname] = avatar
                    changed = True
                if changed:
                    updated_job = await self.repo.merge_job_params(
                        job_id,
                        {"profile_avatars": dict(url_profile_avatars), "profile_names": dict(url_profile_names)},
                    )
                    if updated_job:
                        self.events.broadcast("job_updated", updated_job)

        if item_ids and not ctx.cancel_event.is_set():
            await self._run_download_upload_pipeline(
                job_id,
                item_ids,
                settings,
                ctx,
                auto_upload=auto_upload,
                redownload=redownload,
                preferred_qualities=qualities,
                item_preferred_qualities=item_qualities or None,
                parse_specs=parse_specs,
                include_keywords=include_kw,
                exclude_keywords=exclude_kw,
                on_detail_parsed=_handle_url_detail_parsed,
                uploader_display_names=url_profile_names,
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
        rescan = params.get("resume_mode") == "rescan"
        auto_upload = _effective_auto_upload(params, settings)
        historical_items = await self.repo.list_job_items(job_id) if auto_download and rescan else []
        existing_keys = {_item_key(item["viewkey"], item["page_url"]) for item in historical_items}
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
        if auto_download and auto_upload and not target_ids:
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
                    "site": normalize_site(video.get("site") or ("xhamster" if is_xhamster_url(video.get("video_page_url")) else "pornhub")),
                    "viewkey": str(video.get("viewkey") or ""),
                    "title": str(video.get("title") or ""),
                    "uploader": str(video.get("uploader") or ""),
                    "target_type": str(video.get("target_type") or "model"),
                    "page_url": video.get("video_page_url")
                    or (
                        f"https://xhamster.com/videos/{video['viewkey']}"
                        if normalize_site(video.get("site")) == "xhamster"
                        else f"https://www.pornhub.com/view_video.php?viewkey={video['viewkey']}"
                    ),
                    "quality": str(video.get("quality") or ""),
                    "status": "pending",
                    "local_path": str(video.get("local_path") or "").strip(),
                    "upload_requested": True,
                    "published_at": str(video.get("published_at") or "").strip(),
                    "duration_seconds": int(video.get("duration_seconds") or 0),
                }
                for video in valid_pending
                if video["viewkey"] not in existing_keys
            ]
            if pending_specs:
                for row in await self.repo.add_job_items_batch(job_id, pending_specs):
                    pending_upload_ids.append(int(row["id"]))
                    existing_keys.add(row["viewkey"])
                    self.events.broadcast("item_updated", row)
            del pending_videos, valid_pending, pending_specs
            if pending_upload_ids:
                refreshed_pending = await self.repo.recalculate_job_counts(job_id)
                if refreshed_pending:
                    self.events.broadcast("job_updated", refreshed_pending)
                self.events.emit_log(
                    "INFO",
                    f"检测到 {len(pending_upload_ids)} 个已下载未上传视频，纳入本次补偿上传。",
                    job_id=job_id,
                )

        if not targets and not pending_upload_ids and not historical_items:
            self.events.emit_log("WARNING", "追更列表为空或没有已启用的目标。", job_id=job_id)
            return

        known_vkeys = set(existing_keys)
        discovered_vkeys: Set[str] = set()
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
                tsite = normalize_site(target.get("site"))
                uname = target["username"]
                utype = target["target_type"]
                is_self_col = utype == "liked" or (tsite == "xhamster" and utype == "favorites")
                if utype == "liked":
                    display = "Liked Videos"
                    display_label = "Liked Videos"
                    default_uploader = "My_Liked"
                elif tsite == "xhamster" and utype == "favorites":
                    display = "xHamster Favorites"
                    display_label = (target.get("display_name") or "").strip() or "xHamster Favorites"
                    default_uploader = "My_Favorites"
                else:
                    display = uname
                    display_label = (target.get("display_name") or "").strip() or uname
                    default_uploader = uname
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

                async def _sync_canonical_target(
                    canon_type: str,
                    canon_user: str,
                    target_id: int = tid,
                ) -> None:
                    nonlocal uname, utype, display, default_uploader
                    if is_self_col or uname == "_currentUser_":
                        return
                    prev_uname, prev_utype = uname, utype
                    try:
                        await self.repo.update_target(
                            target_id,
                            {"username": canon_user, "target_type": canon_type},
                        )
                        uname, utype = canon_user, canon_type
                        display = canon_user
                        default_uploader = canon_user
                        self.events.broadcast("overview_changed", {})
                        self.events.emit_log(
                            "INFO",
                            f"目标 '{prev_uname}' ({prev_utype}) 已重定向至新标识 '{canon_user}' ({canon_type})，已自动更新监控目标及历史归属。",
                            job_id=job_id,
                        )
                    except sqlite3.IntegrityError:
                        uname, utype = canon_user, canon_type
                        display = canon_user
                        default_uploader = canon_user
                        self.events.emit_log(
                            "WARNING",
                            f"目标 '{prev_uname}' 已重定向至 '{canon_user}' ({canon_type})，但监控列表中已存在同名目标，未自动覆盖（请清理重复目标）。",
                            job_id=job_id,
                        )

                list_url = build_target_list_url(uname if uname != "_currentUser_" else None, utype, site=tsite)
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
                        site=tsite,
                        catch_up=(not full_scan and not needs_rescan and not (auto_download and rescan and historical_items)),
                        skip_vkeys=known_vkeys,
                        lookup_skipped=self.repo.get_known_viewkeys,
                        preferred_qualities=qual_set,
                        uploader_hint=(default_uploader, utype),
                        on_profile=_save_profile,
                        on_canonical_target=_sync_canonical_target if not is_self_col else None,
                        on_scan_error=page_errors.append,
                    )
                except Exception as err:
                    target_error_count += 1
                    await self.repo.mark_target_checked(tid, 0, error=str(err), new_viewkeys=[])
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
                canonical_known = await self.repo.get_known_viewkeys(d.viewkey for d in details)
                candidates = [
                    d for d in details
                    if not d.viewkey or (d.viewkey not in known_vkeys and d.viewkey not in canonical_known and d.viewkey not in discovered_vkeys)
                ]
                valid_new = [d for d in candidates if d.download_url and d.viewkey and not d.error]
                valid_new.sort(key=lambda d: (d.page_num, d.item_num))
                discovered_vkeys.update(d.viewkey for d in valid_new)
                detail_failures = self._detail_failures(candidates)
                errors = list(page_errors)
                if detail_failures:
                    errors.append(f"{len(detail_failures)} 个视频解析失败")
                if errors:
                    target_error_count += 1
                await self.repo.mark_target_checked(
                    tid,
                    len(valid_new),
                    error="；".join(errors[:3]),
                    new_viewkeys=[d.viewkey for d in valid_new],
                )

                folder = _target_folder_name(uname, utype, site=tsite)
                if valid_new:
                    new_by_target[folder] = new_by_target.get(folder, 0) + len(valid_new)

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
                        "site": tsite,
                        "uname": uname,
                        "default_uploader": default_uploader,
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

        for idx in range(len(ordered_outcomes)):
            outcome = ordered_outcomes[idx]
            ordered_outcomes[idx] = None
            if outcome is None or ctx.cancel_event.is_set():
                continue
            tsite = outcome["site"]
            default_uploader = outcome["default_uploader"]
            utype = outcome["utype"]
            display = outcome["display"]
            qual_set = outcome["qual_set"]
            candidates = outcome["candidates"]
            valid_new = outcome["valid_new"]
            if auto_download:
                created = await self._create_items_from_details(
                    job_id,
                    [d for d in candidates if d.viewkey not in discovered_vkeys or (d.download_url and not d.error)],
                    default_uploader=default_uploader,
                    default_type=utype,
                    group_by_uploader=False,
                    upload_requested=auto_upload,
                    site=tsite,
                    seen_item_keys=existing_keys,
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

        del ordered_outcomes
        outcome = candidates = valid_new = None
        known_vkeys.clear()
        discovered_vkeys.clear()
        self.events.broadcast("overview_changed", {})

        if target_error_count > 0:
            message = f"{target_error_count} 个目标检查失败，可查看目标列表或实时日志"
            await self.repo.update_job(job_id, error_message=message)
            self.events.emit_log("WARNING", message, job_id=job_id)

        if ctx.cancel_event.is_set():
            return

        if auto_download:
            resumed_ids, parse_specs, resumed_qualities = await self._prepare_resume_items(historical_items, params, settings)
            del historical_items, existing_keys
            item_qual_map.update(resumed_qualities)
            pipeline_item_ids = [*resumed_ids, *pending_upload_ids, *new_item_ids]
            if pipeline_item_ids:
                self.events.clear_scan_status(job_id)
                await self._release_idle_http_sessions(ctx)
                await reclaim_memory(self.repo.db)
                auto_display_names = {
                    str(t.get("username") or "").strip(): str(t.get("display_name") or "").strip()
                    for t in targets
                    if str(t.get("username") or "").strip() and str(t.get("display_name") or "").strip()
                }
                uploaded_by, failed_urls_by = await self._run_download_upload_pipeline(
                    job_id,
                    pipeline_item_ids,
                    settings,
                    ctx,
                    auto_upload=auto_upload,
                    redownload=bool(params.get("redownload", False)),
                    preferred_qualities=_quality_set(settings.preferred_quality),
                    item_preferred_qualities=item_qual_map,
                    parse_specs=parse_specs or None,
                    uploader_display_names=auto_display_names,
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

        tsite = normalize_site(target.get("site"))
        site_ctx = self._ctx_for_site(ctx, tsite)
        uname = target["username"]
        utype = target["target_type"]
        is_self_col = utype == "liked" or (tsite == "xhamster" and utype == "favorites")
        pages_spec = (params.get("pages") or "").strip() or None
        list_url = build_target_list_url(uname if uname != "_currentUser_" else None, utype, site=tsite)

        target_display_name = (target.get("display_name") or "").strip()

        async def _save_profile(url: str, display_name: str) -> None:
            nonlocal target_display_name
            if (display_name or "").strip():
                target_display_name = display_name.strip()
            await self.repo.update_target_profile(
                int(target["id"]),
                avatar_url=url,
                display_name=display_name,
            )

        async def _sync_baseline_canonical(canon_type: str, canon_user: str) -> None:
            nonlocal uname, utype
            if is_self_col or uname == "_currentUser_":
                return
            prev_uname, prev_utype = uname, utype
            try:
                await self.repo.update_target(
                    int(target["id"]),
                    {"username": canon_user, "target_type": canon_type},
                )
                uname, utype = canon_user, canon_type
                self.events.broadcast("overview_changed", {})
                self.events.emit_log(
                    "INFO",
                    f"目标 '{prev_uname}' ({prev_utype}) 已重定向至新标识 '{canon_user}' ({canon_type})，已自动更新监控目标及历史归属。",
                    job_id=job_id,
                )
            except sqlite3.IntegrityError:
                uname, utype = canon_user, canon_type
                self.events.emit_log(
                    "WARNING",
                    f"目标 '{prev_uname}' 已重定向至 '{canon_user}' ({canon_type})，但监控列表中已存在同名目标，未自动覆盖。",
                    job_id=job_id,
                )

        if utype == "liked":
            target_label = "Liked Videos"
            default_uploader = "My_Liked"
        elif tsite == "xhamster" and utype == "favorites":
            target_label = (target.get("display_name") or "").strip() or "xHamster Favorites"
            default_uploader = "My_Favorites"
        else:
            target_label = (target.get("display_name") or "").strip() or uname
            default_uploader = uname

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
        scan_kwargs: Dict[str, Any] = {
            "target_type": utype,
            "pages_spec": pages_spec,
            "delay_min": settings.page_delay_min,
            "delay_max": settings.page_delay_max,
            "page_concurrency": settings.max_concurrent_list_pages,
            "timeout": settings.request_timeout,
            "profile_username": uname,
            "on_profile": _save_profile,
            "on_page_progress": _emit_baseline_page_progress,
            "on_error": page_errors.append,
        }
        if not is_self_col:
            scan_kwargs["on_canonical_target"] = _sync_baseline_canonical
        listed = await scan_list_pages(
            site_ctx,
            list_url,
            settings.selectors,
            **scan_kwargs,
        )

        batch_records = []
        for lv in listed:
            if ctx.cancel_event.is_set():
                break
            batch_records.append(
                {
                    "site": tsite,
                    "viewkey": lv.viewkey,
                    "title": lv.title,
                    "uploader": default_uploader if is_self_col else uname,
                    "uploader_display_name": "" if is_self_col else target_display_name,
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
            await self.repo.mark_target_checked(int(target["id"]), 0, error=error, new_viewkeys=[])
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
        resumed = params.get("resume_mode") == "enrich"
        item_ids: Dict[str, int] = {}
        if resumed:
            await self.repo.reconcile_metadata_job_items(job_id)
            item_ids = {item["viewkey"]: int(item["id"]) for item in await self.repo.list_job_items(job_id)}
            self.events.broadcast("job_items_changed", {"job_id": job_id})
        candidates = await self.repo.list_baseline_metadata_candidates(
            limit=limit, resume_job_id=job_id if resumed else None,
        )
        if not candidates:
            self.events.emit_log("INFO", "没有需要补全元数据的基线视频。", job_id=job_id)
            return

        self.events.emit_log(
            "INFO",
            f"开始补全 {len(candidates)} 条基线视频的画质和文件大小...",
            job_id=job_id,
        )
        def _candidate_site(candidate: Dict[str, Any]) -> str:
            return normalize_site(
                candidate.get("site")
                or ("xhamster" if is_xhamster_url(candidate.get("video_page_url")) else "pornhub")
            )

        item_specs = [
            {
                "site": _candidate_site(candidate),
                "viewkey": candidate["viewkey"],
                "title": candidate.get("title") or "",
                "uploader": candidate.get("uploader") or "",
                "target_type": candidate.get("target_type") or "model",
                "page_url": candidate.get("video_page_url") or "",
                "quality": candidate.get("quality") or "",
                "status": "pending",
            }
            for candidate in candidates
            if candidate["viewkey"] not in item_ids
        ]
        item_ids.update(
            (row["viewkey"], int(row["id"])) for row in await self.repo.add_job_items_batch(job_id, item_specs)
        )
        del item_specs

        await self.repo.update_job(job_id, status="running")
        created_job = await self.repo.recalculate_job_counts(job_id)
        if created_job:
            self.events.broadcast("job_updated", created_job)

        size_limit = max(1, min(20, settings.max_concurrent_video_details))
        size_sem = asyncio.Semaphore(size_limit)

        async def _probe(
            size_session: aiohttp.ClientSession,
            candidate: Dict[str, Any],
            detail: VideoDetail,
            csite: str,
        ):
            if csite == "xhamster" or is_hls_url(detail.download_url or ""):
                return candidate, detail, None, False, True
            async with size_sem:
                if ctx.cancel_event.is_set():
                    return candidate, detail, None, True, False
                site_cookie = self._ctx_for_site(ctx, csite).cookie_string()
                try:
                    size = await until_stopped(
                        probe_download_size(
                            size_session,
                            detail.download_url or "",
                            referer_url=detail.referer_url or detail.page_url,
                            cookie_str=site_cookie,
                            proxy_url=settings.normalized_proxy,
                            timeout=settings.request_timeout,
                            site=csite,
                        ),
                        ctx.cancel_event,
                    )
                except StopRequested:
                    return candidate, detail, None, True, False
                return candidate, detail, size, False, False

        chunk_size = max(20, min(60, int(settings.max_concurrent_video_details) * 2))
        detail_failures = 0
        size_failures = 0
        completed_count = 0

        timeout = aiohttp.ClientTimeout(total=25.0, connect=10.0, sock_read=20.0)
        connector, _ = create_aiohttp_connector(
            settings.normalized_proxy,
            limit=size_limit,
            limit_per_host=size_limit,
            keepalive_timeout=20.0,
        )
        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector, cookie_jar=aiohttp.DummyCookieJar(),
        ) as size_session:
            for start in range(0, len(candidates), chunk_size):
                if ctx.cancel_event.is_set():
                    break
                batch = candidates[start : start + chunk_size]
                by_site_batch: Dict[str, List[Tuple[str, int, int]]] = defaultdict(list)
                for idx, candidate in enumerate(batch):
                    csite = _candidate_site(candidate)
                    by_site_batch[csite].append((candidate["video_page_url"], 0, start + idx + 1))
                details: List[VideoDetail] = []
                for csite, c_items in by_site_batch.items():
                    details.extend(
                        await fetch_video_details_batch(
                            self._ctx_for_site(ctx, csite),
                            c_items,
                            settings.selectors,
                            concurrency=settings.max_concurrent_video_details,
                            skip_dom=True,
                            xh_preferred_codec=settings.xh_preferred_codec,
                            timeout=settings.request_timeout,
                        )
                    )
                if ctx.cancel_event.is_set():
                    break
                detail_by_viewkey = {
                    detail.viewkey: detail for detail in details if detail.viewkey
                }

                ready: List[Tuple[Dict[str, Any], VideoDetail, str]] = []
                for candidate in batch:
                    csite = _candidate_site(candidate)
                    detail = detail_by_viewkey.get(candidate["viewkey"])
                    if detail is not None and detail.error == "操作已取消":
                        continue
                    if detail is None or not detail.download_url:
                        detail_failures += 1
                        error = detail.error if detail else "详情页未返回结果"
                        await self.repo.update_job_item(
                            item_ids[candidate["viewkey"]],
                            status="failed",
                            error_message=error or "没有可用媒体地址",
                        )
                        continue
                    ready.append((candidate, detail, csite))

                if ctx.cancel_event.is_set():
                    break

                if ready:
                    probe_results = await asyncio.gather(
                        *[
                            _probe(size_session, candidate, detail, csite)
                            for candidate, detail, csite in ready
                        ]
                    )
                    for candidate, detail, file_size, cancelled, is_hls in probe_results:
                        if cancelled or ctx.cancel_event.is_set():
                            continue
                        quality_val = (detail.quality or "").strip()
                        if is_hls:
                            item_ok = bool(quality_val)
                            err_msg = "" if item_ok else "无法解析视频画质"
                            if item_ok:
                                completed_count += 1
                            else:
                                detail_failures += 1
                        else:
                            item_ok = bool(file_size)
                            err_msg = "" if item_ok else "画质已保存，但无法确认媒体文件大小"
                            if item_ok:
                                completed_count += 1
                            else:
                                size_failures += 1
                        await self.repo.update_video_metadata(
                            candidate["viewkey"],
                            quality=quality_val,
                            file_size=int(file_size or 0),
                            published_at=detail.published_at or "",
                            duration_seconds=int(detail.duration_seconds or 0),
                        )
                        await self.repo.update_job_item(
                            item_ids[candidate["viewkey"]],
                            status="done" if item_ok else "failed",
                            quality=quality_val,
                            bytes_total=int(file_size or 0),
                            error_message=err_msg,
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
                f"元数据补全已取消：已完成 {completed_count} 条，其余条目已批量取消。",
                job_id=job_id,
            )
            return

        self.events.emit_log(
            "INFO",
            f"元数据补全完成：成功 {completed_count} 条，"
            f"详情失败 {detail_failures} 条，大小获取失败 {size_failures} 条。",
            job_id=job_id,
        )

    def _existing_temp_size(self, temp_path_str: str) -> int:
        if not temp_path_str:
            return 0
        try:
            p = Path(temp_path_str)
            if p.is_symlink() or not is_within_directory(p, self.env.temp_dir):
                return 0
            if p.is_file():
                return p.stat().st_size
            if p.is_dir():
                total = 0
                for child in p.iterdir():
                    if not child.is_symlink() and child.is_file() and child.name not in {"manifest.json", "manifest.json.tmp"} and not child.name.endswith(".ytdl"):
                        with contextlib.suppress(OSError):
                            total += child.stat().st_size
                return total
        except OSError:
            pass
        return 0

    def _existing_local_file(
        self, local_path: str, min_size: int, expected_size: Optional[int],
    ) -> Optional[Tuple[Path, int]]:
        if local_path:
            path = Path(local_path)
            with contextlib.suppress(OSError, RuntimeError):
                if not path.is_symlink() and is_within_directory(path, self.env.downloads_dir) and is_valid_video_file(
                    path, min_size, expected_size,
                ):
                    return path, path.stat().st_size
        return None

    def _retry_checkpoint(self, item: Dict[str, Any]) -> Tuple[str, int]:
        temp_path = item.get("temp_path") or ""
        if temp_path:
            part = Path(temp_path)
            with contextlib.suppress(OSError, RuntimeError):
                if part.name.endswith((".mp4.tmp", ".hls")):
                    size = self._existing_temp_size(temp_path)
                    if size > 0:
                        return temp_path, size
        if item["viewkey"]:
            site = _item_site(item)
            folder = _target_folder_name(item.get("uploader"), item.get("target_type") or "model", site=site)
            parent = self.env.temp_dir / "xHamster" / folder if site == "xhamster" else self.env.temp_dir / folder
            base_name = video_filename_base(item.get("title") or "", item["viewkey"])
            suffixes = (".hls", ".mp4.tmp") if is_hls_url(item.get("download_url")) else (".mp4.tmp", ".hls")
            for suffix in suffixes:
                candidate = str(parent / f"{base_name}{suffix}")
                size = self._existing_temp_size(candidate)
                if size > 0:
                    return candidate, size
        return "", 0

    def _resume_item_resets(self, items: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        resets = {}
        for item in items:
            temp_path, tmp_size = self._retry_checkpoint(item)
            resets[int(item["id"])] = {"temp_path": temp_path, "bytes_downloaded": tmp_size}
        return resets

    async def _prepare_resume_items(
        self, rows: List[Dict[str, Any]], params: Dict[str, Any], settings: RuntimeSettings,
    ) -> Tuple[List[int], Dict[int, _PendingParseSpec], Dict[int, Optional[Set[str]]]]:
        pending = [item for item in rows if item["status"] == "pending"]
        if not pending:
            return [], {}, {}
        await asyncio.to_thread(pending.sort, key=lambda it: not self._existing_temp_size(it["temp_path"]))
        quality_option = params.get("quality")
        qualities = _quality_set(settings.preferred_quality if quality_option is None else quality_option)
        target_qual_by_pair: Dict[Tuple[str, str, str], Optional[Set[str]]] = {}
        target_qual_by_user: Dict[Tuple[str, str], Optional[Set[str]]] = {}
        if quality_option is None:
            for t in await self.repo.list_targets():
                q_set = _quality_set(t.get("preferred_quality"))
                if not q_set:
                    continue
                s_low = normalize_site(t.get("site"))
                u_low = str(t.get("username") or "").strip().lower()
                t_low = str(t.get("target_type") or "model").strip().lower()
                if t_low == "liked":
                    target_qual_by_pair[(s_low, "my_liked", "liked")] = q_set
                    target_qual_by_pair[(s_low, "_currentuser_", "liked")] = q_set
                elif s_low == "xhamster" and t_low == "favorites":
                    target_qual_by_pair[(s_low, "my_favorites", "favorites")] = q_set
                    target_qual_by_pair[(s_low, "_currentuser_", "favorites")] = q_set
                else:
                    target_qual_by_pair[(s_low, u_low, t_low)] = q_set
                    if t_low not in {"favorites", "liked"} and (s_low, u_low) not in target_qual_by_user:
                        target_qual_by_user[(s_low, u_low)] = q_set

        def _item_default_qualities(item: Dict[str, Any], item_site: str) -> Optional[Set[str]]:
            if quality_option is not None:
                return qualities
            u_low = str(item.get("uploader") or "").strip().lower()
            t_low = str(item.get("target_type") or "model").strip().lower()
            if (item_site, u_low, t_low) in target_qual_by_pair:
                return target_qual_by_pair[(item_site, u_low, t_low)]
            if t_low not in {"favorites", "liked"} and (item_site, u_low) in target_qual_by_user:
                return target_qual_by_user[(item_site, u_low)]
            return qualities


        item_ids = []
        parse_specs: Dict[int, _PendingParseSpec] = {}
        item_qualities: Dict[int, Optional[Set[str]]] = {}
        from_urls = bool(params.get("urls"))
        for idx, item in enumerate(pending, start=1):
            iid = int(item["id"])
            item_ids.append(iid)
            site = _item_site(item)
            previous_quality = normalized_quality(item.get("quality") or "")
            item_qualities[iid] = _item_default_qualities(item, site) or (
                {previous_quality} if previous_quality and quality_option is None else None
            )
            if item.get("download_url") or not item.get("page_url"):
                continue
            if item.get("local_path") and await asyncio.to_thread(
                self._existing_local_file, item["local_path"], settings.min_file_size_bytes,
                item.get("bytes_total") or None,
            ):
                continue
            uploader = item.get("uploader") or ""
            target_type = item.get("target_type") or ("creator" if site == "xhamster" else "model")
            parse_specs[iid] = _PendingParseSpec(
                item_id=iid,
                page_url=item["page_url"],
                page_num=0,
                item_num=idx,
                default_uploader=uploader,
                default_type=target_type,
                group_by_uploader=from_urls and bool(params.get("group_by_uploader", True)),
                uploader_hint=(uploader, target_type) if uploader and not from_urls else None,
                site=site,
            )
        return item_ids, parse_specs, item_qualities

    async def _exec_resume_job_items(
        self,
        job_id: int,
        params: Dict[str, Any],
        settings: RuntimeSettings,
        ctx: TaskHttpContext,
    ) -> None:
        item_ids, parse_specs, item_qualities = await self._prepare_resume_items(
            await self.repo.list_job_items(job_id, status="pending"), params, settings,
        )
        self.events.emit_log(
            "INFO",
            f"已入队 {len(item_ids)} 条重试/续传项"
            + (f"（其中 {len(parse_specs)} 条按需流式解析）" if parse_specs else "")
            + "，开始下载与上传...",
            job_id=job_id,
        )
        await self._run_download_upload_pipeline(
            job_id,
            item_ids,
            settings,
            ctx,
            auto_upload=_effective_auto_upload(params, settings),
            redownload=bool(params.get("redownload")),
            item_preferred_qualities=item_qualities,
            parse_specs=parse_specs or None,
            include_keywords=_split_csv_set(params.get("keywords"), lower=True),
            exclude_keywords=_split_csv_set(params.get("exclude_keywords"), lower=True),
            on_detail_parsed=self._url_creator_linkage_handler(job_id, params, settings, ctx) if params.get("urls") else None,
        )

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
            item for item in source_items
            if item["status"] in {"failed", "cancelled", "pending", "downloaded", "uploading"}
        ]
        if not retryable:
            self.events.emit_log("INFO", f"源任务 #{source_job_id} 没有可重试/续传的条目。", job_id=job_id)
            return

        checkpoints = await asyncio.to_thread(self._resume_item_resets, retryable)
        retryable.sort(key=lambda item: not checkpoints[int(item["id"])]["bytes_downloaded"])
        auto_upload = _effective_auto_upload(params, settings)
        seen: Set[str] = set()
        item_specs: List[Dict[str, Any]] = []
        for old in retryable:
            key = _item_key(old["viewkey"], old["page_url"])
            if key in seen:
                continue
            seen.add(key)
            opt_in = old.get("upload_requested")
            wanted_upload = auto_upload and (bool(opt_in) if opt_in is not None else params.get("auto_upload") is True)
            cancelled = ctx.cancel_event.is_set()
            item_specs.append({
                **old,
                "site": _item_site(old),
                "referer_url": old["referer_url"] or old["page_url"],
                "bytes_downloaded": 0,
                **checkpoints[int(old["id"])],
                "upload_requested": wanted_upload,
                "status": "cancelled" if cancelled else "pending",
                "error_message": "已取消" if cancelled else "",
            })

        for row in await self.repo.add_job_items_batch(job_id, item_specs):
            self.events.broadcast("item_updated", row)
        updated_job = await self.repo.recalculate_job_counts(job_id)
        if updated_job:
            self.events.broadcast("job_updated", updated_job)
        del source_items, retryable, checkpoints, item_specs
        await self._exec_resume_job_items(job_id, params, settings, ctx)

    async def _exec_resume_temp(
        self,
        job_id: int,
        params: Dict[str, Any],
        settings: RuntimeSettings,
        ctx: TaskHttpContext,
    ) -> None:
        rescan = params.get("resume_mode") == "rescan"
        historical_items = await self.repo.list_job_items(job_id) if rescan else []
        handled = {item["viewkey"] for item in historical_items}
        item_ids, parse_specs, preferred_by_item = await self._prepare_resume_items(historical_items, params, settings)
        del historical_items
        temp_files = await asyncio.to_thread(self.list_temp_files)
        if not temp_files and not item_ids:
            self.events.emit_log("INFO", "没有找到可续传的临时文件。", job_id=job_id)
            return

        auto_upload = _effective_auto_upload(params, settings) and (rescan or settings.uploader_enabled)
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
            tf_site = normalize_site(prior.get("site") or tf.get("site") or "pornhub")
            folder = tf["folder"]
            if tf_site == "xhamster":
                utype = "favorites" if folder.lower() == "my_favorites" else "creator"
                uploader = "My_Favorites" if utype == "favorites" else folder
            else:
                utype = "favorites" if folder.lower().endswith("_favorites") else ("liked" if folder.lower() == "my_liked_videos" else "model")
                uploader = folder[:-len("_favorites")] if utype == "favorites" else folder
            title = prior.get("title") or tf["name"].rsplit("[", 1)[0].strip() or vkey
            default_page_url = (
                f"https://xhamster.com/videos/{vkey}"
                if tf_site == "xhamster"
                else f"https://www.pornhub.com/view_video.php?viewkey={vkey}"
            )
            page_url = prior.get("page_url") or default_page_url
            quality = tf["quality"] or prior.get("quality") or ""
            row = await self.repo.add_job_item(
                job_id,
                site=tf_site,
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
            item_ids.append(int(row["id"]))
            wanted_quality = normalized_quality(quality)
            preferred_by_item[int(row["id"])] = {wanted_quality} if wanted_quality else _quality_set(settings.preferred_quality)
            self.events.broadcast("item_updated", row)

        updated_job = await self.repo.recalculate_job_counts(job_id)
        if updated_job:
            self.events.broadcast("job_updated", updated_job)
        if item_ids:
            await self._run_download_upload_pipeline(
                job_id,
                item_ids,
                settings,
                ctx,
                auto_upload=auto_upload,
                redownload=False,
                item_preferred_qualities=preferred_by_item,
                parse_specs=parse_specs or None,
            )

    # ------------------------------------------------------------------
    # Disk / Residual temp file inspection
    # ------------------------------------------------------------------
    def _infer_temp_site(self, path: Path) -> str:
        try:
            rel_parts = path.resolve().relative_to(self.env.temp_dir.resolve()).parts
            if rel_parts and rel_parts[0].lower() == "xhamster":
                return "xhamster"
        except (OSError, ValueError):
            pass
        return "pornhub"

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
                        "site": self._infer_temp_site(tmp),
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
        for hls_dir in sorted(self.env.temp_dir.rglob("*.hls")):
            try:
                if hls_dir.is_symlink() or not hls_dir.is_dir():
                    continue
                total_size = self._existing_temp_size(str(hls_dir))
                if total_size <= 0:
                    continue
                vkey = ""
                quality = ""
                verified = False
                manifest = hls_dir / "manifest.json"
                if manifest.is_file():
                    try:
                        meta = json.loads(manifest.read_text(encoding="utf-8"))
                        if isinstance(meta, dict):
                            vkey = str(meta.get("viewkey") or "").strip()
                            raw_q = str(meta.get("quality") or "").strip()
                            quality = f"{raw_q}p" if raw_q.isdigit() else raw_q
                            verified = bool(vkey)
                    except (OSError, ValueError, TypeError):
                        pass
                if not vkey and "[" in hls_dir.stem and "]" in hls_dir.stem:
                    vkey = hls_dir.stem.rsplit("[", 1)[1].split("]", 1)[0]
                results.append(
                    {
                        "site": self._infer_temp_site(hls_dir),
                        "name": hls_dir.name,
                        "folder": hls_dir.parent.name,
                        "viewkey": vkey,
                        "size_bytes": total_size,
                        "path": str(hls_dir),
                        "quality": quality,
                        "verified_checkpoint": verified,
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
        for hls_dir in list(self.env.temp_dir.rglob("*.hls")):
            if not hls_dir.is_symlink() and hls_dir.is_dir():
                with contextlib.suppress(OSError):
                    shutil.rmtree(hls_dir)
                    removed += 1
        return removed

    async def clear_idle_temp_files(self) -> Optional[int]:
        async with self._queue_lock:
            if self._current_job_id is not None:
                return None
            cleanup = asyncio.create_task(asyncio.to_thread(self.clear_temp_files))
            try:
                return await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await asyncio.gather(cleanup, return_exceptions=True)
                raise
