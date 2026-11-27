"""Regression tests for code-review findings across auth, repo, jobs, targets, and task_manager."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import targets as targets_api
from app.api.auth import _extract_client_ip
from app.config import EnvSettings, RuntimeSettings
from app.db.database import Database
from app.db.repo import Repository
from app.engine import task_manager as engine
from app.engine.events import EventBus
from app.main import create_app
from app.parsers.list_scanner import ListedVideo
from app.parsers.video import VideoDetail
from app.services.cookie import CookieCheckResult
from app.services.downloader import DownloadOutcome
from tests.media_fixtures import mp4_payload


def _make_client(tmp_path: Path) -> TestClient:
    env = EnvSettings(
        data_dir=tmp_path,
        admin_password="super-secret-password",
        secret_key="test-secret-key",
        cookie_secure=False,
    )
    app = create_app(env)
    return TestClient(app)


def test_extract_client_ip_prefers_xff_over_overwritten_gateway_real_ip_and_handles_ula():
    class _Client:
        def __init__(self, host: str):
            self.host = host

    class _Req:
        def __init__(self, host: str, headers: dict):
            self.client = _Client(host)
            self.headers = headers

    # OpenResty -> frontend Nginx where X-Real-IP was bridge IP, XFF has true client IP
    assert (
        _extract_client_ip(
            _Req(
                "172.18.0.3",
                {
                    "X-Real-IP": "172.18.0.1",
                    "X-Forwarded-For": "203.0.113.10, 172.18.0.1",
                },
            )
        )
        == "203.0.113.10"
    )
    # LAN / VPN client behind OpenResty + Nginx (all private hops -> leftmost client hop, not bridge IP)
    assert (
        _extract_client_ip(
            _Req(
                "172.18.0.3",
                {
                    "X-Real-IP": "172.18.0.1",
                    "X-Forwarded-For": "10.0.0.42, 172.18.0.1",
                },
            )
        )
        == "10.0.0.42"
    )
    # IPv6 ULA trusted proxy + IPv6 client
    assert (
        _extract_client_ip(
            _Req(
                "fd00::2",
                {"X-Forwarded-For": "2001:db8::5, fd00::1"},
            )
        )
        == "2001:db8::5"
    )
    # Invalid spoofed header values are ignored
    assert (
        _extract_client_ip(
            _Req("172.18.0.3", {"X-Real-IP": "not-an-ip", "X-Forwarded-For": "garbage"})
        )
        == "172.18.0.3"
    )
    # Direct public IP ignores forwarded headers
    assert (
        _extract_client_ip(
            _Req("198.51.100.9", {"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8"})
        )
        == "198.51.100.9"
    )


async def test_collection_upserts_do_not_overwrite_original_creator_ownership(tmp_path: Path):
    db = Database(tmp_path / "ownership.db")
    await db.connect()
    repo = Repository(db)
    try:
        # Creator video already uploaded
        await repo.upsert_video(
            "vk_owned",
            title="Alice Video",
            uploader="alice",
            target_type="model",
            status="uploaded",
            remote_path="pikpak:Pornhub/alice/clip.mp4",
            force_status=True,
        )
        # Liked baseline sync must NOT change target_type to 'liked' or uploader to 'My_Liked'
        await repo.upsert_video(
            "vk_owned",
            title="Alice Video",
            uploader="My_Liked",
            target_type="liked",
            status="baseline",
        )
        v1 = await repo.get_video("vk_owned")
        assert v1 is not None
        assert v1["uploader"] == "alice"
        assert v1["target_type"] == "model"
        assert v1["status"] == "uploaded"

        # Favorites auto-update download must NOT steal uploader/target_type from original creator
        await repo.upsert_video(
            "vk_owned",
            title="Alice Video",
            uploader="my_account",
            target_type="favorites",
            status="downloaded",
            force_status=True,
        )
        v2 = await repo.get_video("vk_owned")
        assert v2 is not None
        assert v2["uploader"] == "alice"
        assert v2["target_type"] == "model"

        # Reverse: video initially recorded only from liked baseline upgrades to creator once downloaded from creator
        await repo.upsert_video(
            "vk_liked_first",
            title="Liked First",
            uploader="My_Liked",
            target_type="liked",
            status="baseline",
        )
        await repo.upsert_video(
            "vk_liked_first",
            title="Liked First",
            uploader="bob",
            target_type="pornstar",
            status="downloaded",
            force_status=True,
        )
        v3 = await repo.get_video("vk_liked_first")
        assert v3 is not None
        assert v3["uploader"] == "bob"
        assert v3["target_type"] == "pornstar"
        assert v3["status"] == "downloaded"
    finally:
        await db.close()


async def test_liked_baseline_and_legacy_rows_match_history_query_and_facets(tmp_path: Path, monkeypatch):
    env = EnvSettings(data_dir=tmp_path)
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    try:
        await repo.save_settings(RuntimeSettings(ph_cookie="sess=test", uploader_enabled=False))

        async def ok_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        async def fake_scan(*args, **kwargs):
            return [
                ListedVideo(
                    viewkey="vk_liked_new",
                    title="New Liked",
                    url="https://www.pornhub.com/view_video.php?viewkey=vk_liked_new",
                    page_num=1,
                    item_num=1,
                    total_on_page=1,
                )
            ]

        monkeypatch.setattr(engine, "check_cookie_valid", ok_cookie)
        monkeypatch.setattr(engine, "scan_list_pages", fake_scan)

        liked_target = await repo.upsert_target("_currentUser_", "liked", enabled=True)
        # Seed a legacy liked baseline row with empty uploader
        await repo.upsert_video(
            "vk_liked_legacy",
            title="Legacy Liked",
            uploader="",
            target_type="liked",
            status="baseline",
        )

        manager = engine.TaskManager(env, repo, EventBus())
        job = await repo.create_job("baseline_sync", "初始化基线: Liked Videos", {"target_id": liked_target["id"]})
        await manager._run_job(job["id"])

        # New liked baseline writes uploader="My_Liked"
        new_row = await repo.get_video("vk_liked_new")
        assert new_row is not None and new_row["uploader"] == "My_Liked" and new_row["target_type"] == "liked"

        # Target card count and /history?uploader=My_Liked query both return 2 rows!
        targets = await repo.list_targets()
        assert targets[0]["video_total_count"] == 2
        items, total = await repo.query_videos(uploader="My_Liked")
        assert total == 2
        assert {it["viewkey"] for it in items} == {"vk_liked_new", "vk_liked_legacy"}
        assert all(it["uploader_display_name"] == "Liked Videos" for it in items)

        facets = await repo.get_video_filter_facets()
        assert "My_Liked" in facets["uploaders"]
        assert facets["uploader_labels"]["My_Liked"] == "Liked Videos"
    finally:
        await db.close()


async def test_list_pending_upload_videos_includes_published_at_and_duration_seconds(tmp_path: Path):
    db = Database(tmp_path / "pending.db")
    await db.connect()
    repo = Repository(db)
    try:
        await repo.upsert_video(
            "vk_pend",
            title="Pending Video",
            uploader="alice",
            target_type="model",
            quality="1080p",
            status="downloaded",
            local_path="/data/downloads/alice/clip.mp4",
            upload_requested=True,
            published_at="2026-11-20",
            duration_seconds=642,
            force_status=True,
        )
        rows = await repo.list_pending_upload_videos()
        assert len(rows) == 1
        assert rows[0]["published_at"] == "2026-11-20"
        assert rows[0]["duration_seconds"] == 642
    finally:
        await db.close()


async def test_retry_failed_preserves_none_quality_and_applies_target_and_global_preferences(
    tmp_path: Path, monkeypatch
):
    with _make_client(tmp_path) as client:
        assert client.post("/api/auth/login", json={"password": "super-secret-password"}).status_code == 200
        container = client.app.state.container

        async def _seed():
            await container.repo.save_settings(
                RuntimeSettings(
                    ph_cookie="sess=test",
                    preferred_quality="720",
                    uploader_enabled=False,
                    min_file_size_bytes=64,
                )
            )
            await container.repo.upsert_target("alice", "model", enabled=True, preferred_quality="1080")
            # Auto-update job has no 'quality' key in params
            src_auto = await container.repo.create_job("auto_update", "全量自动追更", {"target_ids": None})
            await container.repo.update_job(src_auto["id"], status="partial_failed")
            await container.repo.add_job_item(
                src_auto["id"],
                viewkey="vk_t1",
                title="Alice Failed Parse",
                uploader="alice",
                target_type="model",
                page_url="https://www.pornhub.com/view_video.php?viewkey=vk_t1",
                status="failed",
            )
            await container.repo.add_job_item(
                src_auto["id"],
                viewkey="vk_t2",
                title="Bob Failed Parse",
                uploader="bob",
                target_type="model",
                page_url="https://www.pornhub.com/view_video.php?viewkey=vk_t2",
                status="failed",
            )
            # Manual job explicitly choosing "" (highest available) must retain ""
            src_explicit = await container.repo.create_job(
                "manual_urls", "explicit highest", {"urls": ["https://www.pornhub.com/view_video.php?viewkey=vk_e1"], "quality": ""}
            )
            await container.repo.update_job(src_explicit["id"], status="partial_failed")
            await container.repo.add_job_item(
                src_explicit["id"],
                viewkey="vk_e1",
                title="Explicit",
                uploader="url_batch",
                target_type="model",
                page_url="https://www.pornhub.com/view_video.php?viewkey=vk_e1",
                status="failed",
            )
            return src_auto["id"], src_explicit["id"]

        src_auto_id, src_explicit_id = client.portal.call(_seed)

        resp_auto = client.post(f"/api/jobs/{src_auto_id}/retry-failed")
        assert resp_auto.status_code == 200
        retry_auto = resp_auto.json()["job"]
        assert retry_auto["id"] == src_auto_id and retry_auto["kind"] == "auto_update"
        assert "quality" not in retry_auto["params"]

        resp_explicit = client.post(f"/api/jobs/{src_explicit_id}/retry-failed")
        assert resp_explicit.status_code == 200
        retry_explicit = resp_explicit.json()["job"]
        assert retry_explicit["id"] == src_explicit_id
        assert retry_explicit["params"]["quality"] == ""

    # In-place resume uses the target preference for Alice and the global preference for Bob.
    env = EnvSettings(data_dir=tmp_path / "exec")
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    try:
        await repo.save_settings(
            RuntimeSettings(ph_cookie="sess=test", preferred_quality="720", uploader_enabled=False, min_file_size_bytes=64)
        )
        await repo.upsert_target("alice", "model", enabled=True, preferred_quality="1080")
        src = await repo.create_job("auto_update", "全量自动追更", {"target_ids": None})
        await repo.update_job(src["id"], status="partial_failed")
        await repo.add_job_item(
            src["id"],
            viewkey="vk_a",
            title="A",
            uploader="alice",
            target_type="model",
            page_url="https://www.pornhub.com/view_video.php?viewkey=vk_a",
            status="failed",
        )
        await repo.add_job_item(
            src["id"],
            viewkey="vk_b",
            title="B",
            uploader="bob",
            target_type="model",
            page_url="https://www.pornhub.com/view_video.php?viewkey=vk_b",
            status="failed",
        )

        seen_fetch_quals: dict[str, set[str] | None] = {}

        async def ok_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        async def fake_details(_ctx, items, _selectors, **kwargs):
            out = []
            for url, pnum, inum in items:
                vk = url.split("viewkey=")[1]
                seen_fetch_quals[vk] = kwargs.get("preferred_qualities")
                q_pick = next(iter(kwargs.get("preferred_qualities") or ["1080"])) + "p"
                out.append(
                    VideoDetail(
                        viewkey=vk,
                        title=vk,
                        page_url=url,
                        referer_url=url,
                        download_url=f"https://cdn.example/{vk}.mp4",
                        quality=q_pick,
                        page_num=pnum,
                        item_num=inum,
                    )
                )
            return out

        async def fake_download(**kwargs):
            dest = kwargs["download_dir"] / f"{kwargs['viewkey']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = mp4_payload()
            dest.write_bytes(data)
            return DownloadOutcome(
                success=True,
                local_path=dest,
                file_size=len(data),
                quality=kwargs["quality"],
                download_url=kwargs["download_url"],
            )

        monkeypatch.setattr(engine, "check_cookie_valid", ok_cookie)
        monkeypatch.setattr(engine, "fetch_video_details_batch", fake_details)
        monkeypatch.setattr(engine, "download_mp4_with_resume", fake_download)

        manager = engine.TaskManager(env, repo, EventBus())
        retry_job, _, status = await manager.resume_job_in_place(src["id"])
        assert status == 200 and retry_job["id"] == src["id"]
        await manager._run_job(src["id"])

        assert seen_fetch_quals["vk_a"] == {"1080"}
        assert seen_fetch_quals["vk_b"] == {"720"}
        assert (await repo.get_job(src["id"]))["params"] == {"target_ids": None, "resume_count": 1}
    finally:
        await db.close()


async def test_enrich_metadata_cancellation_marks_items_cancelled_not_failed_or_pending(
    tmp_path: Path, monkeypatch
):
    env = EnvSettings(data_dir=tmp_path)
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    try:
        await repo.save_settings(RuntimeSettings(ph_cookie="sess=test", uploader_enabled=False))
        await repo.upsert_video("vk_e1", title="E1", uploader="alice", status="baseline")
        await repo.upsert_video("vk_e2", title="E2", uploader="alice", status="baseline")

        async def ok_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        monkeypatch.setattr(engine, "check_cookie_valid", ok_cookie)

        # Case 1: cancelled during detail fetch phase
        async def fake_details_cancel(ctx, items, _selectors, **kwargs):
            ctx.cancel_event.set()
            return [
                VideoDetail(
                    viewkey=url.split("viewkey=")[1],
                    title="",
                    page_url=url,
                    referer_url=url,
                    error="操作已取消",
                )
                for url, _, _ in items
            ]

        monkeypatch.setattr(engine, "fetch_video_details_batch", fake_details_cancel)
        manager = engine.TaskManager(env, repo, EventBus())
        j1 = await repo.create_job("enrich_metadata", "补全1", {})
        await manager._run_job(j1["id"])
        j1_final = await repo.get_job(j1["id"])
        j1_items = await repo.list_job_items(j1["id"])
        assert j1_final["status"] == "cancelled"
        assert all(it["status"] == "cancelled" for it in j1_items)
        assert j1_final["failed_items"] == 0

        # Case 2: cancelled during size probe phase
        async def fake_details_ok(ctx, items, _selectors, **kwargs):
            return [
                VideoDetail(
                    viewkey=url.split("viewkey=")[1],
                    title="T",
                    page_url=url,
                    referer_url=url,
                    download_url=f"https://cdn.example/{url.split('viewkey=')[1]}.mp4",
                    quality="1080p",
                )
                for url, _, _ in items
            ]

        async def fake_probe_cancel(_session, download_url, **_kwargs):
            if manager._current_cancel_event is not None:
                manager._current_cancel_event.set()
            await asyncio.sleep(0)
            return None

        monkeypatch.setattr(engine, "fetch_video_details_batch", fake_details_ok)
        monkeypatch.setattr(engine, "probe_download_size", fake_probe_cancel)
        j2 = await repo.create_job("enrich_metadata", "补全2", {})
        await manager._run_job(j2["id"])
        j2_final = await repo.get_job(j2["id"])
        j2_items = await repo.list_job_items(j2["id"])
        assert j2_final["status"] == "cancelled"
        assert all(it["status"] == "cancelled" for it in j2_items)
    finally:
        await db.close()


def test_target_validation_empty_username_fallback_public_auto_probe_and_case_conflict(
    tmp_path: Path, monkeypatch
):
    probed: list[str] = []

    async def fake_resolve(_ctx, username: str, **_kwargs):
        probed.append(username)
        return "pornstar" if username.lower() == "star-public" else "model"

    monkeypatch.setattr(targets_api, "resolve_target_type", fake_resolve)

    with _make_client(tmp_path) as client:
        assert client.post("/api/auth/login", json={"password": "super-secret-password"}).status_code == 200
        # Configure logged-in username WITHOUT cookie
        assert client.put("/api/settings", json={"ph_logged_in_username": "myowner", "ph_cookie": ""}).status_code == 200

        # 1. Invalid URL-like usernames rejected by targets, batch targets, and manual_target jobs
        bad_name = "https://www.pornhub.com/view_video.php?viewkey=abc123"
        assert client.post("/api/targets", json={"username": bad_name, "target_type": "auto"}).status_code == 400
        assert (
            client.post(
                "/api/targets/batch",
                json={"items": [{"username": "channels/bad", "target_type": "model"}]},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/jobs",
                json={"source_mode": "target", "usernames": [bad_name], "target_type": "auto"},
            ).status_code
            == 400
        )

        # 2. Empty username in manual_target ONLY falls back to ph_logged_in_username for favorites
        assert (
            client.post(
                "/api/jobs",
                json={"source_mode": "target", "usernames": [], "target_type": "model"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/jobs",
                json={"source_mode": "target", "usernames": [], "target_type": "pornstar"},
            ).status_code
            == 400
        )
        fav_job = client.post(
            "/api/jobs",
            json={"source_mode": "target", "usernames": [], "target_type": "favorites"},
        )
        assert fav_job.status_code == 200
        assert fav_job.json()["job"]["params"]["usernames"] == ["myowner"]

        # 3. auto target type probes publicly even when ph_cookie is empty
        auto_res = client.post("/api/targets", json={"username": "star-public", "target_type": "auto"})
        assert auto_res.status_code == 200
        assert auto_res.json()["target"]["target_type"] == "pornstar"
        assert "star-public" in probed

        # 4. PATCH /api/targets/{id} enforces case-insensitive uniqueness & mutual exclusion -> 409
        t_upper = client.post("/api/targets", json={"username": "Alice", "target_type": "model"}).json()["target"]
        t_other = client.post("/api/targets", json={"username": "bob", "target_type": "pornstar"}).json()["target"]
        conflict = client.patch(f"/api/targets/{t_other['id']}", json={"username": "alice"})
        assert conflict.status_code == 409
        assert t_upper["id"] != t_other["id"]


async def test_bulk_cancel_pending_job_items_avoids_per_item_ws_flood(tmp_path: Path, monkeypatch):
    env = EnvSettings(data_dir=tmp_path)
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    try:
        await repo.save_settings(RuntimeSettings(ph_cookie="sess=test", uploader_enabled=False))
        for i in range(60):
            await repo.upsert_video(f"vk_m{i}", title=f"M{i}", uploader="alice", status="baseline")

        async def ok_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")

        async def fake_details_cancel(ctx, items, _selectors, **kwargs):
            ctx.cancel_event.set()
            return [
                VideoDetail(
                    viewkey=url.split("viewkey=")[1],
                    title="",
                    page_url=url,
                    referer_url=url,
                    error="操作已取消",
                )
                for url, _, _ in items
            ]

        monkeypatch.setattr(engine, "check_cookie_valid", ok_cookie)
        monkeypatch.setattr(engine, "fetch_video_details_batch", fake_details_cancel)

        events = EventBus()
        seen_events: list[str] = []
        orig_broadcast = events.broadcast
        events.broadcast = lambda etype, data: (seen_events.append(etype), orig_broadcast(etype, data))

        manager = engine.TaskManager(env, repo, events)
        job = await repo.create_job("enrich_metadata", "补全 60 条", {})
        await manager._run_job(job["id"])

        items = await repo.list_job_items(job["id"])
        assert len(items) == 60
        assert all(it["status"] == "cancelled" for it in items)
        assert "job_items_changed" in seen_events
        # Must NOT spam 60 individual item_updated events on cancel!
        assert seen_events.count("item_updated") == 0
    finally:
        await db.close()
