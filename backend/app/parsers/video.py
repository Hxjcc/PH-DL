"""Read the player configuration and resolve media sources."""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..config import SelectorSettings
from ..services.filename import extract_date_from_cdn_url, normalize_published_date
from .http_session import TaskHttpContext
from .target import SubscribeAction, extract_subscribe_action, select_one

_FLASHVARS_START_RE = re.compile(r"var\s+flashvars_\d+\s*=\s*\{")
_JSONLD_SCRIPT_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_SUBMITTED_DATE_RE = re.compile(r"Submitted:\s*(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)
_ISO_DURATION_RE = re.compile(
    r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$",
    re.IGNORECASE,
)


def parse_duration_seconds(raw: Any) -> int:
    """Parse ``MM:SS``, ``HH:MM:SS``, ISO-8601 ``PT..``, or numeric seconds into an integer."""
    if raw is None or isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        return max(0, int(raw))
    text = str(raw).strip()
    if not text:
        return 0
    if text.isdigit():
        return max(0, int(text))
    m_iso = _ISO_DURATION_RE.match(text)
    if m_iso and any(m_iso.groups()):
        hours = int(m_iso.group(1) or 0)
        minutes = int(m_iso.group(2) or 0)
        seconds = int(m_iso.group(3) or 0)
        return max(0, hours * 3600 + minutes * 60 + seconds)
    if ":" in text:
        parts = text.split(":")
        if 2 <= len(parts) <= 3 and all(p.strip().isdigit() for p in parts):
            nums = [int(p.strip()) for p in parts]
            if len(nums) == 2:
                return max(0, nums[0] * 60 + nums[1])
            return max(0, nums[0] * 3600 + nums[1] * 60 + nums[2])
    return 0


def extract_detail_published_and_duration(
    html: str,
    video_data: Optional[Dict[str, Any]] = None,
) -> Tuple[str, int]:
    """Extract ``(YYYY-MM-DD, duration_seconds)`` from detail-page HTML without DOM parsing."""
    published_at = ""
    duration_seconds = 0
    thumb_candidate = ""

    if html:
        for match in _JSONLD_SCRIPT_RE.finditer(html):
            body = (match.group(1) or "").strip()
            if not body:
                continue
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                continue
            nodes = parsed if isinstance(parsed, list) else [parsed]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                if not published_at:
                    published_at = normalize_published_date(node.get("uploadDate") or node.get("datePublished"))
                if duration_seconds <= 0:
                    duration_seconds = parse_duration_seconds(node.get("duration"))
                if not thumb_candidate and isinstance(node.get("thumbnailUrl"), str):
                    thumb_candidate = node["thumbnailUrl"]
            if published_at and duration_seconds > 0:
                break

        if not published_at:
            m_sub = _SUBMITTED_DATE_RE.search(html)
            if m_sub:
                published_at = normalize_published_date(m_sub.group(1))

    if isinstance(video_data, dict):
        if duration_seconds <= 0:
            duration_seconds = parse_duration_seconds(
                video_data.get("video_duration") or video_data.get("duration")
            )
        if not published_at:
            published_at = extract_date_from_cdn_url(
                video_data.get("image_url") or video_data.get("poster") or ""
            )

    if not published_at and thumb_candidate:
        published_at = extract_date_from_cdn_url(thumb_candidate)

    return published_at, duration_seconds


@dataclass(frozen=True)
class MediaSource:
    url: str
    quality: str


@dataclass
class VideoDetail:
    viewkey: str
    title: str
    page_url: str
    referer_url: str
    download_url: Optional[str] = None
    quality: str = ""
    video_format: str = "mp4"
    uploader_username: str = ""
    uploader_type: str = "model"
    page_num: int = 0
    item_num: int = 0
    error: Optional[str] = None
    filtered_out: bool = False
    mp4_sources: Tuple[MediaSource, ...] = ()
    published_at: str = ""
    duration_seconds: int = 0
    subscribe_action: Optional[SubscribeAction] = None


def extract_viewkey_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"(?:viewkey=|/embed/|/shorties/)([a-zA-Z0-9]+)", url)
    return m.group(1) if m else None


