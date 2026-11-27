"""Remove credentials and signed request details from published diagnostics."""

import re
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_URL = re.compile(r"\b(?:https?|socks[45]h?)://[^\s<>'\"]+", re.IGNORECASE)
_USERINFO = re.compile(r"(?<=://)[^/?#]*@")
_CREDENTIAL_HEADER = re.compile(r"\b(set-cookie|cookie|(?:proxy-)?authorization)(:\s*)[^\r\n]+", re.IGNORECASE)
_CREDENTIAL_VALUE = re.compile(
    r"\b(password|client_?key|send_?key|access_?token|proxy_?pass|csrf_?token)(\"?\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^,\s&}]+)", re.IGNORECASE,
)


def redact_diagnostic(message: str, secrets: Iterable[str] = ()) -> str:
    text = str(message or "")
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, "***")
    text = _CREDENTIAL_HEADER.sub(r"\1\2***", text)
    text = _CREDENTIAL_VALUE.sub(r"\1\2***", text)

    def clean_url(match: re.Match[str]) -> str:
        url = _USERINFO.sub("***@", match[0])
        try:
            parsed = urlsplit(url)
            public = [(key, value) for key, value in parse_qsl(parsed.query) if (
                key == "page" and re.fullmatch(r"[0-9]{1,8}", value)
                or key == "viewkey" and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
            )]
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(public), ""))
        except ValueError:
            return re.split(r"[?#]", url, maxsplit=1)[0]

    return _URL.sub(clean_url, text).replace("\r", " ")[:4096]
