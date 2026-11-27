"""Job creation, queue inspection, cancellation, retry, and temp-file resume endpoints."""

from __future__ import annotations

import asyncio
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..db.repo import normalize_site
from ..parsers.list_scanner import parse_page_specs, parse_page_video_specs
from ..parsers.video import collect_video_urls
from .deps import AppContainer, require_auth

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
_INVALID_USERNAME_RE = re.compile(r"[/\\:?#\s]")


class CreateManualDownloadRequest(BaseModel):
    source_mode: str = Field(default="target", pattern="^(target|urls)$")
    site: str = "pornhub"
    usernames: List[str] = Field(default_factory=list)
    target_type: str = "auto"  # PH: auto/model/pornstar/favorites/liked; XH: auto/creator/pornstar/users/channel/favorites
    urls: List[str] = Field(default_factory=list)
    group_by_uploader: bool = True
    batch_name: str = "url_batch"
    quality: Optional[str] = None  # omitted inherits settings; empty selects the highest available
    pages: str = ""
    videos: str = ""
    keywords: str = ""
    exclude_keywords: str = ""
    redownload: bool = False
    auto_upload: Optional[bool] = None
    export_only: bool = False
    add_to_auto_track: bool = False
    subscribe_creator: bool = False


@router.get("")
async def list_jobs(
    limit: int = Query(default=30, ge=1, le=200),
    container: AppContainer = Depends(require_auth),
):
    jobs = await container.repo.list_jobs(limit=limit)
    return {"items": jobs}


@router.get("/temp-files")
async def get_temp_files(container: AppContainer = Depends(require_auth)):
    return {"items": await asyncio.to_thread(container.tasks.list_temp_files)}


@router.post("/temp-files/resume")
async def resume_temp_files(container: AppContainer = Depends(require_auth)):
    temp_files = await asyncio.to_thread(container.tasks.list_temp_files)
    if not temp_files:
        raise HTTPException(status_code=400, detail="当前没有可续传的临时文件")
    settings = await container.repo.get_settings()
    folders = list(dict.fromkeys(tf["folder"] for tf in temp_files if tf.get("folder")))
    if folders:
        folder_text = ", ".join(folders[:2]) + (f" 等 {len(folders)} 个目录" if len(folders) > 2 else "")
        label = f"仅续传临时文件: {folder_text} ({len(temp_files)} 个)"
    else:
        label = f"仅续传临时文件 ({len(temp_files)} 个)"
    job = await container.tasks.enqueue_job(
        kind="resume_temp",
        label=label,
        params={"auto_upload": settings.uploader_enabled},
    )
    return {"job": job}


@router.delete("/temp-files")
async def clear_temp_files(container: AppContainer = Depends(require_auth)):
    removed = await container.tasks.clear_idle_temp_files()
    if removed is None:
        raise HTTPException(status_code=409, detail="有任务正在运行，请先停止后再清理临时文件")
    container.events.broadcast("overview_changed", {})
    return {"removed": removed}


