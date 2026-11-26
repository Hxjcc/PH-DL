"""xHamster parser: URL detection, 7-PRNG media decryption, list/favorites/subscription scanning, and video details."""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import os
import random
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional, Set, Tuple

from ..cancel import StopRequested, until_stopped
from ..services.filename import normalize_published_date
from .http_session import TaskHttpContext, parse_cookie_string

XH_ORIGIN = "https://xhamster.com"
_XH_HOST_RE = re.compile(
    r"^(?:[a-z0-9-]+\.)*xhamster\d*\.(?:com|desi|one|tv|xxx|live)$",
    re.IGNORECASE,
)
_XH_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_XH_PRINTABLE_RE = re.compile(r"^[\x20-\x7E]+$")
_XH_INITIALS_RE = re.compile(r"window\.initials\s*=\s*\{")
_XH_RESOLUTION_RE = re.compile(r"RESOLUTION\s*=\s*(\d+)\s*x\s*(\d+)", re.IGNORECASE)
_XH_BANDWIDTH_RE = re.compile(r"(?:AVERAGE-)?BANDWIDTH\s*=\s*(\d+)", re.IGNORECASE)
_XH_CODECS_RE = re.compile(r'CODECS\s*=\s*"([^"]+)"', re.IGNORECASE)
_XH_QUALITY_IN_URI_RE = re.compile(r"(?:^|[/_.-])(\d{3,4})p(?=[._/-]|$)", re.IGNORECASE)
_OFFLOAD_HTML_THRESHOLD = 32_768


def is_xhamster_host(host: str) -> bool:
    clean = (host or "").strip().rstrip(".").lower()
    return bool(clean and _XH_HOST_RE.fullmatch(clean))


def is_xhamster_url(url: Optional[str]) -> bool:
    raw = (url or "").strip()
    if not raw or "\\" in raw or any(ch.isspace() for ch in raw):
        return False
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError:
        return False
    return parsed.scheme in {"https", "http"} and is_xhamster_host(parsed.hostname or "")


def detect_site_from_url(url: Optional[str]) -> str:
    return "xhamster" if is_xhamster_url(url) else "pornhub"


def extract_xhamster_viewkey(url: Optional[str]) -> Optional[str]:
    """Extract canonical xHamster video identifier (e.g. ``xhOwQIZ`` or legacy ``xh_12345678``)."""
    raw = (url or "").strip()
    if not raw:
        return None
    if not is_xhamster_url(raw):
        return None
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError:
        return None
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    head = parts[0].lower()
    if head in {"videos", "movies"}:
        slug = parts[1].strip()
        m_hash = re.search(r"(?:^|-)(xh[A-Za-z0-9]{4,})$", slug)
        if m_hash:
            return m_hash.group(1)
        m_num = re.search(r"(?:^|-)(\d{4,})$", slug)
        if m_num:
            return f"xh_{m_num.group(1)}"
        return None
    if head == "embed":
        slug = parts[1].strip()
        if re.fullmatch(r"xh[A-Za-z0-9]{4,}", slug):
            return slug
        if re.fullmatch(r"\d{4,}", slug):
            return f"xh_{slug}"
    return None


def is_xhamster_video_url(url: Optional[str]) -> bool:
    return extract_xhamster_viewkey(url) is not None


def _xh_headers(referer: str = "") -> Dict[str, str]:
    return {
        "Referer": referer or f"{XH_ORIGIN}/",
        "Accept-Language": "en-US,en;q=0.9",
    }


# ---------------------------------------------------------------------------
# 7-PRNG Hex URL Decryption (exact port of xplayer-no-ads.js `oY` & `oV`)
# ---------------------------------------------------------------------------
def _prng_bytes(algo: int, seed: int) -> Iterator[int]:
    i = seed & 0xFFFFFFFF
    if algo == 1:
        while True:
            i = ((i * 1664525) + 0x3C6EF35F) & 0xFFFFFFFF
            yield i & 0xFF
    elif algo == 2:
        while True:
            i ^= (i << 13) & 0xFFFFFFFF
            i ^= i >> 17
            i ^= (i << 5) & 0xFFFFFFFF
            yield i & 0xFF
    elif algo == 3:
        while True:
            i = (i + 0x9E3779B9) & 0xFFFFFFFF
            e = i
            e ^= e >> 16
            e = (e * 0x85EBCA77) & 0xFFFFFFFF
            e ^= e >> 13
            e = (e * 0xC2B2AE3D) & 0xFFFFFFFF
            e ^= e >> 16
            yield e & 0xFF
    elif algo == 4:
        while True:
            i = (i + 0x6D2B79F5) & 0xFFFFFFFF
            e = ((i << 7) | (i >> 25)) & 0xFFFFFFFF
            e = (e + 0x9E3779B9) & 0xFFFFFFFF
            e ^= e >> 11
            e = (e * 0x27D4EB2D) & 0xFFFFFFFF
            yield e & 0xFF
    elif algo == 5:
        while True:
            i ^= (i << 7) & 0xFFFFFFFF
            i ^= i >> 9
            i ^= (i << 8) & 0xFFFFFFFF
            i = (i + 0xA5A5A5A5) & 0xFFFFFFFF
            yield i & 0xFF
    elif algo == 6:
        while True:
            i = ((i * 0x2C9277B5) + 0xAC564B05) & 0xFFFFFFFF
            yield ((i ^ (i >> 18)) >> ((i >> 27) & 31)) & 0xFF
    elif algo == 7:
        while True:
            i = (i + 0x9E3779B9) & 0xFFFFFFFF
            e = (i ^ ((i << 5) & 0xFFFFFFFF)) & 0xFFFFFFFF
            e = (e * 0x7FEB352D) & 0xFFFFFFFF
            e ^= e >> 15
            e = (e * 0x846CA68B) & 0xFFFFFFFF
            yield e & 0xFF
    else:
        raise ValueError(f"Unknown xHamster algoId: {algo}")


