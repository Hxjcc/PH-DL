"""Tests for PoW solver, TaskHttpContext pinning/sharing, list scanner, and video parser."""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from app.config import SelectorSettings
from app.parsers import challenge
from app.parsers.http_session import CHROME_FINGERPRINTS, PowFetchResult, TaskHttpContext
from app.parsers.list_scanner import (
    extract_target_avatar,
    extract_target_display_name,
    extract_video_links_from_list_page,
    parse_page_specs,
    parse_page_video_specs,
    scan_list_pages,
)
from app.parsers.video import (
    extract_flashvars,
    fetch_single_video_detail,
    is_mp4_media_option,
    is_video_url,
    select_best_mp4_option,
)

# p=100; s=5 (101b): bit0=1 -> p+=6 (106); bit1=0 -> p-=4 (102); tail p+=8 -> 110
# leastFactor(110)=2 -> KEY="2*55:5:424242:1"
SAMPLE_POW_HTML = """<html><body><script>
var p=100;
var s=5;
/* noise */
if ((s>>0)&1) p+=2*3; else p-=1*1;
if ((s>>1)&1) p+=10; else p-=4;
p+=8; n=leastFactor(p);
document.cookie="KEY="+n+"*"+(p/n)+":"+s+":424242:1";
location.reload(true);
</script></body></html>"""


def test_pow_challenge_detection_and_solve():
    assert challenge.is_challenge(SAMPLE_POW_HTML) is True
    assert challenge.is_challenge("<html><body>normal page</body></html>") is False
    assert challenge.solve_challenge(SAMPLE_POW_HTML) == "2*55:5:424242:1"


def test_chrome_fingerprints_exclude_safari():
    assert len(CHROME_FINGERPRINTS) > 0
    for fp in CHROME_FINGERPRINTS:
        assert fp.startswith("chrome")
        assert not fp.startswith("safari")


async def test_task_http_context_pins_fingerprint_and_shares_pow_cookies(monkeypatch):
    calls = []
    responses = [
        (SAMPLE_POW_HTML, "https://www.pornhub.com/page1", ["htjschalref=abc123; Path=/"], 200),
        ("<html>ok1</html>", "https://www.pornhub.com/page1", [], 200),
        ("<html>ok2</html>", "https://www.pornhub.com/page2", [], 200),
    ]

    ctx = TaskHttpContext(cookie_str="sess=base1", retry_delay_base=0.0, retry_delay_max=0.0)

    async def fake_raw_get(url, cookies, headers=None, timeout=None):
        calls.append((url, dict(cookies), ctx.impersonate))
        return responses.pop(0)

    monkeypatch.setattr(ctx, "_raw_get", fake_raw_get)

    res1, err1 = await ctx.fetch_with_pow("https://www.pornhub.com/page1")
    assert err1 is None and res1 is not None
    assert res1.text == "<html>ok1</html>"
    assert res1.challenge_solves == 1

    # Second request should automatically carry the solved KEY and htjschalref from shared pool!
    res2, err2 = await ctx.fetch_with_pow("https://www.pornhub.com/page2")
    assert err2 is None and res2 is not None
    assert res2.text == "<html>ok2</html>"
    assert res2.challenge_solves == 0

    assert len(calls) == 3
    assert calls[0][1] == {"sess": "base1"}
    assert calls[1][1]["KEY"] == "2*55:5:424242:1"
    assert calls[1][1]["htjschalref"] == "abc123"
    assert calls[2][1]["KEY"] == "2*55:5:424242:1"
    assert calls[2][1]["htjschalref"] == "abc123"
    # Same pinned fingerprint across all requests
    assert len({c[2] for c in calls}) == 1


async def test_task_http_context_absorbs_and_reuses_response_cookies(monkeypatch):
    ctx = TaskHttpContext(cookie_str="sess=base", retry_delay_base=0.0, retry_delay_max=0.0)
    await ctx._absorb_set_cookies(
        [
            "sessid=session-123; Path=/; HttpOnly",
            "platform=desktop; Path=/",
        ]
    )

    calls = []

    async def fake_raw_get(url, cookies, headers=None, timeout=None):
        calls.append(dict(cookies))
        return "<html>ok</html>", url, [], 200

    monkeypatch.setattr(ctx, "_raw_get", fake_raw_get)
    res, err = await ctx.fetch_with_pow("https://www.pornhub.com/model/alice/videos")

    assert err is None and res is not None
    assert calls == [
        {
            "sess": "base",
            "sessid": "session-123",
            "platform": "desktop",
        }
    ]
    assert "sessid=session-123" in ctx.cookie_string()


