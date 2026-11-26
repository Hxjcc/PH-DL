"""Cookie validation and PornHub login helper (fully async, no CLI prompts)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urljoin, urlparse
from weakref import WeakKeyDictionary

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..cancel import StopRequested, until_stopped
from ..parsers.http_session import TaskHttpContext, parse_cookie_string
from .captcha import CaptchaError, solve_recaptcha, solve_recaptcha_v2

PORNHUB_LOGIN_URL = "https://www.pornhub.com/login"
# Stable reCAPTCHA sitekey of the PornHub login modal (fallback only).
PORNHUB_DEFAULT_SITEKEY = "6LfyQRoUAAAAAApqS-inJxUwCOtvOHwKxJuZCsuv"

XHAMSTER_LOGIN_URL = "https://xhamster.com/login"
XHAMSTER_LOGIN_API_URL = "https://xhamster.com/api/front/user/login"
XHAMSTER_DEFAULT_CSRF = "1"
# Fallback only; the live v2 key is parsed from the login page whenever possible.
XHAMSTER_RECAPTCHA_V2_KEY = "6LdamIQUAAAAAOI6zxYRCbYtaLKkzfddgrWQForz"
# Both are standard reCAPTCHA v2, proxyless, on different YesCaptcha routes.
XHAMSTER_CAPTCHA_V2_TASK_TYPES = ("NoCaptchaTaskProxyless", "RecaptchaV2TaskProxyless")
XHAMSTER_DEFAULT_CAPTCHA_V2_TASK_TYPE = "RecaptchaV2TaskProxyless"

LogCallback = Callable[[str, str], None]
SuccessCallback = Callable[["CookieLoginResult"], Awaitable[None]]


class _InflightLogin:
    """Shared single-flight login state supporting cooperative cancel and cookie persistence."""

    def __init__(self) -> None:
        self.cancel_event = asyncio.Event()
        self.waiters = 0
        self.log_listeners: List[LogCallback] = []
        self.success_callbacks: List[SuccessCallback] = []
        self.task: Optional[asyncio.Task["CookieLoginResult"]] = None

    def emit_raw(self, level: str, message: str) -> None:
        for cb in list(self.log_listeners):
            with contextlib.suppress(Exception):
                cb(level, message)


# Coalesce concurrent auto-login calls per event loop + (account, proxy) to avoid
# firing duplicate YesCaptcha solves or triggering PornHub's 1-hour account lock.
_INFLIGHT_LOGINS: "WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[Tuple[str, str], _InflightLogin]]" = (
    WeakKeyDictionary()
)


async def _run_deduped_login(
    dedup_key: Tuple[str, str],
    site_label: str,
    factory: Callable[[asyncio.Event, LogCallback], Awaitable["CookieLoginResult"]],
    *,
    on_log: Optional[LogCallback] = None,
    cancel_event: Optional[asyncio.Event] = None,
    on_success: Optional[SuccessCallback] = None,
) -> "CookieLoginResult":
    if cancel_event is not None and cancel_event.is_set():
        raise StopRequested

    emit = _make_site_logger(site_label, on_log)
    loop = asyncio.get_running_loop()
    inflight = _INFLIGHT_LOGINS.setdefault(loop, {})
    existing = inflight.get(dedup_key)
    if (
        existing is not None
        and existing.task is not None
        and not existing.task.done()
        and not existing.cancel_event.is_set()
    ):
        entry = existing
        emit("INFO", "检测到已有正在进行的登录任务，复用本次结果...")
    else:
        entry = _InflightLogin()
        inflight[dedup_key] = entry

        async def _runner() -> CookieLoginResult:
            result = await factory(entry.cancel_event, entry.emit_raw)
            if result.success and result.cookie_str and entry.success_callbacks:
                for cb in list(entry.success_callbacks):
                    with contextlib.suppress(Exception):
                        await cb(result)
            return result

        entry.task = asyncio.create_task(_runner())

        def _cleanup(finished: asyncio.Task[CookieLoginResult], target: _InflightLogin = entry) -> None:
            if inflight.get(dedup_key) is target:
                inflight.pop(dedup_key, None)

        entry.task.add_done_callback(_cleanup)

    entry.waiters += 1
    if on_log is not None:
        entry.log_listeners.append(on_log)
    if on_success is not None:
        entry.success_callbacks.append(on_success)

    assert entry.task is not None
    try:
        if cancel_event is not None:
            return await until_stopped(asyncio.shield(entry.task), cancel_event)
        return await asyncio.shield(entry.task)
    finally:
        if on_log is not None:
            with contextlib.suppress(ValueError):
                entry.log_listeners.remove(on_log)
        entry.waiters = max(0, entry.waiters - 1)
        if entry.waiters == 0 and not entry.task.done():
            entry.cancel_event.set()
            if inflight.get(dedup_key) is entry:
                inflight.pop(dedup_key, None)


@dataclass(frozen=True)
class CookieCheckResult:
    valid: bool
    message: str
    logged_in_username: str = ""


@dataclass(frozen=True)
class CookieLoginResult:
    success: bool
    cookie_str: str = ""
    logged_in_username: str = ""
    message: str = ""


def prefer_cased_username(detected: str, existing: str = "") -> str:
    """Resolve username without downgrading an existing mixed-case username (e.g. ``Hxiay``) to lowercase."""
    det = (detected or "").strip()
    cur = (existing or "").strip()
    if det and cur and det.lower() == cur.lower():
        if det == det.lower() and cur != cur.lower():
            return cur
        return det
    return det or cur


def format_cookie_valid_message(username: str = "") -> str:
    user = (username or "").strip()
    return f"Cookie 有效（当前账号: {user}）" if user else "Cookie 有效"


def format_login_success_message(site_label: str, username: str = "") -> str:
    user = (username or "").strip()
    suffix = f"（当前账号: {user}）" if user else ""
    return f"{site_label} 登录并更新 Cookie 成功{suffix}"


def _make_site_logger(site_label: str, on_log: Optional[LogCallback]) -> LogCallback:
    """Prefix every login & captcha log line uniformly with ``[<Site> 登录]``."""
    prefix = f"[{site_label} 登录] "

    def emit(level: str, message: str) -> None:
        if not on_log:
            return
        text = str(message or "").strip()
        if not text:
            return
        if not text.startswith(prefix):
            text = f"{prefix}{text}"
        on_log(level, text)

    return emit


def _log_login_outcome(emit: LogCallback, result: CookieLoginResult) -> CookieLoginResult:
    if result.success:
        if result.logged_in_username:
            emit("INFO", f"登录成功，Cookie 校验通过（当前账号: {result.logged_in_username}）")
        else:
            emit("INFO", "登录成功，Cookie 校验通过")
    else:
        emit("WARNING", f"登录失败: {result.message}")
    return result


def _iter_template_subtrees(tree: HTMLParser):
    """Yield parsed subtrees for <template> elements whose inner HTML is inert in lexbor."""
    for tpl in tree.css("template"):
        raw = tpl.html or ""
        if not raw:
            continue
        inner = re.sub(r"^\s*<template\b[^>]*>|</template>\s*$", "", raw, flags=re.I)
        if inner.strip():
            yield HTMLParser(inner)


def _extract_from_inputs(inputs) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    token_val: Optional[str] = None
    redirect_val: Optional[str] = None
    from_val: Optional[str] = None

    for inp in inputs:
        attrs = getattr(inp, "attributes", {}) or {}
        name = (attrs.get("name") or "").strip()
        val = (attrs.get("value") or "").strip()
        if not name:
            continue
        if token_val is None and val and re.search(r"(token|csrf|_token)", name, re.I):
            token_val = val
        elif redirect_val is None and val and re.search(r"redirect", name, re.I):
            redirect_val = val
        elif from_val is None and val and name.lower() == "from":
            from_val = val

    return token_val, redirect_val, from_val


def _extract_login_form_fields(html: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract (token, redirect, from) from login HTML, including <template> login forms."""
    tree = HTMLParser(html or "")
    subtrees = [tree, *_iter_template_subtrees(tree)]

    # 1. Prefer login-specific <form> blocks (e.g. js-loginForm inside <template id="signinModal">)
    best_form_match: Optional[Tuple[Optional[str], Optional[str], Optional[str]]] = None
    for st in subtrees:
        for form in st.css("form"):
            tok, redir, frm = _extract_from_inputs(form.css("input"))
            if not tok:
                continue
            form_attrs = getattr(form, "attributes", {}) or {}
            form_cls = (form_attrs.get("class") or "").lower()
            form_id = (form_attrs.get("id") or "").lower()
            if "login" in form_cls or "login" in form_id or (frm and "login" in frm.lower()):
                return tok, redir, frm
            if best_form_match is None:
                best_form_match = (tok, redir, frm)
    if best_form_match is not None:
        return best_form_match

    # 2. Fallback: scan all inputs across document and <template> subtrees
    all_inputs = []
    for st in subtrees:
        all_inputs.extend(st.css("input"))
    tok, redir, frm = _extract_from_inputs(all_inputs)
    if tok:
        return tok, redir, frm

    # 3. Fallback: <meta> csrf/token
    for meta in tree.css("meta"):
        attrs = getattr(meta, "attributes", {}) or {}
        name = (attrs.get("name") or "").strip()
        content = (attrs.get("content") or "").strip()
        if name and content and re.search(r"(csrf|token)", name, re.I):
            return content, redir, frm

    # 4. Fallback: inline <script>
    for script in tree.css("script"):
        txt = script.text() or ""
        if not txt:
            continue
        m = re.search(r'(?:token|csrf_token|CSRF_TOKEN|_token)\s*[:=]\s*[\'"]([A-Za-z0-9\-\._\+%=]+)[\'"]', txt, re.I)
        if m:
            return m.group(1), redir, frm

    m = re.search(r'token[\'"]?\s*[:=]\s*[\'"]([A-Za-z0-9\-\._\+%=]+)[\'"]', html or "", re.I)
    if m:
        return m.group(1), redir, frm
    return None, redir, frm


