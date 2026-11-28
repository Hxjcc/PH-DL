"""Repository layer for Targets, Videos, Jobs, JobItems, and Settings."""

from __future__ import annotations

import json
import secrets
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import aiosqlite

from ..config import RuntimeSettings
from ..parsers.video import build_video_page_url
from ..redaction import redact_diagnostic
from .database import Database
from .schema import TARGET_IDENTITY_TYPE

STATUS_RANK = {"baseline": 1, "downloaded": 2, "uploaded": 3}
_SCAN_JOB_KINDS = frozenset({"manual_target", "manual_urls", "auto_update", "check_updates", "baseline_sync", "resume_temp"})
_BASELINE_METADATA_FILTER = """
    status = 'baseline' AND (video_page_url != '') = 1
    AND (quality = '' OR (site != 'xhamster' AND file_size <= 0))
"""
_RECALCULATE_JOB_COUNTS = """
    UPDATE jobs
    SET (total_items, done_items, uploaded_items, failed_items, skipped_items) = (
        SELECT COUNT(*),
               COUNT(CASE WHEN status IN ('done', 'downloaded', 'uploading') THEN 1 END),
               CASE WHEN jobs.kind = 'enrich_metadata' THEN 0 ELSE COUNT(CASE WHEN status = 'done' THEN 1 END) END,
               COUNT(CASE WHEN status = 'failed' THEN 1 END),
               COUNT(CASE WHEN status = 'skipped' THEN 1 END)
        FROM job_items WHERE job_id = jobs.id
    )
    WHERE id = ?
"""


def _parse_job_params(raw: Optional[str]) -> Dict[str, Any]:
    try:
        params = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return params if isinstance(params, dict) else {}


def job_scan_incomplete(job: Dict[str, Any]) -> bool:
    return (
        job["status"] == "scanning"
        and job["kind"] in _SCAN_JOB_KINDS
        and (job.get("params") or {}).get("resume_mode") != "items"
    )


def job_uses_item_counts(job: Dict[str, Any]) -> bool:
    return job["kind"] not in {"baseline_sync", "check_updates"} and not job["params"].get("export_only")


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


