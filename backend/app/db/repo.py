"""Repository layer for Targets, Videos, Jobs, JobItems, and Settings."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..config import RuntimeSettings
from .database import Database

STATUS_RANK = {"baseline": 1, "downloaded": 2, "uploaded": 3}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_day_start_utc(now: Optional[datetime] = None) -> datetime:
    local_now = (now or datetime.now().astimezone()).astimezone()
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)


def _parse_iso_utc(raw: Optional[str]) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    async def get_settings(self) -> RuntimeSettings:
        row = await self.db.fetch_one("SELECT value_json FROM settings WHERE key = ?", ("runtime",))
        if not row:
            return RuntimeSettings()
        try:
            data = json.loads(row["value_json"])
            return RuntimeSettings.model_validate(data)
        except Exception:
            return RuntimeSettings()

    async def save_settings(self, settings: RuntimeSettings) -> RuntimeSettings:
        payload = json.dumps(settings.model_dump(), ensure_ascii=False)
        await self.db.execute(
            """
            INSERT INTO settings (key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            ("runtime", payload, utc_now_iso()),
        )
        return settings

    async def update_settings_patch(self, patch: Dict[str, Any]) -> RuntimeSettings:
        current = await self.get_settings()
        updated = current.merge_update(patch)
        return await self.save_settings(updated)

    async def get_scheduler_last_run_date(self) -> Optional[str]:
        row = await self.db.fetch_one("SELECT value_json FROM settings WHERE key = ?", ("scheduler_state",))
        if not row:
            return None
        try:
            data = json.loads(row["value_json"])
            val = data.get("last_run_date")
            return str(val) if val else None
        except Exception:
            return None

    async def set_scheduler_last_run_date(self, date_str: str) -> None:
        payload = json.dumps({"last_run_date": date_str}, ensure_ascii=False)
        await self.db.execute(
            """
            INSERT INTO settings (key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            ("scheduler_state", payload, utc_now_iso()),
        )

    async def get_auth_generation(self) -> int:
        # Use a fresh nonzero generation on upgrades so previously revoked
        # in-memory sessions cannot become valid during the first restart.
        await self.db.execute(
            "INSERT OR IGNORE INTO settings (key,value_json,updated_at) VALUES (?,?,?)",
            ("auth_generation", str(1 + secrets.randbelow(2 ** 60)), utc_now_iso()),
        )
        row = await self.db.fetch_one("SELECT value_json FROM settings WHERE key='auth_generation'")
        return int(row["value_json"])

    async def revoke_auth_sessions(self) -> int:
        await self.db.execute(
            "UPDATE settings SET value_json=CAST(value_json AS INTEGER)+1, updated_at=? WHERE key='auth_generation'",
            (utc_now_iso(),),
        )
        row = await self.db.fetch_one("SELECT value_json FROM settings WHERE key='auth_generation'")
        return int(row["value_json"])

    # ------------------------------------------------------------------
    # Targets (auto-update tracking list)
    # ------------------------------------------------------------------
    async def list_targets(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        where = "WHERE enabled = 1" if enabled_only else ""
        rows = await self.db.fetch_all(f"SELECT * FROM targets {where} ORDER BY LOWER(username) ASC, id ASC")
        if not rows:
            return rows

        today_start_dt = local_day_start_utc()
        today_start_iso = today_start_dt.isoformat(timespec="seconds")
        today_rows = await self.db.fetch_all(
            """
            SELECT LOWER(uploader) AS uploader_lower,
                   LOWER(target_type) AS target_type_lower,
                   COUNT(*) AS cnt
            FROM videos
            WHERE status IN ('downloaded', 'uploaded')
              AND COALESCE(downloaded_at, uploaded_at, created_at) >= ?
            GROUP BY LOWER(uploader), LOWER(target_type)
            """,
            (today_start_iso,),
        )
        counts_by_pair: Dict[Tuple[str, str], int] = {}
        non_special_counts_by_uploader: Dict[str, int] = {}
        liked_today_count = 0
        for item in today_rows:
            u_low = str(item.get("uploader_lower") or "").strip()
            t_low = str(item.get("target_type_lower") or "").strip()
            cnt = int(item.get("cnt") or 0)
            counts_by_pair[(u_low, t_low)] = cnt
            if t_low == "liked" or u_low == "my_liked":
                liked_today_count += cnt
            elif t_low != "favorites" and u_low:
                non_special_counts_by_uploader[u_low] = non_special_counts_by_uploader.get(u_low, 0) + cnt

        non_special_targets_per_user: Dict[str, int] = {}
        for r in rows:
            u_low = str(r.get("username") or "").strip().lower()
            t_low = str(r.get("target_type") or "model").strip().lower()
            if t_low not in ("liked", "favorites") and u_low:
                non_special_targets_per_user[u_low] = non_special_targets_per_user.get(u_low, 0) + 1

        for r in rows:
            r["enabled"] = bool(r["enabled"])
            u_low = str(r.get("username") or "").strip().lower()
            t_low = str(r.get("target_type") or "model").strip().lower()
            if t_low == "liked":
                downloaded_today = liked_today_count
            elif t_low == "favorites":
                downloaded_today = counts_by_pair.get((u_low, "favorites"), 0)
            elif non_special_targets_per_user.get(u_low, 0) == 1:
                downloaded_today = non_special_counts_by_uploader.get(u_low, 0)
            else:
                downloaded_today = counts_by_pair.get((u_low, t_low), 0)

            checked_dt = _parse_iso_utc(r.get("last_checked_at"))
            checked_today = bool(checked_dt and checked_dt >= today_start_dt)
            last_new_today = int(r.get("last_new_count") or 0) if checked_today else 0
            r["today_downloaded_count"] = downloaded_today
            r["today_new_count"] = max(downloaded_today, last_new_today)
        return rows

    async def get_target(self, target_id: int) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one("SELECT * FROM targets WHERE id = ?", (target_id,))
        if row:
            row["enabled"] = bool(row["enabled"])
        return row

    async def upsert_target(
        self,
        username: str,
        target_type: str = "model",
        enabled: bool = True,
        preferred_quality: str = "",
        avatar_url: str = "",
        display_name: str = "",
    ) -> Dict[str, Any]:
        norm_type = (target_type or "model").strip().lower()
        norm_user = "_currentUser_" if norm_type == "liked" else username.strip()
        existing = await self.db.fetch_one(
            "SELECT * FROM targets WHERE LOWER(username) = LOWER(?) AND target_type = ?",
            (norm_user, norm_type),
        )
        if existing:
            await self.db.execute(
                """
                UPDATE targets
                SET enabled = ?, preferred_quality = ?,
                    avatar_url = CASE WHEN ? != '' THEN ? ELSE avatar_url END,
                    display_name = CASE WHEN ? != '' THEN ? ELSE display_name END
                WHERE id = ?
                """,
                (
                    1 if enabled else 0,
                    preferred_quality.strip(),
                    avatar_url.strip(),
                    avatar_url.strip(),
                    display_name.strip(),
                    display_name.strip(),
                    existing["id"],
                ),
            )
            return (await self.get_target(existing["id"])) or existing

        new_id = await self.db.execute(
            """
            INSERT INTO targets (
                username, target_type, enabled, preferred_quality, avatar_url,
                display_name, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                norm_user,
                norm_type,
                1 if enabled else 0,
                preferred_quality.strip(),
                avatar_url.strip(),
                display_name.strip(),
                utc_now_iso(),
            ),
        )
        return (await self.get_target(new_id)) or {}

    async def update_target_profile(
        self,
        target_id: int,
        *,
        avatar_url: str = "",
        display_name: str = "",
    ) -> None:
        sets: List[str] = []
        params: List[Any] = []
        if (avatar_url or "").strip():
            sets.append("avatar_url = ?")
            params.append(avatar_url.strip())
        if (display_name or "").strip():
            sets.append("display_name = ?")
            params.append(display_name.strip())
        if not sets:
            return
        params.append(target_id)
        await self.db.execute(f"UPDATE targets SET {', '.join(sets)} WHERE id = ?", params)

    async def update_target_profile_by_username(
        self,
        username: str,
        target_type: str,
        *,
        avatar_url: str = "",
        display_name: str = "",
    ) -> None:
        sets: List[str] = []
        params: List[Any] = []
        if (avatar_url or "").strip():
            sets.append("avatar_url = ?")
            params.append(avatar_url.strip())
        if (display_name or "").strip():
            sets.append("display_name = ?")
            params.append(display_name.strip())
        if not sets:
            return
        params.extend([username.strip(), (target_type or "model").strip().lower()])
        await self.db.execute(
            f"""
            UPDATE targets SET {', '.join(sets)}
            WHERE LOWER(username) = LOWER(?) AND target_type = ?
            """,
            params,
        )

    async def update_target_avatar(self, target_id: int, avatar_url: str) -> None:
        await self.update_target_profile(target_id, avatar_url=avatar_url)

    async def update_target_avatar_by_username(
        self,
        username: str,
        target_type: str,
        avatar_url: str,
    ) -> None:
        await self.update_target_profile_by_username(
            username,
            target_type,
            avatar_url=avatar_url,
        )

    async def update_target(self, target_id: int, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        existing = await self.get_target(target_id)
        if not existing:
            return None
        allowed = {"enabled", "target_type", "preferred_quality", "username", "display_name"}
        sets = []
        params: List[Any] = []
        for k, v in patch.items():
            if k not in allowed:
                continue
            if k == "enabled":
                sets.append("enabled = ?")
                params.append(1 if v else 0)
            else:
                sets.append(f"{k} = ?")
                params.append(str(v).strip())
        if sets:
            params.append(target_id)
            await self.db.execute(f"UPDATE targets SET {', '.join(sets)} WHERE id = ?", params)
        return await self.get_target(target_id)

    async def mark_target_checked(self, target_id: int, new_count: int, error: str = "") -> None:
        await self.db.execute(
            """
            UPDATE targets
            SET last_checked_at = ?, last_new_count = ?, last_error = ?
            WHERE id = ?
            """,
            (utc_now_iso(), int(new_count), error, target_id),
        )

    async def delete_target(self, target_id: int) -> bool:
        existing = await self.get_target(target_id)
        if not existing:
            return False
        await self.db.execute("DELETE FROM targets WHERE id = ?", (target_id,))
        return True

    # ------------------------------------------------------------------
    # Videos (unified dedup & history ledger)
    # ------------------------------------------------------------------
    async def get_known_viewkeys(self, viewkeys: Optional[Iterable[str]] = None) -> Set[str]:
        if viewkeys is None:
            rows = await self.db.fetch_all("SELECT viewkey FROM videos")
            return {r["viewkey"] for r in rows}
        vlist = [v for v in set(viewkeys) if v]
        if not vlist:
            return set()
        placeholders = ",".join("?" for _ in vlist)
        rows = await self.db.fetch_all(f"SELECT viewkey FROM videos WHERE viewkey IN ({placeholders})", vlist)
        return {r["viewkey"] for r in rows}

    async def get_uploaded_viewkeys(self) -> Set[str]:
        rows = await self.db.fetch_all("SELECT viewkey FROM videos WHERE status = 'uploaded'")
        return {r["viewkey"] for r in rows}

    async def get_completed_viewkeys(self) -> Set[str]:
        rows = await self.db.fetch_all("SELECT viewkey FROM videos WHERE status IN ('downloaded', 'uploaded')")
        return {r["viewkey"] for r in rows}

    async def list_pending_upload_videos(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Downloaded videos whose local file may still need to be uploaded."""
        sql = """
            SELECT viewkey, title, uploader, target_type, quality, local_path,
                   file_size, video_page_url, downloaded_at
            FROM videos
            WHERE status = 'downloaded'
              AND upload_requested = 1
              AND local_path != ''
            ORDER BY COALESCE(downloaded_at, created_at) ASC, viewkey ASC
        """
        params: List[Any] = []
        if limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        return await self.db.fetch_all(sql, params)

    async def get_video(self, viewkey: str) -> Optional[Dict[str, Any]]:
        return await self.db.fetch_one("SELECT * FROM videos WHERE viewkey = ?", (viewkey,))

    async def get_videos_by_viewkeys(self, viewkeys: Iterable[str]) -> List[Dict[str, Any]]:
        unique_keys = [k for k in dict.fromkeys(str(v).strip() for v in viewkeys) if k]
        if not unique_keys:
            return []
        placeholders = ",".join("?" for _ in unique_keys)
        return await self.db.fetch_all(
            f"SELECT * FROM videos WHERE viewkey IN ({placeholders})",
            unique_keys,
        )

    async def clear_missing_pending_uploads(self, viewkeys: Iterable[str]) -> int:
        unique_keys = [k for k in dict.fromkeys(str(v).strip() for v in viewkeys) if k]
        if not unique_keys:
            return 0
        placeholders = ",".join("?" for _ in unique_keys)
        await self.db.execute(
            f"""
            UPDATE videos
            SET upload_requested = 0, local_path = ''
            WHERE status = 'downloaded' AND viewkey IN ({placeholders})
            """,
            unique_keys,
        )
        return len(unique_keys)

    async def count_baseline_metadata_candidates(self) -> int:
        row = await self.db.fetch_one(
            """
            SELECT COUNT(*) AS cnt
            FROM videos
            WHERE status = 'baseline'
              AND video_page_url != ''
              AND (quality = '' OR file_size <= 0)
            """
        )
        return int(row["cnt"]) if row else 0

    async def list_baseline_metadata_candidates(
        self,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        sql = """
            SELECT viewkey, title, uploader, target_type, quality, file_size, video_page_url
            FROM videos
            WHERE status = 'baseline'
              AND video_page_url != ''
              AND (quality = '' OR file_size <= 0)
            ORDER BY created_at ASC, viewkey ASC
        """
        params: List[Any] = []
        if limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        return await self.db.fetch_all(sql, params)

    async def update_video_metadata(
        self,
        viewkey: str,
        *,
        quality: str = "",
        file_size: int = 0,
    ) -> bool:
        sets: List[str] = []
        params: List[Any] = []
        if quality.strip():
            sets.append("quality = ?")
            params.append(quality.strip())
        if int(file_size or 0) > 0:
            sets.append("file_size = ?")
            params.append(int(file_size))
        if not sets:
            return False
        params.append(viewkey)
        await self.db.execute(
            f"UPDATE videos SET {', '.join(sets)} WHERE viewkey = ?",
            params,
        )
        return True

    _UPSERT_VIDEO_SQL = """
        INSERT INTO videos (
            viewkey, title, uploader, target_type, quality, video_page_url,
            status, local_path, remote_path, file_size, downloaded_at, uploaded_at, created_at,
            upload_requested
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(viewkey) DO UPDATE SET
            title = CASE WHEN excluded.title != '' THEN excluded.title ELSE videos.title END,
            uploader = CASE WHEN excluded.uploader != '' THEN excluded.uploader ELSE videos.uploader END,
            target_type = CASE WHEN ? != '' THEN ? ELSE videos.target_type END,
            quality = CASE WHEN excluded.quality != '' THEN excluded.quality ELSE videos.quality END,
            video_page_url = CASE WHEN ? != '' THEN ? ELSE videos.video_page_url END,
            status = CASE
                WHEN ? = 1 THEN excluded.status
                WHEN (CASE excluded.status WHEN 'uploaded' THEN 3 WHEN 'downloaded' THEN 2 WHEN 'baseline' THEN 1 ELSE 0 END)
                     >= (CASE videos.status WHEN 'uploaded' THEN 3 WHEN 'downloaded' THEN 2 WHEN 'baseline' THEN 1 ELSE 0 END)
                THEN excluded.status
                ELSE videos.status
            END,
            local_path = CASE
                WHEN excluded.local_path != '' THEN excluded.local_path
                WHEN (
                    CASE
                        WHEN ? = 1 THEN excluded.status
                        WHEN (CASE excluded.status WHEN 'uploaded' THEN 3 WHEN 'downloaded' THEN 2 WHEN 'baseline' THEN 1 ELSE 0 END)
                             >= (CASE videos.status WHEN 'uploaded' THEN 3 WHEN 'downloaded' THEN 2 WHEN 'baseline' THEN 1 ELSE 0 END)
                        THEN excluded.status
                        ELSE videos.status
                    END
                ) = 'uploaded' THEN ''
                ELSE videos.local_path
            END,
            remote_path = CASE
                WHEN excluded.remote_path != '' THEN excluded.remote_path
                WHEN ? = 1 AND excluded.status = 'downloaded' THEN ''
                ELSE videos.remote_path
            END,
            file_size = CASE WHEN excluded.file_size > 0 THEN excluded.file_size ELSE videos.file_size END,
            downloaded_at = COALESCE(excluded.downloaded_at, videos.downloaded_at),
            uploaded_at = CASE
                WHEN ? = 1 AND excluded.status = 'downloaded' THEN NULL
                ELSE COALESCE(excluded.uploaded_at, videos.uploaded_at)
            END,
            upload_requested = COALESCE(?, videos.upload_requested)
    """

    @staticmethod
    def _build_upsert_video_params(
        viewkey: str,
        *,
        title: str = "",
        uploader: str = "",
        target_type: str = "",
        quality: str = "",
        video_page_url: str = "",
        status: str = "baseline",
        local_path: str = "",
        remote_path: str = "",
        file_size: int = 0,
        downloaded_at: Optional[str] = None,
        uploaded_at: Optional[str] = None,
        force_status: bool = False,
        upload_requested: Optional[bool] = None,
        now_iso: Optional[str] = None,
    ) -> tuple[Any, ...]:
        now = now_iso or utc_now_iso()
        explicit_type = (target_type or "").strip()
        insert_type = explicit_type or "model"
        explicit_url = (video_page_url or "").strip()
        insert_url = explicit_url or f"https://www.pornhub.com/view_video.php?viewkey={viewkey}"
        force_flag = 1 if force_status else 0
        return (
            viewkey,
            title or "",
            uploader or "",
            insert_type,
            quality or "",
            insert_url,
            status,
            local_path or "",
            remote_path or "",
            int(file_size or 0),
            downloaded_at,
            uploaded_at,
            now,
            int(bool(upload_requested)),
            explicit_type,
            explicit_type,
            explicit_url,
            explicit_url,
            force_flag,
            force_flag,
            force_flag,
            force_flag,
            None if upload_requested is None else int(upload_requested),
        )

    async def upsert_video(
        self,
        viewkey: str,
        *,
        title: str = "",
        uploader: str = "",
        target_type: str = "",
        quality: str = "",
        video_page_url: str = "",
        status: str = "baseline",
        local_path: str = "",
        remote_path: str = "",
        file_size: int = 0,
        downloaded_at: Optional[str] = None,
        uploaded_at: Optional[str] = None,
        force_status: bool = False,
        upload_requested: Optional[bool] = None,
    ) -> None:
        if not viewkey:
            return
        params = self._build_upsert_video_params(
            viewkey,
            title=title,
            uploader=uploader,
            target_type=target_type,
            quality=quality,
            video_page_url=video_page_url,
            status=status,
            local_path=local_path,
            remote_path=remote_path,
            file_size=file_size,
            downloaded_at=downloaded_at,
            uploaded_at=uploaded_at,
            force_status=force_status,
            upload_requested=upload_requested,
        )
        await self.db.execute(self._UPSERT_VIDEO_SQL, params)

    async def upsert_videos_batch(
        self,
        records: List[Dict[str, Any]],
        *,
        chunk_size: int = 200,
    ) -> int:
        if not records:
            return 0
        now = utc_now_iso()
        param_rows = []
        for rec in records:
            vk = str(rec.get("viewkey") or "").strip()
            if not vk:
                continue
            param_rows.append(
                self._build_upsert_video_params(
                    vk,
                    title=rec.get("title", ""),
                    uploader=rec.get("uploader", ""),
                    target_type=rec.get("target_type", ""),
                    quality=rec.get("quality", ""),
                    video_page_url=rec.get("video_page_url", ""),
                    status=rec.get("status", "baseline"),
                    local_path=rec.get("local_path", ""),
                    remote_path=rec.get("remote_path", ""),
                    file_size=rec.get("file_size", 0),
                    downloaded_at=rec.get("downloaded_at"),
                    uploaded_at=rec.get("uploaded_at"),
                    force_status=bool(rec.get("force_status", False)),
                    upload_requested=rec.get("upload_requested"),
                    now_iso=now,
                )
            )
        if not param_rows:
            return 0
        step = max(1, int(chunk_size))
        for start in range(0, len(param_rows), step):
            await self.db.executemany(self._UPSERT_VIDEO_SQL, param_rows[start : start + step])
        return len(param_rows)

    async def mark_video_uploaded(self, viewkey: str, remote_path: str = "") -> None:
        if not viewkey:
            return
        now = utc_now_iso()
        await self.upsert_video(
            viewkey,
            status="uploaded",
            local_path="",
            remote_path=remote_path,
            uploaded_at=now,
            force_status=True,
        )

    async def record_verified_upload(self, viewkey: str, local_path: str, remote_path: str) -> bool:
        """Persist the verified destination before it is safe to remove a local copy."""
        await self.db.execute(
            """
            UPDATE videos
            SET status='uploaded', remote_path=?, uploaded_at=?, upload_requested=0
            WHERE viewkey=? AND local_path=?
            """,
            (remote_path, utc_now_iso(), viewkey, local_path),
        )
        row = await self.get_video(viewkey)
        return bool(row and row["status"] == "uploaded" and row["local_path"] == local_path
                    and row["remote_path"] == remote_path)

    async def clear_uploaded_local_path(self, viewkey: str, local_path: str) -> None:
        await self.db.execute(
            "UPDATE videos SET local_path='' WHERE viewkey=? AND local_path=? AND status='uploaded'",
            (viewkey, local_path),
        )

    async def delete_video(self, viewkey: str) -> bool:
        existing = await self.get_video(viewkey)
        if not existing:
            return False
        await self.db.execute("DELETE FROM videos WHERE viewkey = ?", (viewkey,))
        return True

    async def delete_videos(self, viewkeys: List[str]) -> List[str]:
        unique_keys = list(dict.fromkeys(
            str(key).strip() for key in viewkeys if str(key).strip()
        ))
        if not unique_keys:
            return []
        placeholders = ",".join("?" for _ in unique_keys)
        rows = await self.db.fetch_all(
            f"SELECT viewkey FROM videos WHERE viewkey IN ({placeholders})",
            unique_keys,
        )
        deleted = [row["viewkey"] for row in rows]
        if deleted:
            del_placeholders = ",".join("?" for _ in deleted)
            await self.db.execute(
                f"DELETE FROM videos WHERE viewkey IN ({del_placeholders})",
                deleted,
            )
        return deleted

    async def _get_target_display_map(self) -> Dict[str, str]:
        rows = await self.db.fetch_all(
            """
            SELECT LOWER(username) AS uname, display_name
            FROM targets
            WHERE display_name != ''
            ORDER BY id ASC
            """
        )
        mapping: Dict[str, str] = {}
        for r in rows:
            uname = r["uname"]
            if uname and uname not in mapping:
                mapping[uname] = r["display_name"]
        return mapping

    async def query_videos(
        self,
        *,
        search: str = "",
        uploader: str = "",
        status: str = "",
        quality: str = "",
        page: int = 1,
        page_size: int = 30,
    ) -> Tuple[List[Dict[str, Any]], int]:
        clauses = []
        params: List[Any] = []
        search_clean = search.strip()
        if search_clean:
            like = f"%{search_clean}%"
            matched_targets = await self.db.fetch_all(
                "SELECT DISTINCT LOWER(username) AS uname FROM targets WHERE display_name LIKE ?",
                (like,),
            )
            matched_unames = [r["uname"] for r in matched_targets if r["uname"]]
            if matched_unames:
                placeholders = ",".join("?" for _ in matched_unames)
                clauses.append(
                    f"(v.title LIKE ? OR v.viewkey LIKE ? OR v.uploader LIKE ? OR LOWER(v.uploader) IN ({placeholders}))"
                )
                params.extend([like, like, like, *matched_unames])
            else:
                clauses.append("(v.title LIKE ? OR v.viewkey LIKE ? OR v.uploader LIKE ?)")
                params.extend([like, like, like])
        if uploader.strip():
            clauses.append("LOWER(v.uploader) = LOWER(?)")
            params.append(uploader.strip())
        if status.strip():
            clauses.append("v.status = ?")
            params.append(status.strip())
        if quality.strip():
            clauses.append("v.quality = ?")
            params.append(quality.strip())

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total_row = await self.db.fetch_one(
            f"SELECT COUNT(*) AS cnt FROM videos v {where_sql}",
            params,
        )
        total = int(total_row["cnt"]) if total_row else 0

        offset = max(0, (page - 1) * page_size)
        rows = await self.db.fetch_all(
            f"""
            SELECT v.*
            FROM videos v
            {where_sql}
            ORDER BY COALESCE(v.downloaded_at, v.uploaded_at, v.created_at) DESC,
                     v.viewkey DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, offset],
        )
        display_map = await self._get_target_display_map() if rows else {}
        for row in rows:
            uname = str(row.get("uploader") or "").strip().lower()
            row["uploader_display_name"] = display_map.get(uname, "")
        return rows, total

    async def get_video_filter_facets(self) -> Dict[str, Any]:
        uploader_rows = await self.db.fetch_all(
            """
            SELECT DISTINCT uploader
            FROM videos
            WHERE uploader != ''
            ORDER BY LOWER(uploader) ASC
            """
        )
        display_map = await self._get_target_display_map()
        qualities = await self.db.fetch_all(
            """
            SELECT DISTINCT quality FROM videos
            WHERE quality != ''
            ORDER BY CAST(REPLACE(REPLACE(LOWER(quality), 'p', ''), 'k', '') AS INTEGER) DESC,
                     quality DESC
            """
        )
        uploaders = [r["uploader"] for r in uploader_rows]
        return {
            "uploaders": uploaders,
            "uploader_labels": {
                u: (display_map.get(u.strip().lower()) or u) for u in uploaders
            },
            "qualities": [r["quality"] for r in qualities],
        }

    async def get_video_stats(self) -> Dict[str, int]:
        rows = await self.db.fetch_all("SELECT status, COUNT(*) AS cnt FROM videos GROUP BY status")
        counts = {"total": 0, "uploaded": 0, "downloaded": 0, "baseline": 0}
        for r in rows:
            c = int(r["cnt"])
            counts["total"] += c
            if r["status"] in counts:
                counts[r["status"]] = c
        counts["baseline_missing_metadata"] = await self.count_baseline_metadata_candidates()
        return counts

    # ------------------------------------------------------------------
    # Jobs & JobItems
    # ------------------------------------------------------------------
    async def create_job(
        self,
        kind: str,
        label: str,
        params: Dict[str, Any],
        trigger: str = "manual",
    ) -> Dict[str, Any]:
        job_id = await self.db.execute(
            """
            INSERT INTO jobs (kind, trigger, status, label, params_json, created_at)
            VALUES (?, ?, 'queued', ?, ?, ?)
            """,
            (kind, trigger, label, json.dumps(params, ensure_ascii=False), utc_now_iso()),
        )
        return (await self.get_job(job_id)) or {}

    async def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row:
            try:
                row["params"] = json.loads(row.get("params_json") or "{}")
            except Exception:
                row["params"] = {}
        return row

    async def list_jobs(self, limit: int = 30) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        )
        for row in rows:
            try:
                row["params"] = json.loads(row.get("params_json") or "{}")
            except Exception:
                row["params"] = {}
        return rows

    async def list_queued_job_ids(self) -> List[int]:
        rows = await self.db.fetch_all("SELECT id FROM jobs WHERE status='queued' ORDER BY id")
        return [int(row["id"]) for row in rows]

    async def claim_queued_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        changed = await self.db.execute_returning(
            "UPDATE jobs SET status='scanning', started_at=? WHERE id=? AND status='queued' RETURNING id",
            (utc_now_iso(), job_id),
        )
        return await self.get_job(job_id) if changed else None

    async def cancel_queued_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        changed = await self.db.execute_returning(
            """
            UPDATE jobs SET status='cancelled', error_message='用户取消排队任务', finished_at=?
            WHERE id=? AND status='queued' RETURNING id
            """,
            (utc_now_iso(), job_id),
        )
        return await self.get_job(job_id) if changed else None

    async def update_job(self, job_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = {
            "status",
            "label",
            "total_items",
            "done_items",
            "uploaded_items",
            "failed_items",
            "skipped_items",
            "error_message",
            "export_text",
            "started_at",
            "finished_at",
        }
        sets = []
        params: List[Any] = []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                params.append(v)
        if sets:
            params.append(job_id)
            await self.db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
        return await self.get_job(job_id)

    async def merge_job_params(self, job_id: int, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        job = await self.get_job(job_id)
        if not job:
            return None
        merged = {**(job.get("params") or {}), **patch}
        await self.db.execute(
            "UPDATE jobs SET params_json = ? WHERE id = ?",
            (json.dumps(merged, ensure_ascii=False), job_id),
        )
        return await self.get_job(job_id)

    async def delete_job(self, job_id: int) -> Tuple[bool, str]:
        job = await self.get_job(job_id)
        if not job:
            return False, "not_found"
        if job.get("status") in {"queued", "scanning", "running"}:
            return False, "active"
        await self.db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return True, ""

    async def delete_jobs(self, job_ids: List[int]) -> Dict[str, Any]:
        unique_ids = []
        seen = set()
        for raw_id in job_ids:
            try:
                jid = int(raw_id)
            except (TypeError, ValueError):
                continue
            if jid not in seen:
                seen.add(jid)
                unique_ids.append(jid)
        if not unique_ids:
            return {"deleted_ids": [], "skipped_active_ids": []}

        placeholders = ",".join("?" for _ in unique_ids)
        rows = await self.db.fetch_all(
            f"SELECT id, status FROM jobs WHERE id IN ({placeholders})",
            unique_ids,
        )
        deletable: List[int] = []
        skipped_active: List[int] = []
        for row in rows:
            if row["status"] in {"queued", "scanning", "running"}:
                skipped_active.append(int(row["id"]))
            else:
                deletable.append(int(row["id"]))

        if deletable:
            del_placeholders = ",".join("?" for _ in deletable)
            await self.db.execute(
                f"DELETE FROM jobs WHERE id IN ({del_placeholders})",
                deletable,
            )
        return {"deleted_ids": deletable, "skipped_active_ids": skipped_active}

    async def prune_old_jobs(self, max_finished_jobs: int = 200) -> int:
        if max_finished_jobs <= 0:
            return 0
        rows = await self.db.fetch_all(
            """
            SELECT id FROM jobs
            WHERE status NOT IN ('queued', 'scanning', 'running')
            ORDER BY id DESC
            LIMIT -1 OFFSET ?
            """,
            (int(max_finished_jobs),),
        )
        if not rows:
            return 0
        stale_ids = [int(r["id"]) for r in rows]
        placeholders = ",".join("?" for _ in stale_ids)
        await self.db.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", stale_ids)
        return len(stale_ids)

    async def add_job_items_batch(
        self,
        job_id: int,
        items: List[Dict[str, Any]],
        *,
        chunk_size: int = 200,
    ) -> List[Dict[str, Any]]:
        if not items:
            return []
        now = utc_now_iso()
        inserted: List[Dict[str, Any]] = []
        step = max(1, int(chunk_size))
        sql = """
            INSERT INTO job_items (
                job_id, viewkey, title, uploader, target_type, page_url,
                download_url, referer_url, quality, status, temp_path, local_path,
                bytes_downloaded, updated_at, upload_requested, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING *
        """
        for start in range(0, len(items), step):
            batch = items[start : start + step]
            async with self.db.transaction() as conn:
                for spec in batch:
                    up_req = spec.get("upload_requested")
                    params = (
                        job_id,
                        spec.get("viewkey", ""),
                        spec.get("title", ""),
                        spec.get("uploader", ""),
                        spec.get("target_type", "model"),
                        spec.get("page_url", ""),
                        spec.get("download_url", ""),
                        spec.get("referer_url", ""),
                        spec.get("quality", ""),
                        spec.get("status", "pending"),
                        spec.get("temp_path", ""),
                        spec.get("local_path", ""),
                        int(spec.get("bytes_downloaded") or 0),
                        now,
                        None if up_req is None else int(bool(up_req)),
                        spec.get("error_message", ""),
                    )
                    async with conn.execute(sql, params) as cur:
                        row = await cur.fetchone()
                        if row is not None:
                            inserted.append(dict(row))
        return inserted

    async def add_job_item(
        self,
        job_id: int,
        *,
        viewkey: str,
        title: str,
        uploader: str,
        target_type: str,
        page_url: str,
        download_url: str = "",
        referer_url: str = "",
        quality: str = "",
        status: str = "pending",
        temp_path: str = "",
        local_path: str = "",
        bytes_downloaded: int = 0,
        upload_requested: Optional[bool] = None,
        error_message: str = "",
    ) -> Dict[str, Any]:
        item_id = await self.db.execute(
            """
            INSERT INTO job_items (
                job_id, viewkey, title, uploader, target_type, page_url,
                download_url, referer_url, quality, status, temp_path, local_path,
                bytes_downloaded, updated_at, upload_requested, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                viewkey,
                title,
                uploader,
                target_type,
                page_url,
                download_url,
                referer_url,
                quality,
                status,
                temp_path,
                local_path,
                int(bytes_downloaded or 0),
                utc_now_iso(),
                None if upload_requested is None else int(upload_requested),
                error_message,
            ),
        )
        return (await self.get_job_item(item_id)) or {}

    async def get_job_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        return await self.db.fetch_one("SELECT * FROM job_items WHERE id = ?", (item_id,))

    async def list_job_items(self, job_id: int) -> List[Dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM job_items WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        )

    async def find_temp_job_item(self, viewkey: str, temp_path: str) -> Optional[Dict[str, Any]]:
        return await self.db.fetch_one(
            """
            SELECT i.*, j.params_json FROM job_items i JOIN jobs j ON j.id=i.job_id
            WHERE i.viewkey=? AND i.temp_path=? ORDER BY i.id DESC LIMIT 1
            """,
            (viewkey, temp_path),
        )

    async def update_job_item(self, item_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = {
            "viewkey",
            "title",
            "uploader",
            "target_type",
            "download_url",
            "referer_url",
            "quality",
            "status",
            "temp_path",
            "local_path",
            "bytes_downloaded",
            "bytes_total",
            "retry_count",
            "error_message",
            "upload_requested",
        }
        sets = ["updated_at = ?"]
        params: List[Any] = [utc_now_iso()]
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                params.append(v)
        params.append(item_id)
        await self.db.execute(f"UPDATE job_items SET {', '.join(sets)} WHERE id = ?", params)
        return await self.get_job_item(item_id)

    async def recalculate_job_counts(self, job_id: int) -> Optional[Dict[str, Any]]:
        job = await self.get_job(job_id)
        job_kind = (job or {}).get("kind")
        rows = await self.db.fetch_all(
            "SELECT status, COUNT(*) AS cnt FROM job_items WHERE job_id = ? GROUP BY status",
            (job_id,),
        )
        total = 0
        done = 0
        uploaded = 0
        failed = 0
        skipped = 0
        for r in rows:
            st = r["status"]
            c = int(r["cnt"])
            total += c
            if st == "done":
                done += c
                if job_kind != "enrich_metadata":
                    uploaded += c
            elif st in {"downloaded", "uploading"}:
                done += c
            elif st == "failed":
                failed += c
            elif st == "skipped":
                skipped += c
        return await self.update_job(
            job_id,
            total_items=total,
            done_items=done,
            uploaded_items=uploaded,
            failed_items=failed,
            skipped_items=skipped,
        )

    async def recover_interrupted_jobs(self) -> int:
        """Mark interrupted work cancelled; unstarted jobs remain durably queued."""
        active = await self.db.fetch_all(
            "SELECT id FROM jobs WHERE status IN ('scanning', 'running')"
        )
        now = utc_now_iso()
        for row in active:
            jid = row["id"]
            await self.db.execute(
                """
                UPDATE job_items
                SET status = 'cancelled', error_message = '服务重启中断', updated_at = ?
                WHERE job_id = ? AND status IN ('pending', 'downloading', 'uploading')
                """,
                (now, jid),
            )
            await self.db.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', error_message = '服务重启中断，可点击续传或重试', finished_at = ?
                WHERE id = ?
                """,
                (now, jid),
            )
        return len(active)