def decode_xhamster_hex(encoded: str) -> Optional[str]:
    """Decode a full hex-obfuscated xHamster string (``oY``). Returns ``None`` if not encoded."""
    if not isinstance(encoded, str):
        return None
    length = len(encoded)
    if length < 12 or (length & 1) != 0 or not _XH_HEX_RE.fullmatch(encoded):
        return None
    try:
        raw = bytes.fromhex(encoded)
    except ValueError:
        return None
    if len(raw) < 6:
        return None
    algo = raw[0]
    if algo not in (1, 2, 3, 4, 5, 6, 7):
        return None
    seed = raw[1] | (raw[2] << 8) | (raw[3] << 16) | (raw[4] << 24)
    gen = _prng_bytes(algo, seed)
    out = bytes((b ^ next(gen)) & 0xFF for b in raw[5:])
    try:
        decoded = out.decode("utf-8")
    except UnicodeDecodeError:
        decoded = out.decode("latin-1")
    if decoded and _XH_PRINTABLE_RE.fullmatch(decoded):
        return decoded
    return None


def decode_xhamster_url(value: str) -> str:
    """Decode either a full hex-obfuscated URL or an obfuscated first path token (``oV``)."""
    if not isinstance(value, str) or not value:
        return value
    direct = decode_xhamster_hex(value)
    if direct is not None:
        return f"https:{direct}" if direct.startswith("//") else direct

    work = f"https:{value}" if value.startswith("//") else value
    scheme_pos = work.find("://")
    slash_pos = work.find("/", scheme_pos + 3) if scheme_pos != -1 else work.find("/")
    if slash_pos != -1 and slash_pos + 1 < len(work):
        start = slash_pos + 1
        next_slash = work.find("/", start)
        next_comma = work.find(",", start)
        if next_slash != -1 and next_comma != -1:
            end = min(next_slash, next_comma)
        elif next_slash != -1:
            end = next_slash
        elif next_comma != -1:
            end = next_comma
        else:
            end = -1
        if end > start:
            decoded_seg = decode_xhamster_hex(work[start:end])
            if decoded_seg is not None:
                return work[:start] + decoded_seg + work[end:]

    return work


def decode_xhamster_sources(obj: Any) -> Any:
    """Recursively decrypt ``url`` and ``fallback`` entries inside ``xplayerSettings.sources``."""
    if isinstance(obj, list):
        return [decode_xhamster_sources(item) for item in obj]
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                out[k] = decode_xhamster_sources(v)
            elif k in ("url", "fallback") and isinstance(v, str):
                out[k] = decode_xhamster_url(v)
            else:
                out[k] = v
        return out
    return obj


# ---------------------------------------------------------------------------
# window.initials JSON extraction
# ---------------------------------------------------------------------------
def _extract_balanced_brace(text: str, start_idx: int) -> Optional[str]:
    if start_idx < 0 or start_idx >= len(text) or text[start_idx] != "{":
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
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start_idx : idx + 1]
    return None


def extract_xhamster_initials(html: str) -> Optional[Dict[str, Any]]:
    """Extract and parse the ``window.initials = {...}`` JSON payload from any xHamster page."""
    if not html:
        return None
    m = _XH_INITIALS_RE.search(html)
    if not m:
        return None
    brace_start = m.end() - 1
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, brace_start)
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass
    segment = _extract_balanced_brace(html, brace_start)
    if not segment:
        return None
    try:
        parsed = json.loads(segment)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Target URL builders, canonicalization, and type resolution
# ---------------------------------------------------------------------------
def build_xhamster_list_url(username: Optional[str], target_type: str, page: int = 1) -> str:
    t = (target_type or "creator").strip().lower()
    p = max(1, int(page or 1))
    if t == "favorites":
        return f"{XH_ORIGIN}/my/favorites/videos" if p == 1 else f"{XH_ORIGIN}/my/favorites/videos?page={p}"
    quoted = urllib.parse.quote((username or "").strip(), safe="")
    if t in {"user", "users"}:
        return f"{XH_ORIGIN}/users/{quoted}/videos" if p == 1 else f"{XH_ORIGIN}/users/{quoted}/videos/{p}"
    if t == "pornstar":
        return f"{XH_ORIGIN}/pornstars/{quoted}/newest" if p == 1 else f"{XH_ORIGIN}/pornstars/{quoted}/newest/{p}"
    if t == "channel":
        return f"{XH_ORIGIN}/channels/{quoted}/newest" if p == 1 else f"{XH_ORIGIN}/channels/{quoted}/newest/{p}"
    # Default: creator (including legacy "model" fallback)
    return f"{XH_ORIGIN}/creators/{quoted}/newest" if p == 1 else f"{XH_ORIGIN}/creators/{quoted}/newest/{p}"


def parse_xhamster_target_from_url(url: str) -> Optional[Tuple[str, str]]:
    """Extract ``(target_type, username)`` from an xHamster profile/list URL."""
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    parts = [urllib.parse.unquote(p).strip() for p in parsed.path.strip("/").split("/") if p]
    if not parts:
        return None
    head = parts[0].lower()
    if head == "my" and len(parts) >= 2 and parts[1].lower() == "favorites":
        return "favorites", "_currentUser_"
    if len(parts) < 2:
        return None
    slug = parts[1].strip()
    if not slug:
        return None
    if head == "creators":
        return "creator", slug
    if head == "pornstars":
        return "pornstar", slug
    if head == "users":
        return "users", slug
    if head == "channels":
        return "channel", slug
    return None


def _build_xhamster_paged_url(initial_url: str, target_type: Optional[str], page_num: int) -> str:
    p = max(1, int(page_num))
    if p <= 1:
        return initial_url
    parsed = urllib.parse.urlparse(initial_url)
    path = parsed.path.rstrip("/")
    norm_type = (target_type or "").strip().lower()
    if norm_type == "favorites" or "/my/favorites" in path.lower():
        qs = dict(urllib.parse.parse_qsl(parsed.query))
        qs["page"] = str(p)
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(qs)))
    # Strip trailing numeric page segment if present
    clean_path = re.sub(r"/\d+$", "", path)
    return urllib.parse.urlunparse(parsed._replace(path=f"{clean_path}/{p}"))


