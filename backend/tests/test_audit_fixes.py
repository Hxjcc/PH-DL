"""Regression tests covering all 12 audit findings (high, medium, and low priority)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List

import aiohttp
import pytest
from aiohttp_socks import ProxyConnector
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.auth import _extract_client_ip
from app.cancel import StopRequested
from app.config import EnvSettings, RuntimeSettings
from app.db.database import Database
from app.db.repo import Repository
from app.engine import task_manager as engine
from app.engine.events import EventBus
from app.main import create_app
from app.parsers.http_session import PowFetchResult, TaskHttpContext
from app.parsers.video import VideoDetail
from app.services import cookie as cookie_mod
from app.services import hls_downloader as hls_mod
from app.services.captcha import CaptchaSolution
from app.services.cookie import CookieCheckResult
from app.services.http_proxy import (
    create_aiohttp_connector,
    effective_request_proxy,
    is_socks_proxy,
)
from app.services.playback import PlaybackService, _Playback


def _make_client(tmp_path: Path, **env_overrides: Any) -> TestClient:
    env = EnvSettings(
        data_dir=tmp_path,
        admin_password="test-password",
        secret_key="s" * 48,
        cookie_secure=False,
        **env_overrides,
    )
    return TestClient(create_app(env))


def _login(client: TestClient) -> None:
    resp = client.post("/api/auth/login", json={"password": "test-password"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Finding 1: xHamster baseline metadata enrichment loop + site + fresh cookie
# ---------------------------------------------------------------------------
async def test_xhamster_baseline_enrichment_completes_without_size_probe_loop(
    tmp_path: Path, monkeypatch
):
    env = EnvSettings(data_dir=tmp_path, admin_password="pw", secret_key="k" * 48)
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = engine.TaskManager(env, repo, events)

    try:
        await repo.update_settings_patch({"ph_cookie": "sess=initial"})
        await repo.upsert_video(
            viewkey="xh_base_001",
            site="xhamster",
            title="XH Baseline Clip",
            uploader="xhcreator",
            target_type="creator",
            quality="",
            file_size=0,
            status="baseline",
            video_page_url="https://xhamster.com/videos/xh-baseline-clip-xh_base_001",
        )
        await repo.upsert_video(
            viewkey="ph_base_001",
            site="pornhub",
            title="PH Baseline Clip",
            uploader="phmodel",
            target_type="model",
            quality="",
            file_size=0,
            status="baseline",
            video_page_url="https://www.pornhub.com/view_video.php?viewkey=ph_base_001",
        )

        assert await repo.count_baseline_metadata_candidates() == 2
        assert await repo.count_baseline_metadata_candidates(site="xhamster") == 1
        assert await repo.count_baseline_metadata_candidates(site="pornhub") == 1

        async def fake_check_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="Cookie 有效")

        async def fake_fetch_batch(ctx, items, selectors, **kwargs):
            results: List[VideoDetail] = []
            for page_url, _, _ in items:
                if "xhamster.com" in page_url:
                    results.append(
                        VideoDetail(
                            viewkey="xh_base_001",
                            title="XH Baseline Clip",
                            page_url=page_url,
                            referer_url=page_url,
                            download_url="https://cdn.xhcdn.com/hls/master.m3u8",
                            quality="1080p",
                            published_at="2026-05-01",
                            duration_seconds=420,
                        )
                    )
                else:
                    # Simulate PoW cookie refresh during detail fetch
                    ctx.pow_cookies["accessAgeDisclaimerPH"] = "1"
                    ctx.pow_cookies["pow_token"] = "solved"
                    results.append(
                        VideoDetail(
                            viewkey="ph_base_001",
                            title="PH Baseline Clip",
                            page_url=page_url,
                            referer_url=page_url,
                            download_url="https://cdn.phncdn.com/video.mp4",
                            quality="1080p",
                            published_at="2026-05-02",
                            duration_seconds=300,
                        )
                    )
            return results

        probed_calls: List[Dict[str, Any]] = []

        async def fake_probe(session, download_url, *, referer_url, cookie_str="", **kwargs):
            probed_calls.append({"url": download_url, "cookie": cookie_str})
            return 123_456_789

        monkeypatch.setattr(engine, "check_cookie_valid", fake_check_cookie)
        monkeypatch.setattr(engine, "fetch_video_details_batch", fake_fetch_batch)
        monkeypatch.setattr(engine, "probe_download_size", fake_probe)

        job = await repo.create_job("enrich_metadata", "补全基线元数据", {})
        await tm._run_job(job["id"])

        finished = await repo.get_job(job["id"])
        assert finished is not None
        assert finished["status"] == "completed"
        assert finished["done_items"] == 2
        assert finished["failed_items"] == 0

        items = {it["viewkey"]: it for it in await repo.list_job_items(job["id"])}
        assert items["xh_base_001"]["site"] == "xhamster"
        assert items["xh_base_001"]["status"] == "done"
        assert items["ph_base_001"]["site"] == "pornhub"
        assert items["ph_base_001"]["status"] == "done"

        # Probe must skip xHamster HLS completely and send refreshed PoW cookie for PH MP4
        assert len(probed_calls) == 1
        assert probed_calls[0]["url"] == "https://cdn.phncdn.com/video.mp4"
        assert "pow_token=solved" in probed_calls[0]["cookie"]

        # Both records now leave the baseline enrichment candidate queue
        xh_rec = await repo.get_video("xh_base_001")
        assert xh_rec is not None and xh_rec["quality"] == "1080p"
        assert await repo.count_baseline_metadata_candidates() == 0
        assert await repo.count_baseline_metadata_candidates(site="xhamster") == 0
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Finding 2: xHamster manual target subscribe_creator + xHamster Referer
# ---------------------------------------------------------------------------
def test_manual_target_xhamster_keeps_subscribe_creator(tmp_path: Path):
    with _make_client(tmp_path) as client:
        _login(client)
        resp = client.post(
            "/api/jobs",
            json={
                "source_mode": "target",
                "site": "xhamster",
                "target_type": "creator",
                "usernames": ["alice"],
                "subscribe_creator": True,
                "export_only": True,
            },
        )
        assert resp.status_code == 200
        job = resp.json()["job"]
        assert job["params"]["site"] == "xhamster"
        assert job["params"]["subscribe_creator"] is True


async def test_xhamster_profile_fallback_sends_xhamster_referer(tmp_path: Path, monkeypatch):
    env = EnvSettings(data_dir=tmp_path, admin_password="pw", secret_key="k" * 48)
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    events = EventBus()
    tm = engine.TaskManager(env, repo, events)

    seen_profile_headers: List[Dict[str, str]] = []

    async def fake_scan_list_pages(ctx, list_url, selectors, **kwargs):
        return []

    async def fake_fetch_with_pow(self_ctx, url, **kwargs):
        if "xhamster.com/creators/alice" in url:
            seen_profile_headers.append(dict(kwargs.get("headers") or {}))
        return PowFetchResult("<html></html>", url, 200, 0, False), None

    monkeypatch.setattr(engine, "scan_list_pages", fake_scan_list_pages)
    monkeypatch.setattr(TaskHttpContext, "fetch_with_pow", fake_fetch_with_pow)

    try:
        await repo.update_settings_patch({"xh_cookie": "UID=1; _id=tok"})
        job = await repo.create_job(
            "manual_target",
            "xH target",
            {
                "site": "xhamster",
                "usernames": ["alice"],
                "target_type": "creator",
                "subscribe_creator": True,
                "export_only": True,
            },
        )
        await tm._run_job(job["id"])
        assert seen_profile_headers
        assert seen_profile_headers[0].get("Referer") == "https://xhamster.com/"
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Finding 3: Auto-login cancel aborts YesCaptcha & never drops obtained cookie
# ---------------------------------------------------------------------------
async def test_login_cancel_aborts_yescaptcha_and_detaches_logs(monkeypatch):
    class _CaptchaLoginCtx:
        def __init__(self, *args, cancel_event=None, **kwargs):
            self.cancel_event = cancel_event or asyncio.Event()
            self.post_count = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def cookie_string(self, include_pow: bool = True) -> str:
            return "sessid=fresh_paid_cookie"

        async def fetch_with_pow(self, url, **kwargs):
            if kwargs.get("method") == "POST":
                self.post_count += 1
                return PowFetchResult('{"success":"0","requireCaptcha":true}', url, 200, 0, False), None
            return PowFetchResult("<html></html>", url, 200, 0, False), None

    solver_started = asyncio.Event()
    solver_cancelled = asyncio.Event()

    async def blocking_solver(*args, cancel_event=None, on_log=None, **kwargs):
        solver_started.set()
        assert cancel_event is not None
        await cancel_event.wait()
        solver_cancelled.set()
        if on_log:
            on_log("INFO", "should-not-reach-cancelled-caller")
        raise StopRequested

    monkeypatch.setattr(cookie_mod, "TaskHttpContext", _CaptchaLoginCtx)
    monkeypatch.setattr(cookie_mod, "solve_recaptcha_v2", blocking_solver)

    cancel_evt = asyncio.Event()
    caller_logs: List[str] = []

    task = asyncio.create_task(
        cookie_mod.login_and_get_cookies(
            "cancel-user@example.com",
            "secret",
            yescaptcha_enabled=True,
            yescaptcha_client_key="yk",
            on_log=lambda lvl, msg: caller_logs.append(msg),
            cancel_event=cancel_evt,
        )
    )
    await asyncio.wait_for(solver_started.wait(), timeout=2.0)
    cancel_evt.set()
    with pytest.raises(StopRequested):
        await asyncio.wait_for(task, timeout=2.0)
    await asyncio.wait_for(solver_cancelled.wait(), timeout=2.0)
    assert all("should-not-reach-cancelled-caller" not in m for m in caller_logs)


async def test_login_persists_cookie_via_on_success_and_keeps_running_for_other_waiter(monkeypatch):
    release_solver = asyncio.Event()
    solver_started = asyncio.Event()

    class _OkLoginCtx:
        def __init__(self, *args, cancel_event=None, **kwargs):
            self.cancel_event = cancel_event or asyncio.Event()
            self.posts = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def cookie_string(self, include_pow: bool = True) -> str:
            return "sessid=paid_cookie_123"

        async def fetch_with_pow(self, url, **kwargs):
            if kwargs.get("method") == "POST":
                self.posts += 1
                if self.posts == 1:
                    return PowFetchResult('{"success":"0","requireCaptcha":true}', url, 200, 0, False), None
                return PowFetchResult('{"success":"1","username":"Alice"}', url, 200, 0, False), None
            return PowFetchResult("<html></html>", url, 200, 0, False), None

    async def gated_solver(*args, cancel_event=None, **kwargs):
        solver_started.set()
        await release_solver.wait()
        return CaptchaSolution(token="tok-ok", task_id="tid", task_type="type")

    async def fake_check(*args, **kwargs):
        return CookieCheckResult(valid=True, message="Cookie 有效", logged_in_username="Alice")

    monkeypatch.setattr(cookie_mod, "TaskHttpContext", _OkLoginCtx)
    monkeypatch.setattr(cookie_mod, "solve_recaptcha_v2", gated_solver)
    monkeypatch.setattr(cookie_mod, "check_cookie_valid", fake_check)

    saved_cookies: List[str] = []

    async def on_success(res: cookie_mod.CookieLoginResult) -> None:
        saved_cookies.append(res.cookie_str)

    cancel_job1 = asyncio.Event()
    t1 = asyncio.create_task(
        cookie_mod.login_and_get_cookies(
            "shared-user@example.com",
            "secret",
            yescaptcha_enabled=True,
            yescaptcha_client_key="yk",
            cancel_event=cancel_job1,
            on_success=on_success,
        )
    )
    await asyncio.wait_for(solver_started.wait(), timeout=2.0)

    # Second waiter joins same single-flight login (e.g., Settings refresh)
    t2 = asyncio.create_task(
        cookie_mod.login_and_get_cookies(
            "shared-user@example.com",
            "secret",
            yescaptcha_enabled=True,
            yescaptcha_client_key="yk",
        )
    )
    await asyncio.sleep(0.02)

    # First waiter cancels -> second waiter keeps login alive, and on_success persists cookie!
    cancel_job1.set()
    with pytest.raises(StopRequested):
        await asyncio.wait_for(t1, timeout=2.0)

    release_solver.set()
    res2 = await asyncio.wait_for(t2, timeout=2.0)
    assert res2.success is True
    assert res2.cookie_str == "sessid=paid_cookie_123"
    assert saved_cookies == ["sessid=paid_cookie_123"]


# ---------------------------------------------------------------------------
# Finding 4: SOCKS5 proxy support across aiohttp downloader / HLS / playback
# ---------------------------------------------------------------------------
async def test_socks5_connector_helpers_and_playback_pool():
    assert is_socks_proxy("socks5://127.0.0.1:1080") is True
    assert is_socks_proxy("socks5h://user:pw@127.0.0.1:1080") is True
    assert is_socks_proxy("http://127.0.0.1:7890") is False

    socks_conn, socks_req_proxy = create_aiohttp_connector("socks5://127.0.0.1:1080")
    assert isinstance(socks_conn, ProxyConnector)
    assert socks_req_proxy is None
    async with aiohttp.ClientSession(connector=socks_conn) as socks_session:
        assert effective_request_proxy(socks_session, "socks5://127.0.0.1:1080") is None

    http_conn, http_req_proxy = create_aiohttp_connector("http://127.0.0.1:7890")
    assert isinstance(http_conn, aiohttp.TCPConnector)
    assert http_req_proxy == "http://127.0.0.1:7890"
    async with aiohttp.ClientSession(connector=http_conn) as http_session:
        assert effective_request_proxy(http_session, "http://127.0.0.1:7890") == "http://127.0.0.1:7890"

    svc = PlaybackService()
    try:
        client, req_proxy = await svc._get_http_client("socks5://127.0.0.1:1080")
        assert isinstance(client.connector, ProxyConnector)
        assert req_proxy is None
        assert svc._http_client is None  # default non-SOCKS slot untouched

        # Reuses cached SOCKS client
        client2, req_proxy2 = await svc._get_http_client("socks5://127.0.0.1:1080")
        assert client2 is client
        assert req_proxy2 is None
    finally:
        await svc.aclose()
    assert svc._socks_http_clients == {}


async def test_hls_and_playback_handle_proxy_value_error_cleanly(tmp_path: Path):
    class _BrokenProxySession:
        connector = None

        def get(self, *args, **kwargs):
            raise ValueError("Only http proxies are supported")

    outcome = await hls_mod._download_hls_aiohttp(
        session=_BrokenProxySession(),  # type: ignore[arg-type]
        playlist_url="https://cdn.xhcdn.com/test.m3u8",
        hls_dir=tmp_path / "hls",
        viewkey="xh1",
        quality="720p",
        referer="https://xhamster.com/",
        cookie_str="",
        proxy_url="socks5://127.0.0.1:1080",
        segment_concurrency=2,
        request_timeout=10.0,
        stop=asyncio.Event(),
        prepare_write=lambda _n: None,
        on_tick=lambda _d, _t, _s: None,
    )
    assert outcome.fallback_to_ytdlp is True

    svc = PlaybackService()

    async def _bad_client(_proxy=None):
        return _BrokenProxySession(), "socks5://127.0.0.1:1080"

    svc._get_http_client = _bad_client  # type: ignore[method-assign]
    entry = _Playback(
        viewkey="v1",
        referer="https://xhamster.com/",
        cookie="",
        proxy_url="socks5://127.0.0.1:1080",
        sources=[],
        expires_at=9e12,
    )
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        with pytest.raises(HTTPException) as exc:
            await svc._open_upstream(stack, entry, "https://cdn.xhcdn.com/seg.mp4", None)
        assert exc.value.status_code == 502


# ---------------------------------------------------------------------------
# Finding 5: upsert_target mutual exclusion for PH & xH creator types
# ---------------------------------------------------------------------------
async def test_upsert_target_enforces_same_creator_mutual_exclusion(tmp_path: Path):
    db = Database(tmp_path / "targets.db")
    await db.connect()
    repo = Repository(db)
    try:
        ph1 = await repo.upsert_target("alice", "pornstar", site="pornhub", preferred_quality="2160")
        ph2 = await repo.upsert_target("Alice", "model", site="pornhub", preferred_quality="1080", display_name="Alice")
        assert ph1["id"] == ph2["id"]
        assert ph2["target_type"] == "pornstar"
        assert ph2["preferred_quality"] == "1080"

        xh1 = await repo.upsert_target("bob", "pornstar", site="xhamster")
        xh2 = await repo.upsert_target("bob", "creator", site="xhamster", display_name="Bob")
        assert xh1["id"] == xh2["id"]
        assert xh2["target_type"] == "pornstar"

        all_targets = await repo.list_targets()
        assert len(all_targets) == 2
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Finding 6: WebSocket unauthorized receives close code 4401 (after accept)
# ---------------------------------------------------------------------------
def test_ws_unauthorized_closes_with_4401(tmp_path: Path):
    with _make_client(tmp_path) as client:
        with client.websocket_connect("/api/ws") as ws:
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4401


# ---------------------------------------------------------------------------
# Finding 7: Client IP extraction resists LAN/VPN XFF spoofing & honors override
# ---------------------------------------------------------------------------
def test_extract_client_ip_blocks_lan_and_vpn_spoofing():
    class _Client:
        def __init__(self, host: str):
            self.host = host

    class _Req:
        def __init__(self, host: str, headers: dict):
            self.client = _Client(host)
            self.headers = headers

    # LAN attacker directly hitting nginx container with forged public XFF
    assert (
        _extract_client_ip(
            _Req("172.18.0.3", {"X-Forwarded-For": "8.8.8.8, 192.168.1.50"})
        )
        == "192.168.1.50"
    )
    # VPN / 10.x / Tailscale CGNAT direct client with forged public XFF
    assert (
        _extract_client_ip(
            _Req("172.18.0.3", {"X-Forwarded-For": "1.2.3.4, 10.0.0.99"})
        )
        == "10.0.0.99"
    )
    assert (
        _extract_client_ip(
            _Req("172.18.0.3", {"X-Forwarded-For": "1.2.3.4, 100.64.1.20"})
        )
        == "100.64.1.20"
    )
    # PHDL_TRUSTED_PROXIES=none disables forwarding header trust completely
    env_none = EnvSettings(trusted_proxies="none")
    assert env_none.parsed_trusted_proxies() == ()
    assert (
        _extract_client_ip(
            _Req("172.18.0.3", {"X-Forwarded-For": "203.0.113.10", "X-Real-IP": "203.0.113.10"}),
            env_none.parsed_trusted_proxies(),
        )
        == "172.18.0.3"
    )
    # Custom trusted proxy CIDR explicitly allows an enterprise proxy on 10.0.0.5
    env_custom = EnvSettings(trusted_proxies="172.16.0.0/12,10.0.0.5/32")
    assert (
        _extract_client_ip(
            _Req("172.18.0.3", {"X-Forwarded-For": "203.0.113.25, 10.0.0.5"}),
            env_custom.parsed_trusted_proxies(),
        )
        == "203.0.113.25"
    )


# ---------------------------------------------------------------------------
# Finding 8 & 9: Schedule/delay validation & cookie memory status reset
# ---------------------------------------------------------------------------
def test_settings_validation_and_cookie_status_reset(tmp_path: Path):
    with _make_client(tmp_path) as client:
        _login(client)
        container = client.app.state.container

        # Reject empty schedule_time when schedule_enabled is True
        bad_sched = client.put("/api/settings", json={"schedule_enabled": True, "schedule_time": ""})
        assert bad_sched.status_code == 400

        # Allow empty schedule_time when schedule_enabled is False
        ok_disabled = client.put("/api/settings", json={"schedule_enabled": False, "schedule_time": ""})
        assert ok_disabled.status_code == 200

        # Re-enabling schedule without restoring schedule_time must fail
        re_enable_fail = client.put("/api/settings", json={"schedule_enabled": True})
        assert re_enable_fail.status_code == 400

        # Inverted delay ranges fail Pydantic model validation
        bad_page_delay = client.put("/api/settings", json={"page_delay_min": 5.0, "page_delay_max": 1.0})
        assert bad_page_delay.status_code == 400
        bad_retry_delay = client.put("/api/settings", json={"retry_delay_base": 20.0, "retry_delay_max": 5.0})
        assert bad_retry_delay.status_code == 400

        # Cookie status resets when cookie is modified/cleared, but NOT when placeholder is echoed back
        client.put("/api/settings", json={"ph_cookie": "sess=1", "xh_cookie": "UID=1"})
        container.tasks.set_cookie_status(True, "Cookie 有效")
        container.tasks.set_xh_cookie_status(True, "Cookie 有效")

        unchanged = client.put("/api/settings", json={"ph_cookie": "********", "xh_cookie": "********"})
        assert unchanged.status_code == 200
        assert container.tasks.cookie_status["valid"] is True
        assert container.tasks.xh_cookie_status["valid"] is True

        cleared = client.put("/api/settings", json={"ph_cookie": "", "xh_cookie": "UID=2"})
        assert cleared.status_code == 200
        assert container.tasks.cookie_status["valid"] is None
        assert container.tasks.xh_cookie_status["valid"] is None


# ---------------------------------------------------------------------------
# Finding 10: Site-scoped video stats & case-insensitive uploader facet dedupe
# ---------------------------------------------------------------------------
def test_videos_stats_site_filter_and_case_insensitive_uploader_facets(tmp_path: Path):
    with _make_client(tmp_path) as client:
        _login(client)
        repo = client.app.state.container.repo

        async def _seed():
            await repo.upsert_video(
                viewkey="ph_1", site="pornhub", title="A1", uploader="alice", status="uploaded", quality="1080p"
            )
            await repo.upsert_video(
                viewkey="ph_2", site="pornhub", title="A2", uploader="Alice", status="downloaded", quality="720p"
            )
            await repo.upsert_video(
                viewkey="xh_1", site="xhamster", title="X1", uploader="bob", status="baseline", quality="1080p"
            )

        asyncio.run(_seed())

        all_res = client.get("/api/videos").json()
        assert all_res["stats"]["total"] == 3
        assert all_res["stats"]["uploaded"] == 1
        assert all_res["stats"]["downloaded"] == 1
        assert all_res["stats"]["baseline"] == 1

        xh_res = client.get("/api/videos", params={"site": "xhamster"}).json()
        assert xh_res["stats"]["total"] == 1
        assert xh_res["stats"]["baseline"] == 1
        assert xh_res["stats"]["uploaded"] == 0

        ph_res = client.get("/api/videos", params={"site": "pornhub"}).json()
        assert ph_res["stats"]["total"] == 2
        assert ph_res["facets"]["uploaders"] == ["Alice"]


# ---------------------------------------------------------------------------
# Finding 11: xHamster favorites failure folder name
# ---------------------------------------------------------------------------
def test_xhamster_favorites_folder_name():
    assert engine._target_folder_name("_currentUser_", "favorites", site="xhamster") == "My_Favorites"
    assert engine._target_folder_name("alice", "favorites", site="pornhub") == "alice_favorites"
