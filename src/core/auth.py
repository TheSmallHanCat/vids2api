"""Dual-track authentication: API key for generation routes, sessions for the admin UI."""

import secrets

from fastapi import HTTPException, Request

from .config import config


class AuthManager:
    @staticmethod
    def verify_api_key(api_key: str) -> bool:
        return bool(api_key) and secrets.compare_digest(api_key, config.api_key)

    @staticmethod
    def verify_admin(username: str, password: str) -> bool:
        return secrets.compare_digest(username.encode("utf-8"), config.admin_username.encode("utf-8")) and secrets.compare_digest(
            password.encode("utf-8"), config.admin_password.encode("utf-8")
        )


active_admin_tokens: set[str] = set()


def _extract_api_key(request: Request) -> str | None:
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        candidate = authorization[7:].strip()
        if candidate:
            return candidate
    goog_key = request.headers.get("x-goog-api-key", "").strip()
    if goog_key:
        return goog_key
    query_key = request.query_params.get("key", "").strip()
    if query_key:
        return query_key
    return None


async def verify_api_key_flexible(request: Request) -> str:
    api_key = _extract_api_key(request)
    if not api_key or not AuthManager.verify_api_key(api_key):
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Invalid API key", "type": "auth_error"}},
        )
    return api_key


def get_admin_token_from_cookie(request: Request) -> str | None:
    return request.cookies.get("admin_session", "")


def is_admin_session_token_valid(token: str | None) -> bool:
    return bool(token) and token in active_admin_tokens


def issue_admin_token() -> str:
    token = f"admin-{secrets.token_urlsafe(32)}"
    active_admin_tokens.add(token)
    return token


def revoke_admin_token(token: str | None):
    active_admin_tokens.discard(token or "")


async def verify_admin_token(request: Request) -> str:
    token = request.headers.get("Authorization", "")
    if token.startswith("Bearer "):
        token = token[7:].strip()
    if not token or not is_admin_session_token_valid(token):
        token = get_admin_token_from_cookie(request)
    if not is_admin_session_token_valid(token):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return token


async def optional_admin_token(request: Request) -> str | None:
    """For routes that serve both cookie sessions and bearer tokens."""
    token = request.headers.get("Authorization", "")
    if token.startswith("Bearer admin-"):
        token = token[7:].strip()
    else:
        token = get_admin_token_from_cookie(request)
    return token if is_admin_session_token_valid(token) else None
