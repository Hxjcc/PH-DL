"""Tests for PornHub creator subscription extraction/POST and download-time auto-track linkage."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import EnvSettings, RuntimeSettings, SelectorSettings
from app.db.database import Database
from app.db.repo import Repository, local_date_days_ago, utc_now_iso
from app.engine import task_manager as engine
from app.engine.events import EventBus
from app.parsers.http_session import PowFetchResult, TaskHttpContext
from app.parsers.target import (
    SubscribeAction,
    extract_subscribe_action,
    subscribe_from_action,
)
from app.parsers.video import VideoDetail
from app.services.cookie import CookieCheckResult
from app.services.downloader import DownloadOutcome
from tests.media_fixtures import mp4_payload


def test_extract_subscribe_action_supports_model_pornstar_channel_and_states():
    model_html = """
    <button class="buttonBase" data-id="1495232871" data-login="0"
      data-subscribe-url="/user/subscribe_add_json?id=1495232871&amp;token=tok123"
      data-subscribed="0" type="button">Subscribe</button>
    """
    act = extract_subscribe_action(model_html)
    assert act == SubscribeAction(
        subscribe_url="/user/subscribe_add_json?id=1495232871&token=tok123",
        already_subscribed=False,
        login_required=False,
        creator_id="1495232871",
    )

    pornstar_html = """
    <button data-id="319282" data-login="0"
      data-subscribe-url="/api/v1/pornstar/319282/subscribe_add_json?token=pstok"
      data-subscribed="1" type="button">Subscribed</button>
    """
    ps = extract_subscribe_action(pornstar_html)
    assert ps is not None
    assert ps.subscribe_url == "/api/v1/pornstar/319282/subscribe_add_json?token=pstok"
    assert ps.already_subscribed is True
    assert ps.login_required is False

    channel_html = """
    <button data-id="30571" data-login="1"
      data-subscribe-url="/channel/subscribe_add_json?id=30571&token=chtok"
      data-subscribed="0" type="button">Subscribe</button>
    """
    ch = extract_subscribe_action(channel_html)
    assert ch is not None
    assert ch.login_required is True
    assert ch.creator_id == "30571"

    assert extract_subscribe_action("<div>no button here</div>") is None


async def test_subscribe_from_action_handles_skip_post_and_errors(monkeypatch):
    ctx = TaskHttpContext()
    calls = []

    async def fake_fetch(url, **kwargs):
        calls.append((url, kwargs))
        body = json.dumps({
            "success": "PASS",
            "message": "Subscription added.",
            "user": {
                "avatar": "https://ei.phncdn.com/pics/users/0014/9523/2871/avatar.jpg",
                "profile": "/model/julie-jess",
            },
        })
        return PowFetchResult(body, url, 200, 0, False), None

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_fetch)

    already = await subscribe_from_action(
        ctx,
        SubscribeAction(subscribe_url="/user/subscribe_add_json?id=1&token=t", already_subscribed=True),
    )
    assert already.ok and already.status == "already_subscribed"
    assert calls == []

    unlogged = await subscribe_from_action(
        ctx,
        SubscribeAction(subscribe_url="/user/subscribe_add_json?id=1&token=t", login_required=True),
    )
    assert not unlogged.ok and unlogged.status == "login_required"
    assert calls == []

    ok_res = await subscribe_from_action(
        ctx,
        SubscribeAction(subscribe_url="/user/subscribe_add_json?id=1495232871&token=tok", creator_id="1495232871"),
        referer_url="https://www.pornhub.com/model/julie-jess/videos",
        label="julie-jess",
    )
    assert ok_res.ok and ok_res.status == "subscribed"
    assert ok_res.avatar_url.endswith("/avatar.jpg")
    assert len(calls) == 1
    req_url, req_kwargs = calls[0]
    assert req_url == "https://www.pornhub.com/user/subscribe_add_json?id=1495232871&token=tok"
    assert req_kwargs["method"] == "POST"
    assert req_kwargs["data"] is None
    assert req_kwargs["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert req_kwargs["headers"]["Origin"] == "https://www.pornhub.com"
    assert req_kwargs["headers"]["Referer"] == "https://www.pornhub.com/model/julie-jess/videos"


@pytest.fixture
async def linkage_engine(tmp_path: Path, monkeypatch):
    env = EnvSettings(data_dir=tmp_path)
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    try:
        repo = Repository(db)
        await repo.save_settings(
            RuntimeSettings(
                ph_cookie="sess=test",
                uploader_enabled=False,
                page_delay_min=0.0,
                page_delay_max=0.0,
                retry_delay_base=0.0,
                retry_delay_max=0.0,
                min_file_size_bytes=64,
            )
        )
        async def ok_cookie(*args, **kwargs):
            return CookieCheckResult(valid=True, message="ok")
        monkeypatch.setattr(engine, "check_cookie_valid", ok_cookie)
        events = EventBus()
        manager = engine.TaskManager(env, repo, events)
        yield manager, repo, env, events
    finally:
        await db.close()


async def test_manual_target_links_subscribe_and_auto_track_from_list_page_even_without_videos(
    linkage_engine, monkeypatch
):
    manager, repo, _env, events = linkage_engine
    seen_events = []
    orig_broadcast = events.broadcast
    events.broadcast = lambda etype, data: (seen_events.append(etype), orig_broadcast(etype, data))

    post_urls = []
    get_urls = []

    async def fake_fetch(self, url, **kwargs):
        method = (kwargs.get("method") or "GET").upper()
        if method == "POST":
            post_urls.append(url)
            return PowFetchResult(
                json.dumps({
                    "success": "PASS",
                    "message": "Subscription added.",
                    "user": {"avatar": "https://ei.phncdn.com/pics/users/1/avatar.jpg"},
                }),
                url,
                200,
                0,
                False,
            ), None
        get_urls.append(url)
        html = """
        <html><body>
          <img id="getAvatar" src="https://ei.phncdn.com/pics/users/1/avatar.jpg" alt="Julie Jess" />
          <button data-id="1495232871" data-login="0" data-subscribed="0"
            data-subscribe-url="/user/subscribe_add_json?id=1495232871&token=listtok"></button>
        </body></html>
        """
        return PowFetchResult(html, url, 200, 0, False), None

    monkeypatch.setattr(TaskHttpContext, "fetch_with_pow", fake_fetch)

    job = await repo.create_job(
        "manual_target",
        "下载目标: julie-jess",
        {
            "usernames": ["julie-jess"],
            "target_type": "model",
            "auto_upload": False,
            "add_to_auto_track": True,
            "subscribe_creator": True,
        },
    )
    await manager._run_job(job["id"])

    # Zero extra profile homepage GET requests: only list pages were fetched.
    assert get_urls and all("/videos" in u for u in get_urls)
    assert "https://www.pornhub.com/model/julie-jess" not in get_urls
    assert post_urls == ["https://www.pornhub.com/user/subscribe_add_json?id=1495232871&token=listtok"]
    targets = await repo.list_targets()
    assert len(targets) == 1
    assert targets[0]["username"] == "julie-jess"
    assert targets[0]["target_type"] == "model"
    assert targets[0]["display_name"] == "Julie Jess"
    assert targets[0]["avatar_url"] == "https://ei.phncdn.com/pics/users/1/avatar.jpg"
    assert targets[0]["enabled"] is True
    assert "targets_changed" in seen_events
    assert "subscriptions_changed" in seen_events


async def test_ensure_target_preserves_existing_enabled_and_quality(linkage_engine):
    _manager, repo, _env, _events = linkage_engine
    created = await repo.upsert_target(
        "arinafox",
        "model",
        enabled=False,
        preferred_quality="2160",
        avatar_url="",
        display_name="",
    )
    ensured, was_created = await repo.ensure_target(
        "arinafox",
        "model",
        avatar_url="https://ei.phncdn.com/pics/users/2/avatar.jpg",
        display_name="ArinaFox",
    )
    assert was_created is False
    assert ensured["id"] == created["id"]
    assert ensured["enabled"] is False
    assert ensured["preferred_quality"] == "2160"
    assert ensured["avatar_url"] == "https://ei.phncdn.com/pics/users/2/avatar.jpg"
    assert ensured["display_name"] == "ArinaFox"


async def test_manual_urls_deduplicates_subscribe_and_track_and_never_blocks_download(
    linkage_engine, monkeypatch
):
    manager, repo, env, _events = linkage_engine
    subscribe_calls = []

    async def fake_details(_ctx, items, _selectors, **kwargs):
        out = []
        for page_url, page_num, item_num in items:
            vkey = page_url.split("viewkey=")[1]
            out.append(
                VideoDetail(
                    viewkey=vkey,
                    title=f"Clip {vkey}",
                    page_url=page_url,
                    referer_url=page_url,
                    download_url=f"https://cdn.example/{vkey}.mp4",
                    quality="1080p",
                    uploader_username="julie-jess",
                    uploader_type="model",
                    page_num=page_num,
                    item_num=item_num,
                    subscribe_action=SubscribeAction(
                        subscribe_url="/user/subscribe_add_json?id=1495232871&token=vidtok",
                        already_subscribed=False,
                        login_required=False,
                        creator_id="1495232871",
                    ),
                )
            )
        return out

    async def fake_fetch(self, url, **kwargs):
        subscribe_calls.append((url, kwargs.get("method")))
        # Simulate subscribe failure: must NOT fail or block the video downloads!
        return PowFetchResult("Internal Error", url, 500, 0, False), None

    async def fake_download(**kwargs):
        dest = kwargs["download_dir"] / f"{kwargs['viewkey']}.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload = mp4_payload()
        dest.write_bytes(payload)
        return DownloadOutcome(
            success=True,
            local_path=dest,
            file_size=len(payload),
            quality=kwargs["quality"] or "1080p",
            download_url=kwargs["download_url"],
        )

    monkeypatch.setattr(engine, "fetch_video_details_batch", fake_details)
    monkeypatch.setattr(TaskHttpContext, "fetch_with_pow", fake_fetch)
    monkeypatch.setattr(engine, "download_mp4_with_resume", fake_download)

    job = await repo.create_job(
        "manual_urls",
        "链接下载: 2 条链接",
        {
            "urls": [
                "https://www.pornhub.com/view_video.php?viewkey=vk111",
                "https://www.pornhub.com/view_video.php?viewkey=vk222",
            ],
            "group_by_uploader": True,
            "auto_upload": False,
            "add_to_auto_track": True,
            "subscribe_creator": True,
        },
    )
    await manager._run_job(job["id"])
    final_job = await repo.get_job(job["id"])

    assert final_job["status"] == "completed"
    assert final_job["done_items"] == 2
    assert len(subscribe_calls) == 1  # Deduplicated across both videos by creator_id!
    assert subscribe_calls[0][1] == "POST"
    targets = await repo.list_targets()
    assert len(targets) == 1
    assert targets[0]["username"] == "julie-jess"
    assert targets[0]["target_type"] == "model"


async def test_list_targets_includes_indexed_video_counts(linkage_engine):
    _manager, repo, _env, _events = linkage_engine
    await repo.upsert_target("ailish", "model", enabled=True, display_name="Ailish")
    await repo.upsert_target("_currentUser_", "liked", enabled=True)

    await repo.upsert_video(
        "vk1", title="V1", uploader="Ailish", target_type="model", status="uploaded", force_status=True
    )
    await repo.upsert_video(
        "vk2", title="V2", uploader="ailish", target_type="pornstar", status="baseline", force_status=True
    )
    await repo.upsert_video(
        "vk3", title="V3", uploader="ailish", target_type="model", status="downloaded", force_status=True
    )
    await repo.upsert_video(
        "vk4", title="V4", uploader="My_Liked", target_type="liked", status="uploaded", force_status=True
    )

    rows = await repo.list_targets()
    by_user = {r["username"].lower(): r for r in rows}
    assert by_user["ailish"]["video_total_count"] == 3
    assert by_user["ailish"]["video_saved_count"] == 2
    assert by_user["ailish"]["video_baseline_count"] == 1
    assert by_user["_currentuser_"]["video_total_count"] == 1
    assert by_user["_currentuser_"]["video_saved_count"] == 1
    assert by_user["_currentuser_"]["video_baseline_count"] == 0


async def test_ensure_target_deduplicates_model_and_pornstar_without_overwriting(linkage_engine):
    _manager, repo, _env, _events = linkage_engine
    existing_model = await repo.upsert_target(
        "julie-jess",
        "model",
        enabled=False,
        preferred_quality="1080",
        avatar_url="",
        display_name="",
    )
    ensured_ps, created_ps = await repo.ensure_target(
        "Julie-Jess",
        "pornstar",
        avatar_url="https://ei.phncdn.com/pics/users/1/avatar.jpg",
        display_name="Julie Jess",
    )
    assert created_ps is False
    assert ensured_ps["id"] == existing_model["id"]
    assert ensured_ps["target_type"] == "model"
    assert ensured_ps["enabled"] is False
    assert ensured_ps["preferred_quality"] == "1080"
    assert ensured_ps["avatar_url"] == "https://ei.phncdn.com/pics/users/1/avatar.jpg"
    assert ensured_ps["display_name"] == "Julie Jess"

    # Reverse direction: existing pornstar matched when ensure_target called as model.
    existing_ps = await repo.upsert_target("starslug", "pornstar", enabled=True, preferred_quality="2160")
    ensured_model, created_model = await repo.ensure_target("starslug", "model")
    assert created_model is False
    assert ensured_model["id"] == existing_ps["id"]
    assert ensured_model["target_type"] == "pornstar"
    assert ensured_model["preferred_quality"] == "2160"

    # Favorites target with the same username stays distinct.
    fav, created_fav = await repo.ensure_target("julie-jess", "favorites")
    assert created_fav is True
    assert fav["target_type"] == "favorites"
    assert len(await repo.list_targets()) == 3


async def test_list_targets_today_count_excludes_old_published_backfills(linkage_engine):
    _manager, repo, _env, _events = linkage_engine
    await repo.upsert_target("julie-jess", "model", enabled=True)
    now_iso = utc_now_iso()

    await repo.upsert_video(
        "vk_old",
        title="Old Archive Clip",
        uploader="julie-jess",
        target_type="model",
        status="uploaded",
        uploaded_at=now_iso,
        published_at=local_date_days_ago(90),
        force_status=True,
    )
    await repo.upsert_video(
        "vk_recent",
        title="Recent Clip",
        uploader="julie-jess",
        target_type="model",
        status="uploaded",
        uploaded_at=now_iso,
        published_at=local_date_days_ago(3),
        force_status=True,
    )
    await repo.upsert_video(
        "vk_blank",
        title="Undated Clip",
        uploader="julie-jess",
        target_type="model",
        status="downloaded",
        downloaded_at=now_iso,
        published_at="",
        force_status=True,
    )

    rows = await repo.list_targets()
    assert len(rows) == 1
    assert rows[0]["video_total_count"] == 3
    assert rows[0]["video_saved_count"] == 3
    assert rows[0]["today_downloaded_count"] == 2
    assert rows[0]["today_new_count"] == 2


async def test_query_videos_time_range_and_stats_published_7d(linkage_engine):
    _manager, repo, _env, _events = linkage_engine
    now_iso = utc_now_iso()
    old_activity_iso = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat(timespec="seconds")

    await repo.upsert_video(
        "vk_p3",
        title="Published 3d",
        uploader="julie-jess",
        status="uploaded",
        uploaded_at=now_iso,
        published_at=local_date_days_ago(3),
        force_status=True,
    )
    await repo.upsert_video(
        "vk_p5_base",
        title="Published 5d Baseline",
        uploader="julie-jess",
        status="baseline",
        published_at=local_date_days_ago(5),
        force_status=True,
    )
    await repo.upsert_video(
        "vk_p20",
        title="Published 20d",
        uploader="julie-jess",
        status="downloaded",
        downloaded_at=now_iso,
        published_at=local_date_days_ago(20),
        force_status=True,
    )
    await repo.upsert_video(
        "vk_p90_old_act",
        title="Old Published Old Upload",
        uploader="julie-jess",
        status="uploaded",
        uploaded_at=old_activity_iso,
        published_at=local_date_days_ago(90),
        force_status=True,
    )
    await repo.upsert_video(
        "vk_blank_today",
        title="Undated Downloaded Today",
        uploader="julie-jess",
        status="downloaded",
        downloaded_at=now_iso,
        published_at="",
        force_status=True,
    )

    stats = await repo.get_video_stats()
    assert stats["total"] == 5
    assert stats["published_7d"] == 2

    items_7d, total_7d = await repo.query_videos(time_range="published_7d")
    assert total_7d == 2
    assert {v["viewkey"] for v in items_7d} == {"vk_p3", "vk_p5_base"}

    items_7d_up, total_7d_up = await repo.query_videos(time_range="published_7d", status="uploaded")
    assert total_7d_up == 1
    assert [v["viewkey"] for v in items_7d_up] == ["vk_p3"]

    items_30d, total_30d = await repo.query_videos(time_range="published_30d")
    assert total_30d == 3
    assert {v["viewkey"] for v in items_30d} == {"vk_p3", "vk_p5_base", "vk_p20"}

    items_act_7d, total_act_7d = await repo.query_videos(time_range="activity_7d")
    assert total_act_7d == 3
    assert {v["viewkey"] for v in items_act_7d} == {"vk_p3", "vk_p20", "vk_blank_today"}
