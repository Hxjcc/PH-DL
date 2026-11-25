"""Auto-login reCAPTCHA/YesCaptcha flow tests (no real network calls)."""

from __future__ import annotations

import asyncio
import json

from app.parsers.http_session import PowFetchResult
from app.services import cookie as cookie_mod
from app.services.captcha import CaptchaSolution

LOGIN_HTML = """
<html><head><script>
var connectionModalData = {"configs":{
  "loginUrl":"/front/authenticate",
  "redirectFieldValue":"CFG_REDIRECT",
  "fromValue":"pc_login_modal_:login",
  "token":"CFG_TOKEN",
  "captchaKey":"6LfyQRoUAAAAAApqS-inJxUwCOtvOHwKxJuZCsuv",
  "showCaptcha":"",
  "captchaType":"checkbox"
}};
</script></head><body>
<template id="signinModal"><form class="js-loginFormModal js-loginForm">
  <input type="hidden" name="redirect" value="TEMPLATE_REDIRECT" />
  <input type="hidden" name="token" value="TEMPLATE_TOKEN" />
  <input type="hidden" name="from" value="pc_login_modal_:login" />
</form></template>
</body></html>
"""


def test_extract_login_config_prefers_hydrated_values():
    config = cookie_mod._extract_login_config(LOGIN_HTML)
    assert config.redirect == "CFG_REDIRECT"
    assert config.token == "CFG_TOKEN"
    assert config.sitekey == "6LfyQRoUAAAAAApqS-inJxUwCOtvOHwKxJuZCsuv"
    assert config.login_url == "/front/authenticate"


def test_captcha_detection_and_success_helpers():
    assert cookie_mod._is_captcha_required({"requireCaptcha": True})
    assert cookie_mod._is_captcha_required({"requireCaptcha": "1"})
    assert cookie_mod._is_captcha_required({"message": "Please complete the captcha"})
    assert not cookie_mod._is_captcha_required({"message": "Invalid credentials."})
    assert cookie_mod._is_auth_json_success({"success": "1", "username": "u"})
    assert not cookie_mod._is_auth_json_success({"success": "0", "requireCaptcha": True})


def test_friendly_auth_failure_messages():
    blocked = cookie_mod._friendly_auth_failure("Your account has been blocked for one hour.")
    assert "临时锁定" in blocked and "1 小时" in blocked
    invalid = cookie_mod._friendly_auth_failure("Invalid credentials.")
    assert "账号或密码错误" in invalid
    assert cookie_mod._friendly_auth_failure("Some other error") == "Some other error"


