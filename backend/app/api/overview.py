"""System overview & subscriptions lookup endpoints."""

from __future__ import annotations

import asyncio
import shutil
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..db.repo import normalize_site
from ..parsers.http_session import TaskHttpContext
from ..parsers.target import fetch_subscriptions
from ..parsers.xhamster import fetch_xhamster_subscriptions_detailed
from ..services.cookie import format_cookie_valid_message, login_xhamster_and_get_cookies
from .deps import AppContainer, require_auth

router = APIRouter(prefix="/api", tags=["overview"])


@router.get("/overview")
async def get_overview(container: AppContainer = Depends(require_auth)):
    settings = await container.repo.get_settings()
    video_stats = await container.repo.get_video_stats()
    target_count, enabled_targets = await container.repo.count_targets()
    temp_files = await asyncio.to_thread(container.tasks.list_temp_files)
    next_run = await container.scheduler.next_run_iso()

    disk_free_bytes = 0
    disk_total_bytes = 0
    try:
        usage = shutil.disk_usage(container.env.resolved_data_dir)
        disk_free_bytes = usage.free
        disk_total_bytes = usage.total
    except OSError:
        pass

    job_id = container.tasks.current_job_id
    current_job = await container.repo.get_job(job_id) if job_id is not None else None
    running = job_id is not None and job_id == container.tasks.current_job_id

    return {
        "running": running,
        "current_job": current_job if running else None,
        "cancelling_job_id": job_id if running and container.tasks.cancelling_job_id == job_id else None,
        "queued_count": container.tasks.queued_count,
        "account": {
            "username": (settings.ph_logged_in_username or settings.ph_username or "").strip(),
            "avatar_url": (
                f"/api/settings/avatar?v={settings.account_avatar_version}"
                if settings.account_avatar_version
                else ""
            ),
        },
        "cookie": {
            "configured": bool(settings.ph_cookie.strip()),
            "username": (settings.ph_logged_in_username or "").strip(),
            **container.tasks.cookie_status,
        },
        "xh_cookie": {
            "configured": bool(settings.xh_cookie.strip()),
            "username": (settings.xh_logged_in_username or "").strip(),
            **container.tasks.xh_cookie_status,
        },
        "schedule": {
            "enabled": settings.schedule_enabled,
            "time": settings.schedule_time,
            "scan_mode": settings.update_scan_mode,
            "next_run_at": next_run,
        },
        "uploader_enabled": settings.uploader_enabled,
        "event_loop": container.loop_monitor.snapshot(),
        "targets_total": target_count,
        "targets_enabled": enabled_targets,
        "videos": video_stats,
        "temp_files_count": len(temp_files),
        "disk": {
            "free_bytes": disk_free_bytes,
            "total_bytes": disk_total_bytes,
        },
    }


@router.get("/subscriptions")
async def get_subscriptions(
    username: Optional[str] = Query(default=None),
    site: str = Query(default="pornhub"),
    container: AppContainer = Depends(require_auth),
):
    settings = await container.repo.get_settings()
    norm_site = normalize_site(site)

    if norm_site == "xhamster":
        xh_cookie = settings.xh_cookie.strip()
        if not xh_cookie and settings.xh_username.strip() and settings.xh_password.strip():
            login_res = await login_xhamster_and_get_cookies(
                settings.xh_username,
                settings.xh_password,
                proxy_url=settings.normalized_proxy,
                timeout=settings.request_timeout,
                yescaptcha_enabled=settings.yescaptcha_enabled,
                yescaptcha_client_key=settings.yescaptcha_client_key,
                yescaptcha_task_type=settings.yescaptcha_xh_task_type,
                yescaptcha_timeout_seconds=settings.yescaptcha_timeout_seconds,
                on_log=lambda level, message: container.events.emit_log(level, message),
            )
            if login_res.success and login_res.cookie_str:
                refreshed = await container.repo.save_refreshed_cookie(
                    "xh", settings, login_res.cookie_str, login_res.logged_in_username,
                )
                if refreshed is None:
                    raise HTTPException(409, "登录期间连接配置已变更，请重试")
                settings = refreshed
                resolved_user = (login_res.logged_in_username or settings.xh_logged_in_username or "").strip()
                container.tasks.set_xh_cookie_status(True, format_cookie_valid_message(resolved_user))
                xh_cookie = settings.xh_cookie.strip()
            else:
                container.tasks.set_xh_cookie_status(False, login_res.message)
                raise HTTPException(status_code=400, detail=container.repo.redact_diagnostic(login_res.message))
        if not xh_cookie:
            raise HTTPException(
                status_code=400,
                detail="请先在【系统设置 → Cookie 与账号】中登录 xHamster 账号或粘贴有效 Cookie",
            )
        async with TaskHttpContext(
            site="xhamster",
            cookie_str=xh_cookie,
            proxy_url=settings.normalized_proxy,
            concurrency=2,
            timeout=settings.request_timeout,
            max_retries=settings.max_retries_page_fetch,
        ) as ctx:
            subs = await fetch_xhamster_subscriptions_detailed(ctx, timeout=settings.request_timeout)
        return {
            "site": "xhamster",
            "username": (settings.xh_logged_in_username or settings.xh_username).strip(),
            "subscriptions": [
                {
                    "site": "xhamster",
                    "type": item.target_type,
                    "username": item.username,
                    "display_name": item.display_name,
                    "avatar_url": item.avatar_url,
                    "bound_username": item.bound_username,
                    "bound_display_name": item.bound_display_name,
                }
                for item in subs
            ],
        }

    source_user = (username or "").strip() or settings.ph_logged_in_username.strip()
    if not source_user:
        raise HTTPException(
            status_code=400,
            detail="未指定源用户名，请填写用户名或在【系统设置】中配置已登录用户名",
        )
    if not settings.ph_cookie.strip():
        raise HTTPException(status_code=400, detail="请先在【系统设置】中配置有效 Cookie")

    async with TaskHttpContext(
        site="pornhub",
        cookie_str=settings.ph_cookie,
        proxy_url=settings.normalized_proxy,
        concurrency=2,
        timeout=settings.request_timeout,
        max_retries=settings.max_retries_page_fetch,
    ) as ctx:
        subs = await fetch_subscriptions(
            ctx,
            source_user,
            settings.selectors,
            timeout=settings.request_timeout,
        )

    return {
        "site": "pornhub",
        "username": source_user,
        "subscriptions": [
            {
                "type": t,
                "username": u,
                "display_name": display_name,
                "avatar_url": avatar_url,
            }
            for t, u, display_name, avatar_url in subs
        ],
    }
