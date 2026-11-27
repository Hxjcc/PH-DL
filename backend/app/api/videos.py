"""Video history & dedup ledger endpoints (fast server-side pagination, no thumbnails)."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from .deps import AppContainer, require_auth
from ..db.repo import normalize_site
from ..parsers.video import safe_video_page_url
from ..services.downloader import _await_file_io
from ..services.playback import NO_CACHE
from ..services.uploader import FileSnapshot, delete_verified_source, file_snapshot

router = APIRouter(prefix="/api/videos", tags=["videos"])


def _capture_local_videos(downloads_dir: Path, records: list[dict]) -> dict[str, tuple[Path, FileSnapshot]]:
    root = downloads_dir.resolve()
    candidates = {}
    for record in records:
        if not record["local_path"]:
            continue
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            path = Path(record["local_path"])
            if path.is_symlink():
                continue
            path = path.resolve(strict=True)
            if path != root and path.is_relative_to(root) and (snapshot := file_snapshot(path)):
                candidates[record["viewkey"]] = (path, snapshot)
    return candidates


async def _remove_deleted_local_videos(
    container: AppContainer, candidates: dict[str, tuple[Path, FileSnapshot]], deleted: list[str],
) -> int:
    keys = candidates.keys() & set(deleted)
    if not keys:
        return 0

    def unlink_files(allowed: set[str]) -> int:
        root = container.env.downloads_dir.resolve()
        removed = 0
        for key in allowed:
            path, snapshot = candidates[key]
            with contextlib.suppress(OSError, RuntimeError, ValueError):
                if path.resolve().is_relative_to(root) and delete_verified_source(path, snapshot):
                    removed += 1
                    if path.parent != root:
                        path.parent.rmdir()
        return removed

    # A downloader records its active item before publishing the file. Keep SQLite's
    # writer lock until unlink so neither that marker nor a new ledger row can race us.
    async with container.repo.db.transaction(touches_video_meta=False) as conn:
        blocked = await container.repo.video_cleanup_blocked(keys, conn=conn)
        return await _await_file_io(asyncio.create_task(asyncio.to_thread(unlink_files, keys - blocked)))


async def _delete_records_and_local_files(
    container: AppContainer, viewkeys: list[str],
) -> tuple[list[str], int]:
    repo = container.repo
    async with repo.db.transaction() as conn:
        records = await repo.get_videos_by_viewkeys(viewkeys, conn=conn)
        blocked = await repo.video_cleanup_blocked(
            (record["viewkey"] for record in records), conn=conn, include_ledger=False,
        )
        candidates = await asyncio.to_thread(
            _capture_local_videos, container.env.downloads_dir,
            [record for record in records if record["viewkey"] not in blocked],
        )
        deleted = await repo.delete_videos(viewkeys, conn=conn)
    return deleted, await _remove_deleted_local_videos(container, candidates, deleted)


@router.get("")
async def list_videos(
    site: str = Query(default=""),
    search: str = Query(default=""),
    uploader: str = Query(default=""),
    status: str = Query(default=""),
    quality: str = Query(default=""),
    time_range: str = Query(default=""),
    sort_by: str = Query(default="activity"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    include_meta: bool = Query(default=True),
    container: AppContainer = Depends(require_auth),
):
    repo = container.repo
    unfiltered = not any((search.strip(), uploader.strip(), status.strip(), quality.strip(), time_range.strip()))
    filters = dict(
        site=site,
        search=search,
        uploader=uploader,
        status=status,
        quality=quality,
        time_range=time_range,
        sort_by=sort_by,
        page=page,
        page_size=page_size,
    )
    if include_meta:
        async with repo.db.read() as conn:
            snapshot_version = repo.db.video_meta_version
            stats, stats_cached = await repo.get_video_stats_with_cache_info(
                site=site, conn=conn, snapshot_version=snapshot_version,
            )
            items, total = await repo.query_videos(
                **filters, known_total=stats["total"] if unfiltered else None, conn=conn,
            )
            if stats_cached and repo.db.video_meta_version != snapshot_version:
                stats = await repo.get_video_stats(site=site, conn=conn)
                if unfiltered:
                    total = stats["total"]
            facets = await repo.get_video_filter_facets(
                site=site, conn=conn, snapshot_version=snapshot_version,
            )
    else:
        items, total = await repo.query_videos(**filters)
        stats = facets = None
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "facets": facets,
        "stats": stats,
    }


class EnrichMetadataRequest(BaseModel):
    site: str = ""


@router.post("/metadata/enrich")
async def enrich_baseline_metadata(
    payload: EnrichMetadataRequest | None = None,
    site: str | None = Query(default=None),
    container: AppContainer = Depends(require_auth),
):
    raw_site = (site if site is not None else payload.site if payload else "").strip()
    site_filter = normalize_site(raw_site) if raw_site else ""
    count = await container.repo.count_baseline_metadata_candidates(site=site_filter)
    if count == 0:
        raise HTTPException(status_code=400, detail="没有需要补全元数据的基线视频")
    job = await container.tasks.enqueue_job(
        kind="enrich_metadata",
        label=f"补全基线元数据 ({count} 条)",
        params={"site": site_filter} if site_filter else {},
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
    if payload.delete_local_files:
        deleted, deleted_files = await _delete_records_and_local_files(container, payload.viewkeys)
    else:
        deleted, deleted_files = await container.repo.delete_videos(payload.viewkeys), 0
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


@router.get("/{viewkey}/playback/{ticket}/{index}/playlist.m3u8")
async def stream_video_playback_playlist(
    viewkey: str, ticket: str, index: int, container: AppContainer = Depends(require_auth),
):
    return await container.playback.playlist(viewkey, ticket, index)


@router.get("/{viewkey}/playback/{ticket}/{index}/seg/{seg_id}")
async def stream_video_playback_segment(
    viewkey: str, ticket: str, index: int, seg_id: int, request: Request,
    container: AppContainer = Depends(require_auth),
):
    return await container.playback.segment(viewkey, ticket, index, seg_id, request.headers.get("Range"))


@router.delete("/{viewkey}")
async def delete_video_record(
    viewkey: str,
    delete_local_file: bool = Query(default=False),
    container: AppContainer = Depends(require_auth),
):
    vk = viewkey.strip()
    if delete_local_file:
        deleted, removed_files = await _delete_records_and_local_files(container, [vk])
        ok = bool(deleted)
    else:
        ok, removed_files = await container.repo.delete_video(vk), 0
    if not ok:
        raise HTTPException(status_code=404, detail="视频记录不存在")
    container.events.broadcast("overview_changed", {})
    return {"deleted": True, "deleted_local_file": bool(removed_files)}


@router.post("/{viewkey}/redownload")
async def redownload_video(viewkey: str, container: AppContainer = Depends(require_auth)):
    record = await container.repo.get_video(viewkey.strip())
    if not record:
        raise HTTPException(status_code=404, detail="视频记录不存在")

    settings = await container.repo.get_settings()
    page_url = safe_video_page_url(record.get("video_page_url"), normalize_site(record.get("site")), viewkey.strip())
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