async def test_concurrent_pow_challenges_use_single_flight_solver(monkeypatch):
    ctx = TaskHttpContext(concurrency=5, retry_delay_base=0.0, retry_delay_max=0.0)
    challenged_once = set()

    async def fake_raw_get(url, cookies, headers=None, timeout=None):
        if url not in challenged_once:
            challenged_once.add(url)
            return SAMPLE_POW_HTML, url, ["htjschalref=abc123; Path=/"], 200
        return "<html>ok</html>", url, [], 200

    original_solve = challenge.solve_challenge
    solve_calls = 0

    def fake_solve(html: str) -> str:
        nonlocal solve_calls
        solve_calls += 1
        time.sleep(0.05)
        return original_solve(html)

    monkeypatch.setattr(ctx, "_raw_get", fake_raw_get)
    monkeypatch.setattr(challenge, "solve_challenge", fake_solve)

    results = await asyncio.gather(
        *[
            ctx.fetch_with_pow(f"https://www.pornhub.com/video/{idx}", max_retries=0)
            for idx in range(5)
        ]
    )

    assert all(err is None and res is not None for res, err in results)
    assert solve_calls == 1
    assert ctx.challenge_solve_count == 1
    assert ctx.challenge_reuse_count == 4
    assert ctx.pow_cookies["KEY"] == "2*55:5:424242:1"


async def test_task_http_context_retries_transient_403_and_includes_conn_error_detail(monkeypatch):
    logs = []
    ctx = TaskHttpContext(
        cookie_str="sess=base",
        max_retries=2,
        retry_delay_base=0.0,
        retry_delay_max=0.0,
        on_log=lambda lvl, msg: logs.append((lvl, msg)),
    )
    ctx.pow_cookies["KEY"] = "stale-key"
    attempts = 0

    async def fake_raw_get(url, cookies, headers=None, timeout=None):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            return "<html>forbidden</html>", url, [], 403
        return "<html>recovered</html>", url, [], 200

    monkeypatch.setattr(ctx, "_raw_get", fake_raw_get)
    res, err = await ctx.fetch_with_pow("https://www.pornhub.com/model/alice/videos", log_label="列表第 1 页")
    assert err is None and res is not None
    assert res.status_code == 200 and res.retried is True
    assert attempts == 3
    # Consecutive 403s clear stale PoW cookies so the session can recover cleanly.
    assert "KEY" not in ctx.pow_cookies
    assert any("HTTP 403" in msg for _, msg in logs)

    # ConnectionError logs and returned reason include underlying detail.
    async def broken_raw_get(url, cookies, headers=None, timeout=None):
        raise ConnectionError("curl: (56) Recv failure: Connection reset by peer")

    monkeypatch.setattr(ctx, "_raw_get", broken_raw_get)
    res2, err2 = await ctx.fetch_with_pow("https://www.pornhub.com/view_video.php?viewkey=vk1", max_retries=0)
    assert res2 is None and err2 is not None
    assert "Connection reset by peer" in err2


def test_list_page_extraction_chrome_fallback_and_dedup():
    selectors = SelectorSettings()
    items_html = "".join(
        f'<li class="pcVideoListItem videoBox" data-video-vkey="vk{i:03d}">'
        f'<a href="/view_video.php?viewkey=vk{i:03d}" title="Title {i}"></a></li>'
        for i in range(40)
    )
    chrome_html = f'<ul class="videos row-5-thumbs" id="mostRecentVideosSection">{items_html}</ul>'
    extracted = extract_video_links_from_list_page(chrome_html, selectors)
    assert len(extracted) == 40
    assert extracted[0][0] == "vk000"
    assert extracted[-1][0] == "vk039"

    # Bare fallback layout + dedup
    bare_html = (
        '<ul class="custom">'
        '<li data-video-vkey="dup1"><a href="/view_video.php?viewkey=dup1" title="A"></a></li>'
        '<li data-video-vkey="dup1"><a href="/view_video.php?viewkey=dup1" title="A2"></a></li>'
        '<li data-video-vkey="uni2"><a href="/view_video.php?viewkey=uni2" title="B"></a></li>'
        "</ul>"
    )
    bare_res = extract_video_links_from_list_page(bare_html, selectors)
    assert [x[0] for x in bare_res] == ["dup1", "uni2"]


