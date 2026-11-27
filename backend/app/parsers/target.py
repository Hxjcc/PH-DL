"""Target type resolution via redirect, creator subscription, and account subscription parser."""

from __future__ import annotations

import html as html_lib
import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..config import SelectorSettings
from .http_session import TaskHttpContext
from .urls import is_cookie_origin

PH_ORIGIN = "https://www.pornhub.com"
_SUBSCRIBE_TAG_RE = re.compile(
    r"<(?:button|a)\b[^>]*\bdata-subscribe-url\s*=\s*([\"'])([^\"']+)\1[^>]*>",
    re.IGNORECASE,
)
_HTML_ATTR_RE = re.compile(
    r"\b(data-(?:subscribe-url|subscribed|login|id))\s*=\s*([\"'])([^\"']*)\2",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SubscribeAction:
    subscribe_url: str = ""
    already_subscribed: bool = False
    login_required: bool = False
    creator_id: str = ""
    site: str = "pornhub"
    entity_model: str = ""
    entity_id: int = 0


@dataclass(frozen=True)
class SubscribeResult:
    ok: bool
    status: str  # "subscribed" | "already_subscribed" | "login_required" | "not_found" | "failed"
    message: str = ""
    avatar_url: str = ""
    profile_path: str = ""


def extract_subscribe_action(html: str) -> Optional[SubscribeAction]:
    """Extract PornHub subscribe button metadata from profile, list, or video HTML without full DOM parsing."""
    if not html or "data-subscribe-url" not in html.lower():
        return None
    for match in _SUBSCRIBE_TAG_RE.finditer(html):
        tag_html = match.group(0)
        attrs = {
            m.group(1).lower(): html_lib.unescape(m.group(3)).strip()
            for m in _HTML_ATTR_RE.finditer(tag_html)
        }
        sub_url = attrs.get("data-subscribe-url", "")
        if not sub_url or "subscribe_add_json" not in sub_url:
            continue
        return SubscribeAction(
            subscribe_url=sub_url,
            already_subscribed=attrs.get("data-subscribed") == "1",
            login_required=attrs.get("data-login") == "1",
            creator_id=attrs.get("data-id", ""),
        )
    return None


def _is_pornhub_url(url: str) -> bool:
    return is_cookie_origin(url, "pornhub")


async def subscribe_from_action(
    ctx: TaskHttpContext,
    action: Optional[SubscribeAction],
    *,
    referer_url: str = "",
    label: str = "",
    timeout: float = 15.0,
) -> SubscribeResult:
    """Invoke the site's JSON subscribe endpoint using the task's authenticated HTTP context."""
    if action is not None and action.site == "xhamster":
        from .xhamster import subscribe_xhamster_from_action

        return await subscribe_xhamster_from_action(
            ctx,
            action,
            referer_url=referer_url,
            label=label,
            timeout=timeout,
        )
    if action is None or not action.subscribe_url:
        return SubscribeResult(ok=False, status="not_found", message="页面未找到订阅按钮")
    if action.already_subscribed:
        return SubscribeResult(ok=True, status="already_subscribed", message="已订阅")
    if action.login_required:
        return SubscribeResult(ok=False, status="login_required", message="Cookie 未登录或已失效")

    full_url = urllib.parse.urljoin(PH_ORIGIN, action.subscribe_url)
    if not _is_pornhub_url(full_url):
        return SubscribeResult(ok=False, status="failed", message="订阅接口地址不受信任")

    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Origin": PH_ORIGIN,
        "Referer": referer_url or f"{PH_ORIGIN}/",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    res, err = await ctx.fetch_with_pow(
        full_url,
        method="POST",
        data=None,
        headers=headers,
        timeout=max(5.0, float(timeout)),
        max_retries=1,
        log_label=f"订阅 {label or action.creator_id or '博主'}",
    )
    if res is None:
        return SubscribeResult(ok=False, status="failed", message=err or "订阅请求失败")
    if res.status_code >= 400:
        return SubscribeResult(ok=False, status="failed", message=f"HTTP {res.status_code}")

    try:
        payload = json.loads(res.text)
    except (ValueError, TypeError):
        return SubscribeResult(ok=False, status="failed", message="订阅响应不是有效 JSON")

    if not isinstance(payload, dict):
        return SubscribeResult(ok=False, status="failed", message="订阅响应格式异常")

    raw_success = payload.get("success")
    passed = raw_success is True or str(raw_success or "").strip().upper() in {"PASS", "TRUE", "1", "OK"}
    message = str(payload.get("message") or "").strip()
    user_info = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    avatar_url = str(user_info.get("avatar") or "").strip()
    if avatar_url and not avatar_url.startswith(("http://", "https://")):
        avatar_url = urllib.parse.urljoin(PH_ORIGIN, avatar_url)
    if "/pics/users/default/" in avatar_url:
        avatar_url = ""
    profile_path = str(user_info.get("profile") or "").strip()

    if passed:
        return SubscribeResult(
            ok=True,
            status="subscribed",
            message=message or "Subscription added.",
            avatar_url=avatar_url,
            profile_path=profile_path,
        )
    return SubscribeResult(ok=False, status="failed", message=message or "订阅未成功")


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