async def resolve_xhamster_target_type(
    ctx: TaskHttpContext,
    username: str,
    hinted_type: Optional[str] = None,
    timeout: float = 15.0,
) -> Optional[str]:
    norm_user = (username or "").strip()
    if not norm_user or norm_user == "_currentUser_":
        return None

    ordered: List[str] = []
    hint = (hinted_type or "").strip().lower()
    if hint in {"creator", "pornstar", "users", "channel"}:
        ordered.append(hint)
    for cand in ("creator", "pornstar", "users", "channel"):
        if cand not in ordered:
            ordered.append(cand)

    for cand in ordered:
        if ctx.cancel_event.is_set():
            return None
        url = build_xhamster_list_url(norm_user, cand, page=1)
        res, _ = await ctx.fetch_with_pow(
            url,
            headers=_xh_headers(),
            timeout=max(5.0, float(timeout)),
            max_retries=1,
            log_label=f"识别 xHamster 目标 {norm_user}",
        )
        if res is None or res.status_code >= 400:
            continue
        detected = parse_xhamster_target_from_url(res.final_url)
        if detected and detected[0] in {"creator", "pornstar", "users", "channel"}:
            return detected[0]
    return None


# ---------------------------------------------------------------------------
# List-page parsing from window.initials
# ---------------------------------------------------------------------------
def _timestamp_to_ymd(raw: Any) -> str:
    if raw is None or isinstance(raw, bool):
        return ""
    if isinstance(raw, (int, float)):
        ts = int(raw)
        if ts > 1_000_000_000:
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            except (OSError, OverflowError, ValueError):
                return ""
        return ""
    text = str(raw).strip()
    if text.isdigit() and len(text) >= 9:
        return _timestamp_to_ymd(int(text))
    return normalize_published_date(text)


def _normalize_thumb_url(raw: Any) -> str:
    url = str(raw or "").strip()
    if not url or url.startswith("data:"):
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = urllib.parse.urljoin(XH_ORIGIN, url)
    if not url.startswith(("http://", "https://")):
        return ""
    return url


def _extract_profile_meta_from_initials(
    initials: Dict[str, Any],
    target_type: Optional[str] = None,
) -> Tuple[str, str]:
    """Return ``(avatar_url, display_name)`` from xHamster ``window.initials``."""
    avatar_url = ""
    display_name = ""

    def _pick_from_dict(d: Any) -> None:
        nonlocal avatar_url, display_name
        if not isinstance(d, dict):
            return
        if not display_name:
            for key in ("name", "displayName", "title", "pageTitle", "channelName", "pornstarName"):
                val = str(d.get(key) or "").strip()
                if val and val.lower() not in {"xhamster", "videos", "favorites"}:
                    display_name = html_lib.unescape(val)
                    break
        if not avatar_url:
            for key in (
                "imageUserAvatarUrl",
                "avatarURL",
                "avatarUrl",
                "avatar",
                "thumbURL",
                "thumbUrl",
                "imageThumbUrl",
                "logoThumbUrl",
                "logo",
                "icon",
            ):
                cand = _normalize_thumb_url(d.get(key))
                if cand:
                    avatar_url = cand
                    break

    norm_type = (target_type or "").strip().lower()
    if norm_type == "favorites":
        _pick_from_dict(initials.get("profile"))
        return avatar_url, display_name or "xHamster Favorites"

    candidates = [
        initials.get("profile"),
        initials.get("infoComponent"),
        (initials.get("infoComponent") or {}).get("displayUserModel") if isinstance(initials.get("infoComponent"), dict) else None,
        (initials.get("infoComponent") or {}).get("pornstar") if isinstance(initials.get("infoComponent"), dict) else None,
        (initials.get("infoComponent") or {}).get("channel") if isinstance(initials.get("infoComponent"), dict) else None,
        (initials.get("infoComponent") or {}).get("entity") if isinstance(initials.get("infoComponent"), dict) else None,
        (initials.get("layoutPage") or {}).get("channel") if isinstance(initials.get("layoutPage"), dict) else None,
        (initials.get("layoutPage") or {}).get("pornstar") if isinstance(initials.get("layoutPage"), dict) else None,
        (initials.get("layoutPage") or {}).get("creator") if isinstance(initials.get("layoutPage"), dict) else None,
        initials.get("entity"),
    ]
    for cand in candidates:
        _pick_from_dict(cand)
        if avatar_url and display_name:
            break

    # Strip boilerplate suffixes like " Porn Videos" or " - xHamster"
    if display_name:
        display_name = re.sub(r"\s*[-|]\s*xhamster.*$", "", display_name, flags=re.IGNORECASE).strip()
        display_name = re.sub(r"\s+porn\s+videos?$", "", display_name, flags=re.IGNORECASE).strip()

    return avatar_url, display_name


def _extract_video_thumb_list_from_initials(
    initials: Dict[str, Any],
    target_type: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """Locate the primary video thumb list and optional max page number in ``window.initials``."""
    norm_type = (target_type or "").strip().lower()
    max_pages: Optional[int] = None

    def _read_int(val: Any) -> Optional[int]:
        if isinstance(val, bool) or val is None:
            return None
        if isinstance(val, int) and val > 0:
            return val
        if isinstance(val, str) and val.strip().isdigit() and int(val.strip()) > 0:
            return int(val.strip())
        return None

    # Extract max page hints
    for key in ("maxVideoPages", "maxPages"):
        v = _read_int(initials.get(key))
        if v:
            max_pages = v
            break
    if max_pages is None:
        for comp_key in ("paginationComponent", "paginationProps", "favoritesVideoPaging"):
            comp = initials.get(comp_key)
            if isinstance(comp, dict):
                for pkey in ("lastPageNumber", "maxPages", "maxPage", "lastPage"):
                    v = _read_int(comp.get(pkey))
                    if v:
                        max_pages = v
                        break
            if max_pages is not None:
                break

    def _as_thumb_list(val: Any) -> List[Dict[str, Any]]:
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict):
            for k in ("videoThumbProps", "videos", "items", "models", "collection"):
                sub = val.get(k)
                if isinstance(sub, list):
                    return [x for x in sub if isinstance(x, dict)]
                if isinstance(sub, dict):
                    nested = _as_thumb_list(sub)
                    if nested:
                        return nested
        return []

    if norm_type == "favorites":
        for key in ("favoritesVideoCollection", "favoriteVideos", "videoListComponent", "layoutPage"):
            items = _as_thumb_list(initials.get(key))
            if items:
                return items, max_pages
        return [], max_pages

    if norm_type in {"user", "users"}:
        for key in ("videoListComponent", "userVideoListComponent", "layoutPage"):
            items = _as_thumb_list(initials.get(key))
            if items:
                return items, max_pages
        return [], max_pages

    # Creators, pornstars, channels
    candidate_paths: List[Any] = [
        (initials.get("trendingVideoSectionComponent") or {}).get("videoListProps")
        if isinstance(initials.get("trendingVideoSectionComponent"), dict)
        else None,
        (initials.get("newestVideoSectionComponent") or {}).get("videoListProps")
        if isinstance(initials.get("newestVideoSectionComponent"), dict)
        else None,
        (initials.get("publisherVideoSectionComponent") or {}).get("videoListProps")
        if isinstance(initials.get("publisherVideoSectionComponent"), dict)
        else None,
        (initials.get("layoutPage") or {}).get("videoListProps")
        if isinstance(initials.get("layoutPage"), dict)
        else None,
        initials.get("layoutPage"),
        initials.get("videoListComponent"),
        initials.get("channelVideoListComponent"),
    ]
    for cand in candidate_paths:
        items = _as_thumb_list(cand)
        if items:
            return items, max_pages

    return [], max_pages