def _extract_token_and_redirect(html: str) -> Tuple[Optional[str], Optional[str]]:
    token, redirect, _ = _extract_login_form_fields(html)
    return token, redirect


def _looks_like_login_page(html: str) -> bool:
    if not html:
        return False
    lower = html.lower()
    if "/front/authenticate" in lower or 'id="login_form"' in lower or "id='login_form'" in lower:
        return True
    if "<v-login-form" in lower:
        return True
    if ('id="usernamemodal"' in lower or "id='usernamemodal'" in lower) and (
        'id="passwordmodal"' in lower or "id='passwordmodal'" in lower
    ):
        return True
    if ('name="loginpage"' in lower or "js-loginform" in lower) and 'name="password"' in lower:
        return True
    return False


def _is_auth_json_failure(data: dict) -> bool:
    if data.get("error"):
        return True
    if "success" in data:
        val = data.get("success")
        if isinstance(val, bool):
            return not val
        if isinstance(val, (int, float)):
            return val == 0
        if isinstance(val, str):
            return val.strip().lower() in ("", "0", "false", "no")
        if val is None:
            return True
    return False


def _is_auth_json_success(data: dict) -> bool:
    if not isinstance(data, dict) or data.get("error"):
        return False
    if "success" not in data:
        return False
    val = data.get("success")
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes")
    return False