def _header_dropdown_html() -> str:
    """PornHub's global top-nav dropdown menus rendered on every page (4 unrelated videos)."""
    return """
    <header id="header">
      <div id="headerMenuContainer">
        <ul id="headerMainMenu">
          <li id="menuItem2" class="menu item-2 videos">
            <div class="wideDropdown videos js-dropdown">
              <div id="dropdownHeaderSubMenu" class="headerSubMenu">
                <div class="innerHeaderSubMenu langTextSubMenu">
                  <ul id="hottestMenuSection" class="dropdownHottestVideos videos">
                    <li class="pcVideoListItem videoBox" data-video-vkey="hdrhot1">
                      <a href="/view_video.php?viewkey=hdrhot1" title="Header Hot 1"></a>
                    </li>
                    <li class="pcVideoListItem videoBox" data-video-vkey="hdrhot2">
                      <a href="/view_video.php?viewkey=hdrhot2" title="Header Hot 2"></a>
                    </li>
                  </ul>
                  <ul id="recommMenuSection" class="dropdownReccomendedVideos videos">
                    <li class="pcVideoListItem videoBox" data-video-vkey="hdrrec1">
                      <a href="/view_video.php?viewkey=hdrrec1" title="Header Rec 1"></a>
                    </li>
                    <li class="pcVideoListItem videoBox" data-video-vkey="hdrrec2">
                      <a href="/view_video.php?viewkey=hdrrec2" title="Header Rec 2"></a>
                    </li>
                  </ul>
                </div>
              </div>
            </div>
          </li>
        </ul>
      </div>
    </header>
    """


def test_empty_model_profile_ignores_header_dropdown_and_recommended_videos():
    selectors = SelectorSettings()
    # Mirrors live https://www.pornhub.com/model/kagome/videos (0 uploads, 4 header dropdown videos)
    empty_model_html = f"""
    <html><body>
      {_header_dropdown_html()}
      <div class="container amateur-videos-page">
        <div id="profileContent" class="claimed profileVideos">
          <div class="profileContentLeft">
            <section id="videosTab" class="clear-both">
              <div class="sectionWrapper">
                <div class="profileVids">
                  <div class="nf-wrapper"></div>
                  <div class="emptyBoxWrapper">
                    <span class="emptyIcon video"></span>
                    <p>There are no videos...</p>
                  </div>
                </div>
              </div>
            </section>
          </div>
        </div>
        <div class="recommendedVideos">
          <ul class="videos">
            <li class="videoBox" data-video-vkey="recbottom1">
              <a href="/view_video.php?viewkey=recbottom1" title="Recommended Bottom"></a>
            </li>
          </ul>
        </div>
      </div>
    </body></html>
    """
    assert extract_video_links_from_list_page(empty_model_html, selectors, target_type="model") == []
    assert extract_video_links_from_list_page(empty_model_html, selectors, target_type="pornstar") == []
    assert extract_video_links_from_list_page(empty_model_html, selectors, target_type=None) == []

    # Even without div.profileVids or emptyBoxWrapper (e.g. empty favorites/liked), header/rec videos are never scraped.
    empty_liked_html = f"""
    <html><body>
      {_header_dropdown_html()}
      <ul id="likedVideosSection" class="videos"></ul>
      <div class="promo"><li data-video-vkey="stray1"><a href="/view_video.php?viewkey=stray1" title="Stray"></a></li></div>
    </body></html>
    """
    assert extract_video_links_from_list_page(empty_liked_html, selectors, target_type="liked") == []

    header_only_html = f"<html><body>{_header_dropdown_html()}</body></html>"
    assert extract_video_links_from_list_page(header_only_html, selectors, target_type="favorites") == []

    # Populated model page with header dropdowns extracts ONLY the model's real videos.
    populated_model_html = f"""
    <html><body>
      {_header_dropdown_html()}
      <div id="profileContent" class="claimed profileVideos">
        <div class="profileVids">
          <div class="videoUList">
            <ul id="mostRecentVideosSection" class="full-row-thumbs videos row-5-thumbs">
              <li class="pcVideoListItem videoBox" data-video-vkey="realvk1">
                <a href="/view_video.php?viewkey=realvk1" title="Real Upload 1"></a>
              </li>
              <li class="pcVideoListItem videoBox" data-video-vkey="realvk2">
                <a href="/view_video.php?viewkey=realvk2" title="Real Upload 2"></a>
              </li>
            </ul>
          </div>
        </div>
      </div>
    </body></html>
    """
    assert [x[0] for x in extract_video_links_from_list_page(populated_model_html, selectors, target_type="model")] == [
        "realvk1",
        "realvk2",
    ]