def extract_xhamster_video_cards(
    html: str,
    target_type: Optional[str] = None,
) -> Tuple[List[Tuple[str, str, str, str, int]], str, str, Optional[int]]:
    """Parse list HTML into ``(items, avatar_url, display_name, max_pages)``."""
    initials = extract_xhamster_initials(html)
    if not initials:
        return [], "", "", None

    avatar_url, display_name = _extract_profile_meta_from_initials(initials, target_type=target_type)
    raw_thumbs, max_pages = _extract_video_thumb_list_from_initials(initials, target_type=target_type)

    results: List[Tuple[str, str, str, str, int]] = []
    seen_vkeys: Set[str] = set()
    for item in raw_thumbs:
        page_url = str(item.get("pageURL") or item.get("url") or "").strip()
        if page_url and page_url.startswith("/"):
            page_url = urllib.parse.urljoin(XH_ORIGIN, page_url)
        vkey = extract_xhamster_viewkey(page_url)
        if not vkey:
            id_slug = str(item.get("idHashSlug") or "").strip()
            if id_slug:
                vkey = id_slug
            elif item.get("id"):
                vkey = f"xh_{item['id']}"
        if not vkey or vkey in seen_vkeys:
            continue
        if not page_url:
            page_url = f"{XH_ORIGIN}/videos/{vkey}"
        title = html_lib.unescape(str(item.get("title") or "").strip()) or f"未知标题_{vkey}"
        published_at = _timestamp_to_ymd(item.get("created"))
        raw_dur = item.get("duration")
        duration_seconds = max(0, int(raw_dur)) if isinstance(raw_dur, (int, float)) and not isinstance(raw_dur, bool) else 0
        seen_vkeys.add(vkey)
        results.append((vkey, page_url, title, published_at, duration_seconds))

    return results, avatar_url, display_name, max_pages


def _is_xhamster_off_target(initial_url: str, final_url: str, target_type: Optional[str]) -> bool:
    if not initial_url or not final_url:
        return False
    fin = urllib.parse.urlparse(final_url)
    fin_path = fin.path.strip("/").lower()
    if not fin_path:
        return True
    norm_type = (target_type or "").strip().lower()
    if norm_type == "favorites":
        return not fin_path.startswith("my/favorites")
    parsed = parse_xhamster_target_from_url(final_url)
    if parsed is None:
        return True
    return False


