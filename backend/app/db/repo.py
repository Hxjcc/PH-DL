"""Repository layer for Targets, Videos, Jobs, JobItems, and Settings."""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..config import RuntimeSettings
from .database import Database

STATUS_RANK = {"baseline": 1, "downloaded": 2, "uploaded": 3}


def normalize_site(site: Optional[str]) -> str:
    val = str(site or "pornhub").strip().lower()
    return "xhamster" if val in {"xhamster", "xh"} else "pornhub"


def _infer_site_from_url(url: Optional[str]) -> str:
    low = str(url or "").strip().lower()
    if "xhamster." in low:
        return "xhamster"
    return ""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_day_start_utc(now: Optional[datetime] = None) -> datetime:
    local_now = (now or datetime.now().astimezone()).astimezone()
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)


def local_date_days_ago(days: int, now: Optional[datetime] = None) -> str:
    local_now = (now or datetime.now().astimezone()).astimezone()
    return (local_now.date() - timedelta(days=days)).isoformat()


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
            if isinstance(data, dict):
                # Safely normalize any legacy stored range inversions without dropping settings on upgrade.
                if "page_delay_min" in data and "page_delay_max" in data:
                    try:
                        if float(data["page_delay_min"]) > float(data["page_delay_max"]):
                            data["page_delay_min"] = float(data["page_delay_max"])
                    except (TypeError, ValueError):
                        pass
                if "retry_delay_base" in data and "retry_delay_max" in data:
                    try:
                        if float(data["retry_delay_base"]) > float(data["retry_delay_max"]):
                            data["retry_delay_base"] = float(data["retry_delay_max"])
                    except (TypeError, ValueError):
                        pass
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
    async def list_targets(
        self,
        enabled_only: bool = False,
        site: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        query_params: List[Any] = []
        if enabled_only:
            clauses.append("enabled = 1")
        if site and str(site).strip():
            clauses.append("site = ?")
            query_params.append(normalize_site(site))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self.db.fetch_all(
            f"SELECT * FROM targets {where} ORDER BY LOWER(username) ASC, id ASC",
            query_params,
        )
        if not rows:
            return rows

        today_start_dt = local_day_start_utc()
        today_start_iso = today_start_dt.isoformat(timespec="seconds")
        recent_publish_cutoff = local_date_days_ago(30)
        video_rows = await self.db.fetch_all(
            """
            SELECT LOWER(site) AS site_lower,
                   LOWER(uploader) AS uploader_lower,
                   LOWER(target_type) AS target_type_lower,
                   COUNT(*) AS total_cnt,
                   SUM(CASE WHEN status IN ('downloaded', 'uploaded') THEN 1 ELSE 0 END) AS saved_cnt,
                   SUM(CASE WHEN status = 'baseline' THEN 1 ELSE 0 END) AS baseline_cnt,
                   SUM(
                       CASE
                           WHEN status IN ('downloaded', 'uploaded')
                            AND COALESCE(downloaded_at, uploaded_at, created_at) >= ?
                            AND (published_at = '' OR published_at >= ?)
                           THEN 1 ELSE 0
                       END
                   ) AS today_cnt
            FROM videos
            GROUP BY LOWER(site), LOWER(uploader), LOWER(target_type)
            """,
            (today_start_iso, recent_publish_cutoff),
        )

        def _empty_counts() -> Dict[str, int]:
            return {"total": 0, "saved": 0, "baseline": 0, "today": 0}

        def _add_counts(bucket: Dict[str, int], total_c: int, saved_c: int, base_c: int, today_c: int) -> None:
            bucket["total"] += total_c
            bucket["saved"] += saved_c
            bucket["baseline"] += base_c
            bucket["today"] += today_c

        counts_by_pair: Dict[Tuple[str, str, str], Dict[str, int]] = {}
        non_special_counts_by_uploader: Dict[Tuple[str, str], Dict[str, int]] = {}
        liked_counts_by_site: Dict[str, Dict[str, int]] = {}
        xh_favorites_counts = _empty_counts()
        for item in video_rows:
            s_low = normalize_site(item.get("site_lower"))
            u_low = str(item.get("uploader_lower") or "").strip()
            t_low = str(item.get("target_type_lower") or "").strip()
            total_c = int(item.get("total_cnt") or 0)
            saved_c = int(item.get("saved_cnt") or 0)
            base_c = int(item.get("baseline_cnt") or 0)
            today_c = int(item.get("today_cnt") or 0)
            pair_bucket = counts_by_pair.setdefault((s_low, u_low, t_low), _empty_counts())
            _add_counts(pair_bucket, total_c, saved_c, base_c, today_c)
            if t_low == "liked" or u_low == "my_liked":
                liked_bucket = liked_counts_by_site.setdefault(s_low, _empty_counts())
                _add_counts(liked_bucket, total_c, saved_c, base_c, today_c)
            elif s_low == "xhamster" and (t_low == "favorites" or u_low == "my_favorites"):
                _add_counts(xh_favorites_counts, total_c, saved_c, base_c, today_c)
            elif t_low != "favorites" and u_low:
                user_bucket = non_special_counts_by_uploader.setdefault((s_low, u_low), _empty_counts())
                _add_counts(user_bucket, total_c, saved_c, base_c, today_c)

        non_special_targets_per_user: Dict[Tuple[str, str], int] = {}
        for r in rows:
            s_low = normalize_site(r.get("site"))
            u_low = str(r.get("username") or "").strip().lower()
            t_low = str(r.get("target_type") or "model").strip().lower()
            if t_low not in ("liked", "favorites") and u_low:
                key = (s_low, u_low)
                non_special_targets_per_user[key] = non_special_targets_per_user.get(key, 0) + 1

        for r in rows:
            r["enabled"] = bool(r["enabled"])
            s_low = normalize_site(r.get("site"))
            r["site"] = s_low
            u_low = str(r.get("username") or "").strip().lower()
            t_low = str(r.get("target_type") or "model").strip().lower()
            if t_low == "liked":
                matched = liked_counts_by_site.get(s_low) or _empty_counts()
            elif t_low == "favorites":
                if s_low == "xhamster":
                    matched = xh_favorites_counts
                else:
                    matched = counts_by_pair.get((s_low, u_low, "favorites")) or _empty_counts()
            elif non_special_targets_per_user.get((s_low, u_low), 0) == 1:
                matched = non_special_counts_by_uploader.get((s_low, u_low)) or _empty_counts()
            else:
                matched = counts_by_pair.get((s_low, u_low, t_low)) or _empty_counts()

            downloaded_today = matched["today"]
            checked_dt = _parse_iso_utc(r.get("last_checked_at"))
            checked_today = bool(checked_dt and checked_dt >= today_start_dt)
            last_new_today = int(r.get("last_new_count") or 0) if checked_today else 0
            r["today_downloaded_count"] = downloaded_today
            r["today_new_count"] = max(downloaded_today, last_new_today)
            r["video_total_count"] = matched["total"]
            r["video_saved_count"] = matched["saved"]
            r["video_baseline_count"] = matched["baseline"]
        return rows

    async def get_target(self, target_id: int) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one("SELECT * FROM targets WHERE id = ?", (target_id,))
        if row:
            row["enabled"] = bool(row["enabled"])
            row["site"] = normalize_site(row.get("site"))
        return row

    async def _find_target_by_identity(
        self,
        norm_site: str,
        norm_user: str,
        norm_type: str,
    ) -> Optional[Dict[str, Any]]:
        """Look up a target while respecting same-creator mutually exclusive types."""
        if norm_site == "pornhub" and norm_type in ("model", "pornstar"):
            return await self.db.fetch_one(
                """
                SELECT * FROM targets
                WHERE site = ? AND LOWER(username) = LOWER(?) AND target_type IN ('model', 'pornstar')
                ORDER BY CASE WHEN target_type = ? THEN 0 ELSE 1 END, id ASC
                LIMIT 1
                """,
                (norm_site, norm_user, norm_type),
            )
        if norm_site == "xhamster" and norm_type in ("creator", "pornstar"):
            return await self.db.fetch_one(
                """
                SELECT * FROM targets
                WHERE site = ? AND LOWER(username) = LOWER(?) AND target_type IN ('creator', 'pornstar')
                ORDER BY CASE WHEN target_type = ? THEN 0 ELSE 1 END, id ASC
                LIMIT 1
                """,
                (norm_site, norm_user, norm_type),
            )
        return await self.db.fetch_one(
            "SELECT * FROM targets WHERE site = ? AND LOWER(username) = LOWER(?) AND target_type = ?",
            (norm_site, norm_user, norm_type),
        )

    async def upsert_target(
        self,
        username: str,
        target_type: str = "model",
        enabled: bool = True,
        preferred_quality: str = "",
        avatar_url: str = "",
        display_name: str = "",
        site: str = "pornhub",
    ) -> Dict[str, Any]:
        norm_site = normalize_site(site)
        norm_type = (target_type or ("creator" if norm_site == "xhamster" else "model")).strip().lower()
        is_current_user = norm_type == "liked" or (norm_site == "xhamster" and norm_type == "favorites")
        norm_user = "_currentUser_" if is_current_user else username.strip()
        existing = await self._find_target_by_identity(norm_site, norm_user, norm_type)
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
                site, username, target_type, enabled, preferred_quality, avatar_url,
                display_name, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                norm_site,
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

    async def ensure_target(
        self,
        username: str,
        target_type: str = "model",
        *,
        avatar_url: str = "",
        display_name: str = "",
        site: str = "pornhub",
    ) -> Tuple[Dict[str, Any], bool]:
        """Insert an enabled tracking target if absent; never overwrite existing enabled/quality settings."""
        norm_site = normalize_site(site)
        norm_type = (target_type or ("creator" if norm_site == "xhamster" else "model")).strip().lower()
        is_current_user = norm_type == "liked" or (norm_site == "xhamster" and norm_type == "favorites")
        norm_user = "_currentUser_" if is_current_user else username.strip()
        existing = await self._find_target_by_identity(norm_site, norm_user, norm_type)
        if existing:
            clean_avatar = avatar_url.strip()
            clean_name = display_name.strip()
            need_avatar = bool(clean_avatar and not str(existing.get("avatar_url") or "").strip())
            need_name = bool(clean_name and not str(existing.get("display_name") or "").strip())
            if need_avatar or need_name:
                await self.db.execute(
                    """
                    UPDATE targets
                    SET avatar_url = CASE WHEN COALESCE(avatar_url, '') = '' AND ? != '' THEN ? ELSE avatar_url END,
                        display_name = CASE WHEN COALESCE(display_name, '') = '' AND ? != '' THEN ? ELSE display_name END
                    WHERE id = ?
                    """,
                    (clean_avatar, clean_avatar, clean_name, clean_name, existing["id"]),
                )
            return ((await self.get_target(existing["id"])) or existing), False

        created = await self.upsert_target(
            norm_user,
            norm_type,
            enabled=True,
            preferred_quality="",
            avatar_url=avatar_url,
            display_name=display_name,
            site=norm_site,
        )
        return created, True

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
        site: str = "pornhub",
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
        norm_site = normalize_site(site)
        params.extend([norm_site, username.strip(), (target_type or "model").strip().lower()])
        await self.db.execute(
            f"""
            UPDATE targets SET {', '.join(sets)}
            WHERE site = ? AND LOWER(username) = LOWER(?) AND target_type = ?
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
        *,
        site: str = "pornhub",
    ) -> None:
        await self.update_target_profile_by_username(
            username,
            target_type,
            avatar_url=avatar_url,
            site=site,
        )

    async def update_target(self, target_id: int, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        existing = await self.get_target(target_id)
        if not existing:
            return None
        site = normalize_site(existing.get("site"))
        old_username = str(existing.get("username") or "").strip()
        old_type = str(existing.get("target_type") or "model").strip().lower()
        allowed = {"enabled", "target_type", "preferred_quality", "username", "display_name"}
        sets = []
        params: List[Any] = []
        next_username = old_username
        next_type = old_type
        for k, v in patch.items():
            if k not in allowed:
                continue
            if k == "enabled":
                sets.append("enabled = ?")
                params.append(1 if v else 0)
            else:
                val = str(v).strip()
                if k == "target_type":
                    val = val.lower()
                    next_type = val
                elif k == "username":
                    next_username = val
                sets.append(f"{k} = ?")
                params.append(val)
        if "username" in patch or "target_type" in patch:
            if site == "pornhub" and next_type in ("model", "pornstar"):
                conflict = await self.db.fetch_one(
                    "SELECT id FROM targets WHERE site = ? AND LOWER(username) = LOWER(?) AND target_type IN ('model', 'pornstar') AND id != ?",
                    (site, next_username, target_id),
                )
            elif site == "xhamster" and next_type in ("creator", "pornstar"):
                conflict = await self.db.fetch_one(
                    "SELECT id FROM targets WHERE site = ? AND LOWER(username) = LOWER(?) AND target_type IN ('creator', 'pornstar') AND id != ?",
                    (site, next_username, target_id),
                )
            else:
                conflict = await self.db.fetch_one(
                    "SELECT id FROM targets WHERE site = ? AND LOWER(username) = LOWER(?) AND target_type = ? AND id != ?",
                    (site, next_username, next_type, target_id),
                )
            if conflict:
                raise sqlite3.IntegrityError(
                    "UNIQUE constraint failed: targets.site, targets.username, targets.target_type"
                )
        if sets:
            params.append(target_id)
            await self.db.execute(f"UPDATE targets SET {', '.join(sets)} WHERE id = ?", params)

        username_changed = bool(old_username and next_username and old_username.lower() != next_username.lower())
        type_changed = old_type != next_type
        if old_type != "liked" and next_type != "liked" and (username_changed or type_changed):
            if old_type == "favorites" and next_type == "favorites" and username_changed:
                await self.db.execute(
                    """
                    UPDATE videos
                    SET uploader = ?
                    WHERE site = ? AND LOWER(uploader) = LOWER(?) AND LOWER(target_type) = 'favorites'
                    """,
                    (next_username, site, old_username),
                )
            elif site == "pornhub" and old_type in ("model", "pornstar") and next_type in ("model", "pornstar"):
                await self.db.execute(
                    """
                    UPDATE videos
                    SET uploader = ?,
                        target_type = CASE WHEN LOWER(target_type) IN ('model', 'pornstar') THEN ? ELSE target_type END
                    WHERE site = ?
                      AND LOWER(uploader) = LOWER(?)
                      AND LOWER(target_type) NOT IN ('favorites', 'liked')
                    """,
                    (next_username, next_type, site, old_username),
                )
            elif site == "xhamster" and old_type not in ("favorites", "liked") and next_type not in ("favorites", "liked"):
                await self.db.execute(
                    """
                    UPDATE videos
                    SET uploader = ?,
                        target_type = ?
                    WHERE site = ?
                      AND LOWER(uploader) = LOWER(?)
                      AND LOWER(target_type) = ?
                    """,
                    (next_username, next_type, site, old_username, old_type),
                )
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
            SELECT viewkey, site, title, uploader, target_type, quality, local_path,
                   file_size, video_page_url, downloaded_at, published_at, duration_seconds
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

    async def count_baseline_metadata_candidates(self, site: str = "") -> int:
        site_filter = normalize_site(site) if (site or "").strip() else ""
        where_site = " AND LOWER(COALESCE(site, 'pornhub')) = ?" if site_filter else ""
        params: List[Any] = [site_filter] if site_filter else []
        row = await self.db.fetch_one(
            f"""
            SELECT COUNT(*) AS cnt
            FROM videos
            WHERE status = 'baseline'
              AND video_page_url != ''
              AND (quality = '' OR (LOWER(COALESCE(site, 'pornhub')) != 'xhamster' AND file_size <= 0))
              {where_site}
            """,
            params,
        )
        return int(row["cnt"]) if row else 0

    async def list_baseline_metadata_candidates(
        self,
        limit: int = 0,
        site: str = "",
    ) -> List[Dict[str, Any]]:
        site_filter = normalize_site(site) if (site or "").strip() else ""
        where_site = " AND LOWER(COALESCE(site, 'pornhub')) = ?" if site_filter else ""
        sql = f"""
            SELECT viewkey, site, title, uploader, target_type, quality, file_size, video_page_url
            FROM videos
            WHERE status = 'baseline'
              AND video_page_url != ''
              AND (quality = '' OR (LOWER(COALESCE(site, 'pornhub')) != 'xhamster' AND file_size <= 0))
              {where_site}
            ORDER BY created_at ASC, viewkey ASC
        """
        params: List[Any] = [site_filter] if site_filter else []
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
        published_at: str = "",
        duration_seconds: int = 0,
    ) -> bool:
        sets: List[str] = []
        params: List[Any] = []
        if quality.strip():
            sets.append("quality = ?")
            params.append(quality.strip())
        if int(file_size or 0) > 0:
            sets.append("file_size = ?")
            params.append(int(file_size))
        if (published_at or "").strip():
            sets.append("published_at = ?")
            params.append(published_at.strip())
        if int(duration_seconds or 0) > 0:
            sets.append("duration_seconds = ?")
            params.append(int(duration_seconds))
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
            viewkey, site, title, uploader, target_type, quality, video_page_url,
            status, local_path, remote_path, file_size, downloaded_at, uploaded_at, created_at,
            upload_requested, published_at, duration_seconds
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(viewkey) DO UPDATE SET
            site = CASE WHEN ? != '' THEN ? ELSE videos.site END,
            title = CASE WHEN excluded.title != '' THEN excluded.title ELSE videos.title END,
            uploader = CASE
                WHEN excluded.uploader = '' THEN videos.uploader
                WHEN videos.uploader = '' THEN excluded.uploader
                WHEN ? IN ('favorites', 'liked') AND videos.target_type NOT IN ('favorites', 'liked') THEN videos.uploader
                WHEN ? = 0 AND excluded.status = 'baseline' AND videos.status IN ('downloaded', 'uploaded') THEN videos.uploader
                ELSE excluded.uploader
            END,
            target_type = CASE
                WHEN ? = '' THEN videos.target_type
                WHEN ? IN ('favorites', 'liked') AND videos.target_type NOT IN ('favorites', 'liked') AND videos.uploader != '' THEN videos.target_type
                WHEN ? = 0 AND excluded.status = 'baseline' AND videos.status IN ('downloaded', 'uploaded') THEN videos.target_type
                ELSE ?
            END,
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
            upload_requested = COALESCE(?, videos.upload_requested),
            published_at = CASE WHEN excluded.published_at != '' THEN excluded.published_at ELSE videos.published_at END,
            duration_seconds = CASE WHEN excluded.duration_seconds > 0 THEN excluded.duration_seconds ELSE videos.duration_seconds END
    """

    @staticmethod
    def _build_upsert_video_params(
        viewkey: str,
        *,
        site: str = "",
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
        published_at: str = "",
        duration_seconds: int = 0,
        now_iso: Optional[str] = None,
    ) -> tuple[Any, ...]:
        now = now_iso or utc_now_iso()
        explicit_url = (video_page_url or "").strip()
        explicit_site = normalize_site(site) if str(site or "").strip() else _infer_site_from_url(explicit_url)
        insert_site = explicit_site or "pornhub"
        explicit_type = (target_type or "").strip().lower()
        insert_type = explicit_type or ("creator" if insert_site == "xhamster" else "model")
        if explicit_url:
            insert_url = explicit_url
        elif insert_site == "xhamster":
            insert_url = f"https://xhamster.com/videos/{viewkey}"
        else:
            insert_url = f"https://www.pornhub.com/view_video.php?viewkey={viewkey}"
        force_flag = 1 if force_status else 0
        return (
            viewkey,
            insert_site,
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
            (published_at or "").strip(),
            max(0, int(duration_seconds or 0)),
            explicit_site,
            explicit_site,
            explicit_type,
            force_flag,
            explicit_type,
            explicit_type,
            force_flag,
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
        site: str = "",
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
        published_at: str = "",
        duration_seconds: int = 0,
    ) -> None:
        if not viewkey:
            return
        params = self._build_upsert_video_params(
            viewkey,
            site=site,
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
            published_at=published_at,
            duration_seconds=duration_seconds,
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
                    site=str(rec.get("site") or ""),
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
                    published_at=str(rec.get("published_at") or ""),
                    duration_seconds=int(rec.get("duration_seconds") or 0),
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

    async def _get_target_display_map(self) -> Dict[Tuple[str, str], str]:
        rows = await self.db.fetch_all(
            """
            SELECT LOWER(site) AS site_low, LOWER(username) AS uname, display_name
            FROM targets
            WHERE display_name != ''
            ORDER BY id ASC
            """
        )
        mapping: Dict[Tuple[str, str], str] = {}
        for r in rows:
            s_low = normalize_site(r.get("site_low"))
            uname = r["uname"]
            if uname and (s_low, uname) not in mapping:
                mapping[(s_low, uname)] = r["display_name"]
        return mapping

    async def query_videos(
        self,
        *,
        site: str = "",
        search: str = "",
        uploader: str = "",
        status: str = "",
        quality: str = "",
        time_range: str = "",
        sort_by: str = "activity",
        page: int = 1,
        page_size: int = 30,
    ) -> Tuple[List[Dict[str, Any]], int]:
        clauses = []
        params: List[Any] = []
        if (site or "").strip():
            clauses.append("v.site = ?")
            params.append(normalize_site(site))
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
            u_clean = uploader.strip()
            if u_clean.lower() == "my_liked":
                clauses.append("(LOWER(v.uploader) = 'my_liked' OR LOWER(v.target_type) = 'liked')")
            elif u_clean.lower() == "my_favorites":
                clauses.append("(LOWER(v.uploader) = 'my_favorites' OR (v.site = 'xhamster' AND LOWER(v.target_type) = 'favorites'))")
            else:
                clauses.append("LOWER(v.uploader) = LOWER(?)")
                params.append(u_clean)
        if status.strip():
            clauses.append("v.status = ?")
            params.append(status.strip())
        if quality.strip():
            clauses.append("v.quality = ?")
            params.append(quality.strip())
        norm_time_range = (time_range or "").strip().lower()
        if norm_time_range == "published_7d":
            clauses.append("v.published_at != '' AND v.published_at >= ?")
            params.append(local_date_days_ago(7))
        elif norm_time_range == "published_30d":
            clauses.append("v.published_at != '' AND v.published_at >= ?")
            params.append(local_date_days_ago(30))
        elif norm_time_range == "activity_7d":
            activity_cutoff = (local_day_start_utc() - timedelta(days=7)).isoformat(timespec="seconds")
            clauses.append(
                "v.status IN ('uploaded', 'downloaded') AND COALESCE(v.downloaded_at, v.uploaded_at, v.created_at) >= ?"
            )
            params.append(activity_cutoff)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total_row = await self.db.fetch_one(
            f"SELECT COUNT(*) AS cnt FROM videos v {where_sql}",
            params,
        )
        total = int(total_row["cnt"]) if total_row else 0

        if (sort_by or "").strip().lower() == "published":
            order_sql = (
                "(v.published_at = '') ASC, "
                "v.published_at DESC, "
                "COALESCE(v.downloaded_at, v.uploaded_at, v.created_at) DESC, "
                "v.viewkey DESC"
            )
        else:
            order_sql = (
                "COALESCE(v.downloaded_at, v.uploaded_at, v.created_at) DESC, "
                "v.viewkey DESC"
            )

        offset = max(0, (page - 1) * page_size)
        rows = await self.db.fetch_all(
            f"""
            SELECT v.*
            FROM videos v
            {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, offset],
        )
        display_map = await self._get_target_display_map() if rows else {}
        for row in rows:
            s_low = normalize_site(row.get("site"))
            row["site"] = s_low
            uname = str(row.get("uploader") or "").strip().lower()
            t_low = str(row.get("target_type") or "").strip().lower()
            if uname == "my_liked" or (not uname and t_low == "liked"):
                row["uploader_display_name"] = "Liked Videos"
            elif s_low == "xhamster" and (uname == "my_favorites" or (not uname and t_low == "favorites")):
                row["uploader_display_name"] = "xHamster Favorites"
            else:
                row["uploader_display_name"] = display_map.get((s_low, uname), "")
        return rows, total

    async def get_video_filter_facets(self, site: str = "") -> Dict[str, Any]:
        site_filter = normalize_site(site) if (site or "").strip() else ""
        where_site = " AND site = ?" if site_filter else ""
        site_params = (site_filter,) if site_filter else ()
        uploader_rows = await self.db.fetch_all(
            f"""
            SELECT DISTINCT uploader, site
            FROM videos
            WHERE uploader != ''{where_site}
            ORDER BY LOWER(uploader) ASC
            """,
            site_params,
        )
        has_liked_row = await self.db.fetch_one(
            f"SELECT 1 AS hit FROM videos WHERE LOWER(target_type) = 'liked'{where_site} LIMIT 1",
            site_params,
        )
        display_map = await self._get_target_display_map()
        qualities = await self.db.fetch_all(
            f"""
            SELECT DISTINCT quality FROM videos
            WHERE quality != ''{where_site}
            ORDER BY CAST(REPLACE(REPLACE(LOWER(quality), 'p', ''), 'k', '') AS INTEGER) DESC,
                     quality DESC
            """,
            site_params,
        )
        site_rows = await self.db.fetch_all("SELECT DISTINCT site FROM videos WHERE site != '' ORDER BY site ASC")
        uploaders: List[str] = []
        seen_up: Dict[str, int] = {}
        uploader_sites: Dict[str, str] = {}
        for r in uploader_rows:
            u = str(r["uploader"] or "").strip()
            if not u:
                continue
            low = u.lower()
            idx = seen_up.get(low)
            if idx is None:
                seen_up[low] = len(uploaders)
                uploaders.append(u)
                uploader_sites[low] = normalize_site(r.get("site"))
            else:
                # Prefer a mixed-case spelling over an all-lowercase duplicate when both exist.
                prev = uploaders[idx]
                if prev == prev.lower() and u != u.lower():
                    uploaders[idx] = u
        if has_liked_row and "my_liked" not in seen_up:
            uploaders.append("My_Liked")
            uploader_sites["my_liked"] = "pornhub"
            uploaders.sort(key=lambda u: u.lower())
        uploader_labels: Dict[str, str] = {}
        for u in uploaders:
            low = u.strip().lower()
            if low == "my_liked":
                uploader_labels[u] = "Liked Videos"
            elif low == "my_favorites":
                uploader_labels[u] = "xHamster Favorites"
            else:
                s_low = site_filter or uploader_sites.get(low, "pornhub")
                uploader_labels[u] = (
                    display_map.get((s_low, low))
                    or display_map.get(("pornhub", low))
                    or display_map.get(("xhamster", low))
                    or u
                )
        return {
            "sites": [normalize_site(r["site"]) for r in site_rows],
            "uploaders": uploaders,
            "uploader_labels": uploader_labels,
            "qualities": [r["quality"] for r in qualities],
        }

    async def get_video_stats(self, site: str = "") -> Dict[str, int]:
        site_filter = normalize_site(site) if (site or "").strip() else ""
        where_site = " WHERE LOWER(COALESCE(site, 'pornhub')) = ?" if site_filter else ""
        and_site = " AND LOWER(COALESCE(site, 'pornhub')) = ?" if site_filter else ""
        site_params: Tuple[Any, ...] = (site_filter,) if site_filter else ()
        rows = await self.db.fetch_all(
            f"SELECT status, COUNT(*) AS cnt FROM videos{where_site} GROUP BY status",
            site_params,
        )
        counts = {"total": 0, "uploaded": 0, "downloaded": 0, "baseline": 0}
        for r in rows:
            c = int(r["cnt"])
            counts["total"] += c
            if r["status"] in counts:
                counts[r["status"]] = c
        recent_row = await self.db.fetch_one(
            f"SELECT COUNT(*) AS cnt FROM videos WHERE published_at != '' AND published_at >= ?{and_site}",
            (local_date_days_ago(7), *site_params),
        )
        counts["published_7d"] = int(recent_row["cnt"]) if recent_row else 0
        counts["baseline_missing_metadata"] = await self.count_baseline_metadata_candidates(site=site_filter)
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
                job_id, site, viewkey, title, uploader, target_type, page_url,
                download_url, referer_url, quality, status, temp_path, local_path,
                bytes_downloaded, updated_at, upload_requested, error_message,
                published_at, duration_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING *
        """
        for start in range(0, len(items), step):
            batch = items[start : start + step]
            async with self.db.transaction() as conn:
                for spec in batch:
                    up_req = spec.get("upload_requested")
                    page_url = spec.get("page_url", "")
                    raw_site = str(spec.get("site") or "").strip()
                    item_site = normalize_site(raw_site) if raw_site else (_infer_site_from_url(page_url) or "pornhub")
                    params = (
                        job_id,
                        item_site,
                        spec.get("viewkey", ""),
                        spec.get("title", ""),
                        spec.get("uploader", ""),
                        spec.get("target_type", "creator" if item_site == "xhamster" else "model"),
                        page_url,
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
                        str(spec.get("published_at") or "").strip(),
                        max(0, int(spec.get("duration_seconds") or 0)),
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
        site: str = "",
        download_url: str = "",
        referer_url: str = "",
        quality: str = "",
        status: str = "pending",
        temp_path: str = "",
        local_path: str = "",
        bytes_downloaded: int = 0,
        upload_requested: Optional[bool] = None,
        error_message: str = "",
        published_at: str = "",
        duration_seconds: int = 0,
    ) -> Dict[str, Any]:
        item_site = normalize_site(site) if (site or "").strip() else (_infer_site_from_url(page_url) or "pornhub")
        item_id = await self.db.execute(
            """
            INSERT INTO job_items (
                job_id, site, viewkey, title, uploader, target_type, page_url,
                download_url, referer_url, quality, status, temp_path, local_path,
                bytes_downloaded, updated_at, upload_requested, error_message,
                published_at, duration_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                item_site,
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
                (published_at or "").strip(),
                max(0, int(duration_seconds or 0)),
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
            "site",
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
            "published_at",
            "duration_seconds",
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

    async def bulk_cancel_pending_job_items(
        self,
        job_id: int,
        item_ids: Optional[Iterable[int]] = None,
        *,
        error_message: str = "已取消",
    ) -> int:
        now = utc_now_iso()
        if item_ids is None:
            await self.db.execute(
                """
                UPDATE job_items
                SET status = 'cancelled', error_message = ?, updated_at = ?
                WHERE job_id = ? AND status = 'pending'
                """,
                (error_message, now, int(job_id)),
            )
            return 1
        ids = [int(i) for i in dict.fromkeys(item_ids)]
        if not ids:
            return 0
        step = 400
        async with self.db.transaction() as conn:
            for start in range(0, len(ids), step):
                chunk = ids[start : start + step]
                placeholders = ",".join("?" for _ in chunk)
                await conn.execute(
                    f"""
                    UPDATE job_items
                    SET status = 'cancelled', error_message = ?, updated_at = ?
                    WHERE job_id = ? AND id IN ({placeholders})
                    """,
                    [error_message, now, int(job_id), *chunk],
                )
        return len(ids)

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
