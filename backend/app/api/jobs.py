"""Job creation, queue inspection, cancellation, retry, and temp-file resume endpoints."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..parsers.list_scanner import parse_page_specs, parse_page_video_specs
from ..parsers.video import collect_video_urls
from .deps import AppContainer, require_auth

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class CreateManualDownloadRequest(BaseModel):
    source_mode: str = Field(default="target", pattern="^(target|urls)$")
    usernames: List[str] = Field(default_factory=list)
    target_type: str = "auto"  # auto / model / pornstar / favorites / liked
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


@router.get("")
async def list_jobs(
    limit: int = Query(default=30, ge=1, le=200),
    container: AppContainer = Depends(require_auth),
):
    jobs = await container.repo.list_jobs(limit=limit)
    return {"items": jobs}


@router.get("/temp-files")
async def get_temp_files(container: AppContainer = Depends(require_auth)):
    return {"items": container.tasks.list_temp_files()}


@router.post("/temp-files/resume")
async def resume_temp_files(container: AppContainer = Depends(require_auth)):
    temp_files = container.tasks.list_temp_files()
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
    if container.tasks.current_job_id is not None:
        raise HTTPException(status_code=409, detail="有任务正在运行，请先停止后再清理临时文件")
    removed = container.tasks.clear_temp_files()
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
        utype = (payload.target_type or "auto").strip().lower()
        if utype == "user":
            utype = "users"
        if utype not in {"auto", "model", "pornstar", "users", "favorites", "liked"}:
            raise HTTPException(status_code=400, detail="不支持的目标类型")
        try:
            parse_page_specs(payload.pages)
            parse_page_video_specs(payload.videos)
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        if payload.pages.strip() and payload.videos.strip():
            raise HTTPException(status_code=400, detail="页码范围和指定视频不能同时使用")
        clean_users = [u.strip() for u in payload.usernames if u and u.strip()]
        if utype != "liked" and not clean_users:
            if utype in {"favorites", "model", "pornstar", "users"} and settings.ph_logged_in_username:
                clean_users = [settings.ph_logged_in_username]
            else:
                raise HTTPException(status_code=400, detail="请至少输入一个用户名")

        if utype == "liked":
            label_target = "Liked Videos"
        else:
            label_target = ", ".join(clean_users[:3]) + (f" 等 {len(clean_users)} 人" if len(clean_users) > 3 else "")
        prefix = "导出页面链接" if payload.export_only else "下载目标"
        job = await container.tasks.enqueue_job(
            kind="manual_target",
            label=f"{prefix}: {label_target}",
            params={
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
    job = await container.repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    items = await container.repo.list_job_items(job_id)
    live = container.events.live_progress_snapshot()
    for it in items:
        prog = live.get(int(it["id"]))
        if prog:
            it["bytes_downloaded"] = prog["bytes_downloaded"]
            it["bytes_total"] = prog["bytes_total"]
            it["speed_bps"] = prog["speed_bps"]
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


async def _resolve_origin_job_info(container: AppContainer, source_job: dict) -> tuple[int, str]:
    """Walk retry lineage (up to 5 hops) to recover the original descriptive job id and label."""
    cur_job = source_job
    visited = {int(source_job["id"])}
    for _ in range(5):
        params = cur_job.get("params") or {}
        if params.get("origin_label"):
            origin_id = int(params.get("origin_job_id") or cur_job["id"])
            return origin_id, str(params["origin_label"]).strip()
        parent_id = params.get("source_job_id")
        if not parent_id:
            break
        try:
            pid = int(parent_id)
        except (TypeError, ValueError):
            break
        if pid in visited:
            break
        visited.add(pid)
        parent = await container.repo.get_job(pid)
        if not parent:
            break
        cur_job = parent
    return int(cur_job["id"]), str(cur_job.get("label") or cur_job.get("kind") or "").strip()


@router.post("/{job_id}/retry-failed")
async def retry_failed_items(job_id: int, container: AppContainer = Depends(require_auth)):
    source_job = await container.repo.get_job(job_id)
    if not source_job:
        raise HTTPException(status_code=404, detail="源任务不存在")
    if source_job.get("status") in {"queued", "scanning", "running"}:
        raise HTTPException(status_code=409, detail="运行或排队中的任务无法重试，请先停止")
    if source_job.get("kind") == "enrich_metadata":
        count = await container.repo.count_baseline_metadata_candidates()
        if count == 0:
            raise HTTPException(status_code=400, detail="没有仍需补全元数据的基线视频")
        job = await container.tasks.enqueue_job(
            kind="enrich_metadata",
            label=f"重试补全基线元数据 ({count} 条)",
            params={},
        )
        return {"job": job}
    items = await container.repo.list_job_items(job_id)
    retryable = [
        it
        for it in items
        if it["status"] in {"failed", "cancelled", "pending", "downloaded", "uploading"}
    ]
    src_params = source_job.get("params") or {}
    origin_job_id, origin_label = await _resolve_origin_job_info(container, source_job)
    kind = source_job["kind"]
    has_scan_gap = "扫描未完成" in (source_job.get("error_message") or "")
    can_rescan = kind in {"manual_target", "manual_urls", "auto_update", "check_updates",
                          "baseline_sync", "retry_items", "resume_temp"}
    if (
        can_rescan and source_job["status"] in {"cancelled", "failed", "partial_failed"}
        and (not retryable or src_params.get("export_only") or has_scan_gap or kind == "baseline_sync")
    ):
        replay_params = {
            **src_params, "source_job_id": job_id, "origin_job_id": origin_job_id, "origin_label": origin_label,
        }
        if kind == "manual_target" and src_params.get("redownload"):
            # Retain an explicit forced download for failed/missing items, not ones
            # this source task already completed before its scan/transfer stopped.
            completed = set(src_params.get("skip_completed_viewkeys") or ())
            completed.update(
                it["viewkey"] for it in items
                if it["status"] in {"done", "skipped"}
                or (it["status"] == "downloaded" and it.get("upload_requested") == 0)
            )
            replay_params["skip_completed_viewkeys"] = sorted(completed)
        if kind == "retry_items":
            # This job's underlying source is required by the retry executor.
            replay_params["source_job_id"] = src_params.get("source_job_id")
        job = await container.tasks.enqueue_job(kind, f"重试 · {origin_label}", replay_params)
        return {"job": job}
    if not retryable:
        raise HTTPException(status_code=400, detail="该任务没有失败或未完成的条目")

    has_failed = any(it["status"] == "failed" for it in retryable)
    has_incomplete = any(
        it["status"] in {"cancelled", "pending", "downloaded", "uploading"}
        for it in retryable
    )
    if has_failed and not has_incomplete:
        action_prefix = "重试"
    elif has_incomplete and not has_failed:
        action_prefix = "续传"
    else:
        action_prefix = "重试/续传"

    if origin_label:
        label = f"{action_prefix} · {origin_label} ({len(retryable)} 项)"
    else:
        label = f"{action_prefix}任务 #{job_id} ({len(retryable)} 项)"

    job = await container.tasks.enqueue_job(
        kind="retry_items",
        label=label,
        params={
            "source_job_id": job_id,
            "source_job_label": str(source_job.get("label") or ""),
            "origin_job_id": origin_job_id,
            "origin_label": origin_label,
            "auto_upload": src_params.get("auto_upload"),
            "quality": src_params.get("quality", ""),
            "redownload": bool(src_params.get("redownload", False)),
            "keywords": src_params.get("keywords", ""),
            "exclude_keywords": src_params.get("exclude_keywords", ""),
        },
    )
    return {"job": job}
