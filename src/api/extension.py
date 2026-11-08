"""Browser-extension push endpoint: ingest cookies into the account pool.

Extension clients authenticate with a dedicated push token (managed on the
admin config page); each push either imports a new account or refreshes the
credentials of an already-known one.
"""

import secrets
import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..core.database import Database
from ..services.account_manager import AccountManager, AccountManagerError

router = APIRouter()

account_manager: AccountManager | None = None
db: Database | None = None

RATE_LIMIT = 10
RATE_WINDOW = 60.0
_push_times: deque[float] = deque()


def set_dependencies(account_manager_instance, db_instance):
    global account_manager, db
    account_manager = account_manager_instance
    db = db_instance


def _error(status: int, message: str, code: str):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": "extension_error", "code": code}},
    )


def _rate_limited() -> bool:
    now = time.monotonic()
    while _push_times and now - _push_times[0] > RATE_WINDOW:
        _push_times.popleft()
    if len(_push_times) >= RATE_LIMIT:
        return True
    _push_times.append(now)
    return False


async def verify_push_token(request: Request) -> None:
    token = request.headers.get("X-Push-Token", "")
    settings = await db.get_extension_config()
    if not settings["enabled"] or not token or not settings["push_token"]:
        raise HTTPException(status_code=401, detail="Invalid push token")
    if not secrets.compare_digest(token, settings["push_token"]):
        raise HTTPException(status_code=401, detail="Invalid push token")


@router.post("/api/extension/push")
async def push_account(request: Request, _token: None = Depends(verify_push_token)):
    if _rate_limited():
        return _error(429, "推送过于频繁，请稍后再试", "rate_limited")
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "请求体必须是 JSON", "invalid_request")
    raw_cookies = body.get("cookies")
    if isinstance(raw_cookies, list):
        import json as _json

        raw_cookies = _json.dumps(raw_cookies)
    if not isinstance(raw_cookies, str) or not raw_cookies.strip():
        return _error(400, "cookies 为必填项", "invalid_request")
    editor_url = str(body.get("editor_url", "")).strip()
    remark = str(body.get("remark", "")).strip()
    try:
        if not editor_url:
            editor_url = await account_manager.discover_editor_url(raw_cookies) or ""
            if not editor_url:
                return _error(
                    400,
                    "未找到现有 Vids 文档，请先在浏览器中创建一个 Vids 视频后再推送",
                    "document_required",
                )
        result = await account_manager.import_account(
            editor_url=editor_url,
            cookies=raw_cookies,
            remark=remark,
        )
    except AccountManagerError as error:
        if error.code != "duplicate_account" or error.account_id is None:
            return _error(error.status, str(error), error.code)
        try:
            await account_manager.replace_cookies(error.account_id, raw_cookies)
        except AccountManagerError as refresh_error:
            return _error(refresh_error.status, str(refresh_error), refresh_error.code)
        settings = await db.get_extension_config()
        auto_enabled = False
        if settings["auto_enable_on_update"]:
            auto_enabled = await db.unban_account(error.account_id, automatic_only=True)
        return {
            "action": "refreshed",
            "account_id": error.account_id,
            "editor_url": editor_url,
            "auto_enabled": auto_enabled,
        }
    except Exception as error:  # noqa: BLE001 - surface upstream failures
        from ..core.logger import debug_logger

        debug_logger.log_error("extension-push", error)
        return _error(502, "上游初始化失败，请检查 Cookie 与网络", "bootstrap_failed")
    return {
        "action": "imported",
        "account_id": result.get("account_id"),
        "editor_url": editor_url,
    }