def _video_url_key(url: str) -> Optional[str]:
    u = (url or "").strip()
    if "\\" in u or any(char.isspace() for char in u):
        return None
    try:
        parsed = urllib.parse.urlparse(u)
    except ValueError:
        return None
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme not in {"https", "http"} or (host != "pornhub.com" and not host.endswith(".pornhub.com")):
        return None
    return extract_viewkey_from_url(u)


def is_video_url(url: str) -> bool:
    return _video_url_key(url) is not None


def collect_video_urls(values: Iterable[str]) -> tuple[list[str], int]:
    """Keep the first submitted link for each supported video identifier."""
    videos: dict[str, str] = {}
    ignored = 0
    for value in values:
        url = value.strip()
        if not url or url.startswith("#"):
            continue
        key = _video_url_key(url)
        if key is None:
            ignored += 1
        else:
            videos.setdefault(key, url)
    return list(videos.values()), ignored


def _extract_balanced_segment(text: str, start_idx: int, open_ch: str, close_ch: str) -> Optional[str]:
    if start_idx < 0 or start_idx >= len(text) or text[start_idx] != open_ch:
        return None
    depth = 0
    quote: Optional[str] = None
    escaped = False
    for idx in range(start_idx, len(text)):
        ch = text[idx]
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start_idx : idx + 1]
    return None


def _extract_shorties_data(html: str, current_url: str, viewkey: Optional[str]) -> Optional[Dict[str, Any]]:
    if "/shorties/" not in (current_url or "") and "JSON_SHORTIES" not in html:
        return None
    marker = "JSON_SHORTIES = insertAfterNthPosition("
    pos = html.find(marker)
    if pos < 0:
        return None
    arr_start = html.find("[", pos)
    if arr_start < 0:
        return None
    arr_str = _extract_balanced_segment(html, arr_start, "[", "]")
    if not arr_str:
        return None
    try:
        items = json.loads(arr_str)
    except json.JSONDecodeError:
        return None
    if not isinstance(items, list):
        return None

    norm_url = (current_url or "").split("?", 1)[0].rstrip("/")
    for item in items:
        if not isinstance(item, dict):
            continue
        ivk = str(item.get("vkey") or "").strip()
        s_url = str(item.get("shortieUrl") or "").split("?", 1)[0].rstrip("/")
        u_url = str(item.get("uniqueUrl") or "").split("?", 1)[0].rstrip("/")
        l_url = str(item.get("linkUrl") or "")
        matched = (viewkey and (ivk == viewkey or viewkey in s_url or viewkey in u_url or viewkey in l_url)) or (
            norm_url and norm_url in {s_url, u_url}
        )
        if matched and isinstance(item.get("mediaDefinitions"), list) and item["mediaDefinitions"]:
            data = dict(item)
            if "video_title" not in data and item.get("videoTitle"):
                data["video_title"] = item["videoTitle"]
            return data
    return None