async def test_list_scanner_rejects_off_target_redirect_and_allows_creator_promotion(monkeypatch):
    selectors = SelectorSettings()
    ctx = TaskHttpContext()
    errors: list[str] = []
    profile_calls: list[tuple[str, str]] = []
    requested: list[str] = []

    async def fake_redirect_to_directory(url, **kwargs):
        requested.append(url)
        # Deleted/non-existent model redirects to /pornstars with HTTP 200
        html = f"""
        <html><body>
          {_header_dropdown_html()}
          <img id="getAvatar" alt="Wrong Person" src="https://cdn.example/wrong.jpg" />
          <ul class="videos"><li data-video-vkey="dirvk1"><a href="/view_video.php?viewkey=dirvk1" title="Dir"></a></li></ul>
        </body></html>
        """
        return PowFetchResult(html, "https://www.pornhub.com/pornstars", 200, 0, False), None

    async def on_profile(av: str, dn: str):
        profile_calls.append((av, dn))

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_redirect_to_directory)
    listed = await scan_list_pages(
        ctx,
        "https://www.pornhub.com/model/ghost-user/videos",
        selectors,
        target_type="model",
        delay_min=0.0,
        delay_max=0.0,
        on_profile=on_profile,
        on_error=errors.append,
    )
    assert listed == []
    assert len(requested) == 1
    assert profile_calls == []
    assert len(errors) == 1 and "/pornstars" in errors[0]

    # Valid model -> pornstar promotion redirect must still scan normally,
    # and an off-target redirect on page 2 ends natural pagination without error.
    promo_errors: list[str] = []

    async def fake_promoted_creator(url, **kwargs):
        if "page=2" in url:
            return PowFetchResult(_header_dropdown_html(), "https://www.pornhub.com/pornstars", 200, 0, False), None
        html = (
            f"{_header_dropdown_html()}"
            '<div class="profileVids"><ul id="moreData" class="videos">'
            '<li class="videoBox" data-video-vkey="promovk1">'
            '<a href="/view_video.php?viewkey=promovk1" title="Promoted"></a></li></ul></div>'
        )
        return (
            PowFetchResult(html, "https://www.pornhub.com/pornstar/alice/videos/upload", 200, 0, False),
            None,
        )

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_promoted_creator)
    promoted = await scan_list_pages(
        ctx,
        "https://www.pornhub.com/model/alice/videos",
        selectors,
        target_type="model",
        delay_min=0.0,
        delay_max=0.0,
        on_error=promo_errors.append,
    )
    assert [v.viewkey for v in promoted] == ["promovk1"]
    assert promo_errors == []


def test_extract_target_avatar_prefers_profile_image_and_skips_default():
    html = """
    <div class="nav"><img class="smallAvatar" src="https://ei.phncdn.com/pics/users/default/pornhub/male.jpg" alt="Avatar"></div>
    <div class="previewAvatarPicture">
      <img id="getAvatar" class="jcrop-preview" alt="KKFox baby"
           src="https://ei.phncdn.com/pics/users/0034/0859/9091/avatar97164805/(m=ewILGCjadOf)(mh=feUnLdPGRnpZcjPi)200x200.jpg">
    </div>
    """
    assert extract_target_avatar(html, "kkfox-baby") == (
        "https://ei.phncdn.com/pics/users/0034/0859/9091/avatar97164805/"
        "(m=ewILGCjadOf)(mh=feUnLdPGRnpZcjPi)200x200.jpg"
    )
    assert extract_target_display_name(html, "kkfox-baby") == "KKFox baby"

    only_default = (
        '<img class="smallAvatar" '
        'src="https://ei.phncdn.com/pics/users/default/pornhub/(m=x)(mh=y)male.jpg" alt="Avatar">'
        '<img class="avatarTrigger" '
        'src="https://ei.phncdn.com/pics/users/default/pornhub/(m=x)(mh=y)male.jpg" alt="Nobody">'
    )
    assert extract_target_avatar(only_default, "nobody") == ""


