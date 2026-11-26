"""Auto-update tracking targets CRUD and trigger endpoints."""

from __future__ import annotations

import re
import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..db.repo import normalize_site
from ..parsers.list_scanner import parse_page_specs
from ..parsers.http_session import TaskHttpContext
from ..parsers.target import resolve_target_type
from ..parsers.xhamster import resolve_xhamster_target_type
from .deps import AppContainer, require_auth

router = APIRouter(prefix="/api/targets", tags=["targets"])

VALID_PH_TYPES = {"auto", "model", "pornstar", "favorites", "liked"}
VALID_XH_TYPES = {"auto", "creator", "pornstar", "users", "channel", "favorites"}
_INVALID_USERNAME_RE = re.compile(r"[/\\:?#\s]")


def _validate_target_username(uname: str) -> None:
    if uname and _INVALID_USERNAME_RE.search(uname):
        raise HTTPException(
            status_code=400,
            detail=f"用户名格式无效 '{uname}'：请输入博主用户名，不要粘贴视频或频道链接",
        )


def _validate_baseline_pages(pages: str) -> None:
    try:
        parse_page_specs(pages)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


class TargetCreateRequest(BaseModel):
    site: str = "pornhub"
    username: str = ""
    target_type: str = "auto"
    display_name: str = ""
    enabled: bool = True
    preferred_quality: str = ""
    init_baseline: bool = False
    baseline_pages: str = ""


class TargetBatchItem(BaseModel):
    site: str = "pornhub"
    username: str
    target_type: str = "model"
    display_name: str = ""
    avatar_url: str = ""
    preferred_quality: str = ""


class TargetBatchCreateRequest(BaseModel):
    items: List[TargetBatchItem] = Field(default_factory=list)
    init_baseline: bool = False
    baseline_pages: str = "all"


class TargetUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    username: Optional[str] = None
    target_type: Optional[str] = None
    preferred_quality: Optional[str] = None
    display_name: Optional[str] = None


class RunUpdateRequest(BaseModel):
    target_ids: Optional[List[int]] = None
    auto_download: bool = True


@router.get("")
async def list_targets(container: AppContainer = Depends(require_auth)):
    items = await container.repo.list_targets()
    return {"items": items}


@router.post("")
async def create_target(
    payload: TargetCreateRequest,
    container: AppContainer = Depends(require_auth),
):
    if payload.init_baseline:
        _validate_baseline_pages(payload.baseline_pages)
    norm_site = normalize_site(payload.site)
    raw_type = (payload.target_type or "auto").strip().lower()
    valid_types = VALID_XH_TYPES if norm_site == "xhamster" else VALID_PH_TYPES
    if norm_site == "xhamster" and raw_type == "user":
        raw_type = "users"
    if raw_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"不支持的目标类型: {raw_type}")

    is_self_collection = raw_type == "liked" or (norm_site == "xhamster" and raw_type == "favorites")
    uname = "_currentUser_" if is_self_collection else payload.username.strip()
    if not is_self_collection:
        if not uname:
            raise HTTPException(status_code=400, detail="请输入用户名")
        _validate_target_username(uname)

    resolved_type = raw_type
    if raw_type == "auto":
        settings = await container.repo.get_settings()
        probe_timeout = min(float(settings.request_timeout), 10.0)
        async with TaskHttpContext(
            cookie_str=settings.xh_cookie if norm_site == "xhamster" else settings.ph_cookie,
            proxy_url=settings.normalized_proxy,
            concurrency=2,
            timeout=probe_timeout,
            max_retries=1,
        ) as ctx:
            if norm_site == "xhamster":
                detected = await resolve_xhamster_target_type(ctx, uname, timeout=probe_timeout)
                resolved_type = detected or "creator"
            else:
                detected = await resolve_target_type(ctx, uname, timeout=probe_timeout)
                if detected == "channel":
                    raise HTTPException(status_code=400, detail=f"目标 '{uname}' 是 Channel，暂不支持")
                resolved_type = detected or "model"

    target = await container.repo.upsert_target(
        username=uname,
        target_type=resolved_type,
        enabled=payload.enabled,
        preferred_quality=payload.preferred_quality,
        display_name=payload.display_name or ("xHamster Favorites" if norm_site == "xhamster" and resolved_type == "favorites" else ""),
        site=norm_site,
    )

    baseline_job = None
    if payload.init_baseline and target.get("id"):
        target_label = target.get("display_name") or target["username"]
        baseline_job = await container.tasks.enqueue_job(
            kind="baseline_sync",
            label=f"初始化基线: {target_label} ({target['target_type']})",
            params={"target_id": target["id"], "pages": payload.baseline_pages},
        )

    container.events.broadcast("overview_changed", {})
    return {"target": target, "baseline_job": baseline_job}