def local_today_date(now: Optional[datetime] = None) -> str:
    local_now = (now or datetime.now().astimezone()).astimezone()
    return local_now.date().isoformat()


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
    def __init__(self, db: Database, *, rclone_bin: str = "rclone") -> None:
        self.db = db
        self._rclone_bin = rclone_bin.strip() or "rclone"
        self._diagnostic_secrets: tuple[str, ...] = ()
        self._previous_diagnostic_secrets: tuple[str, ...] = ()
        self._display_map_cache: Optional[Tuple[int, Dict[Tuple[str, str], str]]] = None
        self._facets_cache: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        self._stats_cache: Dict[str, Tuple[int, str, Dict[str, int]]] = {}
        self._target_counts_cache: Optional[
            Tuple[
                int,
                Tuple[
                    Dict[Tuple[str, str, str], Dict[str, int]],
                    Dict[Tuple[str, str], Dict[str, int]],
                    Dict[str, Dict[str, int]],
                    Dict[str, int],
                ],
            ]
        ] = None

    def redact_diagnostic(self, message: str) -> str:
        return redact_diagnostic(message, (*self._diagnostic_secrets, *self._previous_diagnostic_secrets))

    def _remember_settings(self, settings: RuntimeSettings) -> RuntimeSettings:
        values = settings.diagnostic_secrets()
        if values != self._diagnostic_secrets:
            self._previous_diagnostic_secrets = self._diagnostic_secrets
            self._diagnostic_secrets = values
        return settings

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    async def get_settings(self, *, conn: Optional[aiosqlite.Connection] = None) -> RuntimeSettings:
        row = await self.db.fetch_one("SELECT value_json FROM settings WHERE key = ?", ("runtime",), conn=conn)
        if not row:
            return RuntimeSettings(rclone_bin=self._rclone_bin)
        try:
            data = json.loads(row["value_json"])
            if isinstance(data, dict):
                data["rclone_bin"] = self._rclone_bin
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
            return self._remember_settings(RuntimeSettings.model_validate(data))
        except Exception:
            return RuntimeSettings(rclone_bin=self._rclone_bin)

    async def save_settings(self, settings: RuntimeSettings) -> RuntimeSettings:
        settings = settings.model_copy(update={"rclone_bin": self._rclone_bin})
        payload = json.dumps(settings.model_dump(exclude={"rclone_bin"}), ensure_ascii=False)
        await self.db.execute(
            """
            INSERT INTO settings (key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            ("runtime", payload, utc_now_iso()),
        )
        return self._remember_settings(settings)

    async def update_settings_patch(self, patch: Dict[str, Any]) -> RuntimeSettings:
        _, updated = await self.patch_settings(patch)
        return updated

    async def patch_settings(self, patch: Dict[str, Any]) -> Tuple[RuntimeSettings, RuntimeSettings]:
        async with self.db.transaction(touches_video_meta=False) as conn:
            current = await self.get_settings(conn=conn)
            return current, await self._save_runtime_settings(conn, current.merge_update(patch))

    async def _save_runtime_settings(self, conn: aiosqlite.Connection, settings: RuntimeSettings) -> RuntimeSettings:
        async with conn.execute(
            """
            INSERT INTO settings (key, value_json, updated_at) VALUES ('runtime', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            (json.dumps(settings.model_dump(exclude={"rclone_bin"}), ensure_ascii=False), utc_now_iso()),
        ):
            return self._remember_settings(settings)

    async def save_cookie_check(
        self, prefix: str, original: RuntimeSettings, username: str = "",
    ) -> Optional[RuntimeSettings]:
        cookie_key = f"{prefix}_cookie"
        username_key = f"{prefix}_logged_in_username"
        async with self.db.transaction(touches_video_meta=False) as conn:
            current = await self.get_settings(conn=conn)
            if getattr(current, cookie_key) != getattr(original, cookie_key) or current.proxy_url != original.proxy_url:
                return None
            if username and username != getattr(current, username_key):
                return await self._save_runtime_settings(conn, current.merge_update({username_key: username}))
            return current

    async def save_refreshed_cookie(
        self, prefix: str, original: RuntimeSettings, cookie: str, username: str,
    ) -> Optional[RuntimeSettings]:
        key = f"{prefix}_cookie"
        async with self.db.transaction(touches_video_meta=False) as conn:
            current = await self.get_settings(conn=conn)
            if any(getattr(current, attr) != getattr(original, attr) for attr in (
                f"{prefix}_username", f"{prefix}_password", "proxy_url",
            )) or getattr(current, key) not in {getattr(original, key), cookie}:
                return None
            patch = {key: cookie}
            if username:
                patch[f"{prefix}_logged_in_username"] = username
            return await self._save_runtime_settings(conn, current.merge_update(patch))

    async def get_scheduler_last_run_date(self, *, conn: Optional[aiosqlite.Connection] = None) -> Optional[str]:
        row = await self.db.fetch_one("SELECT value_json FROM settings WHERE key = ?", ("scheduler_state",), conn=conn)
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
    async def _get_target_video_counts(self) -> Tuple[
        Dict[Tuple[str, str, str], Dict[str, int]],
        Dict[Tuple[str, str], Dict[str, int]],
        Dict[str, Dict[str, int]],
        Dict[str, int],
    ]:
        version = self.db.video_meta_version
        cached = self._target_counts_cache
        if cached is not None and cached[0] == version:
            return cached[1]

        video_rows = await self.db.fetch_all(
            """
            SELECT site AS site_lower,
                   LOWER(uploader) AS uploader_lower,
                   LOWER(target_type) AS target_type_lower,
                   COUNT(*) AS total_cnt,
                   SUM(CASE WHEN status IN ('downloaded', 'uploaded') THEN 1 ELSE 0 END) AS saved_cnt,
                   SUM(CASE WHEN status = 'baseline' THEN 1 ELSE 0 END) AS baseline_cnt
            FROM videos
            GROUP BY site, LOWER(uploader), LOWER(target_type)
            """
        )

        def _empty_counts() -> Dict[str, int]:
            return {"total": 0, "saved": 0, "baseline": 0}

        def _add_counts(bucket: Dict[str, int], total_c: int, saved_c: int, base_c: int) -> None:
            bucket["total"] += total_c
            bucket["saved"] += saved_c
            bucket["baseline"] += base_c

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
            pair_bucket = counts_by_pair.setdefault((s_low, u_low, t_low), _empty_counts())
            _add_counts(pair_bucket, total_c, saved_c, base_c)
            if t_low == "liked" or u_low == "my_liked":
                liked_bucket = liked_counts_by_site.setdefault(s_low, _empty_counts())
                _add_counts(liked_bucket, total_c, saved_c, base_c)
            elif s_low == "xhamster" and (t_low == "favorites" or u_low == "my_favorites"):
                _add_counts(xh_favorites_counts, total_c, saved_c, base_c)
            elif t_low != "favorites" and u_low:
                user_bucket = non_special_counts_by_uploader.setdefault((s_low, u_low), _empty_counts())
                _add_counts(user_bucket, total_c, saved_c, base_c)

        result = (counts_by_pair, non_special_counts_by_uploader, liked_counts_by_site, xh_favorites_counts)
        if version == self.db.video_meta_version:
            self._target_counts_cache = (version, result)
        return result

    async def list_targets(
        self,
        enabled_only: bool = False,
        site: Optional[str] = None,
        *,
        exclude_baseline_pending: bool = False,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        query_params: List[Any] = []
        if enabled_only:
            clauses.append("enabled = 1")
        if exclude_baseline_pending:
            clauses.append("baseline_pending = 0")
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
        today_str = local_today_date()
        (
            counts_by_pair,
            non_special_counts_by_uploader,
            liked_counts_by_site,
            xh_favorites_counts,
        ) = await self._get_target_video_counts()

        def _empty_counts() -> Dict[str, int]:
            return {"total": 0, "saved": 0, "baseline": 0}

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
            r["baseline_pending"] = bool(r["baseline_pending"])
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

            r.pop("today_new_viewkeys", None)
            row_date = str(r.get("today_new_date") or "").strip()
            if row_date == today_str:
                today_new = max(0, int(r.get("today_new_count") or 0))
            elif not row_date:
                checked_dt = _parse_iso_utc(r.get("last_checked_at"))
                checked_today = bool(checked_dt and checked_dt >= today_start_dt)
                today_new = max(0, int(r.get("last_new_count") or 0)) if checked_today else 0
            else:
                today_new = 0
            r["today_new_count"] = today_new
            r["video_total_count"] = matched["total"]
            r["video_saved_count"] = matched["saved"]
            r["video_baseline_count"] = matched["baseline"]
            r["last_error"] = self.redact_diagnostic(r["last_error"])
        return rows

    async def count_targets(self) -> tuple[int, int]:
        row = await self.db.fetch_one("SELECT COUNT(*) AS total, COUNT(CASE WHEN enabled THEN 1 END) AS enabled FROM targets")
        return int(row["total"]), int(row["enabled"])

    async def get_target(
        self, target_id: int, *, conn: Optional[aiosqlite.Connection] = None,
    ) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one("SELECT * FROM targets WHERE id = ?", (target_id,), conn=conn)
        if row:
            row["enabled"] = bool(row["enabled"])
            row["baseline_pending"] = bool(row["baseline_pending"])
            row["site"] = normalize_site(row.get("site"))
            row["last_error"] = self.redact_diagnostic(row["last_error"])
        return row

    async def _find_target_by_identity(
        self,
        norm_site: str,
        norm_user: str,
        norm_type: str,
        *,
        conn: aiosqlite.Connection,
    ) -> Optional[Dict[str, Any]]:
        """Look up a target while respecting same-creator mutually exclusive types."""
        identity_type = {"pornhub": "model", "xhamster": "creator"}[norm_site] if norm_type == "pornstar" else norm_type
        return await self.db.fetch_one(
            f"SELECT * FROM targets WHERE site = ? AND LOWER(TRIM(username)) = LOWER(?) AND ({TARGET_IDENTITY_TYPE}) = ?",
            (norm_site, norm_user, identity_type),
            conn=conn,
        )

    async def _insert_target(
        self, conn: aiosqlite.Connection, site: str, username: str, target_type: str,
        *, enabled: bool = True, preferred_quality: str = "", avatar_url: str = "",
        display_name: str = "", baseline_pending: bool = False,
    ) -> Dict[str, Any]:
        async with conn.execute(
            """
            INSERT INTO targets (
                site, username, target_type, enabled, preferred_quality, avatar_url,
                display_name, created_at, baseline_pending
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                site, username, target_type, int(enabled), preferred_quality.strip(),
                avatar_url.strip(), display_name.strip(), utc_now_iso(), int(baseline_pending),
            ),
        ) as cursor:
            return (await self.get_target(cursor.lastrowid, conn=conn)) or {}

    async def upsert_target(
        self,
        username: str,
        target_type: str = "model",
        enabled: bool = True,
        preferred_quality: str = "",
        avatar_url: str = "",
        display_name: str = "",
        site: str = "pornhub",
        *,
        init_baseline: bool = False,
    ) -> Dict[str, Any]:
        norm_site = normalize_site(site)
        norm_type = (target_type or ("creator" if norm_site == "xhamster" else "model")).strip().lower()
        is_current_user = norm_type == "liked" or (norm_site == "xhamster" and norm_type == "favorites")
        norm_user = "_currentUser_" if is_current_user else username.strip()
        async with self.db.transaction() as conn:
            existing = await self._find_target_by_identity(norm_site, norm_user, norm_type, conn=conn)
            if not existing:
                return await self._insert_target(
                    conn, norm_site, norm_user, norm_type, enabled=enabled,
                    preferred_quality=preferred_quality, avatar_url=avatar_url,
                    display_name=display_name, baseline_pending=init_baseline,
                )
            # A rescan of a target that has already been checked does not pause tracking.
            async with conn.execute(
                """
                UPDATE targets
                SET enabled = ?, preferred_quality = ?,
                    avatar_url = CASE WHEN ? != '' THEN ? ELSE avatar_url END,
                    display_name = CASE WHEN ? != '' THEN ? ELSE display_name END,
                    baseline_pending = CASE WHEN ? AND last_checked_at IS NULL THEN 1 ELSE baseline_pending END
                WHERE id = ?
                """,
                (
                    1 if enabled else 0,
                    preferred_quality.strip(),
                    avatar_url.strip(),
                    avatar_url.strip(),
                    display_name.strip(),
                    display_name.strip(),
                    int(init_baseline),
                    existing["id"],
                ),
            ):
                pass
            return (await self.get_target(existing["id"], conn=conn)) or existing

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
        async with self.db.transaction() as conn:
            existing = await self._find_target_by_identity(norm_site, norm_user, norm_type, conn=conn)
            if not existing:
                created = await self._insert_target(
                    conn, norm_site, norm_user, norm_type, avatar_url=avatar_url, display_name=display_name,
                )
                return created, True
            clean_avatar = avatar_url.strip()
            clean_name = display_name.strip()
            need_avatar = bool(clean_avatar and not str(existing.get("avatar_url") or "").strip())
            need_name = bool(clean_name and not str(existing.get("display_name") or "").strip())
            if need_avatar or need_name:
                async with conn.execute(
                    """
                    UPDATE targets
                    SET avatar_url = CASE WHEN COALESCE(avatar_url, '') = '' AND ? != '' THEN ? ELSE avatar_url END,
                        display_name = CASE WHEN COALESCE(display_name, '') = '' AND ? != '' THEN ? ELSE display_name END
                    WHERE id = ?
                    """,
                    (clean_avatar, clean_avatar, clean_name, clean_name, existing["id"]),
                ):
                    pass
            return ((await self.get_target(existing["id"], conn=conn)) or existing), False

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

    async def mark_unchecked_target_baseline_pending(self, target_id: int) -> None:
        await self.db.execute(
            "UPDATE targets SET baseline_pending = 1 WHERE id = ? AND last_checked_at IS NULL",
            (target_id,),
        )

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
        async with self.db.transaction() as conn:
            existing = await self.get_target(target_id, conn=conn)
            if not existing:
                return None
            site = existing["site"]
            old_username = str(existing.get("username") or "").strip()
            old_type = str(existing.get("target_type") or "model").strip().lower()
            fields: Dict[str, Any] = {
                key: str(patch[key]).strip()
                for key in ("username", "target_type", "preferred_quality", "display_name") if key in patch
            }
            if "target_type" in fields:
                fields["target_type"] = fields["target_type"].lower()
            if "enabled" in patch:
                fields["enabled"] = int(bool(patch["enabled"]))
            if not fields:
                return existing
            sets = ", ".join(f"{key} = ?" for key in fields)
            async with conn.execute(f"UPDATE targets SET {sets} WHERE id = ?", (*fields.values(), target_id)):
                pass

            next_username = fields.get("username", old_username)
            next_type = fields.get("target_type", old_type)
            username_changed = bool(old_username and next_username and old_username.lower() != next_username.lower())
            if "liked" not in (old_type, next_type) and (username_changed or old_type != next_type):
                history_set = "uploader = ?"
                history_where = "site = ? AND LOWER(uploader) = LOWER(?)"
                history_params = (next_username, site, old_username)
                if old_type == next_type == "favorites":
                    history_where += " AND LOWER(target_type) = 'favorites'"
                elif site == "pornhub" and {old_type, next_type} <= {"model", "pornstar"}:
                    history_set += ", target_type = CASE WHEN LOWER(target_type) IN ('model', 'pornstar') THEN ? ELSE target_type END"
                    history_where += " AND LOWER(target_type) NOT IN ('favorites', 'liked')"
                    history_params = (next_username, next_type, site, old_username)
                elif site == "xhamster" and "favorites" not in (old_type, next_type):
                    history_set += ", target_type = ?"
                    history_where += f" AND ({TARGET_IDENTITY_TYPE}) = ?"
                    identity_type = "creator" if old_type == "pornstar" else old_type
                    history_params = (next_username, next_type, site, old_username, identity_type)
                else:
                    return await self.get_target(target_id, conn=conn)
                for table in ("videos", "job_items"):
                    async with conn.execute(f"UPDATE {table} SET {history_set} WHERE {history_where}", history_params):
                        pass
            return await self.get_target(target_id, conn=conn)

    async def mark_target_checked(
        self,
        target_id: int,
        new_count: int,
        error: str = "",
        *,
        new_viewkeys: Optional[Iterable[str]] = None,
    ) -> None:
        today_str = local_today_date()
        today_start_dt = local_day_start_utc()
        existing = await self.db.fetch_one(
            """
            SELECT last_checked_at, last_new_count, today_new_date, today_new_count, today_new_viewkeys
            FROM targets
            WHERE id = ?
            """,
            (target_id,),
        )
        prev_count = 0
        prev_vkeys: List[str] = []
        if existing:
            row_date = str(existing.get("today_new_date") or "").strip()
            if row_date == today_str:
                prev_count = max(0, int(existing.get("today_new_count") or 0))
                try:
                    parsed = json.loads(existing.get("today_new_viewkeys") or "[]")
                    if isinstance(parsed, list):
                        prev_vkeys = [str(k).strip() for k in parsed if str(k or "").strip()]
                except (ValueError, TypeError):
                    prev_vkeys = []
            elif not row_date:
                checked_dt = _parse_iso_utc(existing.get("last_checked_at"))
                if checked_dt and checked_dt >= today_start_dt:
                    prev_count = max(0, int(existing.get("last_new_count") or 0))

        clean_new_count = max(0, int(new_count))
        if new_viewkeys is not None:
            merged_vkeys = list(prev_vkeys)
            seen = set(merged_vkeys)
            added = 0
            for raw_k in new_viewkeys:
                vk = str(raw_k or "").strip()
                if vk and vk not in seen:
                    seen.add(vk)
                    merged_vkeys.append(vk)
                    added += 1
            next_today_count = max(len(merged_vkeys), prev_count + added)
        else:
            merged_vkeys = prev_vkeys
            next_today_count = prev_count + clean_new_count

        await self.db.execute(
            """
            UPDATE targets
            SET last_checked_at = ?,
                last_new_count = ?,
                last_error = ?,
                today_new_date = ?,
                today_new_count = ?,
                today_new_viewkeys = ?
            WHERE id = ?
            """,
            (
                utc_now_iso(),
                clean_new_count,
                self.redact_diagnostic(error),
                today_str,
                next_today_count,
                json.dumps(merged_vkeys, ensure_ascii=False),
                target_id,
            ),
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
    async def get_known_viewkeys(self, viewkeys: Optional[Iterable[str]] = None, *, uploaded_only: bool = False) -> Set[str]:
        if viewkeys is None:
            rows = await self.db.fetch_all("SELECT viewkey FROM videos")
            return {r["viewkey"] for r in rows}
        vlist = [v for v in set(viewkeys) if v]
        found: Set[str] = set()
        status_filter = " AND status = 'uploaded'" if uploaded_only else ""
        for start in range(0, len(vlist), 400):
            batch = vlist[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = await self.db.fetch_all(f"SELECT viewkey FROM videos WHERE viewkey IN ({placeholders}){status_filter}", batch)
            found.update(r["viewkey"] for r in rows)
        return found

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

    async def get_videos_by_viewkeys(
        self, viewkeys: Iterable[str], *, conn: Optional[aiosqlite.Connection] = None,
    ) -> List[Dict[str, Any]]:
        unique_keys = [k for k in dict.fromkeys(str(v).strip() for v in viewkeys) if k]
        if not unique_keys:
            return []
        placeholders = ",".join("?" for _ in unique_keys)
        return await self.db.fetch_all(
            f"SELECT * FROM videos WHERE viewkey IN ({placeholders})",
            unique_keys, conn=conn,
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
            WHERE {_BASELINE_METADATA_FILTER} {where_site}
            """,
            params,
        )
        return int(row["cnt"]) if row else 0

    async def list_baseline_metadata_candidates(
        self,
        limit: int = 0,
        site: str = "",
        *,
        resume_job_id: Optional[int] = None,
        site_only: bool = False,
    ) -> List[Dict[str, Any]]:
        site_filter = normalize_site(site) if (site or "").strip() else ""
        where_site = " AND LOWER(COALESCE(site, 'pornhub')) = ?" if site_filter else ""
        sql = f"""
            SELECT viewkey, site, title, uploader, target_type, quality, file_size, video_page_url
            FROM videos
            WHERE {_BASELINE_METADATA_FILTER} {where_site}
        """
        params: List[Any] = [site_filter] if site_filter else []
        if resume_job_id is not None:
            sql += """
              AND NOT EXISTS (
                  SELECT 1 FROM job_items WHERE job_id = ? AND viewkey = videos.viewkey AND status IN ('done', 'skipped')
              )
              ORDER BY EXISTS (
                  SELECT 1 FROM job_items WHERE job_id = ? AND viewkey = videos.viewkey
              ) DESC, created_at ASC, viewkey ASC
            """
            params.extend((resume_job_id, resume_job_id))
        else:
            sql += " ORDER BY created_at ASC, viewkey ASC"
        if limit > 0:
            if resume_job_id is None:
                sql += " LIMIT ?"
                params.append(int(limit))
            else:
                sql += " LIMIT MAX(0, ? - (SELECT COUNT(*) FROM job_items WHERE job_id = ? AND status IN ('done', 'skipped')))"
                params.extend((int(limit), resume_job_id))
        if site_only:
            sql = f"SELECT DISTINCT site FROM ({sql})"
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
            viewkey, site, title, uploader, uploader_display_name, target_type, quality, video_page_url,
            status, local_path, remote_path, file_size, downloaded_at, uploaded_at, created_at,
            upload_requested, published_at, duration_seconds
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            uploader_display_name = CASE
                WHEN excluded.uploader_display_name = '' THEN videos.uploader_display_name
                WHEN videos.uploader_display_name = '' THEN excluded.uploader_display_name
                WHEN ? IN ('favorites', 'liked') AND videos.target_type NOT IN ('favorites', 'liked') THEN videos.uploader_display_name
                WHEN ? = 0 AND excluded.status = 'baseline' AND videos.status IN ('downloaded', 'uploaded') THEN videos.uploader_display_name
                ELSE excluded.uploader_display_name
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
                WHEN excluded.status = 'uploaded' THEN ''
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
        uploader_display_name: str = "",
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
        insert_url = explicit_url or build_video_page_url(insert_site, viewkey)
        force_flag = 1 if force_status else 0
        return (
            viewkey,
            insert_site,
            title or "",
            uploader or "",
            (uploader_display_name or "").strip(),
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
            None if upload_requested is None else int(upload_requested),
        )

    async def upsert_video(
        self,
        viewkey: str,
        *,
        site: str = "",
        title: str = "",
        uploader: str = "",
        uploader_display_name: str = "",
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
            uploader_display_name=uploader_display_name,
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
                    uploader_display_name=str(rec.get("uploader_display_name") or ""),
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

    async def update_videos_uploader_display_name(
        self,
        site: str,
        uploader: str,
        display_name: str,
    ) -> None:
        clean_u = (uploader or "").strip()
        clean_name = (display_name or "").strip()
        if not clean_u or not clean_name:
            return
        await self.db.execute(
            """
            UPDATE videos
            SET uploader_display_name = ?
            WHERE LOWER(site) = ? AND LOWER(uploader) = LOWER(?) AND uploader_display_name = ''
            """,
            (clean_name, normalize_site(site), clean_u),
        )

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

    async def record_verified_upload(
        self, viewkey: str, local_path: str, remote_path: str, *, item_id: Optional[int] = None,
    ) -> bool:
        """Persist the verified destination before it is safe to remove a local copy."""
        now = utc_now_iso()
        async with self.db.transaction() as conn:
            async with conn.execute(
                """
                UPDATE videos SET status='uploaded', remote_path=?, uploaded_at=?, upload_requested=0
                WHERE viewkey=? AND local_path=?
                """,
                (remote_path, now, viewkey, local_path),
            ) as cursor:
                if not cursor.rowcount:
                    return False
            if item_id is not None:
                async with conn.execute(
                    """
                    UPDATE job_items SET status='done', local_path=?, upload_requested=0, error_message='', updated_at=?
                    WHERE id=? AND job_items.viewkey=? AND status='uploading'
                    """,
                    (local_path, now, item_id, viewkey),
                ) as cursor:
                    if not cursor.rowcount:
                        raise RuntimeError("上传条目状态已变更")
            return True

    async def clear_uploaded_local_path(self, viewkey: str, local_path: str) -> None:
        await self.db.execute(
            "UPDATE videos SET local_path='' WHERE viewkey=? AND local_path=? AND status='uploaded'",
            (viewkey, local_path),
        )

    async def delete_video(self, viewkey: str) -> bool:
        return bool(await self.delete_videos([viewkey]))

    async def delete_videos(
        self, viewkeys: List[str], *, conn: Optional[aiosqlite.Connection] = None,
    ) -> List[str]:
        unique_keys = list(dict.fromkeys(
            str(key).strip() for key in viewkeys if str(key).strip()
        ))
        if not unique_keys:
            return []
        placeholders = ",".join("?" for _ in unique_keys)
        async with (self.db.transaction() if conn is None else nullcontext(conn)) as write_conn:
            rows = await self.db.fetch_all(
                f"DELETE FROM videos WHERE viewkey IN ({placeholders}) RETURNING viewkey",
                unique_keys, conn=write_conn,
            )
        deleted = {row["viewkey"] for row in rows}
        return [key for key in unique_keys if key in deleted]

    async def video_cleanup_blocked(
        self, viewkeys: Iterable[str], *, conn: aiosqlite.Connection, include_ledger: bool = True,
    ) -> Set[str]:
        keys = tuple(viewkeys)
        if not keys:
            return set()
        placeholders = ",".join("?" for _ in keys)
        rows = await self.db.fetch_all(
            f"""
            SELECT viewkey FROM videos WHERE ? AND viewkey IN ({placeholders})
            UNION
            SELECT i.viewkey FROM job_items i JOIN jobs j ON j.id = i.job_id
            WHERE i.viewkey IN ({placeholders}) AND i.status IN ('downloading', 'uploading')
              AND j.status IN ('scanning', 'running')
            """,
            (include_ledger, *keys, *keys), conn=conn,
        )
        return {row["viewkey"] for row in rows}

    async def _get_target_display_map(
        self,
        *,
        conn: Optional[aiosqlite.Connection] = None,
        snapshot_version: Optional[int] = None,
    ) -> Dict[Tuple[str, str], str]:
        version = snapshot_version if snapshot_version is not None else self.db.video_meta_version
        allow_cache = conn is None or (
            snapshot_version is not None and self.db.video_meta_version == snapshot_version
        )
        cached = self._display_map_cache
        if allow_cache and cached is not None and cached[0] == version:
            return cached[1]
        rows = await self.db.fetch_all(
            """
            SELECT LOWER(site) AS site_low, LOWER(username) AS uname, display_name
            FROM targets
            WHERE display_name != ''
            ORDER BY id ASC
            """,
            conn=conn,
        )
        mapping: Dict[Tuple[str, str], str] = {}
        for r in rows:
            s_low = normalize_site(r.get("site_low"))
            uname = r["uname"]
            if uname and (s_low, uname) not in mapping:
                mapping[(s_low, uname)] = r["display_name"]
        if (conn is None or snapshot_version is not None) and version == self.db.video_meta_version:
            self._display_map_cache = (version, mapping)
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
        known_total: Optional[int] = None,
        conn: Optional[aiosqlite.Connection] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        clauses = []
        params: List[Any] = []
        site_filter = normalize_site(site) if (site or "").strip() else ""
        if site_filter:
            clauses.append("v.site = ?")
            params.append(site_filter)
        search_clean = search.strip()
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

        if (sort_by or "").strip().lower() == "published":
            order_sql = (
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
        async with (self.db.read() if conn is None else nullcontext(conn)) as read_conn:
            if search_clean:
                like = f"%{search_clean}%"
                target_rows = await self.db.fetch_all(
                    (
                        "SELECT DISTINCT site, LOWER(TRIM(username)) AS uname FROM targets "
                        "WHERE display_name LIKE ? AND site = ?"
                        if site_filter
                        else "SELECT DISTINCT site, LOWER(TRIM(username)) AS uname FROM targets "
                        "WHERE display_name LIKE ?"
                    ),
                    (like, site_filter) if site_filter else (like,),
                    conn=read_conn,
                )
                matched_by_site: Dict[str, List[str]] = {}
                for trow in target_rows:
                    uname = str(trow.get("uname") or "").strip()
                    if uname:
                        matched_by_site.setdefault(normalize_site(trow.get("site")), []).append(uname)

                search_parts = [
                    "v.title LIKE ?",
                    "v.viewkey LIKE ?",
                    "v.uploader LIKE ?",
                    "v.uploader_display_name LIKE ?",
                ]
                search_params: List[Any] = [like, like, like, like]
                for matched_site, unames in matched_by_site.items():
                    placeholders = ",".join("?" for _ in unames)
                    if site_filter:
                        search_parts.append(f"LOWER(TRIM(v.uploader)) IN ({placeholders})")
                        search_params.extend(unames)
                    else:
                        search_parts.append(f"(v.site = ? AND LOWER(TRIM(v.uploader)) IN ({placeholders}))")
                        search_params.append(matched_site)
                        search_params.extend(unames)
                clauses.append(f"({' OR '.join(search_parts)})")
                params.extend(search_params)

            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            if known_total is not None:
                total = int(known_total)
            else:
                total_row = await self.db.fetch_one(
                    f"SELECT COUNT(*) AS cnt FROM videos v {where_sql}",
                    params,
                    conn=read_conn,
                )
                total = int(total_row["cnt"]) if total_row else 0
            rows = await self.db.fetch_all(
                f"""
                SELECT v.*, (
                    SELECT t.display_name FROM targets t
                    WHERE t.site = v.site AND LOWER(TRIM(t.username)) = LOWER(TRIM(v.uploader))
                      AND t.display_name != ''
                    ORDER BY t.id LIMIT 1
                ) AS target_display_name
                FROM videos v
                {where_sql}
                ORDER BY {order_sql}
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
                conn=read_conn,
            )
        for row in rows:
            s_low = normalize_site(row.get("site"))
            row["site"] = s_low
            uname = str(row.get("uploader") or "").strip().lower()
            t_low = str(row.get("target_type") or "").strip().lower()
            stored_dn = str(row.get("uploader_display_name") or "").strip()
            target_display_name = row.pop("target_display_name")
            if uname == "my_liked" or (not uname and t_low == "liked"):
                row["uploader_display_name"] = "Liked Videos"
            elif s_low == "xhamster" and (uname == "my_favorites" or (not uname and t_low == "favorites")):
                row["uploader_display_name"] = "xHamster Favorites"
            else:
                row["uploader_display_name"] = target_display_name or stored_dn
        return rows, total

    async def get_video_filter_facets(
        self,
        site: str = "",
        *,
        conn: Optional[aiosqlite.Connection] = None,
        snapshot_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        site_filter = normalize_site(site) if (site or "").strip() else ""
        version = snapshot_version if snapshot_version is not None else self.db.video_meta_version
        allow_cache = conn is None or (
            snapshot_version is not None and self.db.video_meta_version == snapshot_version
        )
        cached = self._facets_cache.get(site_filter)
        if allow_cache and cached is not None and cached[0] == version:
            data = cached[1]
            return {
                "sites": list(data["sites"]),
                "uploaders": list(data["uploaders"]),
                "uploader_labels": dict(data["uploader_labels"]),
                "qualities": list(data["qualities"]),
            }

        where_site = " AND site = ?" if site_filter else ""
        site_params = (site_filter,) if site_filter else ()
        async with (self.db.read() if conn is None else nullcontext(conn)) as read_conn:
            uploader_rows = await self.db.fetch_all(
                f"""
                SELECT COALESCE(MIN(CASE WHEN uploader != LOWER(uploader) THEN uploader END), MIN(uploader)) AS uploader,
                       site,
                       MAX(CASE WHEN uploader_display_name != '' THEN uploader_display_name ELSE '' END) AS video_display_name
                FROM videos
                WHERE uploader != ''{where_site}
                GROUP BY site, LOWER(uploader)
                ORDER BY LOWER(uploader) ASC
                """,
                site_params,
                conn=read_conn,
            )
            has_liked_row = await self.db.fetch_one(
                f"SELECT 1 AS hit FROM videos WHERE LOWER(target_type) = 'liked'{where_site} LIMIT 1",
                site_params,
                conn=read_conn,
            )
            qualities = await self.db.fetch_all(
                f"""
                SELECT DISTINCT quality FROM videos
                WHERE quality != ''{where_site}
                ORDER BY CAST(REPLACE(REPLACE(LOWER(quality), 'p', ''), 'k', '') AS INTEGER) DESC,
                         quality DESC
                """,
                site_params,
                conn=read_conn,
            )
            site_rows = await self.db.fetch_all(
                "SELECT DISTINCT site FROM videos WHERE site != '' ORDER BY site ASC",
                conn=read_conn,
            )
            if conn is not None:
                display_map = await self._get_target_display_map(conn=read_conn, snapshot_version=snapshot_version)
        if conn is None:
            display_map = await self._get_target_display_map()
        video_display_map: Dict[Tuple[str, str], str] = {}
        uploaders: List[str] = []
        seen_up: Dict[str, int] = {}
        uploader_sites: Dict[str, str] = {}
        for r in uploader_rows:
            u = str(r["uploader"] or "").strip()
            if not u:
                continue
            low = u.lower()
            s_low = normalize_site(r.get("site"))
            vdn = str(r.get("video_display_name") or "").strip()
            if vdn and (s_low, low) not in video_display_map:
                video_display_map[(s_low, low)] = vdn
            idx = seen_up.get(low)
            if idx is None:
                seen_up[low] = len(uploaders)
                uploaders.append(u)
                uploader_sites[low] = s_low
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
                    or video_display_map.get((s_low, low))
                    or display_map.get(("pornhub", low))
                    or video_display_map.get(("pornhub", low))
                    or display_map.get(("xhamster", low))
                    or video_display_map.get(("xhamster", low))
                    or u
                )
        result = {
            "sites": [normalize_site(r["site"]) for r in site_rows],
            "uploaders": uploaders,
            "uploader_labels": uploader_labels,
            "qualities": [r["quality"] for r in qualities],
        }
        if (conn is None or snapshot_version is not None) and version == self.db.video_meta_version:
            if len(self._facets_cache) >= 4:
                self._facets_cache.clear()
            self._facets_cache[site_filter] = (version, result)
        return {
            "sites": list(result["sites"]),
            "uploaders": list(result["uploaders"]),
            "uploader_labels": dict(result["uploader_labels"]),
            "qualities": list(result["qualities"]),
        }

    async def get_video_stats_with_cache_info(
        self,
        site: str = "",
        *,
        conn: Optional[aiosqlite.Connection] = None,
        snapshot_version: Optional[int] = None,
    ) -> Tuple[Dict[str, int], bool]:
        site_filter = normalize_site(site) if (site or "").strip() else ""
        version = snapshot_version if snapshot_version is not None else self.db.video_meta_version
        today_key = local_today_date()
        allow_cache = conn is None or (
            snapshot_version is not None and self.db.video_meta_version == snapshot_version
        )
        cached = self._stats_cache.get(site_filter)
        if allow_cache and cached is not None and cached[0] == version and cached[1] == today_key:
            return dict(cached[2]), True

        where_site = " WHERE site = ?" if site_filter else ""
        cutoff_7d = local_date_days_ago(7)
        params: Tuple[Any, ...] = (cutoff_7d, site_filter) if site_filter else (cutoff_7d,)
        row = await self.db.fetch_one(
            f"""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status = 'uploaded' THEN 1 ELSE 0 END), 0) AS uploaded,
                COALESCE(SUM(CASE WHEN status = 'downloaded' THEN 1 ELSE 0 END), 0) AS downloaded,
                COALESCE(SUM(CASE WHEN status = 'baseline' THEN 1 ELSE 0 END), 0) AS baseline,
                COALESCE(SUM(CASE WHEN published_at != '' AND published_at >= ? THEN 1 ELSE 0 END), 0) AS published_7d,
                COALESCE(SUM(CASE WHEN {_BASELINE_METADATA_FILTER} THEN 1 ELSE 0 END), 0) AS baseline_missing_metadata
            FROM videos
            {where_site}
            """,
            params,
            conn=conn,
        )
        counts = {
            "total": int(row["total"]) if row else 0,
            "uploaded": int(row["uploaded"]) if row else 0,
            "downloaded": int(row["downloaded"]) if row else 0,
            "baseline": int(row["baseline"]) if row else 0,
            "published_7d": int(row["published_7d"]) if row else 0,
            "baseline_missing_metadata": int(row["baseline_missing_metadata"]) if row else 0,
        }
        if (conn is None or snapshot_version is not None) and version == self.db.video_meta_version:
            if len(self._stats_cache) >= 4:
                self._stats_cache.clear()
            self._stats_cache[site_filter] = (version, today_key, counts)
        return dict(counts), False

    async def get_video_stats(
        self,
        site: str = "",
        *,
        conn: Optional[aiosqlite.Connection] = None,
        snapshot_version: Optional[int] = None,
    ) -> Dict[str, int]:
        counts, _ = await self.get_video_stats_with_cache_info(
            site=site, conn=conn, snapshot_version=snapshot_version,
        )
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

    async def create_scheduled_job(self, date: str) -> Optional[Dict[str, Any]]:
        async with self.db.transaction() as conn:
            if await self.get_scheduler_last_run_date(conn=conn) == date:
                return None
            now = utc_now_iso()
            async with conn.execute(
                "INSERT INTO jobs (kind, trigger, label, created_at) VALUES ('auto_update', 'schedule', '定时自动追更', ?)",
                (now,),
            ) as cursor:
                job_id = cursor.lastrowid
            async with conn.execute(
                """
                INSERT INTO settings (key, value_json, updated_at) VALUES ('scheduler_state', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
                """,
                (json.dumps({"last_run_date": date}), now),
            ):
                pass
            return await self.get_job(job_id, conn=conn)

    async def get_job(self, job_id: int, *, conn: Optional[aiosqlite.Connection] = None) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,), conn=conn)
        if row:
            row["params"] = _parse_job_params(row["params_json"])
            row["error_message"] = self.redact_diagnostic(row["error_message"])
        return row

    async def list_jobs(self, limit: int = 30) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            """
            SELECT * FROM jobs
            ORDER BY CASE WHEN status IN ('running', 'scanning') THEN 0 WHEN status = 'queued' THEN 1 ELSE 2 END,
                     COALESCE(finished_at, created_at) DESC, id DESC LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        )
        for row in rows:
            row["params"] = _parse_job_params(row["params_json"])
            row["error_message"] = self.redact_diagnostic(row["error_message"])
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
        async with self.db.transaction() as conn:
            job = await self.get_job(job_id, conn=conn)
            if job is None or job["status"] != "queued":
                return None
            mode = job["params"].get("resume_mode")
            message = "用户取消排队任务，扫描未完成" if mode == "rescan" else "用户取消排队任务"
            return await self._finish_job(conn, job, "cancelled", message, cancel_items=True)

    async def requeue_job_in_place(
        self,
        job_id: int,
        *,
        params_patch: Dict[str, Any],
        clear_export_text: bool = False,
        item_resets: Optional[Dict[int, Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self.db.transaction() as conn:
            async with conn.execute("SELECT params_json FROM jobs WHERE id = ?", (job_id,)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            params = _parse_job_params(row["params_json"])
            try:
                resume_count = int(params.get("resume_count") or 0)
            except (TypeError, ValueError):
                resume_count = 0
            params.update(params_patch)
            params["resume_count"] = resume_count + 1
            export_reset = ", export_text = ''" if clear_export_text else ""
            async with conn.execute(
                f"""
                UPDATE jobs SET status = 'queued', error_message = '', started_at = NULL, finished_at = NULL,
                                params_json = ? {export_reset}
                WHERE id = ? AND status NOT IN ('queued', 'scanning', 'running') RETURNING id
                """,
                (json.dumps(params, ensure_ascii=False), job_id),
            ) as cursor:
                if await cursor.fetchone() is None:
                    return None
            if item_resets:
                now = utc_now_iso()
                async with conn.executemany(
                    """
                    UPDATE job_items SET status = 'pending', error_message = '', updated_at = ?,
                           temp_path = COALESCE(?, temp_path), bytes_downloaded = COALESCE(?, bytes_downloaded),
                           upload_requested = COALESCE(?, upload_requested)
                    WHERE job_id = ? AND id = ?
                      AND (status IN ('failed', 'cancelled', 'pending', 'downloading', 'uploading')
                           OR (status = 'downloaded' AND upload_requested IS NOT 0))
                    """,
                    (
                        (now, patch.get("temp_path"), patch.get("bytes_downloaded"), patch.get("upload_requested"),
                         job_id, item_id)
                        for item_id, patch in item_resets.items()
                    ),
                ):
                    pass
                async with conn.execute(_RECALCULATE_JOB_COUNTS, (job_id,)):
                    pass
            return await self.get_job(job_id, conn=conn)

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
                params.append(self.redact_diagnostic(v) if k == "error_message" else v)
        if sets:
            params.append(job_id)
            await self.db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
        return await self.get_job(job_id)

    async def merge_job_params(self, job_id: int, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with self.db.transaction(touches_video_meta=False) as conn:
            job = await self.get_job(job_id, conn=conn)
            if not job:
                return None
            merged = {**job["params"], **patch}
            async with conn.execute(
                "UPDATE jobs SET params_json = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False), job_id),
            ):
                pass
            return await self.get_job(job_id, conn=conn)

    async def delete_job(self, job_id: int) -> Tuple[bool, str]:
        result = await self.delete_jobs([job_id])
        if result["deleted_ids"]:
            return True, ""
        return False, "active" if result["skipped_active_ids"] else "not_found"

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

        deletable: List[int] = []
        skipped_active: List[int] = []
        async with self.db.transaction() as conn:
            for start in range(0, len(unique_ids), 400):
                chunk = unique_ids[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                deleted = await self.db.fetch_all(
                    f"""DELETE FROM jobs WHERE id IN ({placeholders})
                        AND status NOT IN ('queued', 'scanning', 'running') RETURNING id""",
                    chunk, conn=conn,
                )
                active = await self.db.fetch_all(
                    f"SELECT id FROM jobs WHERE id IN ({placeholders})", chunk, conn=conn,
                )
                deletable.extend(row["id"] for row in deleted)
                skipped_active.extend(row["id"] for row in active)
        return {"deleted_ids": deletable, "skipped_active_ids": skipped_active}

    async def prune_old_jobs(self, max_finished_jobs: int = 200) -> int:
        if max_finished_jobs <= 0:
            return 0
        async with self.db.transaction() as conn:
            async with conn.execute(
                """
                DELETE FROM jobs WHERE id IN (
                    SELECT id FROM jobs WHERE status NOT IN ('queued', 'scanning', 'running')
                    ORDER BY COALESCE(finished_at, created_at) DESC, id DESC LIMIT -1 OFFSET ?
                )
                """,
                (int(max_finished_jobs),),
            ) as cursor:
                return cursor.rowcount

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
                bytes_downloaded, bytes_total, updated_at, upload_requested, error_message,
                published_at, duration_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING *
        """
        for start in range(0, len(items), step):
            batch = items[start : start + step]
            async with self.db.transaction(touches_video_meta=False) as conn:
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
                        int(spec.get("bytes_total") or 0),
                        now,
                        None if up_req is None else int(bool(up_req)),
                        self.redact_diagnostic(spec.get("error_message", "")),
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

    async def list_job_items(self, job_id: int, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status is not None:
            return await self.db.fetch_all(
                "SELECT * FROM job_items WHERE job_id = ? AND status = ? ORDER BY id ASC",
                (job_id, status),
            )
        return await self.db.fetch_all(
            "SELECT * FROM job_items WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        )

    async def list_retryable_job_items(self, job_id: int, *, auto_upload: bool) -> List[Dict[str, Any]]:
        return await self.db.fetch_all(
            """
            SELECT * FROM job_items WHERE job_id = ?
              AND (status IN ('failed', 'cancelled', 'pending', 'downloading', 'uploading')
                   OR (? AND status = 'downloaded' AND upload_requested IS NOT 0))
            ORDER BY id ASC
            """,
            (job_id, auto_upload),
        )

    async def get_job_detail(self, job_id: int) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        async with self.db.read() as conn:
            job = await self.get_job(job_id, conn=conn)
            items = await self.db.fetch_all(
                "SELECT * FROM job_items WHERE job_id = ? ORDER BY id ASC", (job_id,), conn=conn,
            ) if job else []
            for item in items:
                err = item.get("error_message")
                if err:
                    item["error_message"] = self.redact_diagnostic(err)
            return job, items

    async def get_job_detail_page(
        self,
        job_id: int,
        *,
        page: int = 1,
        page_size: int = 50,
        status: str = "",
    ) -> Optional[Dict[str, Any]]:
        status_filter = (status or "").strip()
        if status_filter == "all":
            status_filter = ""
        safe_page_size = max(1, min(int(page_size or 50), 200))
        req_page = max(1, int(page or 1))

        async with self.db.read() as conn:
            job = await self.get_job(job_id, conn=conn)
            if not job:
                return None
            count_rows = await self.db.fetch_all(
                "SELECT status, COUNT(*) AS cnt FROM job_items WHERE job_id = ? GROUP BY status",
                (job_id,),
                conn=conn,
            )
            status_counts: Dict[str, int] = {}
            total_all = 0
            for row in count_rows:
                st = str(row.get("status") or "").strip()
                cnt = int(row.get("cnt") or 0)
                if st and cnt > 0:
                    status_counts[st] = cnt
                total_all += cnt

            filtered_total = status_counts.get(status_filter, 0) if status_filter else total_all
            max_page = max(1, (filtered_total + safe_page_size - 1) // safe_page_size)
            safe_page = min(req_page, max_page)
            offset = (safe_page - 1) * safe_page_size

            if filtered_total == 0:
                items: List[Dict[str, Any]] = []
            elif status_filter:
                items = await self.db.fetch_all(
                    "SELECT * FROM job_items WHERE job_id = ? AND status = ? ORDER BY id ASC LIMIT ? OFFSET ?",
                    (job_id, status_filter, safe_page_size, offset),
                    conn=conn,
                )
            else:
                items = await self.db.fetch_all(
                    "SELECT * FROM job_items WHERE job_id = ? ORDER BY id ASC LIMIT ? OFFSET ?",
                    (job_id, safe_page_size, offset),
                    conn=conn,
                )

            for item in items:
                err = item.get("error_message")
                if err:
                    item["error_message"] = self.redact_diagnostic(err)

            return {
                "job": job,
                "items": items,
                "total": filtered_total,
                "total_all": total_all,
                "page": safe_page,
                "page_size": safe_page_size,
                "status": status_filter or "all",
                "status_counts": status_counts,
            }

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
                params.append(self.redact_diagnostic(v) if k == "error_message" else v)
        params.append(item_id)
        await self.db.execute(f"UPDATE job_items SET {', '.join(sets)} WHERE id = ?", params)
        return await self.get_job_item(item_id)

    async def cancel_job_uploads(self, job_id: int, item_ids: Iterable[int], message: str) -> None:
        reason, now = self.redact_diagnostic(message), utc_now_iso()
        params = [(reason, now, job_id, item_id) for item_id in item_ids]
        if params:
            await self.db.executemany(
                """
                UPDATE job_items SET status = 'cancelled', error_message = ?, updated_at = ?
                WHERE job_id = ? AND id = ? AND status IN ('downloaded', 'uploading')
                """,
                params,
            )

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
                async with conn.execute(
                    f"""
                    UPDATE job_items
                    SET status = 'cancelled', error_message = ?, updated_at = ?
                    WHERE job_id = ? AND status = 'pending' AND id IN ({placeholders})
                    """,
                    [error_message, now, int(job_id), *chunk],
                ):
                    pass
        return len(ids)

    async def recalculate_job_counts(self, job_id: int) -> Optional[Dict[str, Any]]:
        await self.db.execute(_RECALCULATE_JOB_COUNTS, (job_id,))
        return await self.get_job(job_id)

    async def finish_job(
        self, job_id: int, status: str, error_message: str = "", *, cancel_items: bool = False,
        cancel_requested: Optional[Callable[[], bool]] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self.db.transaction() as conn:
            job = await self.get_job(job_id, conn=conn)
            if job is None or job["status"] not in {"scanning", "running"}:
                return None
            if status == "completed" and cancel_requested is not None and cancel_requested():
                status, error_message, cancel_items = "cancelled", "已取消", True
            return await self._finish_job(conn, job, status, error_message, cancel_items=cancel_items)

    async def _finish_job(
        self, conn: aiosqlite.Connection, job: Dict[str, Any], status: str, message: str, *, cancel_items: bool,
    ) -> Optional[Dict[str, Any]]:
        now = utc_now_iso()
        job_id = job["id"]
        message = self.redact_diagnostic(message)
        item_message = message or ("已取消" if cancel_items else "任务结束时条目尚未处理")
        async with conn.execute(
            """
            UPDATE job_items SET status = ?, error_message = ?, updated_at = ?
            WHERE job_id = ? AND status IN ('pending', 'downloading', 'uploading')
            """,
            ("cancelled" if cancel_items else "failed", item_message, now, job_id),
        ) as cursor:
            if cursor.rowcount and status == "completed":
                status, message = "partial_failed", item_message
        if job_uses_item_counts(job):
            async with conn.execute(_RECALCULATE_JOB_COUNTS, (job_id,)):
                pass
        job["params"].pop("resume_mode", None)
        async with conn.execute(
            "UPDATE jobs SET status = ?, error_message = ?, finished_at = ?, params_json = ? WHERE id = ?",
            (status, message, now, json.dumps(job["params"], ensure_ascii=False), job_id),
        ):
            pass
        if status == "completed" and job["kind"] == "baseline_sync":
            async with conn.execute(
                "UPDATE targets SET baseline_pending = 0 WHERE id = ?", (job["params"].get("target_id"),),
            ):
                pass
        return await self.get_job(job_id, conn=conn)

    async def reconcile_metadata_job_items(self, job_id: int) -> None:
        await self.db.execute(
            f"""
            UPDATE job_items
            SET status = CASE WHEN NOT EXISTS (
                    SELECT 1 FROM videos WHERE videos.viewkey = job_items.viewkey AND videos.status = 'baseline'
                ) THEN 'skipped' WHEN EXISTS (
                    SELECT 1 FROM videos WHERE videos.viewkey = job_items.viewkey AND {_BASELINE_METADATA_FILTER}
                ) THEN 'pending' ELSE 'done' END,
                error_message = '', updated_at = ?
            WHERE job_id = ? AND status IN ('pending', 'failed', 'cancelled')
            """,
            (utc_now_iso(), job_id),
        )

    async def recover_interrupted_jobs(self) -> int:
        """Mark interrupted work cancelled; unstarted jobs remain durably queued."""
        async with self.db.transaction() as conn:
            active = await self.db.fetch_all(
                "SELECT id, kind, status, error_message, params_json FROM jobs WHERE status IN ('scanning', 'running')",
                conn=conn,
            )
            for job in active:
                job["params"] = _parse_job_params(job["params_json"])
                message = "服务重启中断，可点击续传或重试"
                if job_scan_incomplete(job) or any(s in job["error_message"] for s in ("扫描未完成", "目标检查失败")):
                    message = f"扫描未完成，{message}"
                await self._finish_job(conn, job, "cancelled", message, cancel_items=True)
            return len(active)