async def scan_xhamster_list_pages(
    ctx: TaskHttpContext,
    initial_url: str,
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
    on_canonical_target: Optional[Callable[[str, str], Awaitable[None]]] = None,
    on_page: Optional[Callable[[List[Any]], Awaitable[None]]] = None,
    on_page_progress: Optional[Callable[[int, int, int], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
) -> List[Any]:
    """Scan xHamster list pages, returning ``List[ListedVideo]``."""
    from .list_scanner import ListedVideo, parse_page_specs, parse_page_video_specs

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
    canonical_notified = False
    active_list_url = initial_url
    effective_target_type = target_type
    effective_profile_username = profile_username
    known_max_pages: Optional[int] = None

    def report_error(page: int, reason: str) -> None:
        message = f"列表第 {page} 页: {reason}"
        ctx._emit("WARNING", message)
        if on_error:
            on_error(message)

    async def _fetch_page(page_num: int):
        page_url = _build_xhamster_paged_url(active_list_url, effective_target_type, page_num)
        ctx._emit("INFO", f"正在扫描 xHamster 列表第 {page_num} 页: {page_url}")
        res, err = await ctx.fetch_with_pow(
            page_url,
            headers=_xh_headers(),
            timeout=max(5.0, float(timeout)),
            log_label=f"xHamster 列表第 {page_num} 页",
        )
        return page_num, page_url, res, err

    while not end_reached and not ctx.cancel_event.is_set():
        if explicit_remaining is not None:
            if not explicit_remaining:
                break
            batch_pages = explicit_remaining[:prefetch_size]
            explicit_remaining = explicit_remaining[prefetch_size:]
        else:
            if known_max_pages is not None and next_natural_page > known_max_pages:
                break
            batch_size = 1 if (catch_up and next_natural_page == 1) else prefetch_size
            if known_max_pages is not None:
                batch_size = min(batch_size, max(1, known_max_pages - next_natural_page + 1))
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

            if res.status_code == 404 and current_page > 1 and is_natural:
                ctx._emit("INFO", f"xHamster 列表第 {current_page} 页返回 404，已到达末页。")
                end_reached = True
                break
            if res.status_code >= 400:
                report_error(current_page, f"HTTP {res.status_code}")
                batch_failed = True
                continue

            final = urllib.parse.urlparse(res.final_url)
            if "/login" in final.path.lower():
                report_error(current_page, "xHamster Cookie 未登录或已失效，请重新配置")
                batch_failed = True
                end_reached = True
                break
            if _is_xhamster_off_target(initial_url, res.final_url, effective_target_type):
                if current_page > 1 and is_natural:
                    ctx._emit("INFO", f"第 {current_page} 页重定向至非目标页面 {res.final_url}，判定为末页。")
                    end_reached = True
                    break
                report_error(current_page, f"页面重定向至 {final.path or '/'}，目标可能已不存在或已变更")
                batch_failed = True
                if is_natural:
                    end_reached = True
                    break
                continue

            if not canonical_notified and (effective_target_type or "").lower() != "favorites":
                canonical = parse_xhamster_target_from_url(res.final_url)
                if canonical is not None and canonical[0] in {"creator", "pornstar", "users", "channel"}:
                    canonical_notified = True
                    canon_type, canon_user = canonical
                    req_type = (effective_target_type or "").strip().lower()
                    req_user = (effective_profile_username or "").strip()
                    user_changed = bool(
                        req_user and req_user != "_currentUser_" and canon_user.lower() != req_user.lower()
                    )
                    type_changed = bool(req_type and canon_type != req_type)
                    if user_changed or type_changed:
                        effective_target_type = canon_type
                        effective_profile_username = canon_user
                        active_list_url = build_xhamster_list_url(canon_user, canon_type, page=1)
                        if on_canonical_target is not None:
                            await on_canonical_target(canon_type, canon_user)

            if len(res.text) >= _OFFLOAD_HTML_THRESHOLD:
                raw_items, avatar_url, display_name, max_pages = await asyncio.to_thread(
                    extract_xhamster_video_cards, res.text, effective_target_type
                )
            else:
                raw_items, avatar_url, display_name, max_pages = extract_xhamster_video_cards(
                    res.text, effective_target_type
                )

            if max_pages is not None and (known_max_pages is None or max_pages < known_max_pages):
                known_max_pages = max_pages

            if on_profile and not profile_found and (avatar_url or display_name):
                profile_found = True
                await on_profile(avatar_url, display_name)

            if not raw_items:
                if is_natural and current_page == 1:
                    if (effective_target_type or "").lower() == "favorites":
                        ctx._emit("WARNING", "xHamster Favorites 首页为空，可能无收藏或 Cookie 无权访问。")
                    else:
                        ctx._emit("WARNING", "xHamster 列表首页未找到视频项。")
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
                f"xHamster 第 {current_page} 页解析到 {total_on_page} 个视频，筛选后待处理 {added_this_page} 个。",
            )
            if on_page_progress:
                on_page_progress(current_page, total_on_page, len(collected))

            if is_natural and known_max_pages is not None and current_page >= known_max_pages:
                end_reached = True
                break

            if is_natural and current_page > 1 and new_unique_on_page == 0:
                ctx._emit("INFO", f"xHamster 第 {current_page} 页无新增唯一视频，结束扫描。")
                end_reached = True
                break

            if catch_up and raw_items[-1][0] in skip_set and not batch_failed:
                ctx._emit("INFO", f"xHamster 第 {current_page} 页已衔接已知视频，结束补扫。")
                end_reached = True
                break

        if is_natural and batch_failed:
            break

    return collected


# ---------------------------------------------------------------------------
# HLS Master Playlist Parsing & Video Detail Resolution
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HlsVariant:
    url: str
    height: int
    quality: str
    bandwidth: int = 0
    codecs: str = ""


def parse_hls_master_variants(m3u8_text: str, base_url: str) -> List[HlsVariant]:
    """Parse an HLS master playlist into sorted ``HlsVariant`` entries."""
    if not m3u8_text or "#EXT-X-STREAM-INF" not in m3u8_text:
        return []
    lines = [line.strip() for line in m3u8_text.splitlines() if line.strip()]
    variants: List[HlsVariant] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = line[len("#EXT-X-STREAM-INF:") :]
            height = 0
            m_res = _XH_RESOLUTION_RE.search(attrs)
            if m_res:
                height = int(m_res.group(2))
            bw = 0
            m_bw = _XH_BANDWIDTH_RE.search(attrs)
            if m_bw:
                bw = int(m_bw.group(1))
            codecs = ""
            m_c = _XH_CODECS_RE.search(attrs)
            if m_c:
                codecs = m_c.group(1).strip()

            uri = ""
            j = idx + 1
            while j < len(lines):
                if not lines[j].startswith("#"):
                    uri = lines[j]
                    break
                j += 1
            if uri:
                if height <= 0:
                    m_q = _XH_QUALITY_IN_URI_RE.search(uri)
                    if m_q:
                        height = int(m_q.group(1))
                full_uri = urllib.parse.urljoin(base_url, uri)
                variants.append(
                    HlsVariant(
                        url=full_uri,
                        height=height,
                        quality=f"{height}p" if height > 0 else "",
                        bandwidth=bw,
                        codecs=codecs,
                    )
                )
            idx = j + 1
            continue
        idx += 1
    variants.sort(key=lambda v: (v.height, v.bandwidth), reverse=True)
    return variants


def _extract_standard_mp4_options(sources: Dict[str, Any]) -> List[Dict[str, Any]]:
    standard = sources.get("standard")
    if not isinstance(standard, dict):
        return []
    raw_h264 = standard.get("h264")
    if not isinstance(raw_h264, list):
        return []
    options: List[Dict[str, Any]] = []
    for entry in raw_h264:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url or ".m3u8" in url.lower():
            continue
        q_raw = str(entry.get("quality") or "").strip()
        if q_raw.lower() == "auto":
            continue
        m_num = re.search(r"\d{3,4}", q_raw)
        if not m_num:
            m_num = _XH_QUALITY_IN_URI_RE.search(url)
        quality_str = f"{m_num.group(0)}p" if m_num else q_raw
        options.append(
            {
                "videoUrl": url,
                "fallbackUrl": str(entry.get("fallback") or "").strip(),
                "quality": quality_str,
                "format": "mp4",
            }
        )
    return options


def _extract_hls_master_candidates(sources: Dict[str, Any], preferred_codec: str = "h264") -> List[Tuple[str, str]]:
    """Return ordered list of ``(codec, master_m3u8_url)`` according to ``preferred_codec``."""
    hls = sources.get("hls") if isinstance(sources.get("hls"), dict) else {}
    standard = sources.get("standard") if isinstance(sources.get("standard"), dict) else {}

    def _collect_codec(codec: str) -> List[Tuple[str, str]]:
        urls: List[Tuple[str, str]] = []
        seen: Set[str] = set()

        def _add(u: Any) -> None:
            s = str(u or "").strip()
            if s and ".m3u8" in s.lower() and s not in seen:
                seen.add(s)
                urls.append((codec, s))

        entry = hls.get(codec)
        if isinstance(entry, dict):
            _add(entry.get("url"))
            _add(entry.get("fallback"))
        elif isinstance(entry, str):
            _add(entry)

        std_list = standard.get(codec)
        if isinstance(std_list, list):
            for item in std_list:
                if isinstance(item, dict):
                    _add(item.get("url"))
                    _add(item.get("fallback"))
        return urls

    pref = "av1" if str(preferred_codec or "").strip().lower() == "av1" else "h264"
    order = ("av1", "h264") if pref == "av1" else ("h264", "av1")
    result: List[Tuple[str, str]] = []
    for codec in order:
        result.extend(_collect_codec(codec))
    return result


def _extract_xhamster_uploader(video_model: Dict[str, Any], uploader_hint: Optional[Tuple[str, str]]) -> Tuple[str, str]:
    if uploader_hint and uploader_hint[0]:
        return uploader_hint
    for key in ("author", "creator", "pornstar"):
        node = video_model.get(key)
        if isinstance(node, dict):
            parsed = parse_xhamster_target_from_url(str(node.get("pageURL") or node.get("url") or ""))
            if parsed:
                return parsed[1], parsed[0]
            name = str(node.get("name") or "").strip()
            if name:
                return name, "creator"
    channels = video_model.get("channels")
    if isinstance(channels, list) and channels and isinstance(channels[0], dict):
        parsed = parse_xhamster_target_from_url(str(channels[0].get("pageURL") or ""))
        if parsed:
            return parsed[1], parsed[0]
    return "", "creator"


async def fetch_xhamster_video_detail(
    ctx: TaskHttpContext,
    page_url: str,
    *,
    preferred_qualities: Optional[Set[str]] = None,
    include_keywords: Optional[Set[str]] = None,
    exclude_keywords: Optional[Set[str]] = None,
    uploader_hint: Optional[Tuple[str, str]] = None,
    include_mp4_sources: bool = False,
    preferred_codec: str = "h264",
    page_num: int = 0,
    item_num: int = 0,
    timeout: Optional[float] = None,
) -> Any:
    """Fetch and resolve an xHamster video detail page into a ``VideoDetail``."""
    from .video import MediaSource, VideoDetail, _matches_keywords, select_best_mp4_option

    vkey = extract_xhamster_viewkey(page_url) or ""
    req_timeout = max(5.0, float(timeout or 15.0))

    res, err = await ctx.fetch_with_pow(
        page_url,
        headers=_xh_headers(),
        timeout=req_timeout,
        log_label=f"xHamster 详情页 {vkey or page_url}",
    )
    if res is None:
        return VideoDetail(
            viewkey=vkey,
            title=f"解析失败_{vkey}",
            page_url=page_url,
            referer_url=page_url,
            page_num=page_num,
            item_num=item_num,
            error=err or "页面获取失败",
        )
    if res.status_code == 404:
        return VideoDetail(
            viewkey=vkey,
            title=f"解析失败_{vkey}",
            page_url=page_url,
            referer_url=res.final_url,
            page_num=page_num,
            item_num=item_num,
            error="404 无效/已删除视频",
        )
    if res.status_code >= 400:
        return VideoDetail(
            viewkey=vkey,
            title=f"解析失败_{vkey}",
            page_url=page_url,
            referer_url=res.final_url,
            page_num=page_num,
            item_num=item_num,
            error=f"详情页返回 HTTP {res.status_code}",
        )

    final_url = res.final_url or page_url
    if len(res.text) >= _OFFLOAD_HTML_THRESHOLD:
        initials = await asyncio.to_thread(extract_xhamster_initials, res.text)
    else:
        initials = extract_xhamster_initials(res.text)

    if not initials:
        return VideoDetail(
            viewkey=vkey,
            title=f"解析失败_{vkey}",
            page_url=page_url,
            referer_url=final_url,
            page_num=page_num,
            item_num=item_num,
            error="未在页面中找到 xHamster 播放数据",
        )

    video_model = initials.get("videoModel") if isinstance(initials.get("videoModel"), dict) else {}
    if not vkey:
        vkey = str(video_model.get("idHashSlug") or "").strip() or extract_xhamster_viewkey(final_url) or ""
        if not vkey and video_model.get("id"):
            vkey = f"xh_{video_model['id']}"

    raw_title = str(video_model.get("title") or "").strip()
    title = html_lib.unescape(raw_title) if raw_title else f"未知标题_{vkey or os.path.basename(final_url)}"
    published_at = _timestamp_to_ymd(video_model.get("created"))
    raw_dur = video_model.get("duration")
    duration_seconds = max(0, int(raw_dur)) if isinstance(raw_dur, (int, float)) and not isinstance(raw_dur, bool) else 0
    uploader_name, uploader_type = _extract_xhamster_uploader(video_model, uploader_hint)

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
        )

    xplayer = initials.get("xplayerSettings") if isinstance(initials.get("xplayerSettings"), dict) else {}
    raw_sources = xplayer.get("sources") if isinstance(xplayer.get("sources"), dict) else {}
    sources = decode_xhamster_sources(raw_sources) if raw_sources else {}

    mp4_options = _extract_standard_mp4_options(sources)
    mp4_sources_tuple: Tuple[MediaSource, ...] = ()
    if include_mp4_sources and mp4_options:
        def _opt_height(opt: Dict[str, Any]) -> int:
            m = re.search(r"\d{1,4}", str(opt.get("quality") or ""))
            return int(m.group(0)) if m else 0

        mp4_sources_tuple = tuple(
            MediaSource(opt["videoUrl"], opt["quality"])
            for opt in sorted(mp4_options, key=_opt_height, reverse=True)
        )

    pref_codec = "av1" if str(preferred_codec or "").strip().lower() == "av1" else "h264"
    hls_candidates = _extract_hls_master_candidates(sources, preferred_codec=pref_codec)

    selected_variant: Optional[HlsVariant] = None
    selected_codec = ""
    had_any_variants = False

    for codec, master_url in hls_candidates:
        if ctx.cancel_event.is_set():
            return VideoDetail(viewkey=vkey, title=title, page_url=page_url, referer_url=final_url, error="操作已取消")
        m_res, _ = await ctx.fetch_with_pow(
            master_url,
            headers=_xh_headers(final_url),
            timeout=req_timeout,
            max_retries=2,
            log_label=f"xHamster HLS 清单 {vkey} ({codec})",
        )
        if m_res is None or m_res.status_code >= 400 or "#EXTM3U" not in (m_res.text or ""):
            continue
        variants = parse_hls_master_variants(m_res.text, m_res.final_url or master_url)
        if not variants:
            continue
        had_any_variants = True
        usable = variants
        if preferred_qualities:
            filtered = [v for v in variants if str(v.height) in preferred_qualities]
            if not filtered:
                # Keep checking other codec only if current codec lacks requested height
                continue
            usable = filtered
        selected_variant = usable[0]
        selected_codec = codec
        break

    if selected_variant is not None:
        # Optimization: for <=720p H.264, direct progressive MP4 avoids HLS remuxing altogether
        if selected_codec == "h264" and selected_variant.height <= 720 and mp4_options:
            for opt in mp4_options:
                m_h = re.search(r"\d{1,4}", str(opt.get("quality") or ""))
                if m_h and int(m_h.group(0)) == selected_variant.height:
                    return VideoDetail(
                        viewkey=vkey,
                        title=title,
                        page_url=page_url,
                        referer_url=final_url,
                        download_url=opt["videoUrl"],
                        quality=selected_variant.quality or opt["quality"],
                        video_format="mp4",
                        uploader_username=uploader_name,
                        uploader_type=uploader_type,
                        page_num=page_num,
                        item_num=item_num,
                        mp4_sources=mp4_sources_tuple,
                        published_at=published_at,
                        duration_seconds=duration_seconds,
                    )

        return VideoDetail(
            viewkey=vkey,
            title=title,
            page_url=page_url,
            referer_url=final_url,
            download_url=selected_variant.url,
            quality=selected_variant.quality,
            video_format="hls",
            uploader_username=uploader_name,
            uploader_type=uploader_type,
            page_num=page_num,
            item_num=item_num,
            mp4_sources=mp4_sources_tuple,
            published_at=published_at,
            duration_seconds=duration_seconds,
        )

    # If HLS variants existed but none matched preferred_qualities, report filtered_out
    if had_any_variants and preferred_qualities:
        q_str = ",".join(sorted(preferred_qualities, key=lambda x: int(x) if x.isdigit() else 0, reverse=True))
        return VideoDetail(
            viewkey=vkey,
            title=title,
            page_url=page_url,
            referer_url=final_url,
            uploader_username=uploader_name,
            uploader_type=uploader_type,
            page_num=page_num,
            item_num=item_num,
            error=f"无偏好画质 ({q_str}p)",
            filtered_out=True,
            mp4_sources=mp4_sources_tuple,
            published_at=published_at,
            duration_seconds=duration_seconds,
        )

    # Fallback to direct MP4 sources if HLS master failed
    if mp4_options:
        dl_url, quality, sel_err = select_best_mp4_option(mp4_options, preferred_qualities)
        if dl_url and not sel_err:
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
                mp4_sources=mp4_sources_tuple,
                published_at=published_at,
                duration_seconds=duration_seconds,
            )
        return VideoDetail(
            viewkey=vkey,
            title=title,
            page_url=page_url,
            referer_url=final_url,
            uploader_username=uploader_name,
            uploader_type=uploader_type,
            page_num=page_num,
            item_num=item_num,
            error=sel_err or "筛选后无可用链接",
            filtered_out=bool(preferred_qualities),
            mp4_sources=mp4_sources_tuple,
            published_at=published_at,
            duration_seconds=duration_seconds,
        )

    return VideoDetail(
        viewkey=vkey,
        title=title,
        page_url=page_url,
        referer_url=final_url,
        uploader_username=uploader_name,
        uploader_type=uploader_type,
        page_num=page_num,
        item_num=item_num,
        error="无可用 HLS/MP4 媒体源",
        mp4_sources=mp4_sources_tuple,
        published_at=published_at,
        duration_seconds=duration_seconds,
    )


