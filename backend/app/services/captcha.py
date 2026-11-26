"""YesCaptcha client used by the PornHub and xHamster auto-login flows.

YesCaptcha exposes a two-step JSON API: ``createTask`` returns a task id and
``getTaskResult`` is polled until the reCAPTCHA token is ready.  The module is
deliberately dependency-light (aiohttp only) so it can also be exercised from
the settings page balance check.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import aiohttp

from ..cancel import StopRequested, until_stopped

DEFAULT_API_BASE = "https://api.yescaptcha.com"
DEFAULT_TASK_TYPE = "RecaptchaV2EnterpriseTaskProxyless"
SUPPORTED_TASK_TYPES = (
    "RecaptchaV2EnterpriseTaskProxyless",
    "NoCaptchaTaskProxyless",
    "RecaptchaV2TaskProxyless",
    "NoCaptchaTask",
    "RecaptchaV2EnterpriseTask",
    # reCAPTCHA v3 / Enterprise v3 (used by xHamster login)
    "RecaptchaV3TaskProxyless",
    "RecaptchaV3TaskProxylessM1",
    "RecaptchaV3EnterpriseTask",
    "RecaptchaV3EnterpriseTaskM1",
)
MAX_POLL_SECONDS = 5.0

LogCallback = Callable[[str, str], None]


class CaptchaError(Exception):
    """A YesCaptcha request failed (bad key, no balance, timeout, ...)."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CaptchaSolution:
    token: str
    task_id: str
    task_type: str


def normalize_api_base(api_base: Optional[str]) -> str:
    base = (api_base or "").strip().rstrip("/")
    if not base:
        return DEFAULT_API_BASE
    if not re.match(r"^https?://", base, re.I):
        base = "https://" + base
    return base


def _emit(on_log: Optional[LogCallback], level: str, message: str) -> None:
    if on_log:
        on_log(level, message)


def _check_api_error(data: Dict[str, object], client_key: str = "") -> None:
    if not data.get("errorId"):
        return
    code = str(data.get("errorCode") or "ERROR_UNKNOWN")
    description = str(data.get("errorDescription") or "未知错误")
    if client_key:
        description = description.replace(client_key, "***")
    raise CaptchaError(f"{code}: {description}", code)


async def _post_json(
    url: str,
    payload: Dict[str, object],
    timeout: float = 30.0,
    secret: str = "",
) -> Dict[str, object]:
    timeout_cfg = aiohttp.ClientTimeout(total=max(5.0, float(timeout)))
    headers = {"Accept-Language": "zh-CN"}
    try:
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.post(url, json=payload, headers=headers) as response:
                text = await response.text()
                if response.status >= 400:
                    safe_text = text.replace(secret, "***") if secret else text
                    raise CaptchaError(f"YesCaptcha HTTP {response.status}: {safe_text[:300]}")
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
        raise CaptchaError(f"连接 YesCaptcha 失败: {type(err).__name__}: {err}") from err

    safe_text = text.replace(secret, "***") if secret else text
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as err:
        raise CaptchaError(f"YesCaptcha 返回了无法解析的内容: {safe_text[:300]}") from err
    if not isinstance(data, dict):
        raise CaptchaError("YesCaptcha 返回格式异常")
    return data


