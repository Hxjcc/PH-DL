"""Proxy helpers for aiohttp sessions (HTTP/HTTPS + SOCKS4/SOCKS5 via aiohttp-socks)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional, Tuple
from urllib.parse import urljoin

import aiohttp
from aiohttp_socks import ProxyConnector

from ..parsers.urls import is_cookie_origin, is_media_url

_SOCKS_SCHEMES = ("socks4://", "socks4a://", "socks5://", "socks5h://")
_REDIRECTS = {301, 302, 303, 307, 308}


class MediaPolicyError(ValueError):
    pass


@asynccontextmanager
async def open_media_response(session, url: str, *, headers: dict, site: Optional[str], **kwargs):
    if site is None:
        async with session.get(url, headers=headers, allow_redirects=True, **kwargs) as response:
            yield response
        return

    public_headers = {name: value for name, value in headers.items() if name.lower() not in {"cookie", "authorization"}}
    for _ in range(6):
        if not is_media_url(url, site):
            raise MediaPolicyError("媒体源地址不在允许范围内")
        request_headers = headers if is_cookie_origin(url, site) else public_headers
        async with session.get(url, headers=request_headers, allow_redirects=False, **kwargs) as response:
            if response.status not in _REDIRECTS:
                yield response
                return
            location = response.headers.get("Location")
            if not location:
                raise MediaPolicyError("媒体源重定向缺少目标地址")
            url = urljoin(url, location)
    raise MediaPolicyError("媒体源重定向次数过多")


def is_socks_proxy(proxy_url: Optional[str]) -> bool:
    """Return True when ``proxy_url`` uses a SOCKS proxy scheme."""
    cleaned = (proxy_url or "").strip().lower()
    return cleaned.startswith(_SOCKS_SCHEMES)


def create_aiohttp_connector(
    proxy_url: Optional[str] = None,
    *,
    limit: int = 100,
    limit_per_host: int = 32,
    keepalive_timeout: float = 30.0,
    ttl_dns_cache: int = 300,
) -> Tuple[aiohttp.BaseConnector, Optional[str]]:
    """Build an aiohttp connector supporting both HTTP(S) and SOCKS proxies.

    Returns ``(connector, request_proxy)``. When ``proxy_url`` is a SOCKS URL,
    routing is handled by ``ProxyConnector`` at the transport layer and
    ``request_proxy`` is ``None`` (aiohttp rejects non-HTTP per-request proxies).
    """
    cleaned = (proxy_url or "").strip() or None
    if cleaned and is_socks_proxy(cleaned):
        connector = ProxyConnector.from_url(
            cleaned,
            limit=limit,
            limit_per_host=limit_per_host,
            keepalive_timeout=keepalive_timeout,
            ttl_dns_cache=ttl_dns_cache,
        )
        return connector, None
    connector = aiohttp.TCPConnector(
        limit=limit,
        limit_per_host=limit_per_host,
        keepalive_timeout=keepalive_timeout,
        ttl_dns_cache=ttl_dns_cache,
    )
    return connector, cleaned


def effective_request_proxy(
    session: aiohttp.ClientSession,
    proxy_url: Optional[str],
) -> Optional[str]:
    """Return the per-request ``proxy=`` argument safe to pass to ``session.get()``.

    When ``session`` already uses an ``aiohttp_socks.ProxyConnector``, omit the
    per-request ``proxy`` parameter so aiohttp does not raise
    ``ValueError('Only http proxies are supported')``.
    """
    cleaned = (proxy_url or "").strip() or None
    if not cleaned:
        return None
    if is_socks_proxy(cleaned) and isinstance(getattr(session, "connector", None), ProxyConnector):
        return None
    return cleaned
