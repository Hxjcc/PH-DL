"""Target type resolution via redirect and account subscription parser."""

from __future__ import annotations

import urllib.parse
from typing import List, Optional, Set, Tuple

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..config import SelectorSettings
from .http_session import TaskHttpContext


def select_one(node, selectors: List[str]):
    if node is None or not selectors or not hasattr(node, "css"):
        return None
    for sel in selectors:
        if not sel:
            continue
        try:
            matches = node.css(sel)
            if matches:
                return matches[0]
        except Exception:
            continue
    return None


def select_many(node, selectors: List[str]):
    if node is None or not selectors or not hasattr(node, "css"):
        return []
    for sel in selectors:
        if not sel:
            continue
        try:
            matches = node.css(sel)
            if matches:
                return matches
        except Exception:
            continue
    return []


def parse_target_type_from_url(final_url: str) -> Optional[str]:
    path = urllib.parse.urlparse(final_url or "").path.strip().lower()
    if not path:
        return None
    if path.startswith("/pornstar/"):
        return "pornstar"
    if path.startswith("/model/"):
        return "model"
    if path.startswith("/users/") or path.startswith("/user/"):
        return "users"
    if path.startswith("/channels/"):
        return "channel"
    return None


async def resolve_target_type(
    ctx: TaskHttpContext,
    username: str,
    hinted_type: Optional[str] = None,
    timeout: float = 15.0,
) -> Optional[str]:
    norm_user = (username or "").strip()
    if not norm_user or norm_user == "_currentUser_":
        return None

    quoted = urllib.parse.quote(norm_user, safe="")
    ordered: List[str] = []
    hint = (hinted_type or "").strip().lower()
    if hint in {"model", "pornstar"}:
        ordered.append(hint)
    for cand in ("model", "pornstar"):
        if cand not in ordered:
            ordered.append(cand)

    for cand in ordered:
        if ctx.cancel_event.is_set():
            return None
        url = f"https://www.pornhub.com/{cand}/{quoted}"
        res, _ = await ctx.fetch_with_pow(
            url,
            timeout=max(5.0, float(timeout)),
            max_retries=1,
            log_label=f"识别目标 {norm_user}",
        )
        if res is None or res.status_code >= 400:
            continue
        detected = parse_target_type_from_url(res.final_url)
        if detected in {"model", "pornstar", "channel"}:
            return detected
    return None


async def fetch_subscriptions(
    ctx: TaskHttpContext,
    username: str,
    selectors: SelectorSettings,
    timeout: float = 20.0,
) -> List[Tuple[str, str, str, str]]:
    """Fetch subscriptions as (target_type, username, display_name, avatar_url)."""
    subs_url = f"https://www.pornhub.com/users/{urllib.parse.quote(username.strip().lower(), safe='')}/subscriptions"
    page_num = 1
    results: List[Tuple[str, str, str, str]] = []
    seen_profiles: Set[Tuple[str, str]] = set()

    main_sels = selectors.split_list("subs_page_main_container")
    link_sels = selectors.split_list("subs_page_user_link_in_container")

    while page_num <= 50:
        if ctx.cancel_event.is_set():
            break
        current_url = f"{subs_url}?page={page_num}"
        res, _ = await ctx.fetch_with_pow(
            current_url,
            timeout=max(5.0, float(timeout)),
            log_label=f"订阅第 {page_num} 页",
        )
        if res is None or res.status_code >= 400:
            break

        tree = HTMLParser(res.text)
        container = select_one(tree, main_sels)
        if not container:
            break

        found_on_page: List[Tuple[str, str, str, str]] = []
        for link in select_many(container, link_sels):
            href = (link.attributes.get("href") or "").strip() if hasattr(link, "attributes") else ""
            if not href:
                continue
            parts = [p for p in urllib.parse.urlparse(href).path.strip("/").split("/") if p]
            if len(parts) == 2:
                utype, uname = parts[0].lower(), parts[1]
                if utype == "channels":
                    utype = "channel"
                if utype in {"model", "pornstar", "channel"}:
                    profile_key = (utype, uname.lower())
                    if profile_key in seen_profiles:
                        continue
                    avatar = select_one(link, ["img"])
                    parent = link.parent
                    for _ in range(4):
                        if avatar is not None or parent is None:
                            break
                        if parent.tag in {"li", "article"}:
                            avatar = select_one(parent, ["img"])
                            break
                        parent = parent.parent
                    display_name = ""
                    avatar_url = ""
                    if avatar is not None and hasattr(avatar, "attributes"):
                        display_name = (avatar.attributes.get("alt") or "").strip()
                        for attr in ("data-src", "data-original", "data-thumb_url", "src"):
                            candidate = urllib.parse.urljoin(subs_url, (avatar.attributes.get(attr) or "").strip())
                            if urllib.parse.urlparse(candidate).scheme in {"http", "https"} and candidate != subs_url and "/users/default/" not in candidate:
                                avatar_url = candidate
                                break
                    if display_name.lower() in {"avatar", "profile picture"}:
                        display_name = ""
                    if not display_name:
                        display_name = (link.text() or "").strip()
                    seen_profiles.add(profile_key)
                    found_on_page.append((utype, uname, display_name or uname, avatar_url))

        if not found_on_page:
            break
        results.extend(found_on_page)
        page_num += 1

    return results
