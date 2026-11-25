"""List-page pagination parser and video link scanner."""

from __future__ import annotations

import asyncio
import random
import re
import urllib.parse
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Set, Tuple

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..cancel import StopRequested, until_stopped
from ..config import SelectorSettings
from ..services.filename import extract_date_from_cdn_url
from .http_session import TaskHttpContext
from .target import SubscribeAction, extract_subscribe_action, select_many, select_one
from .video import parse_duration_seconds

PH_BASE_URL = "https://www.pornhub.com"
_MAX_SCOPE = 10_000
_OFFLOAD_HTML_THRESHOLD = 32_768


@dataclass(frozen=True)
class ListedVideo:
    viewkey: str
    url: str
    title: str
    page_num: int
    item_num: int
    total_on_page: int
    published_at: str = ""
    duration_seconds: int = 0


def parse_page_specs(spec_str: Optional[str]) -> Optional[List[int]]:
    if not spec_str or not spec_str.strip():
        return None
    cleaned = spec_str.strip().lower()
    if cleaned == "all":
        return None  # Natural scan until end

    return sorted(_parse_positions(cleaned))


def _parse_positions(spec: str) -> Set[int]:
    positions: Set[int] = set()
    for part in spec.split(","):
        match = re.fullmatch(r"([0-9]{1,5})(?:\s*-\s*([0-9]{1,5}))?", part.strip())
        if not match:
            raise ValueError("页码或序号格式无效，应为 1,3-5")
        start, end = int(match[1]), int(match[2] or match[1])
        if not 1 <= start <= end <= _MAX_SCOPE:
            raise ValueError(f"页码或序号必须介于 1 和 {_MAX_SCOPE} 之间")
        positions.update(range(start, end + 1))
    return positions


def parse_page_video_specs(spec_str: Optional[str]) -> Optional[Dict[int, Set[int]]]:
    """Parse specs like '1-1,3,5-7;2-1' into {page_num: {0-based indices}}."""
    if not spec_str or not spec_str.strip():
        return None
    result: Dict[int, Set[int]] = {}
    for page_spec in spec_str.split(";"):
        page_spec = page_spec.strip()
        if not page_spec or "-" not in page_spec:
            raise ValueError("指定视频格式无效，应为 页码-序号，例如 2-1,3-5")
        p_str, idx_spec = page_spec.split("-", 1)
        if not re.fullmatch(r"[0-9]{1,5}", p_str.strip()) or not 1 <= int(p_str.strip()) <= _MAX_SCOPE:
            raise ValueError(f"页码必须介于 1 和 {_MAX_SCOPE} 之间")
        p_num = int(p_str.strip())
        result.setdefault(p_num, set()).update(idx - 1 for idx in _parse_positions(idx_spec))
        if sum(map(len, result.values())) > _MAX_SCOPE:
            raise ValueError(f"一次最多指定 {_MAX_SCOPE} 个视频位置")
    return result


def build_target_list_url(username: Optional[str], target_type: str) -> str:
    t = (target_type or "model").strip().lower()
    if t == "liked":
        return f"{PH_BASE_URL}/likedvideos"
    raw_name = (username or "").strip()
    if t == "favorites":
        quoted_lower = urllib.parse.quote(raw_name.lower(), safe="")
        return f"{PH_BASE_URL}/users/{quoted_lower}/videos/favorites"
    quoted = urllib.parse.quote(raw_name, safe="")
    if t == "pornstar":
        return f"{PH_BASE_URL}/pornstar/{quoted}/videos/upload"
    return f"{PH_BASE_URL}/{t}/{quoted}/videos"


