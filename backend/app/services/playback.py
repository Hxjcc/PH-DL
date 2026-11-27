"""Resolve and relay playback sources for history entries."""

from __future__ import annotations

import asyncio
import gzip
import io
import re
import secrets
import time
from collections import OrderedDict
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlsplit

import aiohttp
import anyio
from fastapi import HTTPException
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from ..config import RuntimeSettings
from ..parsers.http_session import TaskHttpContext
from ..parsers.video import MediaSource, fetch_single_video_detail, safe_video_page_url
from ..parsers.urls import is_cookie_origin, is_media_url, is_site_url
from ..parsers.xhamster import is_xhamster_url
from .downloader import DOWNLOAD_HEADERS
from .http_proxy import create_aiohttp_connector, is_socks_proxy

_REDIRECTS = {301, 302, 303, 307, 308}
_REQUEST_RANGE = re.compile(r"bytes=(?:([0-9]{1,18})-([0-9]{0,18})|-([0-9]{1,18}))")
_REPLY_RANGE = re.compile(r"bytes ([0-9]{1,18})-([0-9]{1,18})/([0-9]{1,18})")
_MP4_TYPES = {"video/mp4", "application/mp4", "application/octet-stream", "binary/octet-stream"}
_HLS_SEGMENT_TYPES = _MP4_TYPES | {
    "video/mp2t",
    "audio/mp4",
    "audio/aac",
    "video/iso.segment",
    "video/webm",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
}
_HLS_URI_ATTR_RE = re.compile(r'URI="([^"]+)"')
_MAX_PLAYLIST_BYTES = 4 * 1024 * 1024
_MAX_HLS_ASSETS = 6_000
_MAX_PLAYBACK_SESSIONS = 32
_MAX_SOCKS_CLIENTS = 2
NO_CACHE = {"Cache-Control": "private, no-store"}


def _media_url(url: str) -> bool:
    return is_media_url(url, "pornhub") or is_media_url(url, "xhamster")


def _playback_page(record: dict, viewkey: str) -> str:
    recorded = str(record.get("video_page_url") or "").strip()
    site = "xhamster" if str(record.get("site") or "").strip().lower() == "xhamster" or is_xhamster_url(recorded) else "pornhub"
    return safe_video_page_url(recorded, site, viewkey)


def _playlist_response(body: str) -> Response:
    return Response(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers={**NO_CACHE, "X-Accel-Buffering": "no", "Cross-Origin-Resource-Policy": "same-origin"},
    )


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