class FakeLoginContext:
    """Returns scripted responses and remembers submitted form payloads."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.post_payloads = []
        self.cancel_event = asyncio.Event()
        FakeLoginContext.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def cookie_string(self, include_pow: bool = True) -> str:
        return "sessid=abc; il=def; KEY=1*2:3:4:1"

    async def fetch_with_pow(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if kwargs.get("method") == "POST":
            payload = kwargs.get("data") or {}
            self.post_payloads.append(payload)
            index = len(self.post_payloads) - 1
            body = self.responses[index] if index < len(self.responses) else self.responses[-1]
            return PowFetchResult(json.dumps(body), url, 200, 0, False), None
        return PowFetchResult(LOGIN_HTML, url, 200, 0, False), None


class FakeCookieContext(FakeLoginContext):
    pass


async def _run_login(monkeypatch, responses, *, enabled=True, solver=None):
    FakeLoginContext.instances = []
    FakeLoginContext.responses = responses
    monkeypatch.setattr(cookie_mod, "TaskHttpContext", FakeLoginContext)

    async def fake_check(cookie_str, proxy_url=None, timeout=20.0):
        return cookie_mod.CookieCheckResult(valid=True, message="Cookie 有效")

    monkeypatch.setattr(cookie_mod, "check_cookie_valid", fake_check)

    solver_calls = []

    async def fake_solver(*args, **kwargs):
        solver_calls.append((args, kwargs))
        if solver is not None:
            return solver(*args, **kwargs)
        return CaptchaSolution(token="03A-solved", task_id="task", task_type="type")

    monkeypatch.setattr(cookie_mod, "solve_recaptcha_v2", fake_solver)

    result = await cookie_mod.login_and_get_cookies(
        "user@example.com",
        "password",
        yescaptcha_enabled=enabled,
        yescaptcha_client_key="client-key",
        yescaptcha_api_base="https://api.yescaptcha.com",
        yescaptcha_task_type="RecaptchaV2EnterpriseTaskProxyless",
        yescaptcha_timeout_seconds=180.0,
    )
    return result, FakeLoginContext.instances[-1], solver_calls


async def test_login_solves_captcha_and_resubmits(monkeypatch):
    result, ctx, solver_calls = await _run_login(
        monkeypatch,
        [
            {"success": "0", "message": "Invalid credentials.", "requireCaptcha": True},
            {"success": "1", "username": "Hxiay"},
        ],
    )

    assert result.success is True
    assert result.logged_in_username == "Hxiay"
    assert len(solver_calls) == 1
    assert solver_calls[0][1]["task_type"] == "RecaptchaV2EnterpriseTaskProxyless"
    assert ctx.post_payloads[1]["g-recaptcha-response"] == "03A-solved"
    assert ctx.post_payloads[1]["redirect"] == "CFG_REDIRECT"
    assert ctx.post_payloads[1]["token"] == "CFG_TOKEN"


async def test_login_without_yescaptcha_reports_clear_message(monkeypatch):
    result, ctx, solver_calls = await _run_login(
        monkeypatch,
        [{"success": "0", "message": "Invalid credentials.", "requireCaptcha": True}],
        enabled=False,
    )

    assert result.success is False
    assert "YesCaptcha" in result.message
    assert "ClientKey" in result.message
    assert solver_calls == []
    assert len(ctx.post_payloads) == 1


async def test_login_invalid_password_without_captcha_does_not_call_solver(monkeypatch):
    result, ctx, solver_calls = await _run_login(
        monkeypatch,
        [{"success": "0", "message": "Invalid credentials."}],
    )

    assert result.success is False
    assert "账号或密码错误" in result.message
    assert solver_calls == []
    assert len(ctx.post_payloads) == 1


async def test_login_captcha_solved_but_password_wrong(monkeypatch):
    result, _ctx, solver_calls = await _run_login(
        monkeypatch,
        [
            {"success": "0", "message": "Invalid credentials.", "requireCaptcha": True},
            {"success": "0", "message": "Invalid credentials."},
        ],
    )

    assert result.success is False
    assert "验证码已通过" in result.message
    assert "账号或密码错误" in result.message
    assert len(solver_calls) == 1


async def test_concurrent_login_calls_coalesce_single_captcha_solve(monkeypatch):
    FakeLoginContext.instances = []
    FakeLoginContext.responses = [
        {"success": "0", "message": "Captcha required", "requireCaptcha": True},
        {"success": "1", "username": "Hxiay"},
    ]
    monkeypatch.setattr(cookie_mod, "TaskHttpContext", FakeLoginContext)

    async def fake_check(cookie_str, proxy_url=None, timeout=20.0):
        return cookie_mod.CookieCheckResult(valid=True, message="Cookie 有效")

    monkeypatch.setattr(cookie_mod, "check_cookie_valid", fake_check)

    solver_calls = 0

    async def slow_solver(*args, **kwargs):
        nonlocal solver_calls
        solver_calls += 1
        await asyncio.sleep(0.05)
        return CaptchaSolution(token="03A-shared", task_id="t1", task_type="type")

    monkeypatch.setattr(cookie_mod, "solve_recaptcha_v2", slow_solver)

    r1, r2 = await asyncio.gather(
        cookie_mod.login_and_get_cookies(
            "user@example.com",
            "password",
            yescaptcha_enabled=True,
            yescaptcha_client_key="client-key",
        ),
        cookie_mod.login_and_get_cookies(
            "user@example.com",
            "password",
            yescaptcha_enabled=True,
            yescaptcha_client_key="client-key",
        ),
    )

    assert r1.success is True and r2.success is True
    assert r1.cookie_str == r2.cookie_str
    assert solver_calls == 1
    assert len(FakeLoginContext.instances) == 1
