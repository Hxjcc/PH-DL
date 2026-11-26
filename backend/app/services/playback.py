"""Resolve and relay playback sources for history entries."""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections import OrderedDict
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlsplit

import aiohttp
import anyio
from fastapi import HTTPException
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from ..config import RuntimeSettings
from ..parsers.http_session import TaskHttpContext
from ..parsers.video import MediaSource, extract_viewkey_from_url, fetch_single_video_detail, is_video_url
from ..parsers.xhamster import is_xhamster_host, is_xhamster_url
from .downloader import DOWNLOAD_HEADERS

_PH_SITE = ("pornhub.com",)
_MEDIA = ("pornhub.com", "phncdn.com", "phprcdn.com", "xhcdn.com")
_REDIRECTS = {301, 302, 303, 307, 308}
_REQUEST_RANGE = re.compile(r"bytes=(?:([0-9]{1,18})-([0-9]{0,18})|-([0-9]{1,18}))")
_REPLY_RANGE = re.compile(r"bytes ([0-9]{1,18})-([0-9]{1,18})/([0-9]{1,18})")
_MP4_TYPES = {"video/mp4", "application/mp4", "application/octet-stream", "binary/octet-stream"}
NO_CACHE = {"Cache-Control": "private, no-store"}


def _allowed(url: str, domains: tuple[str, ...], *, allow_xhamster: bool = False) -> bool:
    if "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        host_ok = any(host == domain or host.endswith("." + domain) for domain in domains) or (
            allow_xhamster and is_xhamster_host(host)
        )
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
            and host_ok
        )
    except ValueError:
        return False


def _ph_site_url(url: str) -> bool:
    return _allowed(url, _PH_SITE)


def _xh_site_url(url: str) -> bool:
    return _allowed(url, (), allow_xhamster=True)


def _site_url(url: str) -> bool:
    return _allowed(url, _PH_SITE, allow_xhamster=True)


def _media_url(url: str) -> bool:
    return _allowed(url, _MEDIA, allow_xhamster=True)


def _playback_page(record: dict, viewkey: str) -> str:
    recorded = str(record.get("video_page_url") or "").strip()
    if _site_url(recorded) and is_video_url(recorded) and extract_viewkey_from_url(recorded) == viewkey:
        return recorded
    if str(record.get("site") or "").strip().lower() == "xhamster":
        return f"https://xhamster.com/videos/{viewkey}"
    return f"https://www.pornhub.com/view_video.php?viewkey={viewkey}"


def _parse_range(value: Optional[str]) -> Optional[re.Match[str]]:
    if value is None:
        return None
    match = _REQUEST_RANGE.fullmatch(value)
    if (
        not match
        or (match[1] and match[2] and int(match[1]) > int(match[2]))
        or (match[3] and int(match[3]) == 0)
    ):
        raise HTTPException(400, "播放范围无效")
    return match


def _valid_content_range(value: str, length: Optional[str], requested: Optional[re.Match[str]]) -> bool:
    match = _REPLY_RANGE.fullmatch(value)
    if not match or requested is None:
        return False
    start, end, total = map(int, match.groups())
    if not start <= end < total or (length is not None and (not length.isdigit() or int(length) != end - start + 1)):
        return False
    if requested[3]:
        return start == max(0, total - int(requested[3])) and end == total - 1
    return start == int(requested[1]) and (not requested[2] or end <= int(requested[2]))


@dataclass(frozen=True)
class _Playback:
    viewkey: str
    sources: tuple[MediaSource, ...]
    referer: str
    cookie: str
    proxy_url: Optional[str]
    expires_at: float


class _RelayResponse(StreamingResponse):
    def __init__(
        self, upstream: aiohttp.ClientResponse, resources: AsyncExitStack,
        headers: dict[str, str], media_type: str,
    ):
        super().__init__(
            upstream.content.iter_chunked(256 * 1024),
            status_code=upstream.status,
            media_type=media_type,
            headers=headers,
        )
        self._resources = resources

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A seek or a closed player can drop the connection mid-response.
            with anyio.CancelScope(shield=True):
                await self._resources.aclose()


