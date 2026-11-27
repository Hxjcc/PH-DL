"""Runtime settings, Cookie management, rclone check, and ServerChan test endpoints."""

from __future__ import annotations

import base64
import binascii
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ValidationError

from ..config import RuntimeSettings
from ..engine.scheduler import parse_hhmm
from ..parsers.xhamster import check_xhamster_cookie_valid
from ..services.captcha import CaptchaError, get_yescaptcha_balance
from ..services.cookie import (
    check_cookie_valid,
    format_cookie_valid_message,
    login_and_get_cookies,
    login_xhamster_and_get_cookies,
    prefer_cased_username,
)
from ..services.notifier import send_serverchan
from ..services.uploader import list_rclone_remotes, test_rclone_remotes
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
        if not candidate.is_symlink() and candidate.is_file():
            return candidate, mime
    return None


def _remove_avatar_files(data_dir: Path, *, keep: Optional[Path] = None) -> None:
    for ext in _AVATAR_EXT_TO_MIME:
        candidate = data_dir / f"account_avatar{ext}"
        if candidate == keep:
            continue
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
    current = await container.repo.get_settings()
    next_schedule_enabled = (
        bool(patch["schedule_enabled"])
        if "schedule_enabled" in patch
        else current.schedule_enabled
    )
    next_schedule_time = (
        str(patch["schedule_time"] or "").strip()
        if "schedule_time" in patch
        else (current.schedule_time or "").strip()
    )
    if "schedule_time" in patch and next_schedule_time:
        if parse_hhmm(next_schedule_time) is None:
            raise HTTPException(status_code=400, detail="定时追更时间格式无效，应为 HH:MM（如 04:00）")
    if next_schedule_enabled and (not next_schedule_time or parse_hhmm(next_schedule_time) is None):
        raise HTTPException(status_code=400, detail="开启定时追更时必须设置有效时间（HH:MM，如 04:00）")

    try:
        current, updated = await container.repo.patch_settings(patch)
    except ValidationError as err:
        fields = [".".join(map(str, e["loc"])) or e["msg"] for e in err.errors(include_input=False, include_context=False)]
        raise HTTPException(status_code=400, detail=f"配置校验失败: {', '.join(fields)}") from err

    if updated.ph_cookie != current.ph_cookie or updated.proxy_url != current.proxy_url:
        container.tasks.reset_cookie_status()
    if updated.xh_cookie != current.xh_cookie or updated.proxy_url != current.proxy_url:
        container.tasks.reset_xh_cookie_status()

    if (
        updated.rclone_remote != current.rclone_remote
        or updated.rclone_backup_remotes != current.rclone_backup_remotes
        or updated.rclone_remote_strategy != current.rclone_remote_strategy
    ):
        container.tasks.clear_remote_cooldowns()

    container.events.broadcast("overview_changed", {})
    return {
        "settings": updated.to_redacted_dict(),
        "cookie_status": container.tasks.cookie_status,
        "xh_cookie_status": container.tasks.xh_cookie_status,
    }


async def _check_saved_cookie(
    container: AppContainer, prefix: str, settings: RuntimeSettings, *, imported: bool = False,
) -> Dict[str, Any]:
    options = {"proxy_url": settings.normalized_proxy, "timeout": settings.request_timeout}
    if prefix == "xh":
        valid, message, username = await check_xhamster_cookie_valid(settings.xh_cookie, **options)
        site_label, set_status = "xHamster", container.tasks.set_xh_cookie_status
    else:
        res = await check_cookie_valid(settings.ph_cookie, **options)
        valid, message, username = res.valid, res.message, res.logged_in_username
        site_label, set_status = "PornHub", container.tasks.set_cookie_status
    resolved_user = prefer_cased_username(username, getattr(settings, f"{prefix}_logged_in_username")) if valid else ""
    if await container.repo.save_cookie_check(prefix, settings, resolved_user) is None:
        return {
            "valid": False, "message": "检测结果已被更新的 Cookie 或代理设置替代，请重新检测。",
            "logged_in_username": "", "superseded": True,
        }
    status_msg = container.repo.redact_diagnostic(format_cookie_valid_message(resolved_user) if valid else message)
    set_status(valid, status_msg)
    container.events.emit_log(
        "INFO" if valid else "WARNING",
        f"[{site_label} Cookie] {'已导入并测活' if imported else '有效性检测'}: {status_msg}",
    )
    return {"valid": valid, "message": status_msg, "logged_in_username": resolved_user}


