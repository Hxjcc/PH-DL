"""End-to-end FastAPI API & WebSocket tests."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import REDACTED_PLACEHOLDER, EnvSettings
from app.db.schema import SCHEMA_VERSION
from app.main import create_app


def _make_client(tmp_path: Path) -> TestClient:
    env = EnvSettings(
        data_dir=tmp_path / "data",
        admin_password="super-secret-password",
        secret_key="a" * 48,
        cookie_secure=False,
    )
    app = create_app(env_settings=env)
    return TestClient(app)


def test_link_job_queues_only_unique_supported_videos(tmp_path: Path):
    with _make_client(tmp_path) as client:
        assert client.post("/api/auth/login", json={"password": "super-secret-password"}).status_code == 200
        first = "https://www.pornhub.com/view_video.php?viewkey=sampleOne"
        second = "https://www.pornhub.com/shorties/sampleTwo"
        response = client.post("/api/jobs", json={
            "source_mode": "urls",
            "urls": [first, "https://www.pornhub.com/embed/sampleOne", "# comment", "not a link", second, first],
            "auto_upload": False,
        })
        assert response.status_code == 200
        job = response.json()["job"]
        assert job["label"] == "链接下载: 2 条链接"
        assert job["params"]["urls"] == [first, second]
        assert job["params"]["skipped_non_video"] == 1


def test_auth_settings_targets_videos_jobs_and_ws(tmp_path: Path):
    with _make_client(tmp_path) as client:
        # Health is public
        assert client.get("/api/health").status_code == 200

        # Protected routes reject unauthenticated calls
        assert client.get("/api/overview").status_code == 401
        assert client.get("/api/targets").status_code == 401
        assert client.get("/api/settings").status_code == 401

        # Wrong password rejected
        assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401

        # Valid login sets HttpOnly session cookie
        login_resp = client.post("/api/auth/login", json={"password": "super-secret-password"})
        assert login_resp.status_code == 200
        assert client.get("/api/auth/me").json()["authenticated"] is True

        # Overview works
        ov = client.get("/api/overview")
        assert ov.status_code == 200
        ov_data = ov.json()
        assert ov_data["running"] is False
        assert ov_data["targets_total"] == 0
        assert ov_data["event_loop"]["warning_threshold_ms"] == 250.0
        assert ov_data["event_loop"]["samples"] >= 0

        # Settings redact sensitive fields & preserve original on placeholder save
        put1 = client.put(
            "/api/settings",
            json={
                "ph_username": "tester",
                "ph_password": "real-password",
                "serverchan_send_key": "real-key",
                "schedule_time": "04:30",
            },
        )
        assert put1.status_code == 200
        s1 = put1.json()["settings"]
        assert s1["ph_password"] == REDACTED_PLACEHOLDER
        assert s1["serverchan_send_key"] == REDACTED_PLACEHOLDER
        assert s1["schedule_time"] == "04:30"

        # Invalid schedule_time rejected
        assert client.put("/api/settings", json={"schedule_time": "25:99"}).status_code == 400

        # Saving back redacted values does not overwrite real secrets
        put2 = client.put(
            "/api/settings",
            json={
                "ph_password": REDACTED_PLACEHOLDER,
                "serverchan_send_key": REDACTED_PLACEHOLDER,
                "max_concurrent_downloads": 4,
            },
        )
        assert put2.status_code == 200
        assert put2.json()["settings"]["max_concurrent_downloads"] == 4

        put40 = client.put("/api/settings", json={"max_concurrent_video_details": 40})
        assert put40.status_code == 200
        assert put40.json()["settings"]["max_concurrent_video_details"] == 40
        put_list = client.put("/api/settings", json={"max_concurrent_list_pages": 4})
        assert put_list.status_code == 200
        assert put_list.json()["settings"]["max_concurrent_list_pages"] == 4
        assert client.put("/api/settings", json={"max_concurrent_video_details": 41}).status_code == 400

        # Targets CRUD
        t_create = client.post(
            "/api/targets",
            json={"username": "alice", "target_type": "model", "enabled": True},
        )
        assert t_create.status_code == 200
        tid = t_create.json()["target"]["id"]

        t_list = client.get("/api/targets").json()["items"]
        assert len(t_list) == 1 and t_list[0]["username"] == "alice"

        t_patch = client.patch(
            f"/api/targets/{tid}",
            json={"enabled": False, "preferred_quality": "1080", "display_name": "Alice Star"},
        )
        assert t_patch.status_code == 200
        assert t_patch.json()["target"]["enabled"] is False
        assert t_patch.json()["target"]["preferred_quality"] == "1080"
        assert t_patch.json()["target"]["display_name"] == "Alice Star"

        assert client.delete(f"/api/targets/{tid}").status_code == 200
        assert client.get("/api/targets").json()["items"] == []

        # Target PATCH conflict returns 409 instead of 500
        t1 = client.post("/api/targets", json={"username": "dupuser", "target_type": "model"}).json()["target"]
        t2 = client.post("/api/targets", json={"username": "dupuser", "target_type": "pornstar"}).json()["target"]
        assert client.patch(f"/api/targets/{t2['id']}", json={"target_type": "model"}).status_code == 409
        assert client.delete(f"/api/targets/{t1['id']}").status_code == 200
        assert client.delete(f"/api/targets/{t2['id']}").status_code == 200

        # Batch create targets passes baseline_pages to baseline_sync job
        b_resp = client.post(
            "/api/targets/batch",
            json={
                "items": [{"username": "subuser", "target_type": "model"}],
                "init_baseline": True,
                "baseline_pages": "1",
            },
        )
        assert b_resp.status_code == 200
        b_jobs = b_resp.json()["baseline_jobs"]
        assert len(b_jobs) == 1
        assert b_jobs[0]["params"]["pages"] == "1"
        sub_tid = b_resp.json()["targets"][0]["id"]
        assert client.delete(f"/api/targets/{sub_tid}").status_code == 200

        # Videos list returns pagination structure
        v_resp = client.get("/api/videos")
        assert v_resp.status_code == 200
        assert v_resp.json()["total"] == 0

        container = client.app.state.container

        async def _seed_metadata_candidate():
            await container.repo.upsert_video(
                "meta_api",
                title="Metadata API",
                uploader="alice",
                target_type="model",
                video_page_url="https://www.pornhub.com/view_video.php?viewkey=meta_api",
                status="baseline",
            )

        client.portal.call(_seed_metadata_candidate)
        enrich = client.post("/api/videos/metadata/enrich")
        assert enrich.status_code == 200
        assert enrich.json()["count"] == 1
        assert enrich.json()["job"]["kind"] == "enrich_metadata"

        # Jobs validation & delete/batch-delete endpoints
        assert client.post("/api/jobs", json={"source_mode": "urls", "urls": []}).status_code == 400
        assert client.delete("/api/jobs/99999").status_code == 404
        assert client.post("/api/jobs/batch-delete", json={"job_ids": []}).status_code == 400
        bd = client.post("/api/jobs/batch-delete", json={"job_ids": [99998, 99999]})
        assert bd.status_code == 200
        assert bd.json()["deleted_count"] == 0

        # WebSocket receives initial snapshot when authenticated
        with client.websocket_connect("/api/ws") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "snapshot"
            assert "logs" in msg["data"]
            assert "live_progress" in msg["data"]

        # WebSocket Origin check blocks cross-site hijacking while allowing same-host & proxy Host without port
        import pytest
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as bad_origin:
            with client.websocket_connect("/api/ws", headers={"Origin": "https://evil.example"}):
                pass
        assert bad_origin.value.code == 4403

        with client.websocket_connect(
            "/api/ws",
            headers={"Host": "panel.local", "Origin": "http://panel.local:8080"},
        ) as ws_same_host:
            assert ws_same_host.receive_json()["type"] == "snapshot"

        # Logout clears the cookie and revokes previously issued tokens.
        session_token = client.cookies.get("phdl_session")
        assert session_token
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/overview").status_code == 401
        assert client.get(
            "/api/overview",
            headers={"Authorization": f"Bearer {session_token}"},
        ).status_code == 401

        # The unified database records its schema version for future upgrades.
        assert client.portal.call(container.db.schema_version) == SCHEMA_VERSION


def test_subscriptions_return_avatars_and_import_preserves_them(tmp_path: Path, monkeypatch):
    from app.api import overview

    async def fake_subscriptions(*args, **kwargs):
        return [("model", "alice", "Alice", "https://cdn.example/alice.jpg")]

    monkeypatch.setattr(overview, "fetch_subscriptions", fake_subscriptions)
    with _make_client(tmp_path) as client:
        assert client.post("/api/auth/login", json={"password": "super-secret-password"}).status_code == 200
        assert client.put("/api/settings", json={"ph_logged_in_username": "owner", "ph_cookie": "sess=test"}).status_code == 200
        response = client.get("/api/subscriptions").json()
        assert response["username"] == "owner"
        assert response["subscriptions"] == [{"type": "model", "username": "alice", "display_name": "Alice", "avatar_url": "https://cdn.example/alice.jpg"}]
        imported = client.post("/api/targets/batch", json={"items": [{"username": "alice", "target_type": "model", "display_name": "Alice", "avatar_url": response["subscriptions"][0]["avatar_url"]}]}).json()
        assert imported["targets"][0]["avatar_url"] == "https://cdn.example/alice.jpg"


def test_login_rate_limit_respects_proxy_ip_headers(tmp_path: Path):
    with _make_client(tmp_path) as client:
        # TestClient connects via testclient host; simulate trusted proxy via X-Real-IP using direct auth helper
        from app.api.auth import _extract_client_ip

        class _Client:
            def __init__(self, host: str):
                self.host = host

        class _Req:
            def __init__(self, host: str, headers: dict):
                self.client = _Client(host)
                self.headers = headers

        # Behind Nginx Docker private IP -> trusts X-Real-IP / X-Forwarded-For
        assert _extract_client_ip(_Req("172.18.0.3", {"X-Real-IP": "203.0.113.10"})) == "203.0.113.10"
        assert _extract_client_ip(_Req("127.0.0.1", {"X-Forwarded-For": "198.51.100.7, 172.18.0.2"})) == "198.51.100.7"
        # Directly exposed public IP -> ignores spoofed headers!
        assert _extract_client_ip(_Req("203.0.113.55", {"X-Real-IP": "1.2.3.4"})) == "203.0.113.55"


async def test_atomic_upsert_video_preserves_target_type_and_status_rank(tmp_path: Path):
    from app.db.database import Database
    from app.db.repo import Repository

    db = Database(tmp_path / "atomic.db")
    await db.connect()
    repo = Repository(db)
    try:
        await repo.upsert_video(
            "vk_atomic",
            title="My Clip",
            uploader="star1",
            target_type="pornstar",
            quality="1080p",
            status="downloaded",
            local_path="/data/downloads/star1/clip.mp4",
        )
        # Baseline upsert must NOT downgrade status from downloaded -> baseline
        await repo.upsert_video("vk_atomic", status="baseline")
        v1 = await repo.get_video("vk_atomic")
        assert v1 and v1["status"] == "downloaded" and v1["target_type"] == "pornstar"

        # mark_video_uploaded must preserve target_type='pornstar' and clear local_path
        await repo.mark_video_uploaded("vk_atomic", remote_path="pikpak:Pornhub/star1/clip.mp4")
        uploaded_video = await repo.get_video("vk_atomic")
        assert uploaded_video and uploaded_video["status"] == "uploaded"
        assert uploaded_video["target_type"] == "pornstar"
        assert uploaded_video["local_path"] == ""
        assert uploaded_video["remote_path"] == "pikpak:Pornhub/star1/clip.mp4"

        # recalculate_job_counts must count 'uploading' in done_items so UI doesn't regress
        job = await repo.create_job("manual_urls", "test", {"redownload": True})
        await repo.add_job_item(
            job["id"],
            viewkey="vk_u1",
            title="Uploading Item",
            uploader="alice",
            target_type="model",
            page_url="https://www.pornhub.com/view_video.php?viewkey=vk_u1",
            status="uploading",
        )
        recalc = await repo.recalculate_job_counts(job["id"])
        assert recalc and recalc["done_items"] == 1 and recalc["uploaded_items"] == 0
    finally:
        await db.close()


def test_retry_failed_preserves_redownload_param(tmp_path: Path):
    with _make_client(tmp_path) as client:
        assert client.post("/api/auth/login", json={"password": "super-secret-password"}).status_code == 200
        container = client.app.state.container

        async def _seed_failed_job():
            job = await container.repo.create_job(
                "manual_urls",
                "forced redownload",
                {"urls": ["https://www.pornhub.com/view_video.php?viewkey=vkretry1"], "redownload": True, "auto_upload": False},
            )
            await container.repo.update_job(job["id"], status="partial_failed")
            await container.repo.add_job_item(
                job["id"],
                viewkey="vkretry1",
                title="Retry Clip",
                uploader="alice",
                target_type="model",
                page_url="https://www.pornhub.com/view_video.php?viewkey=vkretry1",
                status="failed",
            )
            return job["id"]

        # Run seeding on the TestClient portal event loop via repo directly before retry call
        src_id = client.portal.call(_seed_failed_job)
        resp = client.post(f"/api/jobs/{src_id}/retry-failed")
        assert resp.status_code == 200
        retry_job = resp.json()["job"]
        assert retry_job["params"]["redownload"] is True
        assert retry_job["params"]["auto_upload"] is False
        assert retry_job["params"]["source_job_id"] == src_id
        assert retry_job["params"]["origin_job_id"] == src_id
        assert retry_job["params"]["origin_label"] == "forced redownload"
        assert "forced redownload" in retry_job["label"]

        # Chained retry from a legacy-style retry job (no origin_label stored) still resolves root label
        async def _seed_legacy_retry_chain():
            root = await container.repo.create_job("manual_target", "下载目标: lovely-asuna", {"usernames": ["lovely-asuna"]})
            await container.repo.update_job(root["id"], status="cancelled")
            legacy_retry = await container.repo.create_job(
                "retry_items",
                f"重试任务 #{root['id']} (2 项)",
                {"source_job_id": root["id"]},
            )
            await container.repo.update_job(legacy_retry["id"], status="cancelled")
            await container.repo.add_job_item(
                legacy_retry["id"],
                viewkey="vk_chain_1",
                title="Clip 1",
                uploader="lovely-asuna",
                target_type="model",
                page_url="https://www.pornhub.com/view_video.php?viewkey=vk_chain_1",
                status="cancelled",
            )
            return root["id"], legacy_retry["id"]

        root_id, legacy_id = client.portal.call(_seed_legacy_retry_chain)
        resp2 = client.post(f"/api/jobs/{legacy_id}/retry-failed")
        assert resp2.status_code == 200
        chained = resp2.json()["job"]
        assert chained["params"]["source_job_id"] == legacy_id
        assert chained["params"]["origin_job_id"] == root_id
        assert chained["params"]["origin_label"] == "下载目标: lovely-asuna"
        assert chained["label"].startswith("续传 · 下载目标: lovely-asuna")


def test_video_history_uses_target_display_name(tmp_path: Path):
    with _make_client(tmp_path) as client:
        assert client.post("/api/auth/login", json={"password": "super-secret-password"}).status_code == 200
        container = client.app.state.container

        async def _seed():
            await container.repo.upsert_target(
                "alice", "model", enabled=True, display_name="Alice Display"
            )
            await container.repo.upsert_video(
                "vk_display",
                title="Display Name Video",
                uploader="alice",
                target_type="model",
                quality="1080p",
                video_page_url="https://www.pornhub.com/view_video.php?viewkey=vk_display",
                status="uploaded",
            )

        client.portal.call(_seed)
        res = client.get("/api/videos").json()
        assert res["items"][0]["uploader_display_name"] == "Alice Display"
        assert res["facets"]["uploader_labels"]["alice"] == "Alice Display"

        search_res = client.get("/api/videos", params={"search": "Alice Display"}).json()
        assert search_res["total"] == 1

        # The filter value remains the stable slug used by the database (case-insensitive).
        filtered = client.get("/api/videos", params={"uploader": "ALICE"}).json()
        assert filtered["total"] == 1

        # Pagination-only mode skips facets & stats recomputation
        slim = client.get("/api/videos", params={"include_meta": "false"}).json()
        assert slim["total"] == 1 and slim["facets"] is None and slim["stats"] is None

        batch = client.post(
            "/api/videos/batch-delete",
            json={"viewkeys": ["vk_display", "missing"]},
        )
        assert batch.status_code == 200
        assert batch.json()["deleted_count"] == 1
        assert client.get("/api/videos").json()["total"] == 0


def test_targeted_update_label_uses_display_name(tmp_path: Path):
    with _make_client(tmp_path) as client:
        assert client.post("/api/auth/login", json={"password": "super-secret-password"}).status_code == 200
        target = client.post(
            "/api/targets",
            json={
                "username": "kkfox-baby",
                "target_type": "model",
                "display_name": "KKFox baby",
            },
        ).json()["target"]
        res = client.post(
            "/api/targets/run-update",
            json={"target_ids": [target["id"]], "auto_download": False},
        )
        assert res.status_code == 200
        job = res.json()["job"]
        assert job["label"] == "指定检查更新: KKFox baby"
        assert job["params"]["target_ids"] == [target["id"]]


def test_retry_accepts_downloaded_and_uploading_items(tmp_path: Path):
    with _make_client(tmp_path) as client:
        assert client.post("/api/auth/login", json={"password": "super-secret-password"}).status_code == 200
        container = client.app.state.container

        async def _seed_cancelled_job():
            job = await container.repo.create_job(
                "manual_target",
                "取消后补偿",
                {"usernames": ["alice"], "auto_upload": True},
            )
            await container.repo.update_job(job["id"], status="cancelled")
            await container.repo.add_job_item(
                job["id"],
                viewkey="vk_downloaded",
                title="Downloaded Item",
                uploader="alice",
                target_type="model",
                page_url="https://www.pornhub.com/view_video.php?viewkey=vk_downloaded",
                status="downloaded",
                local_path=str(tmp_path / "alice" / "Downloaded Item [vk_downloaded].mp4"),
            )
            await container.repo.add_job_item(
                job["id"],
                viewkey="vk_uploading",
                title="Uploading Item",
                uploader="alice",
                target_type="model",
                page_url="https://www.pornhub.com/view_video.php?viewkey=vk_uploading",
                status="uploading",
                local_path=str(tmp_path / "alice" / "Uploading Item [vk_uploading].mp4"),
            )
            return job["id"]

        source_id = client.portal.call(_seed_cancelled_job)
        resp = client.post(f"/api/jobs/{source_id}/retry-failed")
        assert resp.status_code == 200
        retry_job = resp.json()["job"]
        assert retry_job["params"]["source_job_id"] == source_id
        assert retry_job["kind"] == "retry_items"


def test_delete_video_optionally_removes_local_file_inside_downloads_only(tmp_path: Path):
    from tests.media_fixtures import mp4_payload

    with _make_client(tmp_path) as client:
        assert client.post("/api/auth/login", json={"password": "super-secret-password"}).status_code == 200
        container = client.app.state.container
        dl_dir = container.env.downloads_dir / "alice"
        dl_dir.mkdir(parents=True, exist_ok=True)
        inside_file = dl_dir / "Clip [vk_in].mp4"
        inside_file.write_bytes(mp4_payload())
        outside_file = tmp_path / "outside_keep.mp4"
        outside_file.write_bytes(mp4_payload())

        async def _seed():
            await container.repo.upsert_video(
                "vk_in",
                title="Inside",
                uploader="alice",
                status="downloaded",
                local_path=str(inside_file),
            )
            await container.repo.upsert_video(
                "vk_out",
                title="Outside",
                uploader="alice",
                status="downloaded",
                local_path=str(outside_file),
            )

        client.portal.call(_seed)

        del_in = client.delete("/api/videos/vk_in?delete_local_file=true")
        assert del_in.status_code == 200 and del_in.json()["deleted_local_file"] is True
        assert not inside_file.exists()
        assert not dl_dir.exists()  # Empty uploader directory cleaned up

        # Must NEVER unlink a path outside downloads_dir even if recorded in DB
        del_out = client.post(
            "/api/videos/batch-delete",
            json={"viewkeys": ["vk_out"], "delete_local_files": True},
        )
        assert del_out.status_code == 200
        assert del_out.json()["deleted_count"] == 1
        assert del_out.json()["deleted_files_count"] == 0
        assert outside_file.exists()


def test_list_targets_tracks_today_new_count_across_downloads_and_rechecks(tmp_path: Path):
    from datetime import datetime, timedelta, timezone
    from app.db.repo import utc_now_iso

    with _make_client(tmp_path) as client:
        assert client.post("/api/auth/login", json={"password": "super-secret-password"}).status_code == 200
        container = client.app.state.container

        alice = client.post("/api/targets", json={"username": "alice", "target_type": "model"}).json()["target"]
        bob = client.post("/api/targets", json={"username": "bob", "target_type": "model"}).json()["target"]
        carol = client.post("/api/targets", json={"username": "carol", "target_type": "model"}).json()["target"]
        liked = client.post("/api/targets", json={"username": "_currentUser_", "target_type": "liked"}).json()["target"]

        now_iso = utc_now_iso()
        old_iso = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")

        async def _seed():
            # Alice downloaded 2 videos today, then later re-checked with 0 new
            await container.repo.upsert_video(
                "vk_a1",
                title="Alice 1",
                uploader="alice",
                target_type="model",
                status="uploaded",
                downloaded_at=now_iso,
                uploaded_at=now_iso,
            )
            await container.repo.upsert_video(
                "vk_a2",
                title="Alice 2",
                uploader="alice",
                target_type="model",
                status="downloaded",
                downloaded_at=now_iso,
            )
            await container.repo.mark_target_checked(alice["id"], 0)

            # Bob had check_updates today finding 3 new videos (not downloaded yet)
            await container.repo.mark_target_checked(bob["id"], 3)

            # Carol only had activity 2 days ago
            await container.repo.upsert_video(
                "vk_c1",
                title="Carol Old",
                uploader="carol",
                target_type="model",
                status="uploaded",
                downloaded_at=old_iso,
                uploaded_at=old_iso,
            )
            await container.db.execute(
                "UPDATE targets SET last_checked_at = ?, last_new_count = 4 WHERE id = ?",
                (old_iso, carol["id"]),
            )

            # Liked target received 1 video today
            await container.repo.upsert_video(
                "vk_l1",
                title="Liked Today",
                uploader="My_Liked",
                target_type="liked",
                status="uploaded",
                downloaded_at=now_iso,
                uploaded_at=now_iso,
            )
            await container.repo.mark_target_checked(liked["id"], 0)

        client.portal.call(_seed)

        items = client.get("/api/targets").json()["items"]
        by_user = {(item["username"], item["target_type"]): item for item in items}

        assert by_user[("alice", "model")]["today_downloaded_count"] == 2
        assert by_user[("alice", "model")]["today_new_count"] == 2
        assert by_user[("alice", "model")]["last_new_count"] == 0

        assert by_user[("bob", "model")]["today_downloaded_count"] == 0
        assert by_user[("bob", "model")]["today_new_count"] == 3
        assert by_user[("bob", "model")]["last_new_count"] == 3

        assert by_user[("carol", "model")]["today_downloaded_count"] == 0
        assert by_user[("carol", "model")]["today_new_count"] == 0
        assert by_user[("carol", "model")]["last_new_count"] == 4

        assert by_user[("_currentUser_", "liked")]["today_downloaded_count"] == 1
        assert by_user[("_currentUser_", "liked")]["today_new_count"] == 1


def test_account_avatar_upload_serve_delete_and_batch_targets_with_quality(tmp_path: Path):
    import base64

    # Valid 1x1 transparent PNG
    tiny_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    png_data_url = "data:image/png;base64," + base64.b64encode(tiny_png).decode("ascii")

    with _make_client(tmp_path) as client:
        assert client.post("/api/auth/login", json={"password": "super-secret-password"}).status_code == 200

        # Initially no custom avatar
        assert client.get("/api/settings/avatar").status_code == 404
        assert client.get("/api/overview").json()["account"]["avatar_url"] == ""

        # Reject invalid non-image payload
        bad_payload = "data:text/plain;base64," + base64.b64encode(b"not an image").decode("ascii")
        assert client.post("/api/settings/avatar", json={"data_url": bad_payload}).status_code == 400

        # Upload valid PNG avatar
        up = client.post("/api/settings/avatar", json={"data_url": png_data_url})
        assert up.status_code == 200
        avatar_url = up.json()["avatar_url"]
        assert avatar_url.startswith("/api/settings/avatar?v=")
        assert client.get("/api/overview").json()["account"]["avatar_url"] == avatar_url

        # Serve uploaded avatar bytes
        fetched = client.get("/api/settings/avatar")
        assert fetched.status_code == 200
        assert fetched.headers["content-type"].startswith("image/png")
        assert fetched.content == tiny_png

        # Delete custom avatar restores default
        deleted = client.delete("/api/settings/avatar")
        assert deleted.status_code == 200
        assert deleted.json()["avatar_url"] == ""
        assert client.get("/api/settings/avatar").status_code == 404
        assert client.get("/api/overview").json()["account"]["avatar_url"] == ""

        # Batch create targets supports multiple items with auto type + preferred_quality
        batch = client.post(
            "/api/targets/batch",
            json={
                "items": [
                    {"username": "multi-a", "target_type": "auto", "preferred_quality": "2160,1080"},
                    {"username": "multi-b", "target_type": "pornstar", "preferred_quality": "1080"},
                ],
                "init_baseline": False,
            },
        )
        assert batch.status_code == 200
        assert batch.json()["created_count"] == 2
        by_name = {t["username"]: t for t in client.get("/api/targets").json()["items"]}
        assert by_name["multi-a"]["preferred_quality"] == "2160,1080"
        assert by_name["multi-b"]["target_type"] == "pornstar"
        assert by_name["multi-b"]["preferred_quality"] == "1080"
