"""Video history & dedup ledger endpoints (fast server-side pagination, no thumbnails)."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from .deps import AppContainer, require_auth
from ..services.playback import NO_CACHE

router = APIRouter(prefix="/api/videos", tags=["videos"])


def _safe_unlink_downloaded_video(downloads_dir: Path, raw_path: str) -> bool:
    cleaned = (raw_path or "").strip()
    if not cleaned:
        return False
    try:
        candidate = Path(cleaned)
        if candidate.is_symlink():
            return False
        resolved = candidate.resolve(strict=True)
        root = downloads_dir.resolve()
        if resolved == root or not resolved.is_relative_to(root) or not resolved.is_file():
            return False
        resolved.unlink()
        parent = resolved.parent
        if parent != root and parent.is_relative_to(root):
            with contextlib.suppress(OSError):
                parent.rmdir()
        return True
    except (OSError, ValueError):
        return False


@router.get("")
async def list_videos(
    search: str = Query(default=""),
    uploader: str = Query(default=""),
    status: str = Query(default=""),
    quality: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    include_meta: bool = Query(default=True),
    container: AppContainer = Depends(require_auth),
):
    items, total = await container.repo.query_videos(
        search=search,
        uploader=uploader,
        status=status,
        quality=quality,
        page=page,
        page_size=page_size,
    )
    facets = await container.repo.get_video_filter_facets() if include_meta else None
    stats = await container.repo.get_video_stats() if include_meta else None
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "facets": facets,
        "stats": stats,
    }


@router.post("/metadata/enrich")
async def enrich_baseline_metadata(container: AppContainer = Depends(require_auth)):
    count = await container.repo.count_baseline_metadata_candidates()
    if count == 0:
        raise HTTPException(status_code=400, detail="没有需要补全元数据的基线视频")
    job = await container.tasks.enqueue_job(
        kind="enrich_metadata",
        label=f"补全基线元数据 ({count} 条)",
        params={},
    )
    return {"job": job, "count": count}


class BatchDeleteVideosRequest(BaseModel):
    viewkeys: List[str] = Field(default_factory=list)
    delete_local_files: bool = False


@router.post("/batch-delete")
async def batch_delete_video_records(
    payload: BatchDeleteVideosRequest,
    container: AppContainer = Depends(require_auth),
):
    if not payload.viewkeys:
        raise HTTPException(status_code=400, detail="请至少选择一条视频记录")
    records = await container.repo.get_videos_by_viewkeys(payload.viewkeys) if payload.delete_local_files else []
    deleted = await container.repo.delete_videos(payload.viewkeys)
    deleted_files = 0
    if payload.delete_local_files and deleted:
        deleted_set = set(deleted)
        for rec in records:
            if rec.get("viewkey") in deleted_set and _safe_unlink_downloaded_video(
                container.env.downloads_dir, str(rec.get("local_path") or "")
            ):
                deleted_files += 1
    if deleted:
        container.events.broadcast("overview_changed", {})
    return {
        "deleted_count": len(deleted),
        "deleted_viewkeys": deleted,
        "deleted_files_count": deleted_files,
    }


@router.get("/{viewkey}/playback")
async def resolve_video_playback(
    viewkey: str, response: Response,
    container: AppContainer = Depends(require_auth),
):
    record = await container.repo.get_video(viewkey)
    if not record:
        raise HTTPException(status_code=404, detail="视频记录不存在")
    response.headers.update(NO_CACHE)
    return await container.playback.resolve(record, await container.repo.get_settings())


@router.get("/{viewkey}/playback/{ticket}/{index}")
async def stream_video_playback(
    viewkey: str, ticket: str, index: int, request: Request, container: AppContainer = Depends(require_auth),
):
    return await container.playback.stream(viewkey, ticket, index, request.headers.get("Range"))


@router.delete("/{viewkey}")
async def delete_video_record(
    viewkey: str,
    delete_local_file: bool = Query(default=False),
    container: AppContainer = Depends(require_auth),
):
    vk = viewkey.strip()
    record = await container.repo.get_video(vk) if delete_local_file else None
    ok = await container.repo.delete_video(vk)
    if not ok:
        raise HTTPException(status_code=404, detail="视频记录不存在")
    removed_file = False
    if delete_local_file and record:
        removed_file = _safe_unlink_downloaded_video(
            container.env.downloads_dir, str(record.get("local_path") or "")
        )
    container.events.broadcast("overview_changed", {})
    return {"deleted": True, "deleted_local_file": removed_file}


@router.post("/{viewkey}/redownload")
async def redownload_video(viewkey: str, container: AppContainer = Depends(require_auth)):
    record = await container.repo.get_video(viewkey.strip())
    if not record:
        raise HTTPException(status_code=404, detail="视频记录不存在")

    settings = await container.repo.get_settings()
    page_url = record.get("video_page_url") or f"https://www.pornhub.com/view_video.php?viewkey={viewkey.strip()}"
    job = await container.tasks.enqueue_job(
        kind="manual_urls",
        label=f"重新下载: {record.get('title') or viewkey}",
        params={
            "urls": [page_url],
            "group_by_uploader": True,
            "redownload": True,
            "auto_upload": settings.uploader_enabled,
        },
    )
    return {"job": job}