@router.post("/cookie/test")
async def test_cookie(container: AppContainer = Depends(require_auth)):
    return await _check_saved_cookie(container, "ph", await container.repo.get_settings())


@router.post("/cookie/import")
async def import_cookie(
    payload: CookieImportRequest,
    container: AppContainer = Depends(require_auth),
):
    raw = (payload.cookie or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Cookie 内容不能为空")

    current, settings = await container.repo.patch_settings({"ph_cookie": raw})
    if current.ph_cookie != settings.ph_cookie:
        container.tasks.reset_cookie_status()
    return {"saved": True, **await _check_saved_cookie(container, "ph", settings, imported=True)}


async def _cookie_refresh_failure(
    container: AppContainer, prefix: str, original: RuntimeSettings, message: str,
) -> Dict[str, Any]:
    current = await container.repo.save_cookie_check(prefix, original)
    if current is None or any(
        getattr(current, f"{prefix}_{field}") != getattr(original, f"{prefix}_{field}")
        for field in ("username", "password")
    ):
        return {"success": False, "message": "登录期间连接配置已变更，请重试"}
    set_status = container.tasks.set_xh_cookie_status if prefix == "xh" else container.tasks.set_cookie_status
    set_status(False, message)
    return {"success": False, "message": container.repo.redact_diagnostic(message)}


async def _perform_ph_cookie_refresh(container: AppContainer) -> Dict[str, Any]:
    settings = await container.repo.get_settings()
    try:
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
    except Exception as err:  # pragma: no cover - defensive
        return await _cookie_refresh_failure(container, "ph", settings, f"自动登录异常 ({type(err).__name__})")

    if not res.success:
        return await _cookie_refresh_failure(container, "ph", settings, res.message)

    resolved_user = prefer_cased_username(res.logged_in_username, settings.ph_logged_in_username)
    if await container.repo.save_refreshed_cookie("ph", settings, res.cookie_str, resolved_user) is None:
        return {"success": False, "message": "登录期间连接配置已变更，请重试"}
    container.tasks.set_cookie_status(True, format_cookie_valid_message(resolved_user))
    container.events.broadcast("overview_changed", {})
    return {
        "success": True,
        "logged_in_username": resolved_user,
        "message": container.repo.redact_diagnostic(res.message),
    }


@router.post("/cookie/refresh")
async def refresh_cookie(
    wait: bool = False,
    container: AppContainer = Depends(require_auth),
):
    """Start a PornHub login in the background.

    A reCAPTCHA solve can take several minutes, so the request returns
    immediately and the UI polls ``/cookie/refresh/status``.  Pass ``wait=1``
    to keep the legacy synchronous behaviour.
    """

    return await container.logins.start(
        "pornhub",
        lambda: _perform_ph_cookie_refresh(container),
        wait=wait,
    )


@router.get("/cookie/refresh/status")
async def cookie_refresh_status(container: AppContainer = Depends(require_auth)):
    return container.logins.snapshot("pornhub")


@router.post("/xh-cookie/test")
async def test_xh_cookie(container: AppContainer = Depends(require_auth)):
    return await _check_saved_cookie(container, "xh", await container.repo.get_settings())


@router.post("/xh-cookie/import")
async def import_xh_cookie(
    payload: CookieImportRequest,
    container: AppContainer = Depends(require_auth),
):
    raw = (payload.cookie or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="xHamster Cookie 内容不能为空")

    current, settings = await container.repo.patch_settings({"xh_cookie": raw})
    if current.xh_cookie != settings.xh_cookie:
        container.tasks.reset_xh_cookie_status()
    return {"saved": True, **await _check_saved_cookie(container, "xh", settings, imported=True)}


async def _perform_xh_cookie_refresh(container: AppContainer) -> Dict[str, Any]:
    settings = await container.repo.get_settings()
    try:
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
    except Exception as err:  # pragma: no cover - defensive
        return await _cookie_refresh_failure(container, "xh", settings, f"xHamster 自动登录异常 ({type(err).__name__})")

    if not res.success:
        return await _cookie_refresh_failure(container, "xh", settings, res.message)

    resolved_user = prefer_cased_username(res.logged_in_username, settings.xh_logged_in_username)
    if await container.repo.save_refreshed_cookie("xh", settings, res.cookie_str, resolved_user) is None:
        return {"success": False, "message": "登录期间连接配置已变更，请重试"}
    container.tasks.set_xh_cookie_status(True, format_cookie_valid_message(resolved_user))
    container.events.broadcast("overview_changed", {})
    return {
        "success": True,
        "logged_in_username": resolved_user,
        "message": container.repo.redact_diagnostic(res.message),
    }


@router.post("/xh-cookie/refresh")
async def refresh_xh_cookie(
    wait: bool = False,
    container: AppContainer = Depends(require_auth),
):
    """Start an xHamster login in the background (see ``cookie/refresh``)."""

    return await container.logins.start(
        "xhamster",
        lambda: _perform_xh_cookie_refresh(container),
        wait=wait,
    )


@router.get("/xh-cookie/refresh/status")
async def xh_cookie_refresh_status(container: AppContainer = Depends(require_auth)):
    return container.logins.snapshot("xhamster")


@router.post("/yescaptcha/test")
async def test_yescaptcha(container: AppContainer = Depends(require_auth)):
    settings = await container.repo.get_settings()
    if not (settings.yescaptcha_client_key or "").strip():
        return {"ok": False, "message": "未配置 YesCaptcha ClientKey"}
    try:
        balance = await get_yescaptcha_balance(settings.yescaptcha_client_key)
    except CaptchaError as err:
        return {"ok": False, "message": settings.redact_diagnostic(str(err))}
    except Exception as err:  # pragma: no cover - defensive
        return {"ok": False, "message": f"YesCaptcha 连接异常 ({type(err).__name__})"}
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
    ok, msg, results = await test_rclone_remotes(settings)
    healthy = [str(r["remote"]) for r in results if r.get("ok") and r.get("remote")]
    if healthy:
        container.tasks.clear_remote_cooldowns(healthy)
    for result in results:
        if "message" in result:
            result["message"] = settings.redact_diagnostic(result["message"])
    return {"ok": ok, "message": settings.redact_diagnostic(msg), "results": results}


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
        with path.open("rb") as handle:
            content = handle.read(MAX_AVATAR_BYTES + 1)
    except OSError as err:
        raise HTTPException(status_code=404, detail="头像文件读取失败") from err
    if len(content) > MAX_AVATAR_BYTES or _detect_avatar_image(content) is None:
        raise HTTPException(status_code=404, detail="头像文件无效")
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
    if len(b64_part) > 4 * ((MAX_AVATAR_BYTES + 2) // 3):
        raise HTTPException(status_code=400, detail="头像图片过大（请控制在 2 MB 以内）")
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
    target_path = data_dir / f"account_avatar{ext}"
    draft = None
    try:
        with tempfile.NamedTemporaryFile(dir=data_dir, prefix=".avatar-", delete=False) as handle:
            draft = Path(handle.name)
            handle.write(decoded)
        os.replace(draft, target_path)
        _remove_avatar_files(data_dir, keep=target_path)
    finally:
        if draft is not None:
            draft.unlink(missing_ok=True)

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