def _is_captcha_required(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    val = data.get("requireCaptcha")
    if isinstance(val, str):
        if val.strip().lower() in ("1", "true", "yes"):
            return True
    elif val:
        return True
    message = str(data.get("message") or data.get("error") or "").lower()
    return any(token in message for token in ("captcha", "recaptcha", "robot"))


def _auth_message(data: dict) -> str:
    return str(
        data.get("message")
        or data.get("error")
        or data.get("errorDescription")
        or ""
    ).strip()


def _friendly_auth_failure(message: str) -> str:
    lower = (message or "").lower()
    if "blocked" in lower:
        return (
            "PornHub 临时锁定了该账号（Your account has been blocked for one hour）。"
            "请等待约 1 小时后再试，期间不要频繁点击自动登录。"
        )
    if "invalid credential" in lower:
        return (
            "账号或密码错误 (Invalid credentials)。"
            "请检查设置中的 PornHub 账号密码，或手动粘贴浏览器 Cookie。"
        )
    if "captcha" in lower or "recaptcha" in lower:
        return "PornHub 要求人机验证，请启用 YesCaptcha 后重试。"
    return message


@dataclass(frozen=True)
class LoginPageConfig:
    token: str = ""
    redirect: str = ""
    from_value: str = "pc_login_modal_:login"
    sitekey: str = ""
    login_url: str = "/front/authenticate"


def _extract_connection_modal_configs(html: str) -> Dict[str, object]:
    """Read the hydrated ``connectionModalData.configs`` object.

    The static ``<template id="signinModal">`` contains placeholder redirect
    values.  The browser actually submits the values from this JavaScript
    config, so they must take precedence for auto-login to behave the same.
    """

    if not html:
        return {}
    marker = "connectionModalData"
    idx = html.find(marker)
    if idx < 0:
        return {}
    brace = html.find("{", idx)
    if brace < 0:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(html[brace:])
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    configs = data.get("configs")
    return configs if isinstance(configs, dict) else {}


def _extract_sitekey(html: str, configs: Dict[str, object]) -> str:
    configured = str(configs.get("captchaKey") or "").strip()
    if configured:
        return configured
    for pattern in (
        r'data-sitekey=["\']([^"\']+)["\']',
        r'recaptcha-key=["\']([^"\']+)["\']',
        r'gRecaptchaPublicKey\s*=\s*["\']([^"\']+)["\']',
    ):
        match = re.search(pattern, html or "", re.I)
        if match:
            return match.group(1).strip()
    return ""


def _extract_login_config(html: str) -> LoginPageConfig:
    configs = _extract_connection_modal_configs(html)
    token, redirect, from_val = _extract_login_form_fields(html)

    return LoginPageConfig(
        token=str(configs.get("token") or token or "").strip(),
        redirect=str(configs.get("redirectFieldValue") or redirect or "").strip(),
        from_value=str(configs.get("fromValue") or from_val or "pc_login_modal_:login").strip(),
        sitekey=_extract_sitekey(html, configs),
        login_url=str(configs.get("loginUrl") or "/front/authenticate").strip(),
    )


def _login_payload(
    config: LoginPageConfig,
    username: str,
    password: str,
    captcha_token: str = "",
) -> Dict[str, str]:
    payload: Dict[str, str] = {
        "redirect": config.redirect or "",
        "user_id": "",
        "intended_action": "",
        "token": config.token or "",
        "from": config.from_value or "pc_login_modal_:login",
        "email": username.strip(),
        "password": password,
    }
    if captcha_token:
        # reCAPTCHA v2 injects this hidden field inside the login form and the
        # site posts it verbatim with the rest of the form data.
        payload["g-recaptcha-response"] = captcha_token
    return payload


async def _submit_login(
    ctx: TaskHttpContext,
    config: LoginPageConfig,
    username: str,
    password: str,
    *,
    timeout: float,
    captcha_token: str = "",
    page_url: str = PORNHUB_LOGIN_URL,
) -> Tuple[Optional[object], Optional[str], Optional[dict]]:
    auth_url = config.login_url or "/front/authenticate"
    if not re.match(r"^https?://", auth_url, re.I):
        auth_url = urljoin(page_url, auth_url)

    parsed_page = urlparse(page_url)
    origin = f"{parsed_page.scheme or 'https'}://{parsed_page.netloc or 'www.pornhub.com'}"

    resp, err = await ctx.fetch_with_pow(
        auth_url,
        method="POST",
        data=_login_payload(config, username, password, captcha_token),
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": origin,
            "Referer": page_url,
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
        timeout=timeout,
        max_retries=1,
        log_label="登录提交",
    )
    data: Optional[dict] = None
    if resp is not None:
        try:
            parsed = json.loads(resp.text)
            if isinstance(parsed, dict):
                data = parsed
        except (ValueError, TypeError):
            data = None
    return resp, err, data


def _extract_pornhub_logged_in_username(html: str) -> str:
    """Best-effort extraction of the logged-in PornHub username, preserving original capitalization."""
    if not html:
        return ""
    ignore = {
        "",
        "null",
        "none",
        "guest",
        "anonymous",
        "login",
        "signup",
        "settings",
        "security",
        "subscriptions",
        "profile",
        "my profile",
    }

    def _clean(raw: str) -> str:
        candidate = (raw or "").strip().strip("/")
        if not candidate or len(candidate) > 64 or "/" in candidate or "?" in candidate or " " in candidate:
            return ""
        if candidate.lower() in ignore:
            return ""
        return candidate

    profile_slug = ""
    try:
        tree = HTMLParser(html)
        menu_containers = []
        for menu_sel in ("#topRightProfileMenu", ".profileMenu", "#headerProfileMenu"):
            menu_containers.extend(tree.css(menu_sel))

        # 1. Strictly inspect logged-in profile menu containers first (never match random video uploaders!).
        for container in menu_containers:
            for node in container.css("[data-username]"):
                data_user = _clean(node.attributes.get("data-username") or "")
                if data_user:
                    return data_user
            for node in container.css("a[href*='/users/']"):
                href = (node.attributes.get("href") or "").strip()
                m_href = re.search(r"/users/([^/?#]+)", href, re.I)
                if not m_href:
                    continue
                slug = _clean(m_href.group(1))
                if not slug:
                    continue
                label = _clean(node.text(strip=True) or "")
                if label and label.lower() == slug.lower():
                    return label
                if not profile_slug:
                    profile_slug = slug
            if profile_slug:
                for label_node in container.css("span, a, div, p"):
                    label = _clean(label_node.text(strip=True) or "")
                    if label and label.lower() == profile_slug.lower() and label != label.lower():
                        return label

        # 2. Explicit Profile nav link (still safe because data-mxptext='Profile' is the user's own profile)
        if not profile_slug:
            for node in tree.css("a[data-mxptext='Profile'][href*='/users/']"):
                href = (node.attributes.get("href") or "").strip()
                m_href = re.search(r"/users/([^/?#]+)", href, re.I)
                if m_href:
                    slug = _clean(m_href.group(1))
                    if slug:
                        label = _clean(node.text(strip=True) or "")
                        if label and label.lower() == slug.lower():
                            return label
                        profile_slug = slug
                        break
    except Exception:
        pass

    # 3. If profile_slug was found in the account menu, ONLY upgrade its letter casing with a same-user match.
    # Never replace it with an unrelated user's name from elsewhere on the page.
    if profile_slug:
        for pattern in (
            r'data-username=["\']([^"\']{2,64})["\']',
            r'(?:"username"|\'username\'|\buser_name\b|\bcurrentUsername\b)\s*[:=]\s*["\']([^"\'\\]{2,64})["\']',
        ):
            for m in re.finditer(pattern, html, re.I):
                cand = _clean(m.group(1))
                if cand and cand.lower() == profile_slug.lower() and cand != cand.lower():
                    return cand
        return profile_slug

    # 4. Fallback for minimal HTML without a profile menu
    for pattern in (
        r'data-username=["\']([^"\']{2,64})["\']',
        r'(?:\bcurrentUsername\b|"username"|\'username\'|\buser_name\b)\s*[:=]\s*["\']([^"\'\\]{2,64})["\']',
    ):
        m = re.search(pattern, html, re.I)
        if m:
            user = _clean(m.group(1))
            if user:
                return user
    return ""


async def _finish_login(
    ctx: TaskHttpContext,
    proxy_url: Optional[str],
    timeout: float,
    logged_in_username: str,
) -> CookieLoginResult:
    cookie_str = ctx.cookie_string()
    if not cookie_str:
        return CookieLoginResult(success=False, message="PornHub 登录未返回任何 Cookie")

    verify = await check_cookie_valid(cookie_str, proxy_url=proxy_url, timeout=timeout)
    if not verify.valid:
        return CookieLoginResult(success=False, message=f"PornHub 登录后校验失败: {verify.message}")

    resolved_user = (logged_in_username or verify.logged_in_username or "").strip()
    return CookieLoginResult(
        success=True,
        cookie_str=cookie_str,
        logged_in_username=resolved_user,
        message=format_login_success_message("PornHub", resolved_user),
    )


async def check_cookie_valid(
    cookie_str: str,
    proxy_url: Optional[str] = None,
    timeout: float = 20.0,
) -> CookieCheckResult:
    cookies = parse_cookie_string(cookie_str)
    if not cookies:
        return CookieCheckResult(valid=False, message="未配置 Cookie")

    try:
        ctx = TaskHttpContext(
            cookie_str=cookie_str,
            proxy_url=proxy_url,
            concurrency=1,
            timeout=timeout,
            max_retries=2,
            retry_delay_base=1.5,
            retry_delay_max=6.0,
        )
        async with ctx:
            res, err = await ctx.fetch_with_pow(
                "https://www.pornhub.com/settings/security",
                timeout=timeout,
                max_retries=2,
                log_label="Cookie 校验",
            )
            if res is None:
                return CookieCheckResult(valid=False, message=f"Cookie 校验失败: {err or '请求失败'}")
            if res.status_code >= 400:
                return CookieCheckResult(valid=False, message=f"校验请求返回 HTTP {res.status_code}")

            body = res.text or ""
            final_url = str(res.final_url)
            final_url_lower = final_url.lower()
            if "/login" in final_url_lower:
                return CookieCheckResult(valid=False, message="Cookie 已失效（重定向到登录页）")
            if "/settings/security" not in final_url_lower and "/user/security" not in final_url_lower:
                return CookieCheckResult(valid=False, message=f"Cookie 可能已失效（页面被重定向至 {final_url}）")
            if _looks_like_login_page(body):
                return CookieCheckResult(valid=False, message="Cookie 已失效（页面要求重新登录）")
            if not body.strip():
                return CookieCheckResult(valid=False, message="校验响应为空，无法确认 Cookie 有效性")
            detected_user = _extract_pornhub_logged_in_username(body)
            return CookieCheckResult(
                valid=True,
                message=format_cookie_valid_message(detected_user),
                logged_in_username=detected_user,
            )
    except Exception as err:
        return CookieCheckResult(valid=False, message=f"Cookie 校验失败: {err}")


async def _login_with_captcha(
    ctx: TaskHttpContext,
    config: LoginPageConfig,
    username: str,
    password: str,
    *,
    proxy_url: Optional[str],
    timeout: float,
    yescaptcha_client_key: str,
    yescaptcha_api_base: str,
    yescaptcha_task_type: str,
    yescaptcha_timeout_seconds: float,
    cancel_event: Optional[asyncio.Event] = None,
    on_log: Optional[LogCallback] = None,
) -> CookieLoginResult:
    """Refresh the login state, solve reCAPTCHA with YesCaptcha, then retry."""

    def emit(level: str, message: str) -> None:
        if on_log:
            on_log(level, message)

    # CSRF tokens and the captcha challenge can be rotated after a failed POST.
    page_resp, page_err = await ctx.fetch_with_pow(
        PORNHUB_LOGIN_URL,
        timeout=timeout,
        max_retries=1,
        log_label="登录页",
    )
    if page_resp is None:
        return CookieLoginResult(success=False, message=f"访问登录页失败: {page_err or '请求失败'}")
    if page_resp.status_code >= 400:
        return CookieLoginResult(success=False, message=f"访问登录页失败 (HTTP {page_resp.status_code})")

    fresh_config = _extract_login_config(page_resp.text)
    page_url = str(page_resp.final_url or PORNHUB_LOGIN_URL)
    sitekey = fresh_config.sitekey or config.sitekey or PORNHUB_DEFAULT_SITEKEY
    if yescaptcha_timeout_seconds < 240:
        emit(
            "WARNING",
            f"YesCaptcha 解题超时为 {int(yescaptcha_timeout_seconds)} 秒；"
            "PornHub 企业版验证码通常需要 2-3 分钟，建议在设置中提高到 300 秒。",
        )
    emit("INFO", f"触发人机验证（reCAPTCHA），正在调用 YesCaptcha（{yescaptcha_task_type}）...")

    try:
        solution = await solve_recaptcha_v2(
            yescaptcha_client_key,
            page_url,
            sitekey,
            api_base=yescaptcha_api_base,
            task_type=yescaptcha_task_type,
            timeout=yescaptcha_timeout_seconds,
            proxy=proxy_url,
            cancel_event=cancel_event or ctx.cancel_event,
            on_log=on_log,
        )
    except StopRequested:
        raise
    except CaptchaError as err:
        return CookieLoginResult(success=False, message=f"YesCaptcha 解题失败: {err}")
    except Exception as err:  # pragma: no cover - defensive
        return CookieLoginResult(success=False, message=f"YesCaptcha 调用异常: {err}")

    emit("INFO", "已获取验证码 token，正在重新提交登录...")
    auth_resp, auth_err, data = await _submit_login(
        ctx,
        fresh_config,
        username,
        password,
        timeout=timeout,
        captcha_token=solution.token,
        page_url=page_url,
    )
    if auth_resp is None:
        return CookieLoginResult(success=False, message=f"验证码已通过，但重新登录失败: {auth_err or '请求失败'}")
    if auth_resp.status_code >= 400:
        return CookieLoginResult(
            success=False,
            message=f"验证码已通过，但登录请求返回 HTTP {auth_resp.status_code}",
        )

    if data is not None:
        if _is_auth_json_success(data):
            logged_in_user = str(data.get("username") or "").strip()
            return await _finish_login(ctx, proxy_url, timeout, logged_in_user)
        if _is_captcha_required(data):
            return CookieLoginResult(
                success=False,
                message=(
                    "YesCaptcha 已提交 token，但 PornHub 仍要求人机验证。"
                    "可尝试在设置中切换 YesCaptcha 任务类型，或手动粘贴浏览器 Cookie。"
                ),
            )
        msg = _auth_message(data) or "认证失败"
        return CookieLoginResult(success=False, message=f"验证码已通过，但 {_friendly_auth_failure(msg)}")

    if "login" in str(auth_resp.final_url).lower():
        return CookieLoginResult(
            success=False,
            message=(
                "YesCaptcha token 已提交，但 PornHub 仍跳回登录页。"
                "请检查账号密码，或手动粘贴浏览器 Cookie。"
            ),
        )
    return CookieLoginResult(
        success=False,
        message=f"验证码已通过，但登录响应异常: {auth_resp.text[:200]}",
    )


async def login_and_get_cookies(
    username: str,
    password: str,
    proxy_url: Optional[str] = None,
    timeout: float = 20.0,
    *,
    yescaptcha_enabled: bool = False,
    yescaptcha_client_key: str = "",
    yescaptcha_api_base: str = "https://api.yescaptcha.com",
    yescaptcha_task_type: str = "RecaptchaV2EnterpriseTaskProxyless",
    yescaptcha_timeout_seconds: float = 300.0,
    on_log: Optional[LogCallback] = None,
    cancel_event: Optional[asyncio.Event] = None,
    on_success: Optional[SuccessCallback] = None,
) -> CookieLoginResult:
    emit = _make_site_logger("PornHub", on_log)
    if not (username or "").strip() or not (password or "").strip():
        return _log_login_outcome(emit, CookieLoginResult(success=False, message="未配置 PornHub 账号或密码"))

    dedup_key = (username.strip().lower(), (proxy_url or "").strip())
    return await _run_deduped_login(
        dedup_key,
        "PornHub",
        lambda shared_cancel, shared_log: _login_and_get_cookies_once(
            username,
            password,
            proxy_url=proxy_url,
            timeout=timeout,
            yescaptcha_enabled=yescaptcha_enabled,
            yescaptcha_client_key=yescaptcha_client_key,
            yescaptcha_api_base=yescaptcha_api_base,
            yescaptcha_task_type=yescaptcha_task_type,
            yescaptcha_timeout_seconds=yescaptcha_timeout_seconds,
            cancel_event=shared_cancel,
            on_log=shared_log,
        ),
        on_log=on_log,
        cancel_event=cancel_event,
        on_success=on_success,
    )


async def _login_and_get_cookies_once(
    username: str,
    password: str,
    proxy_url: Optional[str] = None,
    timeout: float = 20.0,
    *,
    yescaptcha_enabled: bool = False,
    yescaptcha_client_key: str = "",
    yescaptcha_api_base: str = "https://api.yescaptcha.com",
    yescaptcha_task_type: str = "RecaptchaV2EnterpriseTaskProxyless",
    yescaptcha_timeout_seconds: float = 300.0,
    cancel_event: Optional[asyncio.Event] = None,
    on_log: Optional[LogCallback] = None,
) -> CookieLoginResult:
    emit = _make_site_logger("PornHub", on_log)

    async def _run() -> CookieLoginResult:
        try:
            if cancel_event is not None and cancel_event.is_set():
                return CookieLoginResult(success=False, message="自动登录已取消")
            emit("INFO", "开始登录，正在加载登录页...")
            ctx = TaskHttpContext(
                cookie_str="",
                proxy_url=proxy_url,
                concurrency=1,
                timeout=timeout,
                max_retries=1,
                retry_delay_base=1.0,
                retry_delay_max=2.0,
                cancel_event=cancel_event,
                on_log=emit,
            )
            async with ctx:
                page_resp, page_err = await ctx.fetch_with_pow(
                    PORNHUB_LOGIN_URL,
                    timeout=timeout,
                    max_retries=1,
                    log_label="登录页",
                )
                if page_resp is None:
                    return CookieLoginResult(
                        success=False,
                        message=f"访问登录页失败: {page_err or '请求失败'}",
                    )
                if page_resp.status_code >= 400:
                    return CookieLoginResult(success=False, message=f"访问登录页失败 (HTTP {page_resp.status_code})")

                config = _extract_login_config(page_resp.text)
                page_url = str(page_resp.final_url or PORNHUB_LOGIN_URL)
                emit(
                    "INFO",
                    f"登录页已加载（reCAPTCHA sitekey: {'已获取' if config.sitekey else '未找到'}），正在提交凭据...",
                )

                auth_resp, auth_err, data = await _submit_login(
                    ctx,
                    config,
                    username,
                    password,
                    timeout=timeout,
                    page_url=page_url,
                )
                if auth_resp is None:
                    return CookieLoginResult(success=False, message=f"自动登录失败: {auth_err or '请求失败'}")
                if auth_resp.status_code >= 400:
                    return CookieLoginResult(
                        success=False,
                        message=f"登录请求返回 HTTP {auth_resp.status_code}",
                    )

                if data is not None:
                    if _is_auth_json_success(data):
                        logged_in_user = str(data.get("username") or "").strip()
                        return await _finish_login(ctx, proxy_url, timeout, logged_in_user)

                    if _is_captcha_required(data):
                        if not yescaptcha_enabled or not (yescaptcha_client_key or "").strip():
                            return CookieLoginResult(
                                success=False,
                                message=(
                                    "登录需要人机验证（reCAPTCHA）。请在【系统设置 → Cookie 与账号】"
                                    "启用 YesCaptcha 并填写 ClientKey 后重试。"
                                ),
                            )
                        return await _login_with_captcha(
                            ctx,
                            config,
                            username,
                            password,
                            proxy_url=proxy_url,
                            timeout=timeout,
                            yescaptcha_client_key=yescaptcha_client_key,
                            yescaptcha_api_base=yescaptcha_api_base,
                            yescaptcha_task_type=yescaptcha_task_type,
                            yescaptcha_timeout_seconds=yescaptcha_timeout_seconds,
                            cancel_event=cancel_event,
                            on_log=emit,
                        )

                    msg = _auth_message(data) or "认证失败"
                    return CookieLoginResult(
                        success=False,
                        message=f"登录被拒绝: {_friendly_auth_failure(msg)}",
                    )

                body_lower = (auth_resp.text or "").lower()
                if "captcha" in body_lower or "recaptcha" in body_lower:
                    if not yescaptcha_enabled or not (yescaptcha_client_key or "").strip():
                        return CookieLoginResult(
                            success=False,
                            message=(
                                "登录需要人机验证（reCAPTCHA）。请在【系统设置 → Cookie 与账号】"
                                "启用 YesCaptcha 并填写 ClientKey 后重试。"
                            ),
                        )
                    return await _login_with_captcha(
                        ctx,
                        config,
                        username,
                        password,
                        proxy_url=proxy_url,
                        timeout=timeout,
                        yescaptcha_client_key=yescaptcha_client_key,
                        yescaptcha_api_base=yescaptcha_api_base,
                        yescaptcha_task_type=yescaptcha_task_type,
                        yescaptcha_timeout_seconds=yescaptcha_timeout_seconds,
                        cancel_event=cancel_event,
                        on_log=emit,
                    )

                if "login" in str(auth_resp.final_url).lower():
                    return CookieLoginResult(
                        success=False,
                        message=(
                            "自动登录失败，登录页未返回可解析的结果。"
                            "请在设置中手动粘贴浏览器 Cookie。"
                        ),
                    )
                return CookieLoginResult(
                    success=False,
                    message=f"自动登录失败，响应异常: {(auth_resp.text or '')[:200]}",
                )
        except StopRequested:
            return CookieLoginResult(success=False, message="自动登录已取消")
        except Exception as err:
            return CookieLoginResult(success=False, message=f"自动登录异常: {err}")

    return _log_login_outcome(emit, await _run())


def _extract_xhamster_login_error(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    extras = data.get("extras")
    containers = [extras, data] if isinstance(extras, dict) else [data]
    for box in containers:
        for key in ("error", "errors", "message", "errorMessage", "errorDescription"):
            val = box.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, dict):
                parts = [str(v).strip() for v in val.values() if str(v or "").strip()]
                if parts:
                    return "; ".join(parts)
            if isinstance(val, list):
                parts = [str(v).strip() for v in val if str(v or "").strip()]
                if parts:
                    return "; ".join(parts)
    return ""


def _friendly_xhamster_auth_failure(message: str) -> str:
    lower = (message or "").lower()
    if "invalid login" in lower or "invalid password" in lower or "wrong password" in lower:
        return f"xHamster 账号或密码错误 ({message})。请检查凭据，或切换到粘贴 Cookie。"
    if "too many" in lower or "blocked" in lower or "locked" in lower or "limit" in lower:
        return f"xHamster 登录尝试过于频繁或被暂时限制 ({message})，请稍后再试或粘贴浏览器 Cookie。"
    if "captcha" in lower or "recaptcha" in lower or "robot" in lower:
        return f"xHamster 要求人机验证 ({message})，请稍后重试或切换到粘贴浏览器 Cookie。"
    return message or "xHamster 认证失败"


def _extract_xhamster_v2_key(html: str) -> str:
    """Return the standard reCAPTCHA v2 sitekey from the xHamster login page."""

    match = re.search(
        r'["\']?recaptchaKeyV2["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        html or "",
        re.I,
    )
    return match.group(1).strip() if match else ""


def _parse_xhamster_response(text: str) -> Optional[dict]:
    try:
        parsed = json.loads(text or "")
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed else None
    return parsed if isinstance(parsed, dict) else None


def _xhamster_extras(data: object) -> Dict[str, object]:
    if not isinstance(data, dict):
        return {}
    extras = data.get("extras")
    return extras if isinstance(extras, dict) else {}


def _xhamster_login_succeeded(data: object) -> bool:
    val = _xhamster_extras(data).get("result")
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes"}
    return val is True


def _xhamster_captcha_required(data: object) -> bool:
    val = _xhamster_extras(data).get("showCaptcha")
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes"}
    return bool(val)


def _xhamster_captcha_rejected(data: object) -> bool:
    """True when xHamster rejected the submitted captcha token itself."""

    message = _extract_xhamster_login_error(data).lower()
    return "recaptcha" in message or "captcha" in message


def _xhamster_login_payload(
    username: str,
    password: str,
    page_url: str,
) -> str:
    payload: Dict[str, object] = {
        "username": username.strip(),
        "password": password,
        "remember": 1,
        "redirectURL": page_url,
        "pageType": None,
        "source": None,
        "isSubscribedToUpdates": None,
    }
    return json.dumps(payload, separators=(",", ":"))


def _xhamster_login_url(
    api_url: str,
    *,
    captcha_token: str = "",
    captcha_version: str = "",
) -> str:
    """Add reCAPTCHA fields to the login URL (the web client sends them as query params)."""

    if not captcha_token:
        return api_url
    params: Dict[str, str] = {"g-recaptcha-response": captcha_token}
    if captcha_version:
        params["g-recaptcha-version"] = captcha_version
    separator = "&" if "?" in api_url else "?"
    return f"{api_url}{separator}{urlencode(params)}"


def _xhamster_login_failure(data: object, auth_resp: object) -> CookieLoginResult:
    status_code = int(getattr(auth_resp, "status_code", 0) or 0)
    err_msg = _extract_xhamster_login_error(data)
    if err_msg:
        return CookieLoginResult(
            success=False,
            message=f"xHamster 登录被拒绝: {_friendly_xhamster_auth_failure(err_msg)}",
        )
    if status_code == 403:
        return CookieLoginResult(
            success=False,
            message="xHamster 登录被拒 (HTTP 403，可能触发 CSRF 或风控校验)，请稍后重试或粘贴浏览器 Cookie。",
        )
    if status_code >= 400:
        return CookieLoginResult(
            success=False,
            message=f"xHamster 登录请求返回 HTTP {status_code}",
        )
    return CookieLoginResult(
        success=False,
        message=f"xHamster 自动登录失败，响应异常: {(getattr(auth_resp, 'text', '') or '')[:200]}",
    )


async def login_xhamster_and_get_cookies(
    username: str,
    password: str,
    proxy_url: Optional[str] = None,
    timeout: float = 20.0,
    *,
    yescaptcha_enabled: bool = False,
    yescaptcha_client_key: str = "",
    yescaptcha_task_type: str = XHAMSTER_DEFAULT_CAPTCHA_V2_TASK_TYPE,
    yescaptcha_api_base: str = "https://api.yescaptcha.com",
    yescaptcha_timeout_seconds: float = 300.0,
    on_log: Optional[LogCallback] = None,
    cancel_event: Optional[asyncio.Event] = None,
    on_success: Optional[SuccessCallback] = None,
) -> CookieLoginResult:
    """Authenticate against ``xhamster.com/api/front/user/login`` and return verified cookies."""
    emit = _make_site_logger("xHamster", on_log)
    if not (username or "").strip() or not (password or "").strip():
        return _log_login_outcome(emit, CookieLoginResult(success=False, message="未配置 xHamster 账号或密码"))

    dedup_key = (f"xh:{username.strip().lower()}", (proxy_url or "").strip())
    return await _run_deduped_login(
        dedup_key,
        "xHamster",
        lambda shared_cancel, shared_log: _login_xhamster_once(
            username,
            password,
            proxy_url=proxy_url,
            timeout=timeout,
            yescaptcha_enabled=yescaptcha_enabled,
            yescaptcha_client_key=yescaptcha_client_key,
            yescaptcha_task_type=yescaptcha_task_type,
            yescaptcha_api_base=yescaptcha_api_base,
            yescaptcha_timeout_seconds=yescaptcha_timeout_seconds,
            cancel_event=shared_cancel,
            on_log=shared_log,
        ),
        on_log=on_log,
        cancel_event=cancel_event,
        on_success=on_success,
    )


async def _login_xhamster_once(
    username: str,
    password: str,
    proxy_url: Optional[str] = None,
    timeout: float = 20.0,
    *,
    yescaptcha_enabled: bool = False,
    yescaptcha_client_key: str = "",
    yescaptcha_task_type: str = XHAMSTER_DEFAULT_CAPTCHA_V2_TASK_TYPE,
    yescaptcha_api_base: str = "https://api.yescaptcha.com",
    yescaptcha_timeout_seconds: float = 300.0,
    cancel_event: Optional[asyncio.Event] = None,
    on_log: Optional[LogCallback] = None,
) -> CookieLoginResult:
    from ..parsers.xhamster import check_xhamster_cookie_valid

    emit = _make_site_logger("xHamster", on_log)

    async def _run() -> CookieLoginResult:
        try:
            if cancel_event is not None and cancel_event.is_set():
                return CookieLoginResult(success=False, message="xHamster 自动登录已取消")
            emit("INFO", "开始登录，正在加载登录页...")
            # xHamster uses a double-submit CSRF cookie (`x_csrf_token` cookie == `X-CSRF-Token` header).
            ctx = TaskHttpContext(
                cookie_str=f"x_csrf_token={XHAMSTER_DEFAULT_CSRF}",
                proxy_url=proxy_url,
                concurrency=1,
                timeout=timeout,
                max_retries=1,
                retry_delay_base=1.0,
                retry_delay_max=2.0,
                cancel_event=cancel_event,
                on_log=emit,
            )
            async with ctx:
                page_resp, page_err = await ctx.fetch_with_pow(
                    XHAMSTER_LOGIN_URL,
                    headers={"Referer": "https://xhamster.com/"},
                    timeout=timeout,
                    max_retries=1,
                    log_label="登录页",
                )
                if page_resp is None:
                    return CookieLoginResult(
                        success=False,
                        message=f"访问 xHamster 登录页失败: {page_err or '请求失败'}",
                    )
                if page_resp.status_code >= 400:
                    return CookieLoginResult(
                        success=False,
                        message=f"访问 xHamster 登录页失败 (HTTP {page_resp.status_code})",
                    )

                current_cookies = parse_cookie_string(ctx.cookie_string())
                csrf_token = (current_cookies.get("x_csrf_token") or XHAMSTER_DEFAULT_CSRF).strip() or XHAMSTER_DEFAULT_CSRF
                page_url = str(page_resp.final_url or XHAMSTER_LOGIN_URL)
                parsed_page = urlparse(page_url)
                origin = f"{parsed_page.scheme or 'https'}://{parsed_page.netloc or 'xhamster.com'}"
                api_url = urljoin(origin, "/api/front/user/login")
                page_v2_key = _extract_xhamster_v2_key(page_resp.text or "")

                login_headers = {
                    "X-CSRF-Token": csrf_token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "text/plain;charset=UTF-8",
                    "Origin": origin,
                    "Referer": page_url,
                    "Accept": "*/*",
                }

                async def submit(
                    captcha_token: str = "",
                    captcha_version: str = "",
                ) -> Tuple[Optional[object], Optional[str]]:
                    return await ctx.fetch_with_pow(
                        _xhamster_login_url(
                            api_url,
                            captcha_token=captcha_token,
                            captcha_version=captcha_version,
                        ),
                        method="POST",
                        data=_xhamster_login_payload(username, password, page_url),
                        headers=login_headers,
                        timeout=timeout,
                        max_retries=1,
                        log_label="登录提交",
                    )

                async def finish() -> CookieLoginResult:
                    cookie_str = ctx.cookie_string()
                    if not cookie_str:
                        return CookieLoginResult(success=False, message="xHamster 登录未返回任何 Cookie")
                    valid, verify_msg, logged_in_user = await check_xhamster_cookie_valid(
                        cookie_str,
                        proxy_url=proxy_url,
                        timeout=timeout,
                    )
                    if not valid:
                        return CookieLoginResult(
                            success=False,
                            message=f"xHamster 登录后校验失败: {verify_msg}",
                        )
                    resolved_user = (logged_in_user or username).strip()
                    return CookieLoginResult(
                        success=True,
                        cookie_str=cookie_str,
                        logged_in_username=resolved_user,
                        message=format_login_success_message("xHamster", resolved_user),
                    )

                emit(
                    "INFO",
                    f"登录页已加载（reCAPTCHA sitekey: {'已获取' if page_v2_key else '未找到'}），正在提交凭据...",
                )
                auth_resp, auth_err = await submit()
                if auth_resp is None:
                    return CookieLoginResult(
                        success=False,
                        message=f"xHamster 自动登录失败: {auth_err or '请求失败'}",
                    )

                data = _parse_xhamster_response(auth_resp.text)
                if _xhamster_login_succeeded(data):
                    return await finish()

                if _xhamster_captcha_required(data):
                    if not yescaptcha_enabled or not (yescaptcha_client_key or "").strip():
                        return CookieLoginResult(
                            success=False,
                            message=(
                                "xHamster 登录要求人机验证（reCAPTCHA v2）。"
                                "请在【系统设置 → Cookie 与账号】中启用 YesCaptcha 并填写 ClientKey，"
                                "或改为粘贴浏览器 Cookie。"
                            ),
                        )

                    v2_key = page_v2_key or XHAMSTER_RECAPTCHA_V2_KEY
                    xh_task_type = (
                        yescaptcha_task_type
                        if yescaptcha_task_type in XHAMSTER_CAPTCHA_V2_TASK_TYPES
                        else XHAMSTER_DEFAULT_CAPTCHA_V2_TASK_TYPE
                    )
                    emit("INFO", f"触发人机验证（reCAPTCHA），正在调用 YesCaptcha（{xh_task_type}）...")
                    try:
                        solution = await solve_recaptcha(
                            yescaptcha_client_key,
                            page_url,
                            v2_key,
                            api_base=yescaptcha_api_base,
                            task_type=xh_task_type,
                            timeout=yescaptcha_timeout_seconds,
                            proxy=proxy_url,
                            cancel_event=cancel_event or ctx.cancel_event,
                            on_log=emit,
                        )
                    except StopRequested:
                        raise
                    except CaptchaError as err:
                        return CookieLoginResult(
                            success=False,
                            message=(
                                f"xHamster 登录需要人机验证，YesCaptcha 解题失败: {err}。"
                                "请检查 YesCaptcha 配置/余额，或粘贴浏览器 Cookie。"
                            ),
                        )
                    except Exception as err:  # pragma: no cover - defensive
                        return CookieLoginResult(
                            success=False,
                            message=f"xHamster 登录需要人机验证，YesCaptcha 调用异常: {err}",
                        )

                    emit("INFO", "已获取验证码 token，正在重新提交登录...")
                    auth_resp, auth_err = await submit(
                        captcha_token=solution.token,
                        captcha_version="v2",
                    )
                    if auth_resp is None:
                        return CookieLoginResult(
                            success=False,
                            message=f"验证码已通过，但 xHamster 登录请求失败: {auth_err or '请求失败'}",
                        )
                    data = _parse_xhamster_response(auth_resp.text)
                    if _xhamster_login_succeeded(data):
                        return await finish()
                    if _xhamster_captcha_required(data) or _xhamster_captcha_rejected(data):
                        return CookieLoginResult(
                            success=False,
                            message=(
                                "xHamster 拒绝了 YesCaptcha v2 token。"
                                "请在设置中切换另一个标准 v2 线路"
                                "（NoCaptchaTaskProxyless / RecaptchaV2TaskProxyless）后重试，"
                                "或粘贴浏览器 Cookie。"
                            ),
                        )
                    return _xhamster_login_failure(data, auth_resp)

                return _xhamster_login_failure(data, auth_resp)
        except StopRequested:
            return CookieLoginResult(success=False, message="xHamster 自动登录已取消")
        except Exception as err:
            return CookieLoginResult(success=False, message=f"xHamster 自动登录异常: {err}")

    return _log_login_outcome(emit, await _run())