async def test_list_scanner_stops_on_404_page(monkeypatch):
    selectors = SelectorSettings()
    ctx = TaskHttpContext()
    pages_fetched = []

    async def fake_fetch(url, **kwargs):
        pages_fetched.append(url)
        if "page=2" in url:
            return PowFetchResult(text="<html>recommended videos</html>", final_url=url, status_code=404, challenge_solves=0, retried=False), None
        html = (
            '<ul class="videos" id="mostRecentVideosSection">'
            '<li class="videoBox" data-video-vkey="vk001">'
            '<a href="/view_video.php?viewkey=vk001" title="First"></a></li></ul>'
        )
        return PowFetchResult(text=html, final_url=url, status_code=200, challenge_solves=0, retried=False), None

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_fetch)

    listed = await scan_list_pages(
        ctx,
        "https://www.pornhub.com/model/alice/videos",
        selectors,
        delay_min=0.0,
        delay_max=0.0,
    )
    assert [v.viewkey for v in listed] == ["vk001"]
    assert len(pages_fetched) == 2


async def test_list_scanner_returns_all_items_or_selected_positions(monkeypatch):
    selectors = SelectorSettings()
    ctx = TaskHttpContext()

    async def fake_fetch(url, **kwargs):
        html = (
            '<ul class="videos" id="mostRecentVideosSection">'
            '<li class="videoBox" data-video-vkey="vk1">'
            '<a href="/view_video.php?viewkey=vk1" title="One"></a></li>'
            '<li class="videoBox" data-video-vkey="vk2">'
            '<a href="/view_video.php?viewkey=vk2" title="Two"></a></li>'
            "</ul>"
        )
        return PowFetchResult(html, url, 200, 0, False), None

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_fetch)
    listed = await scan_list_pages(
        ctx,
        "https://www.pornhub.com/model/alice/videos",
        selectors,
        delay_min=0.0,
        delay_max=0.0,
    )
    assert [item.viewkey for item in listed] == ["vk1", "vk2"]

    selected = await scan_list_pages(
        ctx,
        "https://www.pornhub.com/model/alice/videos",
        selectors,
        videos_spec="1-2",
        delay_min=0.0,
        delay_max=0.0,
    )
    assert [item.viewkey for item in selected] == ["vk2"]


async def test_list_scanner_prefetches_four_pages_and_streams_in_page_order(monkeypatch):
    selectors = SelectorSettings()
    ctx = TaskHttpContext()
    active = 0
    max_active = 0
    requested_pages = []
    callback_pages = []

    async def fake_fetch(url, **kwargs):
        nonlocal active, max_active
        page_num = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
        requested_pages.append(page_num)
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)
            html = (
                '<ul class="videos" id="mostRecentVideosSection">'
                f'<li class="videoBox" data-video-vkey="vk{page_num:03d}">'
                f'<a href="/view_video.php?viewkey=vk{page_num:03d}" title="Page {page_num}"></a>'
                "</li></ul>"
            )
            return PowFetchResult(html, url, 200, 0, False), None
        finally:
            active -= 1

    async def on_page(page_items):
        callback_pages.append(page_items[0].page_num)
        await asyncio.sleep(0)

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_fetch)
    listed = await scan_list_pages(
        ctx,
        "https://www.pornhub.com/model/alice/videos",
        selectors,
        pages_spec="1-6",
        page_concurrency=4,
        delay_min=0.0,
        delay_max=0.0,
        on_page=on_page,
    )

    assert max_active == 4
    assert sorted(requested_pages) == [1, 2, 3, 4, 5, 6]
    assert callback_pages == [1, 2, 3, 4, 5, 6]
    assert [item.page_num for item in listed] == [1, 2, 3, 4, 5, 6]


def test_flashvars_balanced_brackets_handles_semicolons_in_title():
    payload = {
        "video_title": "Part 1; Part 2; Best!",
        "mediaDefinitions": [{"remote": True, "videoUrl": "https://www.pornhub.com/get_media"}],
    }
    html = f"<script>var flashvars_12345 = {json.dumps(payload)}; var next = 1;</script>"
    parsed = extract_flashvars(html)
    assert parsed is not None
    assert parsed["video_title"] == "Part 1; Part 2; Best!"