# ---------------------------------------------------------------------------
# Cookie Validation & Subscriptions Import
# ---------------------------------------------------------------------------
async def check_xhamster_cookie_valid(
    cookie_str: str,
    proxy_url: Optional[str] = None,
    timeout: float = 20.0,
) -> Tuple[bool, str, str]:
    """Validate xHamster Cookie via ``/my/favorites/videos``. Returns ``(valid, message, username)``."""
    cookies = parse_cookie_string(cookie_str)
    if not cookies:
        return False, "未配置 xHamster Cookie", ""

    try:
        async with TaskHttpContext(
            cookie_str=cookie_str,
            proxy_url=proxy_url,
            concurrency=1,
            timeout=timeout,
            max_retries=1,
            retry_delay_base=1.0,
            retry_delay_max=4.0,
        ) as ctx:
            res, err = await ctx.fetch_with_pow(
                f"{XH_ORIGIN}/my/favorites/videos",
                headers=_xh_headers(),
                timeout=timeout,
                max_retries=1,
                log_label="xHamster Cookie 校验",
            )
            if res is None:
                return False, f"xHamster Cookie 校验失败: {err or '请求失败'}", ""
            if res.status_code >= 400:
                return False, f"校验请求返回 HTTP {res.status_code}", ""
            if "/login" in (res.final_url or "").lower():
                return False, "xHamster Cookie 已失效（重定向到登录页）", ""
            initials = extract_xhamster_initials(res.text)
            if not initials:
                return False, "无法解析 xHamster 页面登录态", ""
            user_id = initials.get("userId")
            profile = initials.get("profile") if isinstance(initials.get("profile"), dict) else {}
            username = str(profile.get("name") or initials.get("userName") or "").strip()
            if isinstance(user_id, int) and user_id > 0:
                msg = f"Cookie 有效（当前账号: {username}）" if username else "Cookie 有效"
                return True, msg, username
            if username:
                return True, f"Cookie 有效（当前账号: {username}）", username
            return False, "xHamster Cookie 未登录或已失效", ""
    except Exception as err:
        return False, f"xHamster Cookie 校验失败: {err}", ""