@router.post("/batch")
async def batch_create_targets(
    payload: TargetBatchCreateRequest,
    container: AppContainer = Depends(require_auth),
):
    if payload.init_baseline:
        _validate_baseline_pages(payload.baseline_pages)
    created = []
    jobs = []
    skipped_channels = []

    for item in payload.items:
        item_site = normalize_site(item.site)
        raw_t = (item.target_type or ("creator" if item_site == "xhamster" else "model")).strip().lower()
        u_clean = item.username.strip()
        is_self = raw_t == "liked" or (item_site == "xhamster" and raw_t == "favorites")
        if not is_self:
            _validate_target_username(u_clean)

    needs_ph_auto = any(
        normalize_site(item.site) == "pornhub"
        and (item.target_type or "").strip().lower() == "auto"
        and item.username.strip()
        for item in payload.items
    )
    needs_xh_auto = any(
        normalize_site(item.site) == "xhamster"
        and (item.target_type or "").strip().lower() == "auto"
        and item.username.strip()
        for item in payload.items
    )
    resolved_types: dict[tuple[str, str], str] = {}
    if needs_ph_auto or needs_xh_auto:
        settings = await container.repo.get_settings()
        probe_timeout = min(float(settings.request_timeout), 10.0)
        if needs_ph_auto:
            async with TaskHttpContext(
                cookie_str=settings.ph_cookie,
                proxy_url=settings.normalized_proxy,
                concurrency=3,
                timeout=probe_timeout,
                max_retries=1,
            ) as ctx:
                seen_auto: set[str] = set()
                for item in payload.items:
                    if normalize_site(item.site) != "pornhub" or (item.target_type or "").strip().lower() != "auto":
                        continue
                    uname = item.username.strip()
                    key = uname.lower()
                    if not uname or key in seen_auto:
                        continue
                    seen_auto.add(key)
                    detected = await resolve_target_type(ctx, uname, timeout=probe_timeout)
                    resolved_types[("pornhub", key)] = detected or "model"
        if needs_xh_auto:
            async with TaskHttpContext(
                cookie_str=settings.xh_cookie,
                proxy_url=settings.normalized_proxy,
                concurrency=3,
                timeout=probe_timeout,
                max_retries=1,
            ) as ctx:
                seen_auto_xh: set[str] = set()
                for item in payload.items:
                    if normalize_site(item.site) != "xhamster" or (item.target_type or "").strip().lower() != "auto":
                        continue
                    uname = item.username.strip()
                    key = uname.lower()
                    if not uname or key in seen_auto_xh:
                        continue
                    seen_auto_xh.add(key)
                    detected = await resolve_xhamster_target_type(ctx, uname, timeout=probe_timeout)
                    resolved_types[("xhamster", key)] = detected or "creator"

    for item in payload.items:
        item_site = normalize_site(item.site)
        raw_type = (item.target_type or ("creator" if item_site == "xhamster" else "model")).strip().lower()
        uname = item.username.strip()
        if raw_type == "auto":
            utype = resolved_types.get((item_site, uname.lower()), "creator" if item_site == "xhamster" else "model")
        else:
            utype = raw_type

        if item_site == "xhamster":
            if utype == "user":
                utype = "users"
            if utype not in {"creator", "pornstar", "users", "channel", "favorites"}:
                utype = "creator"
            if utype == "favorites":
                uname = "_currentUser_"
            elif not uname:
                continue
        else:
            if utype == "channel":
                if uname:
                    skipped_channels.append(uname)
                continue
            if utype in {"user", "users"}:
                continue
            if utype not in {"model", "pornstar", "favorites", "liked"}:
                utype = "model"
            if utype != "liked" and not uname:
                continue

        t = await container.repo.upsert_target(
            uname,
            utype,
            enabled=True,
            preferred_quality=item.preferred_quality,
            display_name=item.display_name,
            avatar_url=item.avatar_url,
            site=item_site,
        )
        created.append(t)
        if payload.init_baseline and t.get("id"):
            target_label = t.get("display_name") or t["username"]
            j = await container.tasks.enqueue_job(
                kind="baseline_sync",
                label=f"初始化基线: {target_label} ({t['target_type']})",
                params={"target_id": t["id"], "pages": payload.baseline_pages},
            )
            jobs.append(j)

    container.events.broadcast("overview_changed", {})
    return {
        "created_count": len(created),
        "targets": created,
        "baseline_jobs": jobs,
        "skipped_channels": skipped_channels,
    }


