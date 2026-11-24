"""Authentication endpoints."""

from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from ..auth import SESSION_COOKIE_NAME
from .deps import AppContainer, get_container

router = APIRouter(prefix="/api/auth", tags=["auth"])


_TRUSTED_PROXY_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)


def _is_trusted_proxy_ip(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in _TRUSTED_PROXY_NETWORKS)
    except ValueError:
        return False


def _extract_client_ip(request: Request) -> str:
    direct = request.client.host if request.client else ""
    if direct and _is_trusted_proxy_ip(direct):
        real_ip = (request.headers.get("X-Real-IP") or "").strip()
        if real_ip:
            return real_ip
        xff = (request.headers.get("X-Forwarded-For") or "").strip()
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            # Pick the rightmost non-private hop, or first hop if all private
            for hop in reversed(parts):
                if not _is_trusted_proxy_ip(hop):
                    return hop
            if parts:
                return parts[0]
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
    client_ip = _extract_client_ip(request)
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
