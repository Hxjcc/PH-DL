"""Task-scoped curl_cffi HTTP context with pinned Chrome fingerprint & shared PoW cookie pool."""

from __future__ import annotations

import asyncio
import random
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

warnings.filterwarnings(
    "ignore",
    message=r"(?s).*Proactor event loop does not implement add_reader.*",
)

from curl_cffi.requests import AsyncSession as CffiSession
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

try:
    from curl_cffi import CurlHttpVersion
except ImportError:  # pragma: no cover - compatibility with older curl_cffi
    try:
        from curl_cffi.const import CurlHttpVersion
    except ImportError:  # pragma: no cover
        CurlHttpVersion = None  # type: ignore[assignment]

try:
    from curl_cffi.requests import BrowserType

    _AVAILABLE_BROWSERS = {member.value for member in BrowserType}
except Exception:  # pragma: no cover
    _AVAILABLE_BROWSERS = set()

from ..cancel import StopRequested, until_stopped
from . import challenge
from .urls import is_cookie_origin, is_media_url

# Only Chrome fingerprints: Safari fingerprints trigger a stripped-down PornHub HTML layout
# that omits videos and breaks primary container selectors. Prefer modern desktop Chrome
# fingerprints supported by the installed curl_cffi version.
_PREFERRED_CHROME_FINGERPRINTS: Tuple[str, ...] = (
    "chrome136",
    "chrome131",
    "chrome124",
    "chrome123",
    "chrome120",
    "chrome119",
)
CHROME_FINGERPRINTS: Tuple[str, ...] = tuple(
    fp for fp in _PREFERRED_CHROME_FINGERPRINTS
    if not _AVAILABLE_BROWSERS or fp in _AVAILABLE_BROWSERS
) or ("chrome124", "chrome123", "chrome120")

# Force HTTP/1.1 for PornHub so concurrent requests to www.pornhub.com do not multiplex
# onto a single HTTP/2 TCP socket where one WAF reset or timeout kills every in-flight stream.
# xHamster negotiates HTTP/2 over TLS (with automatic HTTP/1.1 fallback) to match modern
# Chrome fingerprints and reduce handshake latency across paginated scans.
_HTTP_V1_1 = getattr(CurlHttpVersion, "V1_1", None)
_HTTP_V2_TLS = getattr(CurlHttpVersion, "V2TLS", None) or getattr(CurlHttpVersion, "V2_0", None)
_HTTP_VERSION_AUTO = object()
_H2_ERROR_CODES = frozenset({16, 92})


def _is_http2_protocol_error(err: BaseException) -> bool:
    code = getattr(err, "code", None)
    if isinstance(code, int) and code in _H2_ERROR_CODES:
        return True
    msg = str(err).lower()
    return "http/2" in msg or "http2" in msg or "(16)" in msg or "(92)" in msg

DEFAULT_HEADERS: Dict[str, str] = {
    "Referer": "https://www.pornhub.com/",
    "Accept-Language": "en-US,en;q=0.9",
}

LogCallback = Callable[[str, str], None]
_MAX_PAGE_BYTES = 8 * 1024 * 1024
_MAX_SESSION_COOKIES = 128


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


def parse_set_cookie_headers(set_cookie_headers: List[str]) -> Dict[str, Optional[str]]:
    """Parse response Set-Cookie headers into a short-lived task cookie jar."""
    result: Dict[str, Optional[str]] = {}
    now = datetime.now(timezone.utc)
    for header in set_cookie_headers:
        cookie = SimpleCookie()
        try:
            cookie.load(header)
        except Exception:
            continue
        for name, morsel in cookie.items():
            max_age = str(morsel["max-age"] or "").strip()
            expired = False
            try:
                expired = int(max_age) <= 0
            except ValueError:
                try:
                    expiry = parsedate_to_datetime(morsel["expires"])
                    expired = (expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)) <= now
                except (TypeError, ValueError, OverflowError):
                    pass
            result[name] = None if expired else morsel.value
    return result


