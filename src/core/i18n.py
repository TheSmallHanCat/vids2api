"""Backend message translation for JSON responses, keyed by Accept-Language.

The zh-CN locale is the source text; translations live in static/locales/backend.json
and are shared with the frontend i18n layer.
"""

import json

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import BASE_DIR

LOCALES_DIR = BASE_DIR / "static" / "locales"
SUPPORTED = ("zh-CN", "en")
DEFAULT_LOCALE = "zh-CN"
_TRANSLATED_PATH_PREFIXES = ("/api/", "/v1/", "/v1beta/", "/providers/")
_EXEMPT_PATHS = {"/api/logs", "/api/logs/"}


def load_backend_messages() -> dict[str, dict[str, str]]:
    path = LOCALES_DIR / "backend.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    messages = {}
    for locale, catalog in data.items():
        if isinstance(catalog, dict):
            messages[locale] = {
                key: value for key, value in catalog.items() if isinstance(value, str)
            }
    return messages


_BACKEND_MESSAGES = load_backend_messages()


def negotiate_locale(request: Request) -> str:
    header = request.headers.get("Accept-Language", "")
    for part in header.split(","):
        tag = part.split(";")[0].strip()
        if tag in SUPPORTED:
            return tag
        if tag.startswith("zh"):
            return "zh-CN"
        if tag.startswith("en"):
            return "en"
    return DEFAULT_LOCALE


def translate_message(message: str, locale: str) -> str:
    if locale == DEFAULT_LOCALE or not isinstance(message, str):
        return message
    catalog = _BACKEND_MESSAGES.get(locale, {})
    translated = catalog.get(message)
    return translated if isinstance(translated, str) else message


def _translate_detail(detail, locale: str):
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            error["message"] = translate_message(error["message"], locale)
            return detail
        if isinstance(detail.get("detail"), str):
            detail["detail"] = translate_message(detail["detail"], locale)
        if isinstance(detail.get("message"), str):
            detail["message"] = translate_message(detail["message"], locale)
        return detail
    if isinstance(detail, str):
        return translate_message(detail, locale)
    return detail


class I18nMiddleware(BaseHTTPMiddleware):
    """Translate JSON error payloads for API paths; streams and large bodies pass through."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if (
            not any(path.startswith(prefix) for prefix in _TRANSLATED_PATH_PREFIXES)
            or any(path == exempt or path.startswith(exempt) for exempt in _EXEMPT_PATHS)
            or response.status_code < 400
            or "application/json" not in response.headers.get("Content-Type", "")
        ):
            return response
        locale = negotiate_locale(request)
        if locale == DEFAULT_LOCALE:
            return response
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        if len(body) > 1024 * 1024:
            return JSONResponse(
                status_code=response.status_code,
                content=body.decode("utf-8", "replace"),
            )
        try:
            import json as _json

            payload = _json.loads(body)
        except (ValueError, TypeError):
            return JSONResponse(status_code=response.status_code, content=body)
        if isinstance(payload, dict):
            payload["error"] = _translate_detail(payload.get("error"), locale)
            if "detail" in payload:
                payload["detail"] = _translate_detail(payload.get("detail"), locale)
        headers = dict(response.headers)
        headers["Content-Language"] = locale
        headers.setdefault("Vary", "Accept-Language")
        return JSONResponse(status_code=response.status_code, content=payload, headers=headers)
