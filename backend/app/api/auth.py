"""Authentication endpoints."""

from __future__ import annotations

import ipaddress
from typing import Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from ..auth import SESSION_COOKIE_NAME
from .deps import AppContainer, get_container

router = APIRouter(prefix="/api/auth", tags=["auth"])


# Default trusted reverse-proxy hops: loopback, Docker bridge range, and IPv6 ULA.
# Generic LAN ranges (10.0.0.0/8, 192.168.0.0/16) are intentionally NOT trusted as
# chained proxies by default so direct LAN/VPN clients cannot spoof X-Forwarded-For.
# Override via PHDL_TRUSTED_PROXIES (CIDR list or "none").
_TRUSTED_PROXY_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)


def _normalize_ip(raw: str) -> str:
    try:
        return str(ipaddress.ip_address((raw or "").strip()))
    except ValueError:
        return ""


def _is_trusted_proxy_ip(
    host: str,
    trusted_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network] = _TRUSTED_PROXY_NETWORKS,
) -> bool:
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in trusted_networks)
    except ValueError:
        return False


def _extract_client_ip(
    request: Request,
    trusted_networks: Optional[Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network]] = None,
) -> str:
    networks = _TRUSTED_PROXY_NETWORKS if trusted_networks is None else trusted_networks
    raw_direct = (request.client.host or "").strip() if request.client else ""
    direct = _normalize_ip(raw_direct) or raw_direct
    if direct and networks and _is_trusted_proxy_ip(direct, networks):
        xff = (request.headers.get("X-Forwarded-For") or "").strip()
        if xff:
            parts = [ip for p in xff.split(",") if (ip := _normalize_ip(p))]
            # Walk right-to-left and stop at the first hop outside trusted proxies
            for hop in reversed(parts):
                if not _is_trusted_proxy_ip(hop, networks):
                    return hop
            if parts:
                return parts[0]
        real_ip = _normalize_ip(request.headers.get("X-Real-IP") or "")
        if real_ip:
            return real_ip
    return direct or "unknown"


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
):
    client_ip = _extract_client_ip(request, container.env.parsed_trusted_proxies())
    container.auth.check_rate_limit(client_ip)

    if not container.auth.verify_password(payload.password):
        container.auth.record_login_failure(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="密码错误")

    container.auth.clear_login_failures(client_ip)
    token = container.auth.issue_token()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=container.env.cookie_secure,
        max_age=container.auth.max_age_seconds,
        path="/",
    )
    return {"authenticated": True}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
):
    # Only a token holder may revoke the process-wide session generation;
    # otherwise an unauthenticated request could log everyone out.
    if container.auth.verify_token(container.auth.extract_request_token(request)):
        container.auth.set_generation(await container.repo.revoke_auth_sessions())
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return {"authenticated": False}


@router.get("/me")
async def me(request: Request, container: AppContainer = Depends(get_container)):
    token = container.auth.extract_request_token(request)
    return {"authenticated": container.auth.verify_token(token)}