def _extract_card_published_and_duration(li) -> Tuple[str, int]:
    published_at = ""
    duration_seconds = 0
    try:
        imgs = li.css("img")
    except Exception:
        imgs = []
    # Prefer `data-mediabook` (preview video URL) because it stays anchored to the
    # original ingest date even if a creator later swaps the custom thumbnail.
    for attr_name in ("data-mediabook", "data-path", "data-mediumthumb", "data-thumb_url", "data-src", "src"):
        for img in imgs:
            img_attrs = getattr(img, "attributes", {}) or {}
            cand = extract_date_from_cdn_url(img_attrs.get(attr_name))
            if cand:
                published_at = cand
                break
        if published_at:
            break
    if not published_at:
        raw_html = getattr(li, "html", "") or ""
        published_at = extract_date_from_cdn_url(raw_html)

    dur_node = select_one(li, ["var.duration", ".duration"])
    if dur_node is not None:
        duration_seconds = parse_duration_seconds(dur_node.text() or "")
    return published_at, duration_seconds


def extract_video_cards_from_list_page(
    html: str,
    selectors: SelectorSettings,
    target_type: Optional[str] = None,
) -> List[Tuple[str, str, str, str, int]]:
    """Extract unique ``(viewkey, full_url, title, published_at, duration_seconds)`` items from list HTML."""
    if not html:
        return []
    tree = HTMLParser(html)

    if target_type == "pornstar" and selectors.pornstar_page_video_container_check:
        primary = select_one(tree, [selectors.pornstar_page_video_container_check])
        if primary and not select_one(primary, ["li[data-video-vkey]"]):
            return []

    def _collect(element, sel_list: List[str]):
        nodes = []
        seen = set()
        for sel in sel_list:
            if not sel:
                continue
            try:
                for n in element.css(sel):
                    nid = id(n)
                    if nid not in seen:
                        seen.add(nid)
                        nodes.append(n)
            except Exception:
                continue
        return nodes

    li_items = []
    main_containers = _collect(tree, selectors.split_list("list_page_main_containers"))
    li_in_container = selectors.split_list("list_page_li_in_container")
    for container in main_containers:
        candidates = select_many(container, li_in_container)
        if len(candidates) > len(li_items):
            li_items = candidates

    if not li_items:
        li_items = _collect(tree, selectors.split_list("list_page_li_fallback"))

    if not li_items:
        try:
            li_items = list(tree.css("li[data-video-vkey]"))
        except Exception:
            li_items = []

    link_sels = selectors.split_list("list_page_video_link_tag_primary")
    if selectors.list_page_video_link_tag_fallback_href:
        link_sels.append(selectors.list_page_video_link_tag_fallback_href)
    combined_link_sel = ", ".join(link_sels)
    span_title_sels = selectors.split_list("list_page_video_span_div_for_title")

    results: List[Tuple[str, str, str, str, int]] = []
    seen_vkeys: Set[str] = set()

    for li in li_items:
        attrs = getattr(li, "attributes", {}) or {}
        vkey = attrs.get("_vkey") or attrs.get("data-video-vkey") or attrs.get("data-vkey")

        a_tag = None
        if combined_link_sel:
            try:
                found = li.css(combined_link_sel)
                a_tag = found[0] if found else None
            except Exception:
                a_tag = None

        href = (a_tag.attributes.get("href") or "").strip() if a_tag and hasattr(a_tag, "attributes") else ""
        if not (a_tag and (vkey or ("viewkey=" in href))):
            continue

        if href:
            m = re.search(r"viewkey=([a-zA-Z0-9]+)", href)
            if m:
                vkey = m.group(1)

        if not vkey or not href or vkey in seen_vkeys:
            continue

        title = (a_tag.attributes.get("title") or "").strip() if hasattr(a_tag, "attributes") else ""
        if not title and selectors.list_page_video_image_for_title:
            try:
                imgs = a_tag.css(selectors.list_page_video_image_for_title)
                if imgs and hasattr(imgs[0], "attributes"):
                    title = (imgs[0].attributes.get("alt") or "").strip()
            except Exception:
                pass
        if not title:
            tnode = select_one(li, span_title_sels)
            if tnode:
                title = (tnode.text() or "").strip()
        if not title:
            title = "未知标题"

        published_at, duration_seconds = _extract_card_published_and_duration(li)
        seen_vkeys.add(vkey)
        results.append((vkey, urllib.parse.urljoin(PH_BASE_URL, href), title, published_at, duration_seconds))

    return results