class PlaybackService:
    def __init__(self) -> None:
        self._sessions: OrderedDict[str, _Playback] = OrderedDict()
        self._resolve_slots = asyncio.Semaphore(4)
        self._stream_slots = asyncio.Semaphore(12)
        self._http_client: Optional[aiohttp.ClientSession] = None
        self._client_lock = asyncio.Lock()

    async def _get_http_client(self) -> aiohttp.ClientSession:
        if self._http_client is not None and not self._http_client.closed:
            return self._http_client
        async with self._client_lock:
            if self._http_client is None or self._http_client.closed:
                connector = aiohttp.TCPConnector(limit=32, limit_per_host=16)
                self._http_client = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=25),
                    connector=connector,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    auto_decompress=False,
                )
            return self._http_client

    async def aclose(self) -> None:
        async with self._client_lock:
            if self._http_client is not None and not self._http_client.closed:
                await self._http_client.close()
            self._http_client = None

    async def resolve(self, record: dict, settings: RuntimeSettings) -> dict:
        viewkey = str(record["viewkey"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", viewkey):
            raise HTTPException(422, "这条记录没有可用的视频编号")
        page = _playback_page(record, viewkey)
        is_xh = str(record.get("site") or "").strip().lower() == "xhamster" or is_xhamster_url(page)
        try:
            # Resolving a clip must not occupy a download worker or retry indefinitely.
            async with asyncio.timeout(35), self._resolve_slots, TaskHttpContext(
                cookie_str=settings.xh_cookie if is_xh else settings.ph_cookie,
                proxy_url=settings.normalized_proxy,
                concurrency=1,
                timeout=min(settings.request_timeout, 12),
                max_retries=1,
                allow_url=_media_url,
                allow_cookie_url=_xh_site_url if is_xh else _ph_site_url,
            ) as ctx:
                detail = await fetch_single_video_detail(
                    ctx, page, settings.selectors, timeout=min(settings.request_timeout, 12),
                    skip_dom=True,
                    include_mp4_sources=True,
                    xh_preferred_codec=settings.xh_preferred_codec,
                )
                cookie = ctx.cookie_string()
        except TimeoutError as err:
            raise HTTPException(504, "获取播放地址超时，请重试") from err

        sources: dict[str, MediaSource] = {}
        for source in detail.mp4_sources:
            if _media_url(source.url):
                sources.setdefault(source.quality, source)
        if not sources:
            reason = detail.error or "源站未返回可播放的地址"
            raise HTTPException(502, reason)

        # Relay URLs are scoped to this authenticated resource; no client-supplied
        # destination or stored upstream cookie is ever sent back to the browser.
        now = time.monotonic()
        for key in [key for key, entry in self._sessions.items() if entry.expires_at <= now]:
            self._sessions.pop(key)
        ticket = secrets.token_urlsafe(18)
        entry = _Playback(
            viewkey=viewkey, sources=tuple(sources.values()),
            referer=detail.referer_url if _site_url(detail.referer_url) else page,
            cookie=cookie, proxy_url=settings.normalized_proxy, expires_at=now + 30 * 60,
        )
        self._sessions[ticket] = entry
        while len(self._sessions) > 128:
            self._sessions.popitem(last=False)
        return {
            "format": "mp4",
            "sources": [
                {
                    "quality": source.quality,
                    "relay_url": f"/api/videos/{viewkey}/playback/{ticket}/{index}",
                }
                for index, source in enumerate(entry.sources)
            ],
        }

    def _session(self, viewkey: str, ticket: str) -> _Playback:
        entry = self._sessions.get(ticket)
        if not entry or entry.expires_at <= time.monotonic() or entry.viewkey != viewkey:
            raise HTTPException(410, "播放地址已失效，请重新获取")
        return entry

    async def stream(self, viewkey: str, ticket: str, index: int, byte_range: Optional[str]) -> Response:
        entry = self._session(viewkey, ticket)
        if not 0 <= index < len(entry.sources):
            raise HTTPException(404, "没有找到该画质")
        return await self._relay(entry, entry.sources[index].url, byte_range)

    async def _relay(
        self, entry: _Playback, url: str, byte_range: Optional[str],
    ) -> Response:
        requested = _parse_range(byte_range)
        async with AsyncExitStack() as setup:
            try:
                async with asyncio.timeout(6):
                    await setup.enter_async_context(self._stream_slots)
            except TimeoutError as err:
                raise HTTPException(503, "播放请求过多，请稍后重试") from err
            client = await self._get_http_client()
            upstream = None
            try:
                for _ in range(5):
                    if not _media_url(url):
                        raise HTTPException(502, "源站返回了不受支持的播放地址")
                    headers = {**DOWNLOAD_HEADERS, "Referer": entry.referer}
                    if byte_range:
                        headers["Range"] = byte_range
                    if entry.cookie and _site_url(url):
                        headers["Cookie"] = entry.cookie
                    candidate = await client.get(
                        url, headers=headers, proxy=entry.proxy_url,
                        allow_redirects=False,
                    )
                    if candidate.status not in _REDIRECTS:
                        upstream = candidate
                        setup.push_async_callback(upstream.release)
                        break
                    try:
                        url = urljoin(url, candidate.headers["Location"])
                    except (KeyError, ValueError) as err:
                        candidate.close()
                        raise HTTPException(502, "源站的播放跳转无效") from err
                    await candidate.release()
            except (aiohttp.ClientError, TimeoutError) as err:
                raise HTTPException(502, "无法连接视频源，请重新获取播放地址") from err

            if upstream is None:
                raise HTTPException(502, "视频源跳转次数过多")
            if upstream.status == 416:
                reply_range = upstream.headers.get("Content-Range", "")
                headers = {"Content-Range": reply_range} if re.fullmatch(r"bytes \*/[0-9]{1,18}", reply_range) else {}
                return Response(status_code=416, headers={**NO_CACHE, **headers})
            if upstream.status in {401, 403, 404, 410}:
                raise HTTPException(410, "视频源拒绝了此地址，请重新获取")
            if upstream.status not in {200, 206}:
                raise HTTPException(502, "视频源暂时不可用")
            content_type = upstream.headers.get("Content-Type", "").partition(";")[0].strip().lower()
            encoding = upstream.headers.get("Content-Encoding", "identity").strip().lower()
            length = upstream.headers.get("Content-Length")
            headers = {
                **NO_CACHE,
                "X-Accel-Buffering": "no",
                "Cross-Origin-Resource-Policy": "same-origin",
            }
            if content_type not in _MP4_TYPES or encoding not in {"", "identity"}:
                raise HTTPException(502, "视频源没有返回可播放的媒体数据")
            if upstream.status == 206 and not _valid_content_range(
                upstream.headers.get("Content-Range", ""), length, requested,
            ):
                raise HTTPException(502, "视频源返回了无效的播放范围")

            headers.update({
                name: upstream.headers[name]
                for name in ("Accept-Ranges", "Content-Length", "Content-Range")
                if name in upstream.headers
            })
            if upstream.status == 206:
                headers.setdefault("Accept-Ranges", "bytes")
            return _RelayResponse(upstream, setup.pop_all(), headers, media_type="video/mp4")
