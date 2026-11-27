"""Authentication and session cookie handling."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
import urllib.parse
from collections import OrderedDict
from typing import Iterable, List, Mapping, Optional

from fastapi import HTTPException, Request, WebSocket, status

SESSION_COOKIE_NAME = "phdl_session"
_MAX_TRACKED_CLIENTS = 2048


class AuthManager:
    """Stateless HMAC-signed session tokens with simple login rate limiting."""

    def __init__(
        self,
        admin_password: str,
        secret_key: str,
        ttl_hours: int = 168,
        cookie_secure: bool = False,
        generation: Optional[int] = None,
    ) -> None:
        self._admin_password = admin_password
        self._secret_key = secret_key.encode("utf-8")
        self._ttl_seconds = max(3600, int(ttl_hours) * 3600)
        self.cookie_secure = cookie_secure
        self._failed_attempts: OrderedDict[str, List[float]] = OrderedDict()
        self._generation = generation if generation is not None else 1 + secrets.randbelow(2 ** 60)
        self.session_invalidated = asyncio.Event()

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def check_rate_limit(self, client_ip: str) -> None:
        now = time.monotonic()
        window = 300.0  # 5 min
        recent = [ts for ts in self._failed_attempts.get(client_ip, []) if now - ts < window]
        if recent:
            self._failed_attempts[client_ip] = recent
        else:
            self._failed_attempts.pop(client_ip, None)
        if len(recent) >= 10:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="登录尝试过于频繁，请 5 分钟后再试",
            )

    def record_login_failure(self, client_ip: str) -> None:
        attempts = self._failed_attempts.setdefault(client_ip, [])
        attempts.append(time.monotonic())
        self._failed_attempts.move_to_end(client_ip)
        if len(self._failed_attempts) > _MAX_TRACKED_CLIENTS:
            self._failed_attempts.popitem(last=False)

    def clear_login_failures(self, client_ip: str) -> None:
        self._failed_attempts.pop(client_ip, None)

    def verify_password(self, candidate: str) -> bool:
        if not candidate:
            return False
        try:
            return hmac.compare_digest(candidate.encode("utf-8"), self._admin_password.encode("utf-8"))
        except UnicodeEncodeError:
            return False

    def issue_token(self) -> str:
        exp = int(time.time()) + self._ttl_seconds
        nonce = secrets.token_hex(16)
        generation = self._generation
        payload = f"{exp}.{nonce}.{generation}"
        sig = self._sign(payload)
        return f"{payload}.{sig}"

    def set_generation(self, generation: int) -> None:
        """Apply a persisted revocation and wake already-connected streams."""
        if generation > self._generation:
            self._generation = generation
            self.session_invalidated.set()
            self.session_invalidated = asyncio.Event()

    def verify_token(self, token: Optional[str]) -> bool:
        if not token:
            return False
        parts = token.split(".")
        if len(parts) != 4:
            return False
        exp_str, nonce, generation, sig = parts
        if (
            not exp_str.isascii() or not exp_str.isdigit() or len(exp_str) > 20
            or not generation.isascii() or not generation.isdigit() or len(generation) > 20
            or not nonce or len(sig) != 64 or not sig.isascii()
        ):
            return False
        if int(generation) != self._generation:
            return False
        if int(exp_str) < int(time.time()):
            return False
        expected = self._sign(f"{exp_str}.{nonce}.{generation}")
        return hmac.compare_digest(sig, expected)

    def extract_request_token(self, request: Request) -> Optional[str]:
        cookie_tok = request.cookies.get(SESSION_COOKIE_NAME)
        if cookie_tok:
            return cookie_tok
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        return None

    def require_auth(self, request: Request) -> None:
        token = self.extract_request_token(request)
        if not self.verify_token(token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="未登录或会话已过期",
            )

    def verify_websocket(self, websocket: WebSocket) -> bool:
        token = websocket.cookies.get(SESSION_COOKIE_NAME)
        return self.verify_token(token)

    @staticmethod
    def verify_websocket_origin(websocket: WebSocket, allowed_origins: Iterable[str] = ()) -> bool:
        return AuthManager.verify_origin(websocket.headers, allowed_origins)

    @staticmethod
    def verify_origin(headers: Mapping[str, str], allowed_origins: Iterable[str] = ()) -> bool:
        origin = (headers.get("origin") or "").strip().rstrip("/")
        if not origin:
            return True
        try:
            parsed = urllib.parse.urlsplit(origin)
            port = parsed.port
        except ValueError:
            return False
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.path or parsed.query or parsed.fragment or port == 0):
            return False

        normalized_allowed = {o.strip().rstrip("/").lower() for o in allowed_origins if o and o.strip()}
        if origin.lower() in normalized_allowed:
            return True

        try:
            host = urllib.parse.urlsplit(f"//{(headers.get('host') or '').strip()}")
            default_port = 443 if parsed.scheme == "https" else 80
            return (host.username is None and not (host.path or host.query or host.fragment)
                    and parsed.hostname == host.hostname and (port or default_port) == (host.port or default_port))
        except ValueError:
            return False

    @property
    def max_age_seconds(self) -> int:
        return self._ttl_seconds