def test_mp4_filter_and_quality_selection():
    opts = [
        {"videoUrl": "https://cdn.example/fake.mp4/master.m3u8", "quality": "2160", "format": "mp4"},
        {"videoUrl": "https://cdn.example/master.m3u8", "quality": "2160", "format": "hls"},
        {"videoUrl": "https://cdn.example/clip.webm", "quality": "1440", "format": "webm"},
        {"videoUrl": "https://cdn.example/clip_1080.mp4?t=1", "quality": "1080", "format": "mp4"},
        {"videoUrl": "https://cdn.example/clip_720.mp4?t=1", "quality": "720", "format": "mp4"},
    ]
    assert not is_mp4_media_option(opts[0])
    assert is_mp4_media_option(opts[3])

    url, q, err = select_best_mp4_option(opts, preferred_qualities=None)
    assert err is None and url == "https://cdn.example/clip_1080.mp4?t=1" and q == "1080p"

    url2, q2, err2 = select_best_mp4_option(opts, preferred_qualities={"720"})
    assert err2 is None and url2 == "https://cdn.example/clip_720.mp4?t=1" and q2 == "720p"


async def test_detail_page_404_fast_fails(monkeypatch):
    ctx = TaskHttpContext()

    async def fake_fetch(url, **kwargs):
        return PowFetchResult(text="<html>404 page</html>", final_url=url, status_code=404, challenge_solves=0, retried=False), None

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_fetch)
    detail = await fetch_single_video_detail(
        ctx,
        "https://www.pornhub.com/view_video.php?viewkey=deadbeef",
        SelectorSettings(),
    )
    assert detail.download_url is None
    assert detail.error is not None and "404" in detail.error


async def test_flashvars_fast_path_skips_dom_when_uploader_is_known(monkeypatch):
    from app.parsers import video as video_module

    ctx = TaskHttpContext()
    page_url = "https://www.pornhub.com/view_video.php?viewkey=fastpath"
    media_url = "https://www.pornhub.com/video/get_media?v=fastpath"
    payload = {
        "video_title": "Fast &amp; Clean; Title",
        "mediaDefinitions": [
            {"remote": True, "format": "mp4", "videoUrl": media_url}
        ],
    }
    html = f"<script>var flashvars_1 = {json.dumps(payload)};</script>"

    async def fake_fetch(url, **kwargs):
        if url == page_url:
            return PowFetchResult(html, page_url, 200, 0, False), None
        if url == media_url:
            body = json.dumps(
                [
                    {
                        "format": "mp4",
                        "quality": "2160",
                        "videoUrl": "https://cdn.example/fastpath.mp4",
                    }
                ]
            )
            return PowFetchResult(body, media_url, 200, 0, False), None
        raise AssertionError(f"unexpected URL: {url}")

    def forbidden_dom(_html):
        raise AssertionError("DOM parser must not run on the fast path")

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_fetch)
    monkeypatch.setattr(video_module, "HTMLParser", forbidden_dom)
    detail = await fetch_single_video_detail(
        ctx,
        page_url,
        SelectorSettings(),
        uploader_hint=("alice", "model"),
    )

    assert detail.error is None
    assert detail.title == "Fast & Clean; Title"
    assert detail.uploader_username == "alice"
    assert detail.uploader_type == "model"
    assert detail.quality == "2160p"
    assert detail.download_url == "https://cdn.example/fastpath.mp4"


async def test_detail_without_uploader_hint_still_falls_back_to_dom(monkeypatch):
    ctx = TaskHttpContext()
    page_url = "https://www.pornhub.com/view_video.php?viewkey=domfallback"
    media_url = "https://www.pornhub.com/video/get_media?v=domfallback"
    payload = {
        "mediaDefinitions": [
            {"remote": True, "format": "mp4", "videoUrl": media_url}
        ]
    }
    html = f"""
    <script>var flashvars_1 = {json.dumps(payload)};</script>
    <h1 class="title"><span class="inlineFree">DOM Title</span></h1>
    <div class="userInfo"><a href="/model/alice">Alice</a></div>
    """

    async def fake_fetch(url, **kwargs):
        if url == page_url:
            return PowFetchResult(html, page_url, 200, 0, False), None
        if url == media_url:
            body = json.dumps(
                [
                    {
                        "format": "mp4",
                        "quality": "1080",
                        "videoUrl": "https://cdn.example/domfallback.mp4",
                    }
                ]
            )
            return PowFetchResult(body, media_url, 200, 0, False), None
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_fetch)
    detail = await fetch_single_video_detail(
        ctx,
        page_url,
        SelectorSettings(),
    )

    assert detail.error is None
    assert detail.title == "DOM Title"
    assert detail.uploader_username == "alice"
    assert detail.uploader_type == "model"
    assert detail.quality == "1080p"


