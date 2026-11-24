"""YesCaptcha client unit tests (no real network calls)."""

from __future__ import annotations

import pytest

from app.services import captcha


async def test_solve_recaptcha_v2_polls_until_ready(monkeypatch):
    calls = []

    async def fake_post(url, payload, timeout=30.0, secret=""):
        calls.append((url, payload))
        if url.endswith("/createTask"):
            return {"errorId": 0, "taskId": "task-1"}
        return {
            "errorId": 0,
            "status": "ready",
            "solution": {"gRecaptchaResponse": "03A-test-token"},
        }

    monkeypatch.setattr(captcha, "_post_json", fake_post)

    solution = await captcha.solve_recaptcha_v2(
        "client-key",
        "https://www.pornhub.com/login",
        "site-key",
        poll_interval=0.01,
    )

    assert solution.token == "03A-test-token"
    assert solution.task_id == "task-1"
    assert calls[0][0].endswith("/createTask")
    assert calls[0][1]["task"] == {
        "type": captcha.DEFAULT_TASK_TYPE,
        "websiteURL": "https://www.pornhub.com/login",
        "websiteKey": "site-key",
    }
    assert calls[1][0].endswith("/getTaskResult")


async def test_solve_recaptcha_v2_handles_processing_then_ready(monkeypatch):
    results = [
        {"errorId": 0, "taskId": "task-2"},
        {"errorId": 0, "status": "processing"},
        {"errorId": 0, "status": "ready", "solution": {"token": "fallback-field"}},
    ]
    calls = []

    async def fake_post(url, payload, timeout=30.0, secret=""):
        calls.append(url)
        return results.pop(0)

    monkeypatch.setattr(captcha, "_post_json", fake_post)

    solution = await captcha.solve_recaptcha_v2(
        "key",
        "https://www.pornhub.com/login",
        "site",
        poll_interval=0.01,
        timeout=30,
    )

    assert solution.token == "fallback-field"
    assert len(calls) == 3


async def test_solve_recaptcha_v2_reports_api_error(monkeypatch):
    async def fake_post(url, payload, timeout=30.0, secret=""):
        return {
            "errorId": 1,
            "errorCode": "ERROR_ZERO_BALANCE",
            "errorDescription": "帐户余额不足",
        }

    monkeypatch.setattr(captcha, "_post_json", fake_post)

    with pytest.raises(captcha.CaptchaError) as excinfo:
        await captcha.solve_recaptcha_v2("key", "https://example.com", "site")

    assert excinfo.value.code == "ERROR_ZERO_BALANCE"
    assert "ERROR_ZERO_BALANCE" in str(excinfo.value)


async def test_api_error_does_not_leak_client_key(monkeypatch):
    async def fake_post(url, payload, timeout=30.0, secret=""):
        return {
            "errorId": 1,
            "errorCode": "ERROR_KEY_DOES_NOT_EXIST",
            "errorDescription": "帐户密钥错误: 请检查 super-secret-key",
        }

    monkeypatch.setattr(captcha, "_post_json", fake_post)

    with pytest.raises(captcha.CaptchaError) as excinfo:
        await captcha.get_yescaptcha_balance("super-secret-key")

    assert "super-secret-key" not in str(excinfo.value)
    assert "***" in str(excinfo.value)


async def test_solve_recaptcha_v2_requires_client_key():
    with pytest.raises(captcha.CaptchaError) as excinfo:
        await captcha.solve_recaptcha_v2("", "https://example.com", "site")
    assert excinfo.value.code == "ERROR_NO_CLIENT_KEY"


async def test_get_yescaptcha_balance(monkeypatch):
    async def fake_post(url, payload, timeout=30.0, secret=""):
        assert url.endswith("/getBalance")
        assert payload == {"clientKey": "key"}
        return {"errorId": 0, "balance": 12345}

    monkeypatch.setattr(captcha, "_post_json", fake_post)
    assert await captcha.get_yescaptcha_balance("key") == 12345


async def test_solve_recaptcha_v2_falls_back_to_default_task_type(monkeypatch):
    captured = {}

    async def fake_post(url, payload, timeout=30.0, secret=""):
        if url.endswith("/createTask"):
            captured.update(payload)
            return {"errorId": 0, "taskId": "t"}
        return {"errorId": 0, "status": "ready", "solution": {"gRecaptchaResponse": "tok"}}

    monkeypatch.setattr(captcha, "_post_json", fake_post)

    solution = await captcha.solve_recaptcha_v2(
        "key",
        "https://example.com",
        "site",
        task_type="NotARealType",
        poll_interval=0.01,
        timeout=30,
    )

    assert solution.token == "tok"
    assert captured["task"]["type"] == captcha.DEFAULT_TASK_TYPE


def test_normalize_api_base_and_proxy_fields():
    assert captcha.normalize_api_base("") == captcha.DEFAULT_API_BASE
    assert captcha.normalize_api_base("cn.yescaptcha.com/") == "https://cn.yescaptcha.com"
    assert captcha.normalize_api_base("http://localhost:8080") == "http://localhost:8080"

    assert captcha._proxy_fields("") == {}
    fields = captcha._proxy_fields("http://user:pass@proxy.example.com:8080")
    assert fields == {
        "proxyType": "http",
        "proxyAddress": "proxy.example.com",
        "proxyPort": 8080,
        "proxyLogin": "user",
        "proxyPassword": "pass",
    }
