"""Runtime settings, Cookie management, rclone check, and ServerChan test endpoints."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..engine.scheduler import parse_hhmm
from ..services.captcha import CaptchaError, get_yescaptcha_balance
from ..services.cookie import check_cookie_valid, login_and_get_cookies
from ..services.notifier import send_serverchan
from ..services.uploader import test_rclone_connection
from .deps import AppContainer, require_auth

router = APIRouter(prefix="/api/settings", tags=["settings"])


class CookieImportRequest(BaseModel):
    cookie: str


@router.get("")
async def get_settings(container: AppContainer = Depends(require_auth)):
    settings = await container.repo.get_settings()
    return {
        "settings": settings.to_redacted_dict(),
        "cookie_status": container.tasks.cookie_status,
    }


@router.put("")
async def update_settings(
    patch: Dict[str, Any],
    container: AppContainer = Depends(require_auth),
):
    if "schedule_time" in patch and patch["schedule_time"]:
        if parse_hhmm(str(patch["schedule_time"])) is None:
            raise HTTPException(status_code=400, detail="定时追更时间格式无效，应为 HH:MM（如 04:00）")

    try:
        updated = await container.repo.update_settings_patch(patch)
    except Exception as err:
        raise HTTPException(status_code=400, detail=f"配置校验失败: {err}") from err

    container.events.broadcast("overview_changed", {})
    return {"settings": updated.to_redacted_dict()}


@router.post("/cookie/test")
async def test_cookie(container: AppContainer = Depends(require_auth)):
    settings = await container.repo.get_settings()
    res = await check_cookie_valid(
        settings.ph_cookie,
        proxy_url=settings.normalized_proxy,
        timeout=settings.request_timeout,
    )
    container.tasks.set_cookie_status(res.valid, res.message)
    return {"valid": res.valid, "message": res.message}


@router.post("/cookie/import")
async def import_cookie(
    payload: CookieImportRequest,
    container: AppContainer = Depends(require_auth),
):
    raw = (payload.cookie or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Cookie 内容不能为空")

    settings = await container.repo.update_settings_patch({"ph_cookie": raw})
    res = await check_cookie_valid(
        settings.ph_cookie,
        proxy_url=settings.normalized_proxy,
        timeout=settings.request_timeout,
    )
    container.tasks.set_cookie_status(res.valid, res.message)
    return {
        "saved": True,
        "valid": res.valid,
        "message": res.message,
    }


@router.post("/cookie/refresh")
async def refresh_cookie(container: AppContainer = Depends(require_auth)):
    settings = await container.repo.get_settings()
    res = await login_and_get_cookies(
        settings.ph_username,
        settings.ph_password,
        proxy_url=settings.normalized_proxy,
        timeout=settings.request_timeout,
        yescaptcha_enabled=settings.yescaptcha_enabled,
        yescaptcha_client_key=settings.yescaptcha_client_key,
        yescaptcha_api_base=settings.yescaptcha_api_base,
        yescaptcha_task_type=settings.yescaptcha_task_type,
        yescaptcha_timeout_seconds=settings.yescaptcha_timeout_seconds,
        on_log=lambda level, message: container.events.emit_log(level, message),
    )
    if not res.success:
        container.tasks.set_cookie_status(False, res.message)
        return {"success": False, "message": res.message}

    patch: Dict[str, Any] = {"ph_cookie": res.cookie_str}
    if res.logged_in_username:
        patch["ph_logged_in_username"] = res.logged_in_username
    await container.repo.update_settings_patch(patch)
    container.tasks.set_cookie_status(True, res.message)
    return {
        "success": True,
        "logged_in_username": res.logged_in_username,
        "message": res.message,
    }


@router.post("/yescaptcha/test")
async def test_yescaptcha(container: AppContainer = Depends(require_auth)):
    settings = await container.repo.get_settings()
    if not (settings.yescaptcha_client_key or "").strip():
        return {"ok": False, "message": "未配置 YesCaptcha ClientKey"}
    try:
        balance = await get_yescaptcha_balance(
            settings.yescaptcha_client_key,
            api_base=settings.yescaptcha_api_base,
        )
    except CaptchaError as err:
        return {"ok": False, "message": str(err)}
    except Exception as err:  # pragma: no cover - defensive
        return {"ok": False, "message": f"YesCaptcha 连接异常: {err}"}
    return {
        "ok": True,
        "balance": balance,
        "message": f"YesCaptcha 连接成功，账户余额 {balance} 点",
    }


@router.post("/rclone/test")
async def test_rclone(container: AppContainer = Depends(require_auth)):
    settings = await container.repo.get_settings()
    ok, msg = await test_rclone_connection(settings)
    return {"ok": ok, "message": msg}


@router.post("/notify/test")
async def test_notify(container: AppContainer = Depends(require_auth)):
    settings = await container.repo.get_settings()
    ok, msg = await send_serverchan(
        "PH-DL 通知测试",
        "这是一条来自 PH-DL Web 控制面板的测试通知。",
        settings,
        force=True,
    )
    return {"ok": ok, "message": msg}


@router.get("/logs")
async def get_recent_logs(container: AppContainer = Depends(require_auth)):
    return {"items": container.events.recent_logs(300)}


@router.delete("/logs")
async def clear_logs(container: AppContainer = Depends(require_auth)):
    container.events.clear_logs()
    return {"cleared": True}
