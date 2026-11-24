"""Cookie validation and PornHub login helper (fully async, no CLI prompts)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..parsers.http_session import TaskHttpContext, parse_cookie_string


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
            max_retries=1,
            retry_delay_base=1.0,
            retry_delay_max=2.0,
        )
        async with ctx:
            res, err = await ctx.fetch_with_pow(
                "https://www.pornhub.com/settings/security",
                timeout=timeout,
                max_retries=1,
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


async def login_and_get_cookies(
    username: str,
    password: str,
    proxy_url: Optional[str] = None,
    timeout: float = 20.0,
) -> CookieLoginResult:
    if not (username or "").strip() or not (password or "").strip():
        return CookieLoginResult(success=False, message="未配置 PornHub 账号或密码")

    login_url = "https://www.pornhub.com/login"
    auth_url = "https://www.pornhub.com/front/authenticate"

    try:
        ctx = TaskHttpContext(
            cookie_str="",
            proxy_url=proxy_url,
            concurrency=1,
            timeout=timeout,
            max_retries=1,
            retry_delay_base=1.0,
            retry_delay_max=2.0,
        )
        async with ctx:
            page_resp, page_err = await ctx.fetch_with_pow(
                login_url,
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

            token, redirect, from_val = _extract_login_form_fields(page_resp.text)
            payload = {
                "redirect": redirect or "",
                "user_id": "",
                "intended_action": "",
                "token": token or "",
                "from": from_val or "pc_login_modal_:login",
                "email": username.strip(),
                "password": password,
            }
            auth_resp, auth_err = await ctx.fetch_with_pow(
                auth_url,
                method="POST",
                data=payload,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": "https://www.pornhub.com",
                    "Referer": login_url,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                },
                timeout=timeout,
                max_retries=1,
                log_label="登录提交",
            )
            if auth_resp is None:
                return CookieLoginResult(success=False, message=f"自动登录失败: {auth_err or '请求失败'}")
            if auth_resp.status_code >= 400:
                return CookieLoginResult(
                    success=False,
                    message=f"登录请求返回 HTTP {auth_resp.status_code}",
                )

            logged_in_user = ""
            try:
                data = json.loads(auth_resp.text)
                if isinstance(data, dict):
                    if _is_auth_json_failure(data):
                        err_msg = data.get("message") or data.get("error") or "认证失败"
                        return CookieLoginResult(success=False, message=f"登录被拒绝: {err_msg}")
                    logged_in_user = str(data.get("username") or "").strip()
            except (ValueError, TypeError):
                if "login" in str(auth_resp.final_url).lower():
                    return CookieLoginResult(
                        success=False,
                        message="自动登录失败，可能触发了人机验证 (CAPTCHA)，请在设置中手动粘贴浏览器 Cookie",
                    )

            cookie_str = ctx.cookie_string()
            if not cookie_str:
                return CookieLoginResult(success=False, message="登录未返回任何 Cookie")

            verify = await check_cookie_valid(cookie_str, proxy_url=proxy_url, timeout=timeout)
            if not verify.valid:
                return CookieLoginResult(success=False, message=f"登录后验证失败: {verify.message}")

            return CookieLoginResult(
                success=True,
                cookie_str=cookie_str,
                logged_in_username=logged_in_user,
                message="登录并更新 Cookie 成功",
            )
    except Exception as err:
        return CookieLoginResult(success=False, message=f"自动登录异常: {err}")