async def solve_recaptcha(
    client_key: str,
    website_url: str,
    website_key: str,
    *,
    api_base: str = DEFAULT_API_BASE,
    task_type: str = DEFAULT_TASK_TYPE,
    timeout: float = 180.0,
    poll_interval: float = MAX_POLL_SECONDS,
    proxy: Optional[str] = None,
    page_action: Optional[str] = None,
    cancel_event: Optional[asyncio.Event] = None,
    on_log: Optional[LogCallback] = None,
) -> CaptchaSolution:
    """Create a reCAPTCHA task and return the solved token.

    Supports both v2 task types and Enterprise v3 task types.  For v3, pass
    ``page_action`` (the ``action`` used by ``grecaptcha.execute`` on the
    target page) so the token is accepted by the site.

    The caller is responsible for submitting ``token`` to the target site as
    the ``g-recaptcha-response`` field (or the site's equivalent).
    """

    key = (client_key or "").strip()
    if not key:
        raise CaptchaError("未配置 YesCaptcha ClientKey", "ERROR_NO_CLIENT_KEY")
    if not (website_url or "").strip():
        raise CaptchaError("缺少验证码网页地址", "ERROR_NO_WEBSITE_URL")
    if not (website_key or "").strip():
        raise CaptchaError("登录页未找到 reCAPTCHA sitekey", "ERROR_NO_WEBSITE_KEY")

    if task_type not in SUPPORTED_TASK_TYPES:
        task_type = DEFAULT_TASK_TYPE

    task: Dict[str, object] = {
        "type": task_type,
        "websiteURL": website_url.strip(),
        "websiteKey": website_key.strip(),
    }
    if (page_action or "").strip():
        task["pageAction"] = page_action.strip()
    proxy_fields = _proxy_fields(proxy)
    if task_type in {"NoCaptchaTask", "RecaptchaV2EnterpriseTask"}:
        if not proxy_fields:
            raise CaptchaError(
                "该 YesCaptcha 任务类型需要自有代理，请先在网络设置中配置 proxy_url",
                "ERROR_PROXY_REQUIRED",
            )
        task.update(proxy_fields)

    base = normalize_api_base(api_base)
    started = time.perf_counter()
    _emit(on_log, "INFO", f"YesCaptcha 创建解题任务（{task_type}）...")

    create_data = await _post_json(
        f"{base}/createTask",
        {"clientKey": key, "task": task},
        timeout=30.0,
        secret=key,
    )
    _check_api_error(create_data, key)
    task_id = str(create_data.get("taskId") or "").strip()
    if not task_id:
        raise CaptchaError("YesCaptcha 未返回 taskId", "ERROR_NO_TASK_ID")
    _emit(on_log, "INFO", f"YesCaptcha 任务已创建: {task_id}")

    deadline = time.monotonic() + max(30.0, float(timeout))
    delay = max(0.05, min(float(poll_interval), MAX_POLL_SECONDS))
    polls = 0
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        wait_for = min(delay, remaining)
        if wait_for > 0:
            try:
                if cancel_event is not None:
                    await until_stopped(asyncio.sleep(wait_for), cancel_event)
                else:
                    await asyncio.sleep(wait_for)
            except StopRequested:
                raise
        polls += 1
        result = await _post_json(
            f"{base}/getTaskResult",
            {"clientKey": key, "taskId": task_id},
            timeout=30.0,
            secret=key,
        )
        _check_api_error(result, key)
        status = str(result.get("status") or "").lower()
        if status == "ready":
            solution = result.get("solution")
            if not isinstance(solution, dict):
                raise CaptchaError("YesCaptcha 返回结果缺少 solution", "ERROR_NO_SOLUTION")
            token = str(
                solution.get("gRecaptchaResponse")
                or solution.get("token")
                or ""
            ).strip()
            if not token:
                raise CaptchaError("YesCaptcha 返回结果缺少 token", "ERROR_EMPTY_TOKEN")
            elapsed = time.perf_counter() - started
            _emit(on_log, "INFO", f"YesCaptcha 解题完成，耗时 {elapsed:.1f}s")
            return CaptchaSolution(token=token, task_id=task_id, task_type=task_type)
        if status and status != "processing":
            raise CaptchaError(f"YesCaptcha 任务状态异常: {status}", "ERROR_BAD_STATUS")
        _emit(on_log, "INFO", f"YesCaptcha 识别中...（第 {polls} 次查询）")

    raise CaptchaError("YesCaptcha 解题超时", "ERROR_TASK_TIMEOUT")


async def solve_recaptcha_v2(
    client_key: str,
    website_url: str,
    website_key: str,
    *,
    api_base: str = DEFAULT_API_BASE,
    task_type: str = DEFAULT_TASK_TYPE,
    timeout: float = 180.0,
    poll_interval: float = MAX_POLL_SECONDS,
    proxy: Optional[str] = None,
    cancel_event: Optional[asyncio.Event] = None,
    on_log: Optional[LogCallback] = None,
) -> CaptchaSolution:
    """Backward-compatible reCAPTCHA v2 wrapper around :func:`solve_recaptcha`."""

    return await solve_recaptcha(
        client_key,
        website_url,
        website_key,
        api_base=api_base,
        task_type=task_type,
        timeout=timeout,
        poll_interval=poll_interval,
        proxy=proxy,
        cancel_event=cancel_event,
        on_log=on_log,
    )


async def get_yescaptcha_balance(
    client_key: str,
    *,
    api_base: str = DEFAULT_API_BASE,
    timeout: float = 20.0,
) -> int:
    """Return the account balance in points; also validates the clientKey."""

    key = (client_key or "").strip()
    if not key:
        raise CaptchaError("未配置 YesCaptcha ClientKey", "ERROR_NO_CLIENT_KEY")
    base = normalize_api_base(api_base)
    data = await _post_json(
        f"{base}/getBalance",
        {"clientKey": key},
        timeout=timeout,
        secret=key,
    )
    _check_api_error(data, key)
    balance = data.get("balance")
    try:
        return int(balance)
    except (TypeError, ValueError) as err:
        raise CaptchaError("YesCaptcha 余额响应格式异常") from err


def _proxy_fields(proxy_url: Optional[str]) -> Dict[str, object]:
    """Parse ``scheme://user:pass@host:port`` into YesCaptcha proxy fields."""

    raw = (proxy_url or "").strip()
    if not raw:
        return {}
    parsed = re.match(
        r"^(?P<scheme>https?|socks4|socks5)://(?:(?P<user>[^:@/]+)(?::(?P<pw>[^@/]*))?@)?"
        r"(?P<host>[^:/]+):(?P<port>\d+)/?$",
        raw,
        re.I,
    )
    if not parsed:
        return {}
    fields: Dict[str, object] = {
        "proxyType": parsed.group("scheme").lower(),
        "proxyAddress": parsed.group("host"),
        "proxyPort": int(parsed.group("port")),
    }
    if parsed.group("user"):
        fields["proxyLogin"] = parsed.group("user")
        fields["proxyPassword"] = parsed.group("pw") or ""
    return fields