@router.patch("/{target_id}")
async def update_target(
    target_id: int,
    payload: TargetUpdateRequest,
    container: AppContainer = Depends(require_auth),
):
    existing = await container.repo.get_target(target_id)
    if not existing:
        raise HTTPException(status_code=404, detail="目标不存在")

    existing_site = normalize_site(existing.get("site"))
    existing_type = str(existing.get("target_type") or "").strip().lower()
    is_locked_collection = existing_type == "liked" or (existing_site == "xhamster" and existing_type == "favorites")

    patch = payload.model_dump(exclude_none=True)
    if is_locked_collection:
        if "username" in patch and patch["username"].strip() != str(existing.get("username") or ""):
            raise HTTPException(status_code=400, detail="内置收藏/点赞目标不支持修改标识")
        if "target_type" in patch and patch["target_type"].strip().lower() != existing_type:
            raise HTTPException(status_code=400, detail="内置收藏/点赞目标不支持修改类型")
        patch.pop("username", None)
        patch.pop("target_type", None)
    else:
        if "username" in patch:
            uname = patch["username"].strip()
            if not uname:
                raise HTTPException(status_code=400, detail="请输入用户名")
            _validate_target_username(uname)
            patch["username"] = uname
        if "target_type" in patch:
            t = patch["target_type"].strip().lower()
            if existing_site == "xhamster":
                if t == "user":
                    t = "users"
                allowed_types = {"creator", "pornstar", "users", "channel"}
            else:
                allowed_types = {"model", "pornstar", "favorites"}
            if t not in allowed_types:
                raise HTTPException(status_code=400, detail=f"不支持的目标类型: {t}")
            patch["target_type"] = t

    try:
        updated = await container.repo.update_target(target_id, patch)
    except sqlite3.IntegrityError as err:
        raise HTTPException(status_code=409, detail="已存在相同用户名与类型的监控目标") from err
    if not updated:
        raise HTTPException(status_code=404, detail="目标不存在")
    container.events.broadcast("overview_changed", {})
    return {"target": updated}


@router.delete("/{target_id}")
async def delete_target(target_id: int, container: AppContainer = Depends(require_auth)):
    ok = await container.repo.delete_target(target_id)
    if not ok:
        raise HTTPException(status_code=404, detail="目标不存在")
    container.events.broadcast("overview_changed", {})
    return {"deleted": True}


@router.post("/{target_id}/sync-baseline")
async def sync_baseline(
    target_id: int,
    pages: str = "",
    container: AppContainer = Depends(require_auth),
):
    _validate_baseline_pages(pages)
    target = await container.repo.get_target(target_id)
    if not target:
        raise HTTPException(status_code=404, detail="目标不存在")
    target_label = target.get("display_name") or target["username"]
    job = await container.tasks.enqueue_job(
        kind="baseline_sync",
        label=f"初始化基线: {target_label} ({target['target_type']})",
        params={"target_id": target_id, "pages": pages},
    )
    return {"job": job}


@router.post("/run-update")
async def run_update(
    payload: RunUpdateRequest,
    container: AppContainer = Depends(require_auth),
):
    kind = "auto_update" if payload.auto_download else "check_updates"
    if payload.target_ids:
        target_names = []
        for target_id in payload.target_ids:
            target = await container.repo.get_target(int(target_id))
            if target:
                target_names.append(target.get("display_name") or target["username"])
        if target_names:
            target_label = ", ".join(target_names[:2])
            if len(target_names) > 2:
                target_label += f" 等 {len(target_names)} 个"
        else:
            target_label = f"{len(payload.target_ids)} 个目标"
        label = (
            f"指定追更: {target_label}"
            if payload.auto_download
            else f"指定检查更新: {target_label}"
        )
    else:
        label = "全量自动追更" if payload.auto_download else "全量检查更新"

    job = await container.tasks.enqueue_job(
        kind=kind,
        label=label,
        params={"target_ids": payload.target_ids},
        trigger="manual",
    )
    return {"job": job}