@dataclass
class _Playback:
    viewkey: str
    sources: tuple[MediaSource, ...]
    referer: str
    cookie: str
    proxy_url: Optional[str]
    expires_at: float
    format: str = "mp4"
    site: str = "pornhub"
    hls_asset_bytes: int = 0
    hls_asset_urls: dict[int, list[str]] = field(default_factory=dict)
    hls_asset_index: dict[int, dict[str, int]] = field(default_factory=dict)

    def register_hls_asset(self, index: int, url: str) -> int:
        idx_map = self.hls_asset_index.setdefault(index, {})
        existing = idx_map.get(url)
        if existing is not None:
            return existing
        urls = self.hls_asset_urls.setdefault(index, [])
        size = len(url.encode("utf-8"))
        if len(urls) >= _MAX_HLS_ASSETS or self.hls_asset_bytes + size > _MAX_PLAYLIST_BYTES:
            raise HTTPException(502, "播放列表分片数量超出限制")
        seg_id = len(urls)
        urls.append(url)
        idx_map[url] = seg_id
        self.hls_asset_bytes += size
        return seg_id


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
        self._socks_http_clients: OrderedDict[str, aiohttp.ClientSession] = OrderedDict()
        self._socks_client_users: dict[str, int] = {}
        self._client_lock = asyncio.Lock()

    async def _trim_socks_clients(self) -> None:
        idle = [key for key in self._socks_http_clients if key not in self._socks_client_users]
        for key in idle[:-_MAX_SOCKS_CLIENTS]:
            await self._socks_http_clients.pop(key).close()

    @asynccontextmanager
    async def _get_http_client(
        self, proxy_url: Optional[str] = None,
    ):
        cleaned_proxy = (proxy_url or "").strip() or None
        if cleaned_proxy and is_socks_proxy(cleaned_proxy):
            async with self._client_lock:
                existing = self._socks_http_clients.get(cleaned_proxy)
                if existing is None or existing.closed:
                    try:
                        connector, _ = create_aiohttp_connector(
                            cleaned_proxy,
                            limit=32,
                            limit_per_host=16,
                        )
                    except ValueError as err:
                        raise HTTPException(502, "代理地址配置无效，无法连接视频源") from err
                    existing = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=25),
                        connector=connector,
                        cookie_jar=aiohttp.DummyCookieJar(),
                        auto_decompress=False,
                    )
                    self._socks_http_clients[cleaned_proxy] = existing
                self._socks_http_clients.move_to_end(cleaned_proxy)
                self._socks_client_users[cleaned_proxy] = self._socks_client_users.get(cleaned_proxy, 0) + 1
            try:
                yield existing, None
            finally:
                with anyio.CancelScope(shield=True):
                    async with self._client_lock:
                        users = self._socks_client_users.get(cleaned_proxy, 0)
                        if users > 1:
                            self._socks_client_users[cleaned_proxy] = users - 1
                        else:
                            self._socks_client_users.pop(cleaned_proxy, None)
                        await self._trim_socks_clients()
            return
        async with self._client_lock:
            if self._http_client is None or self._http_client.closed:
                connector, _ = create_aiohttp_connector(None, limit=32, limit_per_host=16)
                self._http_client = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=25),
                    connector=connector,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    auto_decompress=False,
                )
            client = self._http_client
        yield client, cleaned_proxy

    async def aclose(self) -> None:
        async with self._client_lock:
            if self._http_client is not None and not self._http_client.closed:
                await self._http_client.close()
            self._http_client = None
            for client in list(self._socks_http_clients.values()):
                if not client.closed:
                    await client.close()
            self._socks_http_clients.clear()
            self._socks_client_users.clear()
            self._sessions.clear()

    async def resolve(self, record: dict, settings: RuntimeSettings) -> dict:
        viewkey = str(record["viewkey"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", viewkey):
            raise HTTPException(422, "这条记录没有可用的视频编号")
        page = _playback_page(record, viewkey)
        is_xh = str(record.get("site") or "").strip().lower() == "xhamster" or is_xhamster_url(page)
        site = "xhamster" if is_xh else "pornhub"
        try:
            # Resolving a clip must not occupy a download worker or retry indefinitely.
            async with asyncio.timeout(35), self._resolve_slots, TaskHttpContext(
                cookie_str=settings.xh_cookie if is_xh else settings.ph_cookie,
                proxy_url=settings.normalized_proxy,
                concurrency=1,
                timeout=min(settings.request_timeout, 12),
                max_retries=1,
                site=site,
                allow_url=_media_url,
            ) as ctx:
                detail = await fetch_single_video_detail(
                    ctx, page, settings.selectors, timeout=min(settings.request_timeout, 12),
                    skip_dom=True,
                    include_mp4_sources=True,
                    xh_preferred_codec="h264" if is_xh else settings.xh_preferred_codec,
                )
                cookie = ctx.cookie_string()
        except TimeoutError as err:
            raise HTTPException(504, "获取播放地址超时，请重试") from err

        def _collect_allowed(candidates: tuple[MediaSource, ...]) -> dict[str, MediaSource]:
            collected: dict[str, MediaSource] = {}
            for source in candidates:
                if _media_url(source.url) and (not source.audio_url or _media_url(source.audio_url)):
                    collected.setdefault(source.quality, source)
            return collected

        selected_format = "mp4"
        if is_xh:
            sources = _collect_allowed(detail.hls_sources)
            if sources:
                selected_format = "hls"
            else:
                sources = _collect_allowed(detail.mp4_sources)
        else:
            sources = _collect_allowed(detail.mp4_sources)
            if not sources:
                sources = _collect_allowed(detail.hls_sources)
                if sources:
                    selected_format = "hls"
        if not sources:
            reason = detail.error or "源站未返回可播放的地址"
            raise HTTPException(502, settings.redact_diagnostic(reason))

        # Relay URLs are scoped to this authenticated resource; no client-supplied
        # destination or stored upstream cookie is ever sent back to the browser.
        now = time.monotonic()
        self._prune_expired_sessions(now)
        ticket = secrets.token_urlsafe(18)
        entry = _Playback(
            viewkey=viewkey, sources=tuple(sources.values()),
            referer=detail.referer_url if is_site_url(detail.referer_url, site) else page,
            cookie=cookie, proxy_url=settings.normalized_proxy, expires_at=now + 30 * 60,
            format=selected_format, site=site,
        )
        self._sessions[ticket] = entry
        while len(self._sessions) > _MAX_PLAYBACK_SESSIONS:
            self._sessions.popitem(last=False)
        return {
            "format": selected_format,
            "sources": [
                {
                    "quality": source.quality,
                    "relay_url": (
                        f"/api/videos/{viewkey}/playback/{ticket}/{index}/playlist.m3u8"
                        if selected_format == "hls"
                        else f"/api/videos/{viewkey}/playback/{ticket}/{index}"
                    ),
                }
                for index, source in enumerate(entry.sources)
            ],
        }

    def _prune_expired_sessions(self, now: float) -> None:
        for key in [key for key, entry in self._sessions.items() if entry.expires_at <= now]:
            del self._sessions[key]

    def _session(self, viewkey: str, ticket: str) -> _Playback:
        self._prune_expired_sessions(time.monotonic())
        entry = self._sessions.get(ticket)
        if not entry or entry.viewkey != viewkey:
            raise HTTPException(410, "播放地址已失效，请重新获取")
        return entry

    async def stream(self, viewkey: str, ticket: str, index: int, byte_range: Optional[str]) -> Response:
        entry = self._session(viewkey, ticket)
        if not 0 <= index < len(entry.sources):
            raise HTTPException(404, "没有找到该画质")
        if entry.format == "hls":
            return await self.playlist(viewkey, ticket, index)
        return await self._relay(entry, entry.sources[index].url, byte_range)

    async def playlist(self, viewkey: str, ticket: str, index: int) -> Response:
        entry = self._session(viewkey, ticket)
        if entry.format != "hls" or not 0 <= index < len(entry.sources):
            raise HTTPException(404, "没有找到该画质")
        source = entry.sources[index]
        if source.audio_url:
            video = entry.register_hls_asset(index, source.url)
            audio = entry.register_hls_asset(index, source.audio_url)
            return _playlist_response(
                '#EXTM3U\n'
                f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio",DEFAULT=YES,AUTOSELECT=YES,URI="seg/{audio}"\n'
                f'#EXT-X-STREAM-INF:BANDWIDTH={max(1, source.bandwidth)},AUDIO="audio"\nseg/{video}\n'
            )
        async with AsyncExitStack() as setup:
            upstream, final_url = await self._open_upstream(
                setup,
                entry,
                source.url,
                byte_range=None,
                extra_headers={"Accept-Encoding": "identity"},
            )
            if upstream.status != 200:
                raise HTTPException(502, "视频源暂时不可用")
            return await self._rewrite_playlist(upstream, final_url, entry, index)

    async def _rewrite_playlist(
        self, upstream: aiohttp.ClientResponse, final_url: str, entry: _Playback, index: int, *, nested: bool = False,
    ) -> Response:
        raw = bytearray()
        try:
            async with asyncio.timeout(30):
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    if len(raw) + len(chunk) > _MAX_PLAYLIST_BYTES:
                        raise HTTPException(502, "播放列表体积异常")
                    raw.extend(chunk)
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            raise HTTPException(502, "无法完整读取播放列表") from err
        encoding = upstream.headers.get("Content-Encoding", "identity").strip().lower()
        if encoding == "gzip":
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                    raw = compressed.read(_MAX_PLAYLIST_BYTES + 1)
            except (OSError, EOFError) as err:
                raise HTTPException(502, "无法解码播放列表") from err
            if len(raw) > _MAX_PLAYLIST_BYTES:
                raise HTTPException(502, "播放列表体积异常")
        elif encoding not in {"", "identity"}:
            raise HTTPException(502, "视频源返回了不受支持的编码格式")

        text = raw.decode("utf-8-sig", errors="replace")
        if not text.lstrip().startswith("#EXTM3U"):
            raise HTTPException(502, "视频源没有返回有效的 HLS 播放列表")

        def _rewrite_uri(raw_uri: str) -> str:
            resolved = urljoin(final_url, raw_uri.strip())
            if not _media_url(resolved):
                raise HTTPException(502, "源站返回了不受支持的播放分片地址")
            seg_id = entry.register_hls_asset(index, resolved)
            return str(seg_id) if nested else f"seg/{seg_id}"

        rewritten_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                rewritten_lines.append("")
                continue
            if stripped.startswith("#"):
                if 'URI="' in stripped:
                    rewritten = _HLS_URI_ATTR_RE.sub(
                        lambda m: f'URI="{_rewrite_uri(m.group(1))}"',
                        stripped,
                    )
                    rewritten_lines.append(rewritten)
                else:
                    rewritten_lines.append(stripped)
            else:
                rewritten_lines.append(_rewrite_uri(stripped))

        return _playlist_response("\n".join(rewritten_lines) + "\n")

    async def segment(
        self,
        viewkey: str,
        ticket: str,
        index: int,
        seg_id: int,
        byte_range: Optional[str],
    ) -> Response:
        entry = self._session(viewkey, ticket)
        if entry.format != "hls" or not 0 <= index < len(entry.sources):
            raise HTTPException(404, "没有找到该画质")
        assets = entry.hls_asset_urls.get(index) or []
        if not 0 <= seg_id < len(assets):
            raise HTTPException(404, "没有找到该播放分片")
        source = entry.sources[index]
        return await self._relay(
            entry,
            assets[seg_id],
            byte_range,
            allowed_types=_HLS_SEGMENT_TYPES,
            forced_media_type=None,
            playlist_index=index,
            is_playlist=bool(source.audio_url and assets[seg_id] in {source.url, source.audio_url}),
        )

    async def _open_upstream(
        self,
        setup: AsyncExitStack,
        entry: _Playback,
        url: str,
        byte_range: Optional[str],
        *,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> tuple[aiohttp.ClientResponse, str]:
        try:
            async with asyncio.timeout(6):
                await setup.enter_async_context(self._stream_slots)
        except TimeoutError as err:
            raise HTTPException(503, "播放请求过多，请稍后重试") from err
        client, request_proxy = await setup.enter_async_context(self._get_http_client(entry.proxy_url))
        upstream = None
        current_url = url
        try:
            for _ in range(5):
                if not _media_url(current_url):
                    raise HTTPException(502, "源站返回了不受支持的播放地址")
                headers = {**DOWNLOAD_HEADERS, "Referer": entry.referer}
                if extra_headers:
                    headers.update(extra_headers)
                if byte_range:
                    headers["Range"] = byte_range
                if entry.cookie and is_cookie_origin(current_url, entry.site):
                    headers["Cookie"] = entry.cookie
                candidate = await client.get(
                    current_url, headers=headers, proxy=request_proxy,
                    allow_redirects=False,
                )
                if candidate.status not in _REDIRECTS:
                    upstream = candidate
                    setup.callback(upstream.release)
                    break
                try:
                    current_url = urljoin(current_url, candidate.headers["Location"])
                except (KeyError, ValueError) as err:
                    candidate.close()
                    raise HTTPException(502, "源站的播放跳转无效") from err
                candidate.release()
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as err:
            raise HTTPException(502, "无法连接视频源，请重新获取播放地址") from err

        if upstream is None:
            raise HTTPException(502, "视频源跳转次数过多")
        if upstream.status in {401, 403, 404, 410}:
            raise HTTPException(410, "视频源拒绝了此地址，请重新获取")
        return upstream, current_url

    async def _relay(
        self,
        entry: _Playback,
        url: str,
        byte_range: Optional[str],
        *,
        allowed_types: Optional[set[str]] = None,
        forced_media_type: Optional[str] = "video/mp4",
        playlist_index: Optional[int] = None,
        is_playlist: bool = False,
    ) -> Response:
        requested = _parse_range(byte_range)
        valid_types = allowed_types if allowed_types is not None else _MP4_TYPES
        async with AsyncExitStack() as setup:
            upstream, final_url = await self._open_upstream(setup, entry, url, byte_range)
            if upstream.status == 416:
                reply_range = upstream.headers.get("Content-Range", "")
                headers = {"Content-Range": reply_range} if re.fullmatch(r"bytes \*/[0-9]{1,18}", reply_range) else {}
                return Response(status_code=416, headers={**NO_CACHE, **headers})
            if upstream.status not in {200, 206}:
                raise HTTPException(502, "视频源暂时不可用")
            content_type = upstream.headers.get("Content-Type", "").partition(";")[0].strip().lower()
            if playlist_index is not None and (
                is_playlist or content_type in {"application/vnd.apple.mpegurl", "application/x-mpegurl"}
                or urlsplit(final_url).path.lower().endswith(".m3u8")
            ):
                if upstream.status != 200:
                    raise HTTPException(502, "视频源返回了不完整的播放列表")
                return await self._rewrite_playlist(upstream, final_url, entry, playlist_index, nested=True)
            encoding = upstream.headers.get("Content-Encoding", "identity").strip().lower()
            length = upstream.headers.get("Content-Length")
            headers = {
                **NO_CACHE,
                "X-Accel-Buffering": "no",
                "Cross-Origin-Resource-Policy": "same-origin",
            }
            if content_type not in valid_types or encoding not in {"", "identity"}:
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
            media_type = forced_media_type or content_type or "application/octet-stream"
            return _RelayResponse(upstream, setup.pop_all(), headers, media_type=media_type)