def extract_flashvars(html: str, current_url: str = "", viewkey: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if "/shorties/" in (current_url or ""):
        shortie = _extract_shorties_data(html, current_url, viewkey)
        if shortie:
            return shortie

    m = _FLASHVARS_START_RE.search(html)
    if m:
        json_str = _extract_balanced_segment(html, m.end() - 1, "{", "}")
        if json_str:
            try:
                parsed = json.loads(json_str)
                if isinstance(parsed, dict) and "mediaDefinitions" in parsed:
                    return parsed
            except json.JSONDecodeError:
                pass

    tree = HTMLParser(html)
    generic_re = re.compile(r"var\s+\w+\s*=\s*(\{.*?\});", re.DOTALL)
    for script in tree.css("script"):
        if hasattr(script, "attributes") and script.attributes.get("src"):
            continue
        txt = script.text() or ""
        if "mediaDefinitions" not in txt:
            continue
        for match in generic_re.finditer(txt):
            try:
                parsed = json.loads(match.group(1))
                if isinstance(parsed, dict) and isinstance(parsed.get("mediaDefinitions"), list):
                    return parsed
            except json.JSONDecodeError:
                continue

    return _extract_shorties_data(html, current_url, viewkey)


def _extract_title(tree: HTMLParser, js_title: Optional[str], viewkey: Optional[str], url: str, selectors: SelectorSettings) -> str:
    h1 = select_one(tree, selectors.split_list("detail_page_h1_titles"))
    title = (h1.text() or "").strip() if h1 else ""
    if not title and selectors.detail_page_og_title_meta:
        og = select_one(tree, [selectors.detail_page_og_title_meta])
        if og and hasattr(og, "attributes"):
            title = (og.attributes.get("content") or "").strip()
    if not title or title.lower() == "pornhub.com":
        if selectors.detail_page_title_tag:
            t_tag = select_one(tree, [selectors.detail_page_title_tag])
            if t_tag:
                raw = (t_tag.text() or "").strip()
                if raw.lower().endswith(" - pornhub.com"):
                    raw = raw[: -len(" - pornhub.com")].strip()
                title = raw
    if not title and js_title:
        title = str(js_title).strip()
    if not title:
        title = f"未知标题_{viewkey or os.path.basename(url)}"
    return title


def _extract_uploader_from_video_data(video_data: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    if not isinstance(video_data, dict):
        return "", "model"
    profile_url = str(video_data.get("profileUrl") or "").strip()
    parts = [p for p in urllib.parse.urlparse(profile_url).path.split("/") if p]
    if len(parts) >= 2:
        utype = parts[0].lower()
        if utype in {"model", "pornstar", "user", "users"}:
            return parts[1], ("users" if utype in {"user", "users"} else utype)
    return "", "model"


def _extract_uploader(tree: HTMLParser, video_data: Optional[Dict[str, Any]], selectors: SelectorSettings) -> Tuple[str, str]:
    container = select_one(tree, selectors.split_list("detail_page_uploader_container"))
    if container:
        link = select_one(container, selectors.split_list("detail_page_uploader_link_in_container"))
        href = (link.attributes.get("href") or "").strip() if link and hasattr(link, "attributes") else ""
        parts = [p for p in urllib.parse.urlparse(href).path.split("/") if p]
        if len(parts) >= 2:
            utype = parts[0].lower()
            if utype in {"model", "pornstar", "user", "users"}:
                return parts[1], ("users" if utype in {"user", "users"} else utype)

    return _extract_uploader_from_video_data(video_data)


def is_mp4_media_option(option: Dict[str, Any]) -> bool:
    video_url = option.get("videoUrl")
    if not isinstance(video_url, str) or not video_url.strip():
        return False
    try:
        path = urllib.parse.urlparse(video_url).path.lower()
    except ValueError:
        return False
    if path.endswith(".m3u8") or path.endswith(".webm"):
        return False
    fmt = str(option.get("format") or "").strip().lower()
    if fmt:
        return fmt in {"mp4", "video/mp4"}
    return path.endswith(".mp4")


def _media_height(option: Dict[str, Any]) -> int:
    match = re.search(r"\d{1,4}(?!\d)", str(option.get("quality", "")))
    return int(match.group()) if match else 0


def _mp4_option_list(values: Any, base_url: str) -> List[Dict[str, Any]]:
    options = []
    for value in values if isinstance(values, list) else ():
        if not isinstance(value, dict) or not is_mp4_media_option(value):
            continue
        try:
            url = urllib.parse.urljoin(base_url, value["videoUrl"].strip())
        except ValueError:
            continue
        options.append({**value, "videoUrl": url})
    return options


def _describe_media_sources(options: List[Dict[str, Any]]) -> Tuple[MediaSource, ...]:
    sources = []
    for option in sorted(options, key=_media_height, reverse=True):
        height = _media_height(option)
        sources.append(MediaSource(option["videoUrl"], f"{height}p" if height else ""))
    return tuple(sources)


def select_best_mp4_option(
    options: List[Dict[str, Any]],
    preferred_qualities: Optional[Set[str]] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    usable = [opt for opt in options if isinstance(opt, dict) and is_mp4_media_option(opt)]
    if not usable:
        return None, None, "无可用 MP4 选项"

    if preferred_qualities:
        filtered = [opt for opt in usable if str(_media_height(opt)) in preferred_qualities]
        if filtered:
            usable = filtered
        else:
            q_str = ",".join(sorted(preferred_qualities, key=lambda x: int(x) if x.isdigit() else 0, reverse=True))
            return None, None, f"无偏好画质 ({q_str}p)"

    best = max(usable, key=_media_height)
    quality = str(best.get("quality", "0"))
    if quality.isdigit():
        quality += "p"
    return best["videoUrl"], quality, None


def _matches_keywords(
    title: str,
    include_kw: Optional[Set[str]],
    exclude_kw: Optional[Set[str]],
) -> Optional[str]:
    lower = (title or "").lower()
    if include_kw and not any(k in lower for k in include_kw):
        return "不含指定关键词"
    if exclude_kw and any(k in lower for k in exclude_kw):
        return "命中排除关键词"
    return None


_OFFLOAD_HTML_THRESHOLD = 32_768


def _parse_video_html_fields(
    html: str,
    final_url: str,
    vkey: str,
    selectors: SelectorSettings,
    uploader_hint: Optional[Tuple[str, str]],
    skip_dom: bool,
) -> Tuple[Optional[Dict[str, Any]], str, str, str, str, int, Optional[SubscribeAction]]:
    vdata = extract_flashvars(html, final_url, vkey)
    published_at, duration_seconds = extract_detail_published_and_duration(html, vdata)
    subscribe_action = extract_subscribe_action(html)
    js_title = (vdata or {}).get("video_title")
    title = html_lib.unescape(str(js_title).strip()) if js_title else ""
    if uploader_hint and uploader_hint[0]:
        uploader_name, uploader_type = uploader_hint
    else:
        uploader_name, uploader_type = _extract_uploader_from_video_data(vdata)

    if not skip_dom and (not title or not uploader_name):
        tree = HTMLParser(html)
        if not title:
            title = _extract_title(tree, js_title, vkey, final_url, selectors)
        if not uploader_name:
            uploader_name, uploader_type = _extract_uploader(tree, vdata, selectors)
    if not title:
        title = f"未知标题_{vkey or os.path.basename(final_url)}"
    return vdata, title, uploader_name, uploader_type, published_at, duration_seconds, subscribe_action


async def fetch_single_video_detail(
    ctx: TaskHttpContext,
    page_url: str,
    selectors: SelectorSettings,
    *,
    preferred_qualities: Optional[Set[str]] = None,
    include_keywords: Optional[Set[str]] = None,
    exclude_keywords: Optional[Set[str]] = None,
    uploader_hint: Optional[Tuple[str, str]] = None,
    skip_dom: bool = False,
    include_mp4_sources: bool = False,
    page_num: int = 0,
    item_num: int = 0,
    timeout: Optional[float] = None,
) -> VideoDetail:
    vkey = extract_viewkey_from_url(page_url) or ""
    page_timeout = max(5.0, float(timeout or 15.0))
    media_timeout = max(5.0, float(timeout or 12.0))

    async def _fetch_page():
        res, err = await ctx.fetch_with_pow(
            page_url, timeout=page_timeout, log_label=f"详情页 {vkey or page_url}",
        )
        if res is None:
            return None, page_url, err or "页面获取失败"
        if res.status_code == 404:
            return None, res.final_url, "404 无效/已删除视频"
        if res.status_code >= 400:
            return None, res.final_url, f"详情页返回 HTTP {res.status_code}"
        return res.text, res.final_url, None

    html, final_url, err = await _fetch_page()
    if err or html is None:
        return VideoDetail(
            viewkey=vkey,
            title=f"解析失败_{vkey}",
            page_url=page_url,
            referer_url=final_url,
            page_num=page_num,
            item_num=item_num,
            error=err or "页面为空",
        )

    retried_reload = False
    while True:
        if ctx.cancel_event.is_set():
            return VideoDetail(viewkey=vkey, title="", page_url=page_url, referer_url=final_url, error="操作已取消")

        if len(html) >= _OFFLOAD_HTML_THRESHOLD:
            vdata, title, uploader_name, uploader_type, published_at, duration_seconds, subscribe_action = await asyncio.to_thread(
                _parse_video_html_fields,
                html,
                final_url,
                vkey,
                selectors,
                uploader_hint,
                skip_dom,
            )
        else:
            vdata, title, uploader_name, uploader_type, published_at, duration_seconds, subscribe_action = _parse_video_html_fields(
                html,
                final_url,
                vkey,
                selectors,
                uploader_hint,
                skip_dom,
            )

        kw_reason = _matches_keywords(title, include_keywords, exclude_keywords)
        if kw_reason:
            return VideoDetail(
                viewkey=vkey,
                title=title,
                page_url=page_url,
                referer_url=final_url,
                uploader_username=uploader_name,
                uploader_type=uploader_type,
                page_num=page_num,
                item_num=item_num,
                error=kw_reason,
                filtered_out=True,
                published_at=published_at,
                duration_seconds=duration_seconds,
                subscribe_action=subscribe_action,
            )

        media_defs = (vdata or {}).get("mediaDefinitions")
        remote_url = None
        mp4_options: List[Dict[str, Any]] = []
        if isinstance(media_defs, list):
            for item in media_defs:
                if not isinstance(item, dict):
                    continue
                if item.get("remote") is True:
                    cand = item.get("videoUrl")
                    if remote_url is None and isinstance(cand, str) and cand.strip():
                        try:
                            remote_url = urllib.parse.urljoin(final_url, cand.strip())
                        except ValueError:
                            continue
                elif is_mp4_media_option(item):
                    mp4_options.extend(_mp4_option_list([item], final_url))

        media_lookup_failed = False
        if remote_url:
            gm_base = min(1.0, ctx.retry_delay_base) if ctx.retry_delay_base >= 0 else 1.0
            gm_cap = max(gm_base, min(6.0, ctx.retry_delay_max)) if ctx.retry_delay_max >= 0 else 6.0
            gm_res, gm_err = await ctx.fetch_with_pow(
                remote_url,
                timeout=media_timeout,
                max_retries=5,
                retry_delay_base=gm_base,
                retry_delay_max=gm_cap,
                log_label=f"get_media {vkey}",
            )
            if gm_res is not None and gm_res.status_code < 400:
                try:
                    raw_list = json.loads(gm_res.text)
                    if isinstance(raw_list, list):
                        mp4_options.extend(_mp4_option_list(raw_list, gm_res.final_url))
                    else:
                        media_lookup_failed = True
                except json.JSONDecodeError:
                    media_lookup_failed = True
            elif gm_err == "操作已取消":
                return VideoDetail(viewkey=vkey, title=title, page_url=page_url, referer_url=final_url, error="操作已取消")
            else:
                media_lookup_failed = True

        if not mp4_options:
            if not retried_reload and not ctx.cancel_event.is_set():
                retried_reload = True
                html, final_url, err = await _fetch_page()
                if err or html is None:
                    return VideoDetail(
                        viewkey=vkey,
                        title=title,
                        page_url=page_url,
                        referer_url=final_url,
                        uploader_username=uploader_name,
                        uploader_type=uploader_type,
                        page_num=page_num,
                        item_num=item_num,
                        error=err or "无可用 MP4 源",
                        published_at=published_at,
                        duration_seconds=duration_seconds,
                    )
                continue
            return VideoDetail(
                viewkey=vkey,
                title=title,
                page_url=page_url,
                referer_url=final_url,
                uploader_username=uploader_name,
                uploader_type=uploader_type,
                page_num=page_num,
                item_num=item_num,
                error="媒体接口获取失败" if media_lookup_failed else "无可用 MP4 媒体源",
                published_at=published_at,
                duration_seconds=duration_seconds,
                subscribe_action=subscribe_action,
            )

        dl_url, quality, sel_err = select_best_mp4_option(mp4_options, preferred_qualities)
        if sel_err or not dl_url:
            return VideoDetail(
                viewkey=vkey,
                title=title,
                page_url=page_url,
                referer_url=final_url,
                uploader_username=uploader_name,
                uploader_type=uploader_type,
                page_num=page_num,
                item_num=item_num,
                error="媒体接口获取失败，无法确认全部可用画质" if media_lookup_failed else sel_err or "筛选后无可用链接",
                filtered_out=bool(preferred_qualities and not media_lookup_failed),
                published_at=published_at,
                duration_seconds=duration_seconds,
                subscribe_action=subscribe_action,
            )

        return VideoDetail(
            viewkey=vkey,
            title=title,
            page_url=page_url,
            referer_url=final_url,
            download_url=dl_url,
            quality=quality or "",
            video_format="mp4",
            uploader_username=uploader_name,
            uploader_type=uploader_type,
            page_num=page_num,
            item_num=item_num,
            mp4_sources=_describe_media_sources(mp4_options) if include_mp4_sources else (),
            published_at=published_at,
            duration_seconds=duration_seconds,
            subscribe_action=subscribe_action,
        )


async def fetch_video_details_batch(
    ctx: TaskHttpContext,
    items: List[Tuple[str, int, int]],  # (page_url, page_num, item_num)
    selectors: SelectorSettings,
    *,
    concurrency: int = 5,
    preferred_qualities: Optional[Set[str]] = None,
    include_keywords: Optional[Set[str]] = None,
    exclude_keywords: Optional[Set[str]] = None,
    uploader_hint: Optional[Tuple[str, str]] = None,
    on_parsed: Optional[Callable[[int, int, VideoDetail], None]] = None,
    semaphore: Optional[asyncio.Semaphore] = None,
    skip_dom: bool = False,
    timeout: Optional[float] = None,
) -> List[VideoDetail]:
    if not items:
        return []
    sem = semaphore or asyncio.Semaphore(max(1, min(len(items), int(concurrency))))
    total = len(items)
    done_count = 0

    async def _worker(page_url: str, page_num: int, item_num: int) -> VideoDetail:
        nonlocal done_count
        async with sem:
            if ctx.cancel_event.is_set():
                return VideoDetail(
                    viewkey=extract_viewkey_from_url(page_url) or "",
                    title="",
                    page_url=page_url,
                    referer_url=page_url,
                    error="操作已取消",
                )
            detail = await fetch_single_video_detail(
                ctx,
                page_url,
                selectors,
                preferred_qualities=preferred_qualities,
                include_keywords=include_keywords,
                exclude_keywords=exclude_keywords,
                uploader_hint=uploader_hint,
                skip_dom=skip_dom,
                page_num=page_num,
                item_num=item_num,
                timeout=timeout,
            )
            done_count += 1
            if on_parsed:
                on_parsed(done_count, total, detail)
            return detail

    tasks = [asyncio.create_task(_worker(u, pn, itn)) for u, pn, itn in items]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    return list(results)
