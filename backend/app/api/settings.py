"""Runtime settings, Cookie management, rclone check, and ServerChan test endpoints."""

from __future__ import annotations

import base64
import binascii
import secrets
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from ..engine.scheduler import parse_hhmm
from ..parsers.xhamster import check_xhamster_cookie_valid
from ..services.captcha import CaptchaError, get_yescaptcha_balance
from ..services.cookie import check_cookie_valid, login_and_get_cookies, login_xhamster_and_get_cookies
from ..services.notifier import send_serverchan
from ..services.uploader import list_rclone_remotes, test_rclone_connection
from .deps import AppContainer, require_auth

router = APIRouter(prefix="/api/settings", tags=["settings"])


class CookieImportRequest(BaseModel):
    cookie: str


class AvatarUploadRequest(BaseModel):
    data_url: str


MAX_AVATAR_BYTES = 2 * 1024 * 1024
_AVATAR_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _detect_avatar_image(raw: bytes) -> Optional[Tuple[str, str]]:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    return None


def _find_avatar_file(data_dir: Path) -> Optional[Tuple[Path, str]]:
    for ext, mime in _AVATAR_EXT_TO_MIME.items():
        candidate = data_dir / f"account_avatar{ext}"
        if candidate.is_file():
            return candidate, mime
    return None


def _remove_avatar_files(data_dir: Path) -> None:
    for ext in _AVATAR_EXT_TO_MIME:
        candidate = data_dir / f"account_avatar{ext}"
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


def _avatar_url_for(version: str) -> str:
    return f"/api/settings/avatar?v={version}" if version else ""


@router.get("")
async def get_settings(container: AppContainer = Depends(require_auth)):
    settings = await container.repo.get_settings()
    return {
        "settings": settings.to_redacted_dict(),
        "cookie_status": container.tasks.cookie_status,
        "xh_cookie_status": container.tasks.xh_cookie_status,
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
        yescaptcha_task_type=settings.yescaptcha_ph_task_type,
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


@router.post("/xh-cookie/test")
async def test_xh_cookie(container: AppContainer = Depends(require_auth)):
    settings = await container.repo.get_settings()
    valid, message, username = await check_xhamster_cookie_valid(
        settings.xh_cookie,
        proxy_url=settings.normalized_proxy,
        timeout=settings.request_timeout,
    )
    if valid and username and username != settings.xh_logged_in_username:
        await container.repo.update_settings_patch({"xh_logged_in_username": username})
    container.tasks.set_xh_cookie_status(valid, message)
    return {"valid": valid, "message": message, "logged_in_username": username}


@router.post("/xh-cookie/import")
async def import_xh_cookie(
    payload: CookieImportRequest,
    container: AppContainer = Depends(require_auth),
):
    raw = (payload.cookie or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="xHamster Cookie 内容不能为空")

    settings = await container.repo.update_settings_patch({"xh_cookie": raw})
    valid, message, username = await check_xhamster_cookie_valid(
        settings.xh_cookie,
        proxy_url=settings.normalized_proxy,
        timeout=settings.request_timeout,
    )
    if valid and username:
        await container.repo.update_settings_patch({"xh_logged_in_username": username})
    container.tasks.set_xh_cookie_status(valid, message)
    return {
        "saved": True,
        "valid": valid,
        "message": message,
        "logged_in_username": username,
    }


@router.post("/xh-cookie/refresh")
async def refresh_xh_cookie(container: AppContainer = Depends(require_auth)):
    settings = await container.repo.get_settings()
    res = await login_xhamster_and_get_cookies(
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
    if not res.success:
        container.tasks.set_xh_cookie_status(False, res.message)
        return {"success": False, "message": res.message}

    patch: Dict[str, Any] = {"xh_cookie": res.cookie_str}
    if res.logged_in_username:
        patch["xh_logged_in_username"] = res.logged_in_username
    await container.repo.update_settings_patch(patch)
    container.tasks.set_xh_cookie_status(True, res.message)
    container.events.broadcast("overview_changed", {})
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
        balance = await get_yescaptcha_balance(settings.yescaptcha_client_key)
    except CaptchaError as err:
        return {"ok": False, "message": str(err)}
    except Exception as err:  # pragma: no cover - defensive
        return {"ok": False, "message": f"YesCaptcha 连接异常: {err}"}
    return {
        "ok": True,
        "balance": balance,
        "message": f"YesCaptcha 连接成功，账户余额 {balance} 点",
    }


@router.get("/rclone/remotes")
async def get_rclone_remotes(container: AppContainer = Depends(require_auth)):
    settings = await container.repo.get_settings()
    remotes = await list_rclone_remotes(settings)
    return {"remotes": remotes}


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


@router.get("/avatar")
async def get_account_avatar(container: AppContainer = Depends(require_auth)):
    found = _find_avatar_file(container.env.resolved_data_dir)
    if not found:
        raise HTTPException(status_code=404, detail="未设置自定义头像")
    path, mime = found
    try:
        content = path.read_bytes()
    except OSError as err:
        raise HTTPException(status_code=404, detail="头像文件读取失败") from err
    return Response(
        content=content,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.post("/avatar")
async def upload_account_avatar(
    payload: AvatarUploadRequest,
    container: AppContainer = Depends(require_auth),
):
    raw_str = (payload.data_url or "").strip()
    if not raw_str:
        raise HTTPException(status_code=400, detail="请选择要上传的图片")
    b64_part = raw_str.split(",", 1)[1] if raw_str.startswith("data:") and "," in raw_str else raw_str
    try:
        decoded = base64.b64decode(b64_part, validate=True)
    except (binascii.Error, ValueError) as err:
        raise HTTPException(status_code=400, detail="图片编码无效，请重新选择图片") from err

    if not decoded or len(decoded) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="头像图片过大（请控制在 2 MB 以内）")

    detected = _detect_avatar_image(decoded)
    if not detected:
        raise HTTPException(status_code=400, detail="仅支持 PNG、JPG、WebP 或 GIF 图片格式")

    ext, _ = detected
    data_dir = container.env.resolved_data_dir
    container.env.ensure_dirs()
    _remove_avatar_files(data_dir)
    target_path = data_dir / f"account_avatar{ext}"
    target_path.write_bytes(decoded)

    version = secrets.token_hex(6)
    updated = await container.repo.update_settings_patch({"account_avatar_version": version})
    container.events.broadcast("overview_changed", {})
    return {
        "avatar_url": _avatar_url_for(updated.account_avatar_version),
        "account_avatar_version": updated.account_avatar_version,
        "settings": updated.to_redacted_dict(),
    }


@router.delete("/avatar")
async def delete_account_avatar(container: AppContainer = Depends(require_auth)):
    _remove_avatar_files(container.env.resolved_data_dir)
    updated = await container.repo.update_settings_patch({"account_avatar_version": ""})
    container.events.broadcast("overview_changed", {})
    return {
        "avatar_url": "",
        "account_avatar_version": "",
        "settings": updated.to_redacted_dict(),
    }


@router.get("/logs")
async def get_recent_logs(container: AppContainer = Depends(require_auth)):
    return {"items": container.events.recent_logs(300)}


@router.delete("/logs")
async def clear_logs(container: AppContainer = Depends(require_auth)):
    container.events.clear_logs()
    return {"cleared": True}
