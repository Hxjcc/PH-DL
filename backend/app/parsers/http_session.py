"""Task-scoped curl_cffi HTTP context with pinned Chrome fingerprint & shared PoW cookie pool."""

from __future__ import annotations

import asyncio
import random
import time
import warnings
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin

warnings.filterwarnings(
    "ignore",
    message=r"(?s).*Proactor event loop does not implement add_reader.*",
)

from curl_cffi.requests import AsyncSession as CffiSession

from ..cancel import StopRequested, until_stopped
from . import challenge

# Only Chrome fingerprints: Safari fingerprints trigger a stripped-down PornHub HTML layout
# that omits videos and breaks primary container selectors.
CHROME_FINGERPRINTS: Tuple[str, ...] = (
    "chrome110",
    "chrome116",
    "chrome119",
    "chrome120",
    "chrome123",
    "chrome124",
)

DEFAULT_HEADERS: Dict[str, str] = {
    "Referer": "https://www.pornhub.com/",
    "Accept-Language": "en-US,en;q=0.9",
}

LogCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class PowFetchResult:
    text: str
    final_url: str
    status_code: int
    challenge_solves: int
    retried: bool


def parse_cookie_string(cookie_str: Optional[str]) -> Dict[str, str]:
    if not cookie_str or not cookie_str.strip():
        return {}
    parsed = SimpleCookie()
    try:
        parsed.load(cookie_str.strip())
        result = {name: morsel.value for name, morsel in parsed.items()}
        if result:
            return result
    except Exception:
        pass
    # Fallback manual parser for cookies containing characters SimpleCookie dislikes
    result = {}
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            k = k.strip()
            if k:
                result[k] = v.strip()
    return result


def extract_set_cookie_headers(headers: Any) -> List[str]:
    if headers is None:
        return []
    multi_items = getattr(headers, "multi_items", None)
    if multi_items is not None:
        return [v for k, v in multi_items() if k.lower() == "set-cookie"]
    getall = getattr(headers, "getall", None)
    if getall is not None:
        return list(getall("Set-Cookie", []))
    get = getattr(headers, "get", None)
    if get is not None:
        val = get("Set-Cookie")
        return [val] if val else []
    return []


def parse_set_cookie_headers(set_cookie_headers: List[str]) -> Dict[str, str]:
    """Parse response Set-Cookie headers into a short-lived task cookie jar."""
    result: Dict[str, str] = {}
    for header in set_cookie_headers:
        cookie = SimpleCookie()
        try:
            cookie.load(header)
        except Exception:
            continue
        for name, morsel in cookie.items():
            max_age = str(morsel["max-age"] or "").strip()
            if max_age.isdigit() and int(max_age) <= 0:
                continue
            result[name] = morsel.value
    return result