def extract_video_links_from_list_page(
    html: str,
    selectors: SelectorSettings,
    target_type: Optional[str] = None,
) -> List[Tuple[str, str, str]]:
    """Extract unique (viewkey, full_url, title) items from a list-page HTML."""
    return [
        (vkey, url, title)
        for vkey, url, title, _, _ in extract_video_cards_from_list_page(html, selectors, target_type=target_type)
    ]


def _normalize_avatar_name(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def extract_target_display_name(html: str, username: Optional[str] = None) -> str:
    """Return the site-facing profile name (for example ``KKFox baby``)."""
    if not html:
        return ""
    tree = HTMLParser(html)
    wanted_name = _normalize_avatar_name(username)

    for img in tree.css("img#getAvatar, img.jcrop-preview"):
        attrs = getattr(img, "attributes", {}) or {}
        alt = (attrs.get("alt") or "").strip()
        if alt:
            return alt

    for img in tree.css("img.avatarTrigger, img.avatar, img.small-avatar"):
        attrs = getattr(img, "attributes", {}) or {}
        alt = (attrs.get("alt") or "").strip()
        alt_name = _normalize_avatar_name(alt)
        if not alt:
            continue
        if wanted_name and alt_name and wanted_name not in alt_name and alt_name not in wanted_name:
            continue
        return alt
    return ""


def extract_target_avatar(html: str, username: Optional[str] = None) -> str:
    """Return the target profile avatar URL from a list/profile page.

    PornHub renders the profile avatar as ``img#getAvatar.jcrop-preview`` on
    model/user/favorites pages. The subscription grid uses the same image, so
    this keeps the card image at the highest resolution the site exposes.
    """
    if not html:
        return ""

    tree = HTMLParser(html)
    wanted_name = _normalize_avatar_name(username)

    def _src_of(img) -> str:
        attrs = getattr(img, "attributes", {}) or {}
        src = (attrs.get("src") or attrs.get("data-src") or "").strip()
        if not src or src.startswith("data:"):
            return ""
        url = urllib.parse.urljoin(PH_BASE_URL, src)
        if "/pics/users/default/" in url:
            return ""
        return url

    for img in tree.css("img#getAvatar, img.jcrop-preview"):
        src = _src_of(img)
        if src:
            return src

    for img in tree.css("img.avatarTrigger, img.avatar, img.small-avatar"):
        attrs = getattr(img, "attributes", {}) or {}
        alt_name = _normalize_avatar_name(attrs.get("alt"))
        if wanted_name and alt_name and wanted_name not in alt_name and alt_name not in wanted_name:
            continue
        src = _src_of(img)
        if src:
            return src
    return ""


def _build_paged_url(initial_url: str, page_num: int) -> str:
    if page_num <= 1:
        return initial_url
    parts = list(urllib.parse.urlparse(initial_url))
    qs = dict(urllib.parse.parse_qsl(parts[4]))
    qs["page"] = str(page_num)
    parts[4] = urllib.parse.urlencode(qs)
    return urllib.parse.urlunparse(parts)


async def scan_list_pages(
    ctx: TaskHttpContext,
    initial_url: str,
    selectors: SelectorSettings,
    *,
    target_type: Optional[str] = None,
    pages_spec: Optional[str] = None,
    videos_spec: Optional[str] = None,
    catch_up: bool = False,
    skip_vkeys: Optional[Set[str]] = None,
    delay_min: float = 0.5,
    delay_max: float = 1.5,
    page_concurrency: int = 1,
    timeout: float = 20.0,
    profile_username: Optional[str] = None,
    on_profile: Optional[Callable[[str, str], Awaitable[None]]] = None,
    on_subscribe_action: Optional[Callable[[SubscribeAction, str], Awaitable[None]]] = None,
    on_page: Optional[Callable[[List[ListedVideo]], Awaitable[None]]] = None,
    on_page_progress: Optional[Callable[[int, int, int], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
) -> List[ListedVideo]:
    """Scan list pages with bounded prefetch, optionally streaming each page to a callback."""
    specific_map = {} if catch_up else (parse_page_video_specs(videos_spec) or {})
    if catch_up:
        explicit_pages: Optional[List[int]] = None
    elif specific_map:
        explicit_pages = sorted(specific_map.keys())
    else:
        explicit_pages = parse_page_specs(pages_spec)

    is_natural = explicit_pages is None
    explicit_remaining = list(explicit_pages) if explicit_pages is not None else None
    next_natural_page = 1
    prefetch_size = max(1, int(page_concurrency))

    collected: List[ListedVideo] = []
    seen_all_vkeys: Set[str] = set()
    skip_set = set(skip_vkeys or ())
    pages_scanned = 0
    end_reached = False
    profile_found = False
    subscribe_notified = False

    def report_error(page: int, reason: str) -> None:
        message = f"列表第 {page} 页: {reason}"
        ctx._emit("WARNING", message)
        if on_error:
            on_error(message)

    async def _fetch_page(page_num: int):
        page_url = _build_paged_url(initial_url, page_num)
        ctx._emit("INFO", f"正在扫描列表第 {page_num} 页: {page_url}")
        res, err = await ctx.fetch_with_pow(page_url, timeout=max(5.0, float(timeout)), log_label=f"列表第 {page_num} 页")
        return page_num, page_url, res, err

    while not end_reached and not ctx.cancel_event.is_set():
        if explicit_remaining is not None:
            if not explicit_remaining:
                break
            batch_pages = explicit_remaining[:prefetch_size]
            explicit_remaining = explicit_remaining[prefetch_size:]
        else:
            batch_size = 1 if (catch_up and next_natural_page == 1) else prefetch_size
            batch_pages = list(range(next_natural_page, next_natural_page + batch_size))
            next_natural_page += len(batch_pages)

        if pages_scanned > 0 and delay_max > 0 and not ctx.cancel_event.is_set():
            try:
                await until_stopped(
                    asyncio.sleep(random.uniform(max(0.0, delay_min), max(delay_min, delay_max))),
                    ctx.cancel_event,
                )
            except StopRequested:
                break

        page_results = await asyncio.gather(*(_fetch_page(page_num) for page_num in batch_pages))
        batch_failed = False
        for current_page, page_url, res, err in page_results:
            if ctx.cancel_event.is_set():
                end_reached = True
                break
            pages_scanned += 1

            if res is None:
                if err != "操作已取消":
                    report_error(current_page, err or "获取失败")
                    batch_failed = True
                continue

            # 404 = beyond last page (PornHub returns a 690KB generic recommendations page on 404!)
            if res.status_code == 404 and current_page > 1 and is_natural:
                ctx._emit("INFO", f"列表第 {current_page} 页返回 404，已到达末页。")
                end_reached = True
                break
            if res.status_code >= 400:
                report_error(current_page, f"HTTP {res.status_code}")
                batch_failed = True
                continue

            # Detect server redirect back to page 1 or another page
            final = urllib.parse.urlparse(res.final_url)
            if "/login" in final.path.lower():
                report_error(current_page, "页面要求重新登录")
                batch_failed = True
                continue
            final_qs = urllib.parse.parse_qs(final.query)
            final_page_str = final_qs.get("page", [None])[0]
            page_mismatch = False
            if final_page_str and final_page_str.isdigit():
                if int(final_page_str) != current_page:
                    page_mismatch = True
            elif "page=" not in res.final_url.lower() and current_page > 1:
                page_mismatch = True

            if page_mismatch:
                ctx._emit("INFO", f"第 {current_page} 页重定向至 {res.final_url}，判定为末页。")
                if is_natural:
                    end_reached = True
                    break
                report_error(current_page, "上游重定向，没有返回指定页")
                continue

            want_profile = bool(on_profile and not profile_found)
            want_subscribe = bool(on_subscribe_action and not subscribe_notified)
            if len(res.text) >= _OFFLOAD_HTML_THRESHOLD:
                def _parse_page_sync(
                    html_body: str = res.text,
                    fetch_profile: bool = want_profile,
                    fetch_subscribe: bool = want_subscribe,
                ) -> Tuple[List[Tuple[str, str, str, str, int]], str, str, Optional[SubscribeAction]]:
                    av = extract_target_avatar(html_body, profile_username) if fetch_profile else ""
                    dn = extract_target_display_name(html_body, profile_username) if fetch_profile else ""
                    sub = extract_subscribe_action(html_body) if fetch_subscribe else None
                    items = extract_video_cards_from_list_page(html_body, selectors, target_type=target_type)
                    return items, av, dn, sub

                raw_items, avatar_url, display_name, subscribe_action = await asyncio.to_thread(_parse_page_sync)
            else:
                avatar_url = extract_target_avatar(res.text, profile_username) if want_profile else ""
                display_name = extract_target_display_name(res.text, profile_username) if want_profile else ""
                subscribe_action = extract_subscribe_action(res.text) if want_subscribe else None
                raw_items = extract_video_cards_from_list_page(res.text, selectors, target_type=target_type)

            if want_profile and (avatar_url or display_name):
                profile_found = True
                await on_profile(avatar_url, display_name)

            if want_subscribe and subscribe_action is not None:
                subscribe_notified = True
                await on_subscribe_action(subscribe_action, res.final_url or page_url)

            if not raw_items:
                if is_natural and current_page == 1:
                    if target_type == "favorites":
                        ctx._emit("WARNING", "Favorites 首页为空，可能无收藏、未公开或 Cookie 无权访问。")
                    else:
                        ctx._emit("WARNING", "列表首页未找到视频项。")
                if is_natural:
                    end_reached = True
                    break
                if specific_map:
                    report_error(current_page, "未找到指定的视频位置")
                continue

            added_this_page = 0
            new_unique_on_page = 0
            total_on_page = len(raw_items)
            if specific_map and any(index >= total_on_page for index in specific_map[current_page]):
                report_error(current_page, f"部分指定位置超出此页 {total_on_page} 个视频")
            page_videos: List[ListedVideo] = []
            for idx, (vkey, url, title, published_at, duration_seconds) in enumerate(raw_items, start=1):
                if specific_map and current_page in specific_map:
                    if (idx - 1) not in specific_map[current_page]:
                        continue
                if vkey in seen_all_vkeys:
                    continue
                seen_all_vkeys.add(vkey)
                new_unique_on_page += 1
                if vkey in skip_set:
                    continue
                page_videos.append(
                    ListedVideo(
                        viewkey=vkey,
                        url=url,
                        title=title,
                        page_num=current_page,
                        item_num=idx,
                        total_on_page=total_on_page,
                        published_at=published_at,
                        duration_seconds=duration_seconds,
                    )
                )
                added_this_page += 1

            if on_page and page_videos:
                await on_page(page_videos)
            collected.extend(page_videos)

            ctx._emit(
                "INFO",
                f"第 {current_page} 页解析到 {total_on_page} 个视频，筛选后待处理 {added_this_page} 个。",
            )
            if on_page_progress:
                on_page_progress(current_page, total_on_page, len(collected))

            if is_natural and current_page > 1 and new_unique_on_page == 0:
                ctx._emit("INFO", f"第 {current_page} 页无新增唯一视频，结束扫描。")
                end_reached = True
                break

            if catch_up and raw_items[-1][0] in skip_set and not batch_failed:
                ctx._emit("INFO", f"第 {current_page} 页已衔接已知视频，结束补扫。")
                end_reached = True
                break

        # Keep the valid pages already returned by this batch, but don't scan
        # arbitrarily far past a failed page without a known boundary.
        if is_natural and batch_failed:
            break

    return collected