async def test_metadata_mode_can_skip_dom_without_uploader_hint(monkeypatch):
    from app.parsers import video as video_module

    ctx = TaskHttpContext()
    page_url = "https://www.pornhub.com/view_video.php?viewkey=nodom"
    media_url = "https://www.pornhub.com/video/get_media?v=nodom"
    payload = {
        "mediaDefinitions": [
            {"remote": True, "format": "mp4", "videoUrl": media_url}
        ]
    }
    html = f"<script>var flashvars_1 = {json.dumps(payload)};</script>"

    async def fake_fetch(url, **kwargs):
        if url == page_url:
            return PowFetchResult(html, page_url, 200, 0, False), None
        if url == media_url:
            body = json.dumps(
                [
                    {
                        "format": "mp4",
                        "quality": "1440",
                        "videoUrl": "https://cdn.example/nodom.mp4",
                    }
                ]
            )
            return PowFetchResult(body, media_url, 200, 0, False), None
        raise AssertionError(f"unexpected URL: {url}")

    def forbidden_dom(_html):
        raise AssertionError("metadata mode must not build a DOM")

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_fetch)
    monkeypatch.setattr(video_module, "HTMLParser", forbidden_dom)
    detail = await fetch_single_video_detail(
        ctx,
        page_url,
        SelectorSettings(),
        skip_dom=True,
    )

    assert detail.error is None
    assert detail.title == "未知标题_nodom"
    assert detail.uploader_username == ""
    assert detail.quality == "1440p"
    assert detail.download_url == "https://cdn.example/nodom.mp4"


def test_video_url_validation_rejects_foreign_hosts():
    assert is_video_url("https://www.pornhub.com/view_video.php?viewkey=vk1")
    assert is_video_url("https://pornhub.com/embed/vk2")
    assert is_video_url("https://de.pornhub.com/shorties/vk3")
    assert not is_video_url("https://evil.example/view_video.php?viewkey=vk4")
    assert not is_video_url("https://pornhub.com.evil.example/view_video.php?viewkey=vk5")


def test_page_and_video_spec_parsers():
    assert parse_page_specs("1-3,5") == [1, 2, 3, 5]
    assert parse_page_specs("all") is None
    assert parse_page_video_specs("1-1,3-4;2-2") == {1: {0, 2, 3}, 2: {1}}


async def test_subscriptions_filter_users_and_build_target_urls(monkeypatch):
    from app.parsers.list_scanner import build_target_list_url
    from app.parsers.target import fetch_subscriptions, parse_target_type_from_url

    assert parse_target_type_from_url("https://www.pornhub.com/users/slutyou9") == "users"
    assert build_target_list_url(None, "liked") == "https://www.pornhub.com/likedvideos"
    assert build_target_list_url("Hxiay", "favorites") == "https://www.pornhub.com/users/hxiay/videos/favorites"
    assert build_target_list_url("amatemure", "model") == "https://www.pornhub.com/model/amatemure/videos"

    ctx = TaskHttpContext()
    requested_urls: list[str] = []

    async def fake_fetch(url, **kwargs):
        requested_urls.append(url)
        if "page=1" in url:
            html = """
            <ul id="moreData" class="userWidgetWrapperGrid">
              <li><div class="user-flag"><a class="userLink" href="/model/aquari"><img alt="AquaRi" data-src="//cdn.example/aquari.jpg" src="data:image/gif;base64,empty"></a></div></li>
              <li><img src="/avatars/user.jpg"><div class="user-flag"><a class="userLink" href="https://www.pornhub.com/users/slutyou9">slutyou9</a></div></li>
              <li><div class="user-flag"><a class="userLink" href="/pornstar/eva-elfie"><img src="/pics/users/default/male.jpg">Eva</a></div></li>
              <li><div class="user-flag"><a class="userLink" href="/model/aquari?duplicate=1">Duplicate</a></div></li>
            </ul>
            """
            return PowFetchResult(text=html, final_url=url, status_code=200, challenge_solves=0, retried=False), None
        return PowFetchResult(text='<ul id="moreData"></ul>', final_url=url, status_code=200, challenge_solves=0, retried=False), None

    monkeypatch.setattr(ctx, "fetch_with_pow", fake_fetch)
    subs = await fetch_subscriptions(ctx, "Hxiay", SelectorSettings())
    assert requested_urls[0] == "https://www.pornhub.com/users/hxiay/subscriptions?page=1"
    assert subs == [
        ("model", "aquari", "AquaRi", "https://cdn.example/aquari.jpg"),
        ("pornstar", "eva-elfie", "Eva", ""),
    ]

    # Title fallback inside <li> when thumbnail <img> has no alt
    list_html = """
    <ul id="mostRecentVideosSection" class="full-row-thumbs videos row-5-thumbs">
      <li class="pcVideoListItem videoBox" data-video-vkey="6afadbe041ccb">
        <a href="/view_video.php?viewkey=6afadbe041ccb" title=""><img src="x.jpg" /></a>
        <span class="title"><a href="/view_video.php?viewkey=6afadbe041ccb">Forest Title</a></span>
      </li>
    </ul>
    """
    items = extract_video_links_from_list_page(list_html, SelectorSettings())
    assert items == [("6afadbe041ccb", "https://www.pornhub.com/view_video.php?viewkey=6afadbe041ccb", "Forest Title")]