class TaskHttpContext:
    """Shared HTTP context for a single job/operation.

    - Pins ONE Chrome TLS fingerprint across list pages, detail pages, get_media, and link refreshes.
    - Shares server-issued session cookies and PoW cookies across the entire job.
    - Reserves 2x concurrency pool size to avoid HTTP/2 multiplexing curl(16) errors.
    """

    def __init__(
        self,
        *,
        cookie_str: str = "",
        proxy_url: Optional[str] = None,
        concurrency: int = 5,
        timeout: float = 20.0,
        max_retries: int = 3,
        retry_delay_base: float = 2.0,
        retry_delay_max: float = 10.0,
        impersonate: Optional[str] = None,
        cancel_event: Optional[asyncio.Event] = None,
        on_log: Optional[LogCallback] = None,
        allow_url: Optional[Callable[[str], bool]] = None,
        allow_cookie_url: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.base_cookies: Dict[str, str] = parse_cookie_string(cookie_str)
        self.proxy_url = (proxy_url or "").strip() or None
        self.pool_size = max(4, int(concurrency) * 2)
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.retry_delay_base = float(retry_delay_base)
        self.retry_delay_max = float(retry_delay_max)
        self.impersonate: str = impersonate or random.choice(CHROME_FINGERPRINTS)
        self.session_cookies: Dict[str, str] = {}
        self.pow_cookies: Dict[str, str] = {}
        self.cancel_event = cancel_event or asyncio.Event()
        self.on_log = on_log
        self._allow_url = allow_url
        self._allow_cookie_url = allow_cookie_url
        self._session: Optional[CffiSession] = None
        self._cookie_lock = asyncio.Lock()
        self._challenge_lock = asyncio.Lock()
        self._challenge_generation = 0
        self.challenge_solve_count = 0
        self.challenge_reuse_count = 0
        self.challenge_solve_seconds = 0.0

    async def __aenter__(self) -> "TaskHttpContext":
        self._session = CffiSession(proxy=self.proxy_url, max_clients=self.pool_size)
        await self._session.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.__aexit__(exc_type, exc, tb)
            self._session = None

    def _emit(self, level: str, msg: str) -> None:
        if self.on_log:
            self.on_log(level, msg)

    async def _absorb_set_cookies(self, set_cookie_headers: List[str]) -> None:
        parsed = parse_set_cookie_headers(set_cookie_headers)
        if not parsed:
            return
        async with self._cookie_lock:
            self.session_cookies.update(parsed)

    def merged_cookies(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        merged = dict(self.base_cookies)
        merged.update(self.session_cookies)
        merged.update(self.pow_cookies)
        if extra:
            merged.update(extra)
        return merged

    def cookie_string(self, include_pow: bool = True) -> str:
        cookies = dict(self.base_cookies)
        cookies.update(self.session_cookies)
        if include_pow:
            cookies.update(self.pow_cookies)
        return "; ".join(f"{name}={value}" for name, value in cookies.items())

    async def _raw_request(
        self,
        method: str,
        url: str,
        cookies: Dict[str, str],
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str, List[str], int]:
        merged_headers = dict(DEFAULT_HEADERS)
        if headers:
            merged_headers.update(headers)
        req_timeout = float(timeout if timeout is not None else self.timeout)
        verb = method.upper()

        async def _do(sess: CffiSession) -> Tuple[str, str, List[str], int]:
            current_url = url
            for _ in range(6):
                if self._allow_url and not self._allow_url(current_url):
                    raise ValueError("请求地址不在允许范围内")
                can_use_cookies = not self._allow_cookie_url or self._allow_cookie_url(current_url)
                request_kwargs = {
                    "cookies": (cookies or None) if can_use_cookies else None,
                    "headers": merged_headers,
                    "timeout": req_timeout,
                    "allow_redirects": self._allow_url is None,
                    "impersonate": self.impersonate,
                    "discard_cookies": True,
                }
                if verb == "POST":
                    resp = await sess.post(current_url, data=data, **request_kwargs)
                else:
                    resp = await sess.get(current_url, **request_kwargs)
                set_cookies = extract_set_cookie_headers(resp.headers) if can_use_cookies else []
                await self._absorb_set_cookies(set_cookies)
                status = int(resp.status_code)
                location = resp.headers.get("Location")
                if self._allow_url and verb == "GET" and status in {301, 302, 303, 307, 308} and location:
                    current_url = urljoin(str(resp.url), location)
                    continue
                return resp.text, str(resp.url), set_cookies, status
            raise ValueError("请求重定向次数过多")

        if self._session is not None:
            return await _do(self._session)
        async with CffiSession(proxy=self.proxy_url, max_clients=2) as temp_session:
            return await _do(temp_session)

    async def _raw_get(
        self,
        url: str,
        cookies: Dict[str, str],
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[str, str, List[str], int]:
        return await self._raw_request("GET", url, cookies, headers=headers, timeout=timeout)

    async def _raw_post(
        self,
        url: str,
        cookies: Dict[str, str],
        data: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[str, str, List[str], int]:
        return await self._raw_request(
            "POST",
            url,
            cookies,
            headers=headers,
            timeout=timeout,
            data=data,
        )

    async def fetch_with_pow(
        self,
        url: str,
        *,
        method: str = "GET",
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        retry_delay_base: Optional[float] = None,
        retry_delay_max: Optional[float] = None,
        log_label: str = "请求",
    ) -> Tuple[Optional[PowFetchResult], Optional[str]]:
        retries_left = self.max_retries if max_retries is None else max(0, int(max_retries))
        initial_retries = retries_left
        challenge_solves = 0
        extra_cookies: Dict[str, str] = {}
        verb = method.upper()

        while retries_left >= 0:
            if self.cancel_event.is_set():
                return None, "操作已取消"

            merged_cookies = self.merged_cookies(extra_cookies)

            try:
                if verb == "POST":
                    request = self._raw_post(
                        url,
                        merged_cookies,
                        data or {},
                        headers=headers,
                        timeout=timeout,
                    )
                else:
                    request = self._raw_get(
                        url,
                        merged_cookies,
                        headers=headers,
                        timeout=timeout,
                    )
                text, final_url, set_cookies, status_code = await until_stopped(request, self.cancel_event)
            except StopRequested:
                return None, "操作已取消"
            except (ConnectionError, TimeoutError, OSError, asyncio.TimeoutError) as err:
                if self.cancel_event.is_set():
                    return None, "操作已取消"
                if retries_left == 0:
                    self._emit("WARNING", f"{log_label}多次重试仍失败 ({type(err).__name__}): {url}")
                    return None, f"网络错误 ({type(err).__name__})"

                retry_num = initial_retries - retries_left
                base = self.retry_delay_base if retry_delay_base is None else float(retry_delay_base)
                cap = self.retry_delay_max if retry_delay_max is None else float(retry_delay_max)
                delay = min(base * (2 ** retry_num), cap)
                delay += random.uniform(0, delay * 0.2)
                self._emit("WARNING", f"{log_label}发生可重试错误 ({type(err).__name__})，{delay:.1f}s 后重试...")
                try:
                    await until_stopped(asyncio.sleep(delay), self.cancel_event)
                except StopRequested:
                    return None, "操作已取消"
                retries_left -= 1
                continue
            except Exception as err:
                self._emit("ERROR", f"{log_label}发生异常 ({type(err).__name__}): {err}")
                return None, f"请求异常 ({type(err).__name__})"

            if challenge.is_challenge(text):
                if challenge_solves >= challenge.MAX_CHALLENGE_SOLVES:
                    self._emit("WARNING", f"{log_label} PoW 质询连续 {challenge_solves} 次未通过")
                    return None, "PoW 质询未通过"
                generation_before_wait = self._challenge_generation
                solved_here = False

                # Multiple concurrent workers often receive the same anti-bot challenge.
                # Only one worker needs to solve it; the others reuse the refreshed cookies.
                async with self._challenge_lock:
                    if (
                        self._challenge_generation > generation_before_wait
                        and "KEY" in self.pow_cookies
                    ):
                        self.challenge_reuse_count += 1
                        self._emit("INFO", f"{log_label} 复用并发请求刚解开的 PoW Cookie")
                    else:
                        solve_started = time.perf_counter()
                        try:
                            solved_key = await asyncio.to_thread(challenge.solve_challenge, text)
                        except Exception as solve_err:
                            self._emit("WARNING", f"{log_label} PoW 求解失败: {solve_err}")
                            return None, "PoW 质询解题失败"
                        solve_elapsed = time.perf_counter() - solve_started

                        extra_cookies["KEY"] = solved_key
                        self.pow_cookies["KEY"] = solved_key
                        ht_val = challenge.extract_set_cookie(set_cookies, "htjschalref")
                        if ht_val:
                            extra_cookies["htjschalref"] = ht_val
                            self.pow_cookies["htjschalref"] = ht_val
                        self._challenge_generation += 1
                        self.challenge_solve_count += 1
                        self.challenge_solve_seconds += solve_elapsed
                        solved_here = True
                        self._emit(
                            "INFO",
                            f"{log_label} PoW 质询求解完成，耗时 {solve_elapsed * 1000:.1f}ms",
                        )

                challenge_solves += 1
                if solved_here:
                    try:
                        await until_stopped(asyncio.sleep(0.5 + random.uniform(0, 0.5)), self.cancel_event)
                    except StopRequested:
                        return None, "操作已取消"
                continue

            if verb == "GET" and (status_code == 429 or 500 <= status_code < 600) and retries_left:
                retry_num = initial_retries - retries_left
                base = self.retry_delay_base if retry_delay_base is None else float(retry_delay_base)
                cap = self.retry_delay_max if retry_delay_max is None else float(retry_delay_max)
                delay = min(base * (2 ** retry_num), cap)
                self._emit("WARNING", f"{log_label}返回 HTTP {status_code}，{delay:.1f}s 后重试...")
                try:
                    await until_stopped(asyncio.sleep(delay), self.cancel_event)
                except StopRequested:
                    return None, "操作已取消"
                retries_left -= 1
                continue

            if challenge_solves > 0:
                self._emit(
                    "INFO",
                    f"{log_label}通过 PoW 质询 x{challenge_solves}（本任务累计求解 "
                    f"{self.challenge_solve_count} 次，复用 {self.challenge_reuse_count} 次）",
                )

            return (
                PowFetchResult(
                    text=text,
                    final_url=final_url,
                    status_code=status_code,
                    challenge_solves=challenge_solves,
                    retried=retries_left < initial_retries,
                ),
                None,
            )

        return None, "未知请求错误"