async def fetch_xhamster_subscriptions(
    ctx: TaskHttpContext,
    timeout: float = 20.0,
) -> List[Tuple[str, str, str, str]]:
    """Fetch logged-in xHamster subscriptions as ``(target_type, username, display_name, avatar_url)``."""
    results: List[Tuple[str, str, str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    req_timeout = max(5.0, float(timeout))

    def _add_item(utype: str, slug: str, display_name: str, avatar_url: str) -> None:
        clean_slug = (slug or "").strip()
        if not clean_slug:
            return
        key = (utype, clean_slug.lower())
        if key in seen:
            return
        seen.add(key)
        results.append((utype, clean_slug, html_lib.unescape(display_name.strip()) or clean_slug, avatar_url))

    # 1. Pornstars & Creators (/my/subscriptions/pornstars)
    for page_num in range(1, 21):
        if ctx.cancel_event.is_set():
            break
        url = (
            f"{XH_ORIGIN}/my/subscriptions/pornstars"
            if page_num == 1
            else f"{XH_ORIGIN}/my/subscriptions/pornstars/{page_num}"
        )
        res, _ = await ctx.fetch_with_pow(
            url,
            headers=_xh_headers(),
            timeout=req_timeout,
            log_label=f"xHamster 创作者订阅第 {page_num} 页",
        )
        if res is None or res.status_code >= 400 or "/login" in (res.final_url or "").lower():
            break
        initials = extract_xhamster_initials(res.text)
        if not initials:
            break
        layout = initials.get("layoutPage") if isinstance(initials.get("layoutPage"), dict) else {}
        entries = layout.get("pornstars") if isinstance(layout.get("pornstars"), list) else []
        if not entries:
            break
        for item in entries:
            if not isinstance(item, dict):
                continue
            sub_model = item.get("subscriptionModel")
            if isinstance(sub_model, dict) and sub_model.get("subscribed") is False:
                continue
            page_url = str(item.get("pageURL") or "").strip()
            parsed = parse_xhamster_target_from_url(page_url)
            if parsed and parsed[0] in {"creator", "pornstar"}:
                utype, slug = parsed
            else:
                slug = str(item.get("inurl") or "").strip()
                raw_t = str(item.get("type") or "creator").strip().lower()
                utype = "pornstar" if raw_t == "pornstar" else "creator"
            avatar = (
                _normalize_thumb_url(item.get("imageUserAvatarUrl"))
                or _normalize_thumb_url(item.get("imageThumbUrl"))
                or _normalize_thumb_url(item.get("logoThumbUrl"))
            )
            _add_item(utype, slug, str(item.get("name") or slug), avatar)

        pag = initials.get("paginationProps") or initials.get("paginationComponent") or {}
        last_page = pag.get("lastPageNumber") if isinstance(pag, dict) else None
        if not isinstance(last_page, int) or page_num >= last_page:
            break

    # 2. Channels (/my/subscriptions/channels) - strictly require subscribed == True!
    for page_num in range(1, 21):
        if ctx.cancel_event.is_set():
            break
        url = (
            f"{XH_ORIGIN}/my/subscriptions/channels"
            if page_num == 1
            else f"{XH_ORIGIN}/my/subscriptions/channels/{page_num}"
        )
        res, _ = await ctx.fetch_with_pow(
            url,
            headers=_xh_headers(),
            timeout=req_timeout,
            log_label=f"xHamster 频道订阅第 {page_num} 页",
        )
        if res is None or res.status_code >= 400 or "/login" in (res.final_url or "").lower():
            break
        initials = extract_xhamster_initials(res.text)
        if not initials:
            break
        layout = initials.get("layoutPage") if isinstance(initials.get("layoutPage"), dict) else {}
        entries = layout.get("channels") if isinstance(layout.get("channels"), list) else []
        if not entries:
            sec = initials.get("channelsSectionComponent")
            if isinstance(sec, dict) and isinstance(sec.get("channels"), list):
                entries = sec["channels"]
        if not entries:
            break
        found_subscribed = 0
        for item in entries:
            if not isinstance(item, dict):
                continue
            sub_model = item.get("subscriptionModel")
            if not (isinstance(sub_model, dict) and sub_model.get("subscribed") is True):
                continue
            found_subscribed += 1
            page_url = str(item.get("pageURL") or "").strip()
            parsed = parse_xhamster_target_from_url(page_url)
            slug = parsed[1] if parsed and parsed[0] == "channel" else str(item.get("inurl") or "").strip()
            avatar = (
                _normalize_thumb_url(item.get("logoThumbUrl"))
                or _normalize_thumb_url(item.get("imageThumbUrl"))
                or _normalize_thumb_url(item.get("thumbURL"))
            )
            _add_item("channel", slug, str(item.get("name") or slug), avatar)

        pag = initials.get("paginationProps") or initials.get("paginationComponent") or {}
        last_page = pag.get("lastPageNumber") if isinstance(pag, dict) else None
        if found_subscribed == 0 or not isinstance(last_page, int) or page_num >= last_page:
            break

    # 3. Subscribed independent /users/{slug} discovered via /my/subscriptions feed
    if not ctx.cancel_event.is_set():
        res_feed, _ = await ctx.fetch_with_pow(
            f"{XH_ORIGIN}/my/subscriptions",
            headers=_xh_headers(),
            timeout=req_timeout,
            log_label="xHamster 订阅动态页",
        )
        if res_feed is not None and res_feed.status_code < 400 and "/login" not in (res_feed.final_url or "").lower():
            initials = extract_xhamster_initials(res_feed.text) or {}
            seen_comp = initials.get("seenVideoListComponent")
            thumbs = seen_comp.get("videoThumbProps") if isinstance(seen_comp, dict) else None
            if isinstance(thumbs, list):
                for v in thumbs:
                    if not isinstance(v, dict):
                        continue
                    landing = v.get("landing")
                    if not isinstance(landing, dict):
                        continue
                    link = str(landing.get("link") or "").strip()
                    parsed = parse_xhamster_target_from_url(link)
                    if parsed and parsed[0] in {"users", "creator", "pornstar", "channel"}:
                        utype, slug = parsed
                        _add_item(
                            utype,
                            slug,
                            str(landing.get("name") or slug),
                            _normalize_thumb_url(landing.get("logo")),
                        )

    return results