@router.post("")
async def create_download_job(
    payload: CreateManualDownloadRequest,
    container: AppContainer = Depends(require_auth),
):
    settings = await container.repo.get_settings()
    auto_upload = settings.uploader_enabled if payload.auto_upload is None else bool(payload.auto_upload)

    if payload.source_mode == "target":
        norm_site = normalize_site(payload.site)
        utype = (payload.target_type or "auto").strip().lower()
        if norm_site == "xhamster" and utype == "user":
            utype = "users"
        valid_types = (
            {"auto", "creator", "pornstar", "users", "channel", "favorites"}
            if norm_site == "xhamster"
            else {"auto", "model", "pornstar", "favorites", "liked"}
        )
        if utype not in valid_types:
            raise HTTPException(status_code=400, detail="不支持的目标类型")
        try:
            parse_page_specs(payload.pages)
            parse_page_video_specs(payload.videos)
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        if payload.pages.strip() and payload.videos.strip():
            raise HTTPException(status_code=400, detail="页码范围和指定视频不能同时使用")
        is_self_collection = utype == "liked" or (norm_site == "xhamster" and utype == "favorites")
        clean_users = [u.strip() for u in payload.usernames if u and u.strip()]
        if not is_self_collection:
            for u in clean_users:
                if _INVALID_USERNAME_RE.search(u):
                    raise HTTPException(
                        status_code=400,
                        detail=f"用户名格式无效 '{u}'：请输入博主用户名，不要粘贴视频或频道链接",
                    )
        if not is_self_collection and not clean_users:
            if utype == "favorites" and settings.ph_logged_in_username:
                clean_users = [settings.ph_logged_in_username]
            else:
                raise HTTPException(status_code=400, detail="请至少输入一个用户名")

        if utype == "liked":
            label_target = "Liked Videos"
        elif norm_site == "xhamster" and utype == "favorites":
            label_target = "xHamster Favorites"
        else:
            label_target = ", ".join(clean_users[:3]) + (f" 等 {len(clean_users)} 人" if len(clean_users) > 3 else "")
        site_prefix = "[xHamster] " if norm_site == "xhamster" else ""
        prefix = "导出页面链接" if payload.export_only else "下载目标"
        job = await container.tasks.enqueue_job(
            kind="manual_target",
            label=f"{site_prefix}{prefix}: {label_target}",
            params={
                "site": norm_site,
                "usernames": clean_users,
                "target_type": utype,
                "quality": payload.quality,
                "pages": payload.pages,
                "videos": payload.videos,
                "keywords": payload.keywords,
                "exclude_keywords": payload.exclude_keywords,
                "redownload": payload.redownload,
                "auto_upload": auto_upload,
                "export_only": payload.export_only,
                "add_to_auto_track": utype not in {"liked", "favorites"} and bool(payload.add_to_auto_track),
                "subscribe_creator": utype not in {"liked", "favorites"} and bool(payload.subscribe_creator),
            },
        )
        return {"job": job}

    # source_mode == "urls"
    video_urls, skipped_non_video = collect_video_urls(payload.urls)
    if not video_urls:
        detail = "未识别到支持的视频链接" if skipped_non_video else "请至少提供一条视频链接"
        raise HTTPException(status_code=400, detail=detail)

    prefix = "导出页面链接" if payload.export_only else "链接下载"
    job = await container.tasks.enqueue_job(
        kind="manual_urls",
        label=f"{prefix}: {len(video_urls)} 条链接",
        params={
            "urls": video_urls,
            "skipped_non_video": skipped_non_video,
            "group_by_uploader": payload.group_by_uploader,
            "batch_name": payload.batch_name,
            "quality": payload.quality,
            "keywords": payload.keywords,
            "exclude_keywords": payload.exclude_keywords,
            "redownload": payload.redownload,
            "auto_upload": auto_upload,
            "export_only": payload.export_only,
            "add_to_auto_track": bool(payload.add_to_auto_track),
            "subscribe_creator": bool(payload.subscribe_creator),
        },
    )
    return {"job": job}


class BatchDeleteJobsRequest(BaseModel):
    job_ids: List[int] = Field(default_factory=list)


@router.post("/batch-delete")
async def batch_delete_jobs(
    payload: BatchDeleteJobsRequest,
    container: AppContainer = Depends(require_auth),
):
    if not payload.job_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个任务")
    active_id = container.tasks.current_job_id
    requested = [jid for jid in payload.job_ids if jid != active_id]
    res = await container.repo.delete_jobs(requested)
    skipped = list(res["skipped_active_ids"])
    if active_id is not None and active_id in payload.job_ids and active_id not in skipped:
        skipped.append(active_id)
    if res["deleted_ids"]:
        container.events.broadcast("overview_changed", {})
    return {
        "deleted_count": len(res["deleted_ids"]),
        "deleted_ids": res["deleted_ids"],
        "skipped_active_ids": skipped,
    }


@router.get("/{job_id}")
async def get_job_detail(job_id: int, container: AppContainer = Depends(require_auth)):
    job, items = await container.repo.get_job_detail(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    live = container.events.live_progress_snapshot()
    for it in items:
        prog = live.get(int(it["id"]))
        if prog:
            phase = prog.get("phase", "downloading")
            it["progress_phase"] = phase
            it["speed_bps"] = prog["speed_bps"]
            if phase in {"uploading", "verifying"}:
                it["upload_bytes"] = prog["bytes_downloaded"]
                if prog["bytes_total"] > 0:
                    it["bytes_total"] = prog["bytes_total"]
            else:
                it["bytes_downloaded"] = prog["bytes_downloaded"]
                it["bytes_total"] = prog["bytes_total"]
        else:
            it["speed_bps"] = 0.0
    return {"job": job, "items": items}


@router.delete("/{job_id}")
async def delete_job(job_id: int, container: AppContainer = Depends(require_auth)):
    if container.tasks.current_job_id == job_id:
        raise HTTPException(status_code=409, detail="运行中的任务无法删除，请先停止")
    ok, reason = await container.repo.delete_job(job_id)
    if not ok:
        if reason == "active":
            raise HTTPException(status_code=409, detail="排队或运行中的任务无法删除，请先停止")
        raise HTTPException(status_code=404, detail="任务不存在")
    container.events.broadcast("overview_changed", {})
    return {"deleted": True, "id": job_id}


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int, container: AppContainer = Depends(require_auth)):
    ok = await container.tasks.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=400, detail="任务不存在或已结束")
    return {"cancelled": True}


@router.post("/{job_id}/retry-failed")
async def retry_failed_items(job_id: int, container: AppContainer = Depends(require_auth)):
    job, message, status_code = await container.tasks.resume_job_in_place(job_id)
    if job is None:
        raise HTTPException(status_code=status_code, detail=message)
    return {"job": job}
