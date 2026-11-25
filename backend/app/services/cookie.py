"""Cookie validation and PornHub login helper (fully async, no CLI prompts)."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse
from weakref import WeakKeyDictionary

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..parsers.http_session import TaskHttpContext, parse_cookie_string
from .captcha import CaptchaError, solve_recaptcha_v2

PORNHUB_LOGIN_URL = "https://www.pornhub.com/login"
# Stable reCAPTCHA sitekey of the PornHub login modal (fallback only).
PORNHUB_DEFAULT_SITEKEY = "6LfyQRoUAAAAAApqS-inJxUwCOtvOHwKxJuZCsuv"

LogCallback = Callable[[str, str], None]

# Coalesce concurrent auto-login calls per event loop + (account, proxy) to avoid
# firing duplicate YesCaptcha solves or triggering PornHub's 1-hour account lock.
_INFLIGHT_LOGINS: "WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[Tuple[str, str], asyncio.Task[CookieLoginResult]]]" = (
    WeakKeyDictionary()
)


@dataclass(frozen=True)
class CookieCheckResult:
    valid: bool
    message: str


@dataclass(frozen=True)
class CookieLoginResult:
    success: bool
    cookie_str: str = ""
    logged_in_username: str = ""
    message: str = ""


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


async def _finish_login(
    ctx: TaskHttpContext,
    proxy_url: Optional[str],
    timeout: float,
    logged_in_username: str,
) -> CookieLoginResult:
    cookie_str = ctx.cookie_string()
    if not cookie_str:
        return CookieLoginResult(success=False, message="登录未返回任何 Cookie")

    verify = await check_cookie_valid(cookie_str, proxy_url=proxy_url, timeout=timeout)
    if not verify.valid:
        return CookieLoginResult(success=False, message=f"登录后验证失败: {verify.message}")

    return CookieLoginResult(
        success=True,
        cookie_str=cookie_str,
        logged_in_username=logged_in_username,
        message="登录并更新 Cookie 成功",
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
            return CookieCheckResult(valid=True, message="Cookie 有效")
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
    on_log: Optional[LogCallback],
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
    emit("INFO", "检测到 PornHub 要求 reCAPTCHA，正在调用 YesCaptcha 解题...")

    try:
        solution = await solve_recaptcha_v2(
            yescaptcha_client_key,
            page_url,
            sitekey,
            api_base=yescaptcha_api_base,
            task_type=yescaptcha_task_type,
            timeout=yescaptcha_timeout_seconds,
            proxy=proxy_url,
            cancel_event=ctx.cancel_event,
            on_log=on_log,
        )
    except CaptchaError as err:
        emit("WARNING", f"YesCaptcha 解题失败: {err}")
        return CookieLoginResult(success=False, message=f"YesCaptcha 解题失败: {err}")
    except Exception as err:  # pragma: no cover - defensive
        emit("WARNING", f"YesCaptcha 调用异常: {type(err).__name__}: {err}")
        return CookieLoginResult(success=False, message=f"YesCaptcha 调用异常: {err}")

    emit("INFO", "YesCaptcha 已返回 token，正在重新提交登录...")
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
    yescaptcha_timeout_seconds: float = 180.0,
    on_log: Optional[LogCallback] = None,
) -> CookieLoginResult:
    if not (username or "").strip() or not (password or "").strip():
        return CookieLoginResult(success=False, message="未配置 PornHub 账号或密码")

    loop = asyncio.get_running_loop()
    inflight = _INFLIGHT_LOGINS.setdefault(loop, {})
    dedup_key = (username.strip().lower(), (proxy_url or "").strip())
    existing = inflight.get(dedup_key)
    if existing is not None and not existing.done():
        if on_log:
            on_log("INFO", "检测到已有正在进行的自动登录任务，复用本次登录结果...")
        return await asyncio.shield(existing)

    task = asyncio.create_task(
        _login_and_get_cookies_once(
            username,
            password,
            proxy_url=proxy_url,
            timeout=timeout,
            yescaptcha_enabled=yescaptcha_enabled,
            yescaptcha_client_key=yescaptcha_client_key,
            yescaptcha_api_base=yescaptcha_api_base,
            yescaptcha_task_type=yescaptcha_task_type,
            yescaptcha_timeout_seconds=yescaptcha_timeout_seconds,
            on_log=on_log,
        )
    )
    inflight[dedup_key] = task

    def _cleanup(finished: asyncio.Task[CookieLoginResult]) -> None:
        if inflight.get(dedup_key) is finished:
            inflight.pop(dedup_key, None)

    task.add_done_callback(_cleanup)
    return await asyncio.shield(task)


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
    yescaptcha_timeout_seconds: float = 180.0,
    on_log: Optional[LogCallback] = None,
) -> CookieLoginResult:

    def emit(level: str, message: str) -> None:
        if on_log:
            on_log(level, message)

    try:
        ctx = TaskHttpContext(
            cookie_str="",
            proxy_url=proxy_url,
            concurrency=1,
            timeout=timeout,
            max_retries=1,
            retry_delay_base=1.0,
            retry_delay_max=2.0,
            on_log=on_log,
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
                f"登录页已加载（reCAPTCHA sitekey: {'已获取' if config.sitekey else '未找到'}）",
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
                        on_log=on_log,
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
                    on_log=on_log,
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
    except Exception as err:
        return CookieLoginResult(success=False, message=f"自动登录异常: {err}")