class TaskHttpContext:
    """Shared HTTP context for a single job/operation.

    - Pins ONE Chrome TLS fingerprint across list pages, detail pages, get_media, and link refreshes.
    - Shares server-issued session cookies and PoW cookies across the entire job.
    - Caps the lazily created connection pool; defaults to HTTP/2 (V2TLS) for xHamster and HTTP/1.1 for PornHub.
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
        http_version: Any = _HTTP_VERSION_AUTO,
        min_request_interval: Optional[float] = None,
        cancel_event: Optional[asyncio.Event] = None,
        on_log: Optional[LogCallback] = None,
        site: Optional[str] = None,
        allow_url: Optional[Callable[[str], bool]] = None,
        allow_cookie_url: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.base_cookies: Dict[str, str] = parse_cookie_string(cookie_str)
        self.proxy_url = (proxy_url or "").strip() or None
        self.pool_size = max(4, min(int(concurrency) + 2, 16))
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.retry_delay_base = float(retry_delay_base)
        self.retry_delay_max = float(retry_delay_max)
        self.impersonate: str = impersonate or random.choice(CHROME_FINGERPRINTS)
        norm_site = (site or "").strip().lower()
        if http_version is _HTTP_VERSION_AUTO:
            self.http_version = _HTTP_V2_TLS if norm_site in {"xhamster", "xh"} else _HTTP_V1_1
        else:
            self.http_version = http_version
        if min_request_interval is None:
            self.min_request_interval = 0.15 if (self.retry_delay_base > 0 and int(concurrency) > 1) else 0.0
        else:
            self.min_request_interval = max(0.0, float(min_request_interval))
        self.session_cookies: Dict[str, str] = {}
        self.pow_cookies: Dict[str, str] = {}
        self.deleted_cookies: set[str] = set()
        self.cancel_event = cancel_event or asyncio.Event()
        self.on_log = on_log
        self.site = site
        self._allow_url = allow_url or ((lambda url: is_media_url(url, site)) if site is not None else None)
        self._allow_cookie_url = allow_cookie_url or ((lambda url: is_cookie_origin(url, site)) if site is not None else None)
        self._entered = False
        self._session: Optional[CffiSession] = None
        self._session_lock = asyncio.Lock()
        self._session_requests = 0
        self._cookie_lock = asyncio.Lock()
        self._challenge_lock = asyncio.Lock()
        self._pacing_lock = asyncio.Lock()
        self._cooldown_lock = asyncio.Lock()
        self._next_dispatch_at = 0.0
        self._cooldown_until = 0.0
        self._pow_ready_at = 0.0
        self._consecutive_403 = 0
        self._consecutive_conn_errors = 0
        self._challenge_generation = 0
        self.challenge_solve_count = 0
        self.challenge_reuse_count = 0
        self.challenge_solve_seconds = 0.0

    def _session_kwargs(self, max_clients: int) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"proxy": self.proxy_url, "max_clients": max_clients}
        if self.http_version is not None:
            kwargs["http_version"] = self.http_version
        return kwargs

    async def __aenter__(self) -> "TaskHttpContext":
        self._entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        async with self._session_lock:
            self._entered = False
            await self._close_session(exc_type, exc, tb)

    async def _close_session(self, exc_type=None, exc=None, tb=None) -> None:
        if self._session is not None:
            session, self._session = self._session, None
            closing = asyncio.create_task(session.__aexit__(exc_type, exc, tb))
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                await asyncio.gather(closing, return_exceptions=True)
                raise

    async def release_idle_session(self) -> None:
        async with self._session_lock:
            if self._session_requests == 0:
                await self._close_session()

    def _emit(self, level: str, msg: str) -> None:
        if self.on_log:
            self.on_log(level, msg)

    async def _wait_for_pacing_and_cooldown(self) -> None:
        """Honor task-wide rate-limit cooldowns and smooth concurrent request bursts."""
        while True:
            remaining = self._cooldown_until - time.monotonic()
            if remaining <= 0:
                break
            stagger = random.uniform(0.05, 0.45) if self.retry_delay_base > 0 else 0.0
            await until_stopped(asyncio.sleep(remaining + stagger), self.cancel_event)

        if self.min_request_interval > 0:
            async with self._pacing_lock:
                now = time.monotonic()
                wait_s = max(0.0, self._next_dispatch_at - now)
                step = self.min_request_interval * random.uniform(0.75, 1.25)
                self._next_dispatch_at = max(now, self._next_dispatch_at) + step
            if wait_s > 0:
                await until_stopped(asyncio.sleep(wait_s), self.cancel_event)

    async def _extend_cooldown(
        self,
        *,
        kind: str,
        retry_num: int,
        base: float,
        cap: float,
    ) -> Tuple[float, int]:
        """Extend the shared task cooldown window to prevent synchronized retry storms."""
        async with self._cooldown_lock:
            now = time.monotonic()
            if kind == "403":
                self._consecutive_403 += 1
                streak = self._consecutive_403
                if base <= 0:
                    delay = 0.0
                else:
                    level = max(retry_num, min(streak - 1, 3))
                    step = max(base * (2 ** (level + 1)), 4.0)
                    delay = min(step, max(cap, 30.0))
                    delay += random.uniform(0.0, delay * 0.25)
            else:
                if kind == "conn":
                    self._consecutive_conn_errors += 1
                streak = self._consecutive_conn_errors
                if base <= 0:
                    delay = 0.0
                else:
                    delay = min(base * (2 ** retry_num), cap)
                    if kind == "conn" and streak >= 3:
                        delay = max(delay, min(max(base * 2.0, 4.0), max(cap, 15.0)))
                    delay += random.uniform(0.0, delay * 0.25)

            target = now + delay
            if target > self._cooldown_until:
                self._cooldown_until = target
            wait_s = max(0.0, self._cooldown_until - now)
            return wait_s, streak

    def _mark_request_healthy(self) -> None:
        self._consecutive_403 = 0
        self._consecutive_conn_errors = 0

    @staticmethod
    def _format_error(err: BaseException) -> str:
        raw = str(err).strip().splitlines()[0].strip() if str(err).strip() else ""
        if raw:
            return f"{type(err).__name__}: {raw[:120]}"
        return type(err).__name__

    async def _absorb_set_cookies(self, set_cookie_headers: List[str]) -> None:
        parsed = parse_set_cookie_headers(set_cookie_headers)
        if not parsed:
            return
        async with self._cookie_lock:
            for name, value in parsed.items():
                if value is None:
                    self.session_cookies.pop(name, None)
                    self.pow_cookies.pop(name, None)
                    self.deleted_cookies.add(name)
                    continue
                if len(name) + len(value) > 4096:
                    continue
                self.deleted_cookies.discard(name)
                self.session_cookies.pop(name, None)
                self.session_cookies[name] = value
            while len(self.session_cookies) > _MAX_SESSION_COOKIES:
                self.session_cookies.pop(next(iter(self.session_cookies)))

    def merged_cookies(
        self, extra: Optional[Dict[str, str]] = None, *, include_pow: bool = True,
    ) -> Dict[str, str]:
        merged = {name: value for name, value in self.base_cookies.items() if name not in self.deleted_cookies}
        merged.update(self.session_cookies)
        if include_pow:
            merged.update(self.pow_cookies)
        if extra:
            merged.update(extra)
        return merged

    def cookie_string(self, include_pow: bool = True) -> str:
        cookies = self.merged_cookies(include_pow=include_pow)
        return "; ".join(f"{name}={value}" for name, value in cookies.items())

    async def _raw_request(
        self,
        method: str,
        url: str,
        cookies: Dict[str, str],
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        data: Optional[Any] = None,
        http_version_override: Any = _HTTP_VERSION_AUTO,
    ) -> Tuple[str, str, List[str], int]:
        merged_headers = dict(DEFAULT_HEADERS)
        if headers:
            merged_headers.update(headers)
        req_timeout = float(timeout if timeout is not None else self.timeout)
        verb = method.upper()
        effective_http_version = (
            self.http_version if http_version_override is _HTTP_VERSION_AUTO else http_version_override
        )

        async def _do(sess: CffiSession) -> Tuple[str, str, List[str], int]:
            current_url = url
            if self._allow_url and url.startswith("http://"):
                parsed = urlsplit(url)
                if parsed.port != 80:
                    current_url = f"https://{url[7:]}"
                else:
                    current_url = parsed._replace(scheme="https", netloc=parsed.netloc.removesuffix(":80")).geturl()
            hop_cookies = dict(cookies)
            for _ in range(6):
                if self._allow_url and not self._allow_url(current_url):
                    raise ValueError("请求地址不在允许范围内")
                can_use_cookies = not self._allow_cookie_url or self._allow_cookie_url(current_url)
                if verb == "POST" and not can_use_cookies:
                    raise ValueError("提交地址不在允许范围内")
                request_headers = merged_headers if can_use_cookies else {
                    k: v for k, v in merged_headers.items() if k.lower() not in {"cookie", "authorization"}
                }
                request_kwargs = {
                    "cookies": (hop_cookies or None) if can_use_cookies else None,
                    "headers": request_headers,
                    "timeout": req_timeout,
                    "allow_redirects": self._allow_url is None,
                    "impersonate": self.impersonate,
                    "discard_cookies": True,
                }
                body = bytearray()
                overflow = False

                def receive(chunk: bytes) -> int:
                    nonlocal overflow
                    if len(body) + len(chunk) > _MAX_PAGE_BYTES:
                        overflow = True
                        return CURL_WRITEFUNC_ERROR
                    body.extend(chunk)
                    return len(chunk)

                if self._allow_url:
                    request_kwargs["content_callback"] = receive
                if effective_http_version is not None:
                    request_kwargs["http_version"] = effective_http_version
                try:
                    if verb == "POST":
                        resp = await sess.post(current_url, data=data, **request_kwargs)
                    else:
                        resp = await sess.get(current_url, **request_kwargs)
                except Exception as err:
                    if overflow:
                        raise ValueError("页面响应体积超出限制") from err
                    raise
                set_cookies = extract_set_cookie_headers(resp.headers) if can_use_cookies else []
                await self._absorb_set_cookies(set_cookies)
                status = int(resp.status_code)
                location = resp.headers.get("Location")
                if self._allow_url and verb == "GET" and status in {301, 302, 303, 307, 308} and location:
                    hop_cookies.update(self.session_cookies)
                    for name in self.deleted_cookies:
                        hop_cookies.pop(name, None)
                    current_url = urljoin(str(resp.url), location)
                    continue
                if body:
                    resp.content = bytes(body)
                return resp.text, str(resp.url), set_cookies, status
            raise ValueError("请求重定向次数过多")

        session = None
        async with self._session_lock:
            if self._entered:
                if self._session is None:
                    session = CffiSession(**self._session_kwargs(self.pool_size))
                    await session.__aenter__()
                    self._session = session
                session = self._session
                self._session_requests += 1
        if session is not None:
            try:
                return await _do(session)
            finally:
                self._session_requests -= 1
        async with CffiSession(**self._session_kwargs(2)) as temp_session:
            return await _do(temp_session)

    async def _raw_get(
        self,
        url: str,
        cookies: Dict[str, str],
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        http_version_override: Any = _HTTP_VERSION_AUTO,
    ) -> Tuple[str, str, List[str], int]:
        return await self._raw_request(
            "GET",
            url,
            cookies,
            headers=headers,
            timeout=timeout,
            http_version_override=http_version_override,
        )

    async def _raw_post(
        self,
        url: str,
        cookies: Dict[str, str],
        data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        http_version_override: Any = _HTTP_VERSION_AUTO,
    ) -> Tuple[str, str, List[str], int]:
        return await self._raw_request(
            "POST",
            url,
            cookies,
            headers=headers,
            timeout=timeout,
            data=data,
            http_version_override=http_version_override,
        )

    async def fetch_with_pow(
        self,
        url: str,
        *,
        method: str = "GET",
        data: Optional[Any] = None,
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
        verb = method.upper()
        attempt_http_version: Any = _HTTP_VERSION_AUTO

        while retries_left >= 0:
            if self.cancel_event.is_set():
                return None, "操作已取消"

            try:
                await self._wait_for_pacing_and_cooldown()
            except StopRequested:
                return None, "操作已取消"

            generation_before_request = self._challenge_generation
            merged_cookies = self.merged_cookies()

            try:
                raw_kwargs: Dict[str, Any] = {"headers": headers, "timeout": timeout}
                if attempt_http_version is not _HTTP_VERSION_AUTO:
                    raw_kwargs["http_version_override"] = attempt_http_version
                if verb == "POST":
                    request = self._raw_post(
                        url,
                        merged_cookies,
                        data,
                        **raw_kwargs,
                    )
                else:
                    request = self._raw_get(
                        url,
                        merged_cookies,
                        **raw_kwargs,
                    )
                text, final_url, set_cookies, status_code = await until_stopped(request, self.cancel_event)
            except StopRequested:
                return None, "操作已取消"
            except (ConnectionError, TimeoutError, OSError, asyncio.TimeoutError) as err:
                if self.cancel_event.is_set():
                    return None, "操作已取消"
                err_summary = self._format_error(err)
                if retries_left == 0:
                    self._emit("WARNING", f"{log_label}多次重试仍失败 ({err_summary}): {url}")
                    return None, f"网络错误 ({err_summary})"
                if (
                    _HTTP_V1_1 is not None
                    and self.http_version != _HTTP_V1_1
                    and attempt_http_version is _HTTP_VERSION_AUTO
                    and _is_http2_protocol_error(err)
                ):
                    attempt_http_version = _HTTP_V1_1

                retry_num = initial_retries - retries_left
                base = self.retry_delay_base if retry_delay_base is None else float(retry_delay_base)
                cap = self.retry_delay_max if retry_delay_max is None else float(retry_delay_max)
                delay, _ = await self._extend_cooldown(
                    kind="conn",
                    retry_num=retry_num,
                    base=base,
                    cap=cap,
                )
                stagger = random.uniform(0.0, min(1.0, base)) if base > 0 else 0.0
                wait_total = delay + stagger
                self._emit("WARNING", f"{log_label}发生可重试错误 ({err_summary})，{wait_total:.1f}s 后重试...")
                try:
                    if wait_total > 0:
                        await until_stopped(asyncio.sleep(wait_total), self.cancel_event)
                except StopRequested:
                    return None, "操作已取消"
                retries_left -= 1
                continue
            except Exception as err:
                self._emit("ERROR", f"{log_label}发生异常 ({self._format_error(err)})")
                return None, f"请求异常 ({type(err).__name__})"

            if challenge.is_challenge(text):
                if challenge_solves >= challenge.MAX_CHALLENGE_SOLVES:
                    self._emit("WARNING", f"{log_label} PoW 质询连续 {challenge_solves} 次未通过")
                    return None, "PoW 质询未通过"
                solved_here = False

                def newer_solution() -> bool:
                    return self._challenge_generation > generation_before_request and "KEY" in self.pow_cookies

                if not newer_solution():
                    async with self._challenge_lock:
                        if not newer_solution():
                            solve_started = time.perf_counter()
                            try:
                                solved_key = await asyncio.to_thread(challenge.solve_challenge, text)
                            except Exception as solve_err:
                                self._emit("WARNING", f"{log_label} PoW 求解失败: {solve_err}")
                                return None, "PoW 质询解题失败"
                            solve_elapsed = time.perf_counter() - solve_started

                            self.deleted_cookies.discard("KEY")
                            self.pow_cookies["KEY"] = solved_key
                            ht_val = parse_set_cookie_headers(set_cookies).get("htjschalref")
                            if ht_val:
                                self.deleted_cookies.discard("htjschalref")
                                self.pow_cookies["htjschalref"] = ht_val
                            self._challenge_generation += 1
                            self.challenge_solve_count += 1
                            self.challenge_solve_seconds += solve_elapsed
                            solved_here = True
                            if self.retry_delay_base > 0:
                                self._pow_ready_at = time.monotonic() + 0.6 + random.uniform(0.0, 0.4)
                            self._emit("INFO", f"{log_label} PoW 质询求解完成，耗时 {solve_elapsed * 1000:.1f}ms")
                if not solved_here:
                    self.challenge_reuse_count += 1
                    self._emit("INFO", f"{log_label} 复用并发请求刚解开的 PoW Cookie")

                challenge_solves += 1
                # Ensure BOTH the solver and reusing workers wait until the PoW cookie
                # settles, with random stagger so waiters don't slam the edge at once.
                pow_wait = max(0.0, self._pow_ready_at - time.monotonic())
                if not solved_here and self.retry_delay_base > 0:
                    pow_wait += random.uniform(0.15, 0.85)
                if pow_wait > 0:
                    try:
                        await until_stopped(asyncio.sleep(pow_wait), self.cancel_event)
                    except StopRequested:
                        return None, "操作已取消"
                continue

            if verb == "GET" and (status_code in {403, 429} or 500 <= status_code < 600) and retries_left:
                retry_num = initial_retries - retries_left
                base = self.retry_delay_base if retry_delay_base is None else float(retry_delay_base)
                cap = self.retry_delay_max if retry_delay_max is None else float(retry_delay_max)
                kind = "403" if status_code == 403 else ("429" if status_code == 429 else "5xx")
                delay, streak = await self._extend_cooldown(
                    kind=kind,
                    retry_num=retry_num,
                    base=base,
                    cap=cap,
                )
                if status_code == 403 and streak >= 2:
                    async with self._cookie_lock:
                        self.pow_cookies.clear()
                stagger = random.uniform(0.0, min(1.0, base)) if base > 0 else 0.0
                wait_total = delay + stagger
                hint = "（触发频控冷却）" if status_code == 403 else ""
                self._emit("WARNING", f"{log_label}返回 HTTP {status_code}{hint}，{wait_total:.1f}s 后重试...")
                try:
                    if wait_total > 0:
                        await until_stopped(asyncio.sleep(wait_total), self.cancel_event)
                except StopRequested:
                    return None, "操作已取消"
                retries_left -= 1
                continue

            if status_code < 400:
                self._mark_request_healthy()

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