def test_cookie_token_extraction_with_selectolax():
    from app.services.cookie import _extract_login_form_fields, _extract_token_and_redirect

    html = (
        '<form><input type="hidden" name="token" value="tok_abc123" />'
        '<input type="hidden" name="redirect" value="/user/security" /></form>'
    )
    tok, redir = _extract_token_and_redirect(html)
    assert tok == "tok_abc123"
    assert redir == "/user/security"

    meta_html = '<head><meta name="csrf-token" content="meta_tok_99" /></head>'
    assert _extract_token_and_redirect(meta_html) == ("meta_tok_99", None)

    template_html = (
        '<template id="signinModal"><form class="js-loginFormModal js-loginForm">'
        '<input type="hidden" name="redirect" value="enc_redir_val" />'
        '<input type="hidden" name="token" value="tpl_tok_777" />'
        '<input type="hidden" name="from" value="pc_login_modal_:login" />'
        "</form></template>"
    )
    assert _extract_login_form_fields(template_html) == (
        "tpl_tok_777",
        "enc_redir_val",
        "pc_login_modal_:login",
    )


def test_auth_json_failure_and_login_page_detection():
    from app.services.cookie import _is_auth_json_failure, _looks_like_login_page

    assert _is_auth_json_failure({"success": "1", "username": "Hxiay"}) is False
    assert _is_auth_json_failure({"success": True}) is False
    assert _is_auth_json_failure({"success": "0", "message": "Invalid"}) is True
    assert _is_auth_json_failure({"success": 0, "message": "Invalid"}) is True
    assert _is_auth_json_failure({"success": False, "message": "Invalid"}) is True
    assert _is_auth_json_failure({"error": "Captcha required"}) is True

    assert _looks_like_login_page('<v-login-form modal="false"></v-login-form>') is True
    assert _looks_like_login_page('<input id="usernameModal"><input id="passwordModal">') is True
    assert _looks_like_login_page("<html><body>Account Security Settings</body></html>") is False


async def test_check_cookie_valid_rejects_pow_challenge_and_redirects(monkeypatch):
    from app.services import cookie as cookie_mod

    results = [
        # 1. Unresolved PoW challenge MUST NOT falsely pass!
        (None, "PoW 质询未通过"),
        # 2. Redirect to homepage MUST NOT falsely pass!
        (PowFetchResult("<html><body>Home</body></html>", "https://www.pornhub.com/", 200, 0, False), None),
        # 3. Inline login form MUST NOT falsely pass!
        (PowFetchResult('<v-login-form modal="false"></v-login-form>', "https://www.pornhub.com/settings/security", 200, 0, False), None),
        # 4. Real security page (/settings/security) passes
        (PowFetchResult("<html><body>Account Security Settings</body></html>", "https://www.pornhub.com/settings/security", 200, 0, False), None),
        # 5. Legacy /user/security URL also passes
        (PowFetchResult("<html><body>Account Security Settings</body></html>", "https://www.pornhub.com/user/security", 200, 0, False), None),
    ]

    class _FakeContext:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def fetch_with_pow(self, *args, **kwargs):
            return results.pop(0)

    monkeypatch.setattr(cookie_mod, "TaskHttpContext", _FakeContext)

    r1 = await cookie_mod.check_cookie_valid("sess=abc")
    assert r1.valid is False and "PoW" in r1.message

    r2 = await cookie_mod.check_cookie_valid("sess=abc")
    assert r2.valid is False and "重定向" in r2.message

    r3 = await cookie_mod.check_cookie_valid("sess=abc")
    assert r3.valid is False

    r4 = await cookie_mod.check_cookie_valid("sess=abc")
    assert r4.valid is True

    r5 = await cookie_mod.check_cookie_valid("sess=abc")
    assert r5.valid is True
