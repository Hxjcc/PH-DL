"""Validate upstream origins before sending requests or site credentials."""

import re
from urllib.parse import urlsplit

_XHAMSTER_HOST = re.compile(r"(?:[a-z0-9-]+\.)*xhamster\d*\.(?:com|desi|one|tv|xxx|live)")
_PORN_HUB_MEDIA = ("phncdn.com", "phprcdn.com")


def _within(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def is_xhamster_host(host: str) -> bool:
    return bool(_XHAMSTER_HOST.fullmatch(host.strip().lower().rstrip(".")))


def upstream_host(url: str, *, legacy_http: bool = False) -> str:
    if "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return ""
    try:
        parsed = urlsplit(url)
        if parsed.username is not None or parsed.password is not None:
            return ""
        if parsed.scheme == "https" and parsed.port in {None, 443}:
            return (parsed.hostname or "").rstrip(".")
        if legacy_http and parsed.scheme == "http" and parsed.port in {None, 80}:
            return (parsed.hostname or "").rstrip(".")
    except ValueError:
        pass
    return ""


def is_site_url(url: str, site: str, *, legacy_http: bool = False) -> bool:
    host = upstream_host(url, legacy_http=legacy_http)
    return is_xhamster_host(host) if site == "xhamster" else _within(host, "pornhub.com")


def is_cookie_origin(url: str, site: str) -> bool:
    host = upstream_host(url)
    return _within(host, "xhamster.com" if site == "xhamster" else "pornhub.com")


def is_media_url(url: str, site: str) -> bool:
    host = upstream_host(url)
    cdns = ("xhcdn.com",) if site == "xhamster" else _PORN_HUB_MEDIA
    return is_site_url(url, site) or any(_within(host, domain) for domain in cdns)
