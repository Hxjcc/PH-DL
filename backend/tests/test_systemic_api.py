"""Authentication, settings, and history API regressions."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import overview, settings, videos
from app.auth import SESSION_COOKIE_NAME
from app.config import REDACTED_PLACEHOLDER, EnvSettings, RuntimeSettings
from app.main import create_app
from app.parsers.video import VideoDetail
from app.services.cookie import CookieCheckResult
from app.services import http_proxy, notifier
from tests.test_backend_regressions import job_engine


def login_client(tmp_path, **startup_options):
    env = EnvSettings(data_dir=tmp_path, admin_password="test-only-password", **startup_options)
    return TestClient(create_app(env))


@pytest.mark.parametrize("password", ["", "  ", "admin", " change-me-strong-password "])
def test_backend_rejects_empty_and_public_admin_passwords(tmp_path, password):
    env = EnvSettings(data_dir=tmp_path, admin_password=password, allow_insecure_default_password=False)
    with pytest.raises(RuntimeError, match="PHDL_ADMIN_PASSWORD"), TestClient(create_app(env)):
        pytest.fail("An insecure configuration started")


def test_explicit_insecure_development_override_allows_startup(tmp_path):
    env = EnvSettings(data_dir=tmp_path, admin_password="admin", allow_insecure_default_password=True)
    with TestClient(create_app(env)) as client:
        assert client.post("/api/auth/login", json={"password": "admin"}).status_code == 200


def test_websocket_token_query_cannot_authenticate_without_session_cookie(tmp_path):
    with login_client(tmp_path) as client:
        assert client.post("/api/auth/login", json={"password": "test-only-password"}).status_code == 200
        token = client.cookies.get(SESSION_COOKIE_NAME)
        assert token
        client.cookies.clear()
        with client.websocket_connect(f"/api/ws?token={token}") as ws:
            with pytest.raises(WebSocketDisconnect) as raised:
                ws.receive_json()
        assert raised.value.code == 4401


def test_deployment_rclone_executable_overrides_legacy_db_and_api_values(tmp_path):
    with login_client(tmp_path, rclone_bin="deployment-rclone") as client:
        assert client.post("/api/auth/login", json={"password": "test-only-password"}).status_code == 200
        db = client.app.state.container.db
        client.portal.call(
            db.execute, "INSERT INTO settings VALUES('runtime', ?, '2026-01-01')",
            (json.dumps({"rclone_bin": "legacy-rclone", "rclone_remote": "primary"}),),
        )
        response = client.put("/api/settings", json={"rclone_bin": "user-supplied-rclone"})
        assert response.status_code == 200
        assert response.json()["settings"]["rclone_bin"] == "deployment-rclone"
        assert client.get("/api/settings").json()["settings"]["rclone_remote"] == "primary"
        assert RuntimeSettings(rclone_bin="test-stub").rclone_bin == "test-stub"


@pytest.mark.parametrize("via", ["query", "json", "empty"])
def test_enrich_metadata_api_scopes_count_and_job_params(tmp_path, via):
    with login_client(tmp_path) as client:
        assert client.post("/api/auth/login", json={"password": "test-only-password"}).status_code == 200
        container = client.app.state.container

        async def prepare():
            await container.tasks.stop()
            await container.repo.upsert_video("ph", site="pornhub")
            await container.repo.upsert_video("xhClip01", site="xhamster")

        client.portal.call(prepare)
        options = {"params": {"site": "xhamster"}} if via == "query" else {"json": {"site": "xhamster" if via == "json" else ""}}
        response = client.post("/api/videos/metadata/enrich", **options)
        assert response.status_code == 200
        assert response.json()["count"] == (2 if via == "empty" else 1)
        assert response.json()["job"]["params"] == ({} if via == "empty" else {"site": "xhamster"})


def test_cookie_less_subscriptions_read_does_not_log_in(tmp_path, monkeypatch):
    async def unexpected_login(*args, **kwargs):
        pytest.fail("The subscriptions read tried to log in")

    monkeypatch.setattr(overview, "login_xhamster_and_get_cookies", unexpected_login, raising=False)
    with login_client(tmp_path) as client:
        assert client.post("/api/auth/login", json={"password": "test-only-password"}).status_code == 200
        assert client.put("/api/settings", json={
            "xh_username": "configured-user", "xh_password": "configured-password",
            "yescaptcha_enabled": True, "yescaptcha_client_key": "configured-key",
        }).status_code == 200
        response = client.get("/api/subscriptions", params={"site": "xhamster"})
        assert response.status_code == 400 and "刷新" in response.json()["detail"]
        assert client.get("/api/settings").json()["settings"]["has_xh_cookie"] is False


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("change", ["file", "ledger"])
async def test_history_delete_keeps_replaced_files_and_recreated_records(job_engine, monkeypatch, batch, change):
    manager, repo, env = job_engine
    path = env.downloads_dir / "download.mp4"
    path.write_bytes(b"first bytes")
    await repo.upsert_video("vkey", status="downloaded", local_path=str(path))
    original_remove = videos._remove_deleted_local_videos

    async def re_download_after_delete(container, candidates, deleted):
        assert await repo.get_video("vkey") is None
        if change == "file":
            draft = path.with_suffix(".tmp")
            draft.write_bytes(b"another set")
            draft.replace(path)
        else:
            await repo.upsert_video("vkey", status="downloaded", local_path=str(path))
        return await original_remove(container, candidates, deleted)

    monkeypatch.setattr(videos, "_remove_deleted_local_videos", re_download_after_delete)
    container = SimpleNamespace(env=env, repo=repo, events=manager.events)
    if batch:
        result = await videos.batch_delete_video_records(
            videos.BatchDeleteVideosRequest(viewkeys=["vkey"], delete_local_files=True), container,
        )
        assert result["deleted_count"] == 1 and result["deleted_files_count"] == 0
    else:
        result = await videos.delete_video_record("vkey", True, container)
        assert result["deleted"] and not result["deleted_local_file"]
    assert path.read_bytes() == (b"another set" if change == "file" else b"first bytes")


async def test_history_delete_keeps_file_while_a_download_is_publishing_its_ledger(job_engine):
    manager, repo, env = job_engine
    path = env.downloads_dir / "inflight.mp4"
    path.write_bytes(b"remuxed file from the current job")
    await repo.upsert_video("vkey", status="downloaded", local_path=str(path))
    job = await repo.create_job("manual_urls", "fixture", {})
    await repo.update_job(job["id"], status="running")
    await repo.add_job_items_batch(job["id"], [{"viewkey": "vkey", "status": "downloading"}])

    result = await videos.delete_video_record("vkey", True, SimpleNamespace(
        env=env, repo=repo, events=manager.events,
    ))
    assert result["deleted"] and not result["deleted_local_file"]
    assert path.read_bytes() == b"remuxed file from the current job"


async def test_signed_job_item_urls_stay_private_and_export_uses_canonical_pages(job_engine):
    from app.api.jobs import batch_delete_jobs, delete_job, get_job_detail, BatchDeleteJobsRequest

    manager, repo, _ = job_engine
    job = await repo.create_job("manual_urls", "export", {})
    signed = "https://media.xhcdn.com/video.m3u8?token=cdn-secret"
    [item] = await repo.add_job_items_batch(job["id"], [{"download_url": signed}])
    subscriber = manager.events.subscribe()
    manager.events.broadcast("item_updated", item)
    public_item = subscriber.get_nowait()["data"]
    assert public_item["is_hls"] and public_item["download_url"] == ""
    container = SimpleNamespace(repo=repo, tasks=manager, events=manager.events)
    response = await get_job_detail(job["id"], container)
    assert response["items"][0]["is_hls"] and response["items"][0]["download_url"] == ""
    assert (await repo.get_job_item(item["id"]))["download_url"] == signed
    assert manager._export_urls([
        VideoDetail("vk", "ph", "https://www.pornhub.com/embed/vk?extra=private", "", download_url=signed),
        VideoDetail("xh_456", "xh", "https://xhamster.com/videos/sluggish-456?private=yes", "", download_url=signed),
    ]) == ["https://www.pornhub.com/view_video.php?viewkey=vk", "https://xhamster.com/videos/video-456"]

    await repo.update_job(job["id"], status="completed")
    await delete_job(job["id"], container)
    assert subscriber.get_nowait()["data"] == {"job_ids": [job["id"]]}
    subscriber.get_nowait()  # overview_changed
    another = await repo.create_job("manual_urls", "fixture", {})
    await repo.update_job(another["id"], status="completed")
    await batch_delete_jobs(BatchDeleteJobsRequest(job_ids=[another["id"]]), container)
    assert subscriber.get_nowait()["data"] == {"job_ids": [another["id"]]}


@pytest.mark.parametrize("flags", [
    "--endpoint=https://account:embedded%2Dpassword@example.test/asset?opaque=query%2Dsecret",
    "--parameter key=inline-secret",
])
def test_extra_rclone_flag_secrets_are_redacted_in_settings_and_diagnostics(flags):
    runtime = RuntimeSettings(rclone_extra_flags=flags)
    assert runtime.to_redacted_dict()["rclone_extra_flags"] == REDACTED_PLACEHOLDER
    for value in ("embedded-password", "query-secret") if "example.test" in flags else ("inline-secret",):
        assert value in runtime.diagnostic_secrets()
        assert value not in runtime.redact_diagnostic(f"operation failed for {value}")


@pytest.mark.parametrize("prefix", ["ph", "xh"])
@pytest.mark.parametrize("importing", [False, True])
async def test_cookie_checks_discard_results_from_replaced_credentials(job_engine, monkeypatch, prefix, importing):
    manager, repo, _ = job_engine
    cookie_key = f"{prefix}_cookie"
    username_key = f"{prefix}_logged_in_username"
    await repo.update_settings_patch({cookie_key: "session=old"})
    started, finish = asyncio.Event(), asyncio.Event()

    async def check(cookie, **kwargs):
        assert cookie == ("session=import" if importing else "session=old")
        started.set()
        await finish.wait()
        return (True, "ok", "stale-user") if prefix == "xh" else CookieCheckResult(True, "ok", "stale-user")

    monkeypatch.setattr(settings, "check_xhamster_cookie_valid" if prefix == "xh" else "check_cookie_valid", check)
    container = SimpleNamespace(repo=repo, tasks=manager, events=manager.events)
    test_endpoint = settings.test_xh_cookie if prefix == "xh" else settings.test_cookie
    import_endpoint = settings.import_xh_cookie if prefix == "xh" else settings.import_cookie
    call = import_endpoint(settings.CookieImportRequest(cookie="session=import"), container) if importing else test_endpoint(container)
    request = asyncio.create_task(call)
    await asyncio.wait_for(started.wait(), 3)
    await repo.update_settings_patch({cookie_key: "session=new", username_key: "new-user", "proxy_url": "http://127.0.0.1:4321"})
    finish.set()
    result = await request
    assert result["superseded"] is True and not result["valid"]
    assert getattr(await repo.get_settings(), username_key) == "new-user"
    assert (manager.xh_cookie_status if prefix == "xh" else manager.cookie_status)["valid"] is None


async def test_remote_cooldowns_only_reset_for_actual_remote_changes(job_engine, monkeypatch):
    manager, repo, _ = job_engine
    await repo.update_settings_patch({"rclone_remote": "primary", "rclone_backup_remotes": "second"})
    cleared = []
    monkeypatch.setattr(manager, "clear_remote_cooldowns", lambda: cleared.append(True))
    container = SimpleNamespace(repo=repo, tasks=manager, events=manager.events)
    await settings.update_settings({"rclone_remote": "primary", "rclone_backup_remotes": "second", "request_timeout": 25}, container)
    assert cleared == []
    await settings.update_settings({"rclone_backup_remotes": "third"}, container)
    assert cleared == [True]


@pytest.mark.parametrize(("scheme", "expected"), [
    ("socks5h", "socks5"), ("socks4a", "socks4"), ("http", "http"), ("https", "https"),
])
async def test_serverchan_uses_the_common_proxy_connector(monkeypatch, scheme, expected):
    target = f"{scheme}://localhost:1080"
    proxy_calls = []
    original = http_proxy.ProxyConnector.from_url

    def proxy_connector(url, **kwargs):
        proxy_calls.append((url, kwargs["rdns"]))
        return original(url, **kwargs)

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def json(self, **kwargs):
            return {"code": 0, "data": {"errno": 0}}

    class Session:
        def __init__(self, *, connector, **kwargs):
            self.connector = connector

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def post(self, _url, *, proxy, **kwargs):
            assert proxy == (None if scheme.startswith("socks") else target)
            return Response()

    monkeypatch.setattr(http_proxy.ProxyConnector, "from_url", proxy_connector)
    monkeypatch.setattr(notifier.aiohttp, "ClientSession", Session)
    ok, _ = await notifier.send_serverchan("title", "message", RuntimeSettings(
        proxy_url=target, serverchan_send_key="test-key",
    ), force=True)
    assert ok
    assert proxy_calls == ([(f"{expected}://localhost:1080", True)] if scheme.startswith("socks") else [])
