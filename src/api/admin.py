"""Admin management API: login sessions, account pool, config, logs and stats."""

import secrets
import sys
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from ..core import auth
from ..core.auth import (
    issue_admin_token,
    revoke_admin_token,
    verify_admin_token,
)
from ..core.config import BASE_DIR, config
from ..core.database import Database
from ..core.settings import (
    APIKeySettings, CacheSettings, CredentialsSettings, DebugSettings, ExtensionSettings,
    GenerationSettings, LoginSettings, PoolSettings, ProxySettings, ProxyTestSettings,
)
from ..services.account_manager import AccountManager, AccountManagerError

router = APIRouter()

account_manager: AccountManager | None = None
proxy_manager = None
db: Database | None = None


def set_dependencies(account_manager_instance, proxy_manager_instance, db_instance):
    global account_manager, proxy_manager, db
    account_manager = account_manager_instance
    proxy_manager = proxy_manager_instance
    db = db_instance


def _admin_error(status: int, message: str, code: str = "invalid_request"):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": "admin_error", "code": code}},
    )


# ---- auth ------------------------------------------------------------------
@router.post("/api/admin/login")
@router.post("/api/login")
async def login(request: Request, response: Response):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    username = str(body.get("username", ""))
    password = str(body.get("password", ""))
    if not auth.AuthManager.verify_admin(username, password):
        return _admin_error(401, "用户名或密码错误", "invalid_credentials")
    token = issue_admin_token()
    response.set_cookie(
        "admin_session", token, httponly=True, samesite="lax", path="/",
        secure=request.url.scheme == "https",
    )
    return {"token": token, "username": username}


@router.post("/api/admin/logout")
@router.post("/api/logout")
async def logout(request: Request, response: Response):
    token = (
        request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        or request.cookies.get("admin_session", "")
    )
    revoke_admin_token(token)
    response.delete_cookie("admin_session", path="/")
    return {"ok": True}


@router.post("/api/admin/change-password")
async def change_password(request: CredentialsSettings, response: Response,
                          _token: str = Depends(verify_admin_token)):
    current = await db.get_admin_config()
    if not current or not secrets.compare_digest(
        str(current["password"]).encode("utf-8"), request.old_password.encode("utf-8")
    ):
        return _admin_error(400, "旧密码错误", "invalid_credentials")
    await db.update_admin_credentials(request.username, request.password)
    config.set_admin_credentials(request.username, request.password)
    auth.active_admin_tokens.clear()
    response.delete_cookie("admin_session", path="/")
    return {"ok": True, "username": request.username, "reauthenticate": True}


@router.get("/api/admin/config")
async def get_admin_config(_token: str = Depends(verify_admin_token)):
    row = await db.get_admin_config()
    if not row:
        return _admin_error(500, "配置缺失")
    masked_key = row["api_key"]
    return {
        "username": row["username"],
        "api_key": masked_key,
        "api_key_hint": masked_key[:6] + "***" + masked_key[-4:]
        if len(masked_key) > 12 else "***",
    }


@router.post("/api/admin/apikey")
async def update_api_key(request: APIKeySettings, _token: str = Depends(verify_admin_token)):
    await db.update_api_key(request.api_key)
    config.set_api_key(request.api_key)
    return {"ok": True}


# ---- accounts ----------------------------------------------------------------
def _account_payload(account, registered) -> dict:
    running = registered is not None
    ready = bool(
        running
        and registered.gateway._ready
        and getattr(registered.gateway.client, "initialization_valid", True) is not False
    )
    return {
        "id": account.id,
        "email": account.email,
        "remark": account.remark,
        "editor_url": account.editor_url,
        "auth_user": account.auth_user,
        "is_active": account.is_active,
        "image_enabled": account.image_enabled,
        "video_enabled": account.video_enabled,
        "speech_enabled": account.speech_enabled,
        "proxy_url": account.proxy_url,
        "status": account.status,
        "last_error": account.last_error,
        "use_count": account.use_count,
        "image_count": account.image_count,
        "video_count": account.video_count,
        "speech_count": account.speech_count,
        "error_count": account.error_count,
        "consecutive_error_count": account.consecutive_error_count,
        "quota": {
            "image": {
                "total": account.quota_image_total,
                "available": account.quota_image_available,
                "checked_at": account.quota_image_checked_at,
            },
            "video": {
                "total": account.quota_video_total,
                "available": account.quota_video_available,
                "checked_at": account.quota_video_checked_at,
            },
            "speech": {
                "total": account.quota_speech_total,
                "available": account.quota_speech_available,
                "checked_at": account.quota_speech_checked_at,
            },
        },
        "ban_reason": account.ban_reason,
        "running": running,
        "ready": ready,
        "busy": bool(running and registered.gateway.lock.locked()),
        "created_at": account.created_at,
        "last_used_at": account.last_used_at,
    }


@router.get("/api/accounts")
async def list_accounts(_token: str = Depends(verify_admin_token)):
    accounts = await db.get_accounts()
    data = [
        _account_payload(account, account_manager.dispatcher._accounts.get(account.id))
        for account in accounts
    ]
    return {"data": data}


@router.post("/api/accounts/import")
async def import_account(request: Request, _token: str = Depends(verify_admin_token)):
    try:
        body = await request.json()
    except ValueError:
        return _admin_error(400, "请求体必须是 JSON")
    editor_url = str(body.get("editor_url", "")).strip()
    cookies = str(body.get("cookies", ""))
    if not editor_url or not cookies.strip():
        return _admin_error(400, "editor_url 与 cookies 为必填项")
    try:
        result = await account_manager.import_account(
            editor_url=editor_url,
            cookies=cookies,
            remark=str(body.get("remark", "")),
            proxy_url=str(body.get("proxy_url", "")),
            image_enabled=bool(body.get("image_enabled", True)),
            video_enabled=bool(body.get("video_enabled", True)),
            speech_enabled=bool(body.get("speech_enabled", True)),
        )
    except AccountManagerError as error:
        return _admin_error(error.status, str(error), error.code)
    return result


@router.put("/api/accounts/{account_id}")
async def update_account(account_id: int, request: Request,
                         _token: str = Depends(verify_admin_token)):
    try:
        body = await request.json()
    except ValueError:
        return _admin_error(400, "请求体必须是 JSON")
    try:
        result = await account_manager.update_account(
            account_id,
            remark=body.get("remark"),
            proxy_url=body.get("proxy_url"),
            image_enabled=body.get("image_enabled"),
            video_enabled=body.get("video_enabled"),
            speech_enabled=body.get("speech_enabled"),
        )
    except AccountManagerError as error:
        return _admin_error(error.status, str(error), error.code)
    return result


@router.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int, _token: str = Depends(verify_admin_token)):
    try:
        result = await account_manager.remove_account(account_id)
    except AccountManagerError as error:
        return _admin_error(error.status, str(error), error.code)
    return result


@router.post("/api/accounts/{account_id}/enable")
async def enable_account(account_id: int, _token: str = Depends(verify_admin_token)):
    try:
        result = await account_manager.set_account_enabled(account_id, True)
    except AccountManagerError as error:
        return _admin_error(error.status, str(error), error.code)
    return result


@router.post("/api/accounts/{account_id}/disable")
async def disable_account(account_id: int, _token: str = Depends(verify_admin_token)):
    try:
        result = await account_manager.set_account_enabled(account_id, False)
    except AccountManagerError as error:
        return _admin_error(error.status, str(error), error.code)
    return result


@router.post("/api/accounts/{account_id}/start")
async def start_account(account_id: int, _token: str = Depends(verify_admin_token)):
    try:
        result = await account_manager.start_account(account_id)
    except AccountManagerError as error:
        return _admin_error(error.status, str(error), error.code)
    return result


@router.post("/api/accounts/{account_id}/refresh-quota")
async def refresh_quota(account_id: int, _token: str = Depends(verify_admin_token)):
    try:
        result = await account_manager.refresh_quota(account_id)
    except AccountManagerError as error:
        return _admin_error(error.status, str(error), error.code)
    return {"account_id": account_id, "quota": result}


@router.post("/api/accounts/{account_id}/replace-cookies")
async def replace_cookies(account_id: int, request: Request,
                          _token: str = Depends(verify_admin_token)):
    try:
        body = await request.json()
    except ValueError:
        return _admin_error(400, "请求体必须是 JSON")
    cookies = str(body.get("cookies", ""))
    if not cookies.strip():
        return _admin_error(400, "cookies 为必填项")
    try:
        result = await account_manager.replace_cookies(account_id, cookies)
    except AccountManagerError as error:
        return _admin_error(error.status, str(error), error.code)
    return result


@router.post("/api/accounts/{account_id}/test")
async def test_account(account_id: int, _token: str = Depends(verify_admin_token)):
    try:
        result = await account_manager.test_account(account_id)
    except AccountManagerError as error:
        return _admin_error(error.status, str(error), error.code)
    return result


# ---- server-browser login -----------------------------------------------------
@router.post("/api/accounts/server-login")
async def start_server_login(request: Request, _token: str = Depends(verify_admin_token)):
    from ..services.browser_profile import BrowserProfileError

    if account_manager.profile_service is None:
        return _admin_error(503, "浏览器 Profile 服务未启用", "profile_disabled")
    body = await _read_json_body(request)
    remark = str(body.get("remark", "")).strip()
    email = str(body.get("email", "")).strip()
    password = str(body.get("password", ""))
    totp_secret = str(body.get("totp_secret", "")).strip()
    credentials = None
    if email and password:
        credentials = {"email": email, "password": password, "totp": totp_secret}
    login_proxy = str(body.get("proxy", "")).strip()
    if login_proxy and urlsplit(login_proxy).scheme not in {"http", "https", "socks5", "socks5h"}:
        return _admin_error(400, "代理格式无效，需为 http(s):// 或 socks5:// 开头", "invalid_proxy")
    proxy = login_proxy or await account_manager.effective_proxy()
    try:
        info = await account_manager.profile_service.start_login(
            remark=remark, proxy_url=proxy, credentials=credentials, persist_credentials=True
        )
    except BrowserProfileError as error:
        return _admin_error(400, str(error), "server_login_error")
    return info


@router.get("/api/accounts/server-login")
async def server_login_status(_token: str = Depends(verify_admin_token)):
    if account_manager.profile_service is None:
        return {"state": "unavailable"}
    return account_manager.profile_service.status()


@router.post("/api/accounts/server-login/cancel")
async def cancel_server_login(_token: str = Depends(verify_admin_token)):
    from ..services.browser_profile import BrowserProfileError

    if account_manager.profile_service is None:
        return _admin_error(503, "浏览器 Profile 服务未启用", "profile_disabled")
    try:
        await account_manager.profile_service.cancel_login()
    except BrowserProfileError as error:
        return _admin_error(400, str(error), "no_active_login")
    return {"cancelled": True}


# ---- config -------------------------------------------------------------------
async def _read_json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    return body if isinstance(body, dict) else {}


@router.get("/api/config/generation")
async def get_generation_config(_token: str = Depends(verify_admin_token)):
    return {
        "image_timeout": config.image_timeout,
        "speech_timeout": config.speech_timeout,
        "video_timeout": config.video_timeout,
        "max_retries": config.max_retries,
    }


@router.post("/api/config/generation")
async def set_generation_config(request: GenerationSettings, _token: str = Depends(verify_admin_token)):
    await db.update_generation_config(**request.model_dump(exclude_none=True))
    await db.reload_config_to_memory()
    return await get_generation_config(_token)


@router.get("/api/config/pool")
async def get_pool_config(_token: str = Depends(verify_admin_token)):
    return {
        "call_mode": config.call_mode,
        "quota_preflight": config.quota_preflight,
        "error_ban_threshold": config.error_ban_threshold,
        "keepalive_enabled": config.keepalive_enabled,
        "keepalive_interval": config.keepalive_interval,
        "yescaptcha_api_key_set": bool(config.yescaptcha_api_key),
    }


@router.post("/api/config/pool")
async def set_pool_config(request: PoolSettings, _token: str = Depends(verify_admin_token)):
    await db.update_pool_config(**request.model_dump(exclude_none=True))
    await db.reload_config_to_memory()
    return await get_pool_config(_token)


@router.get("/api/config/extension")
async def get_extension_config(request: Request, _token: str = Depends(verify_admin_token)):
    settings = await db.get_extension_config()
    settings["connection_url"] = str(request.base_url).rstrip("/") + "/api/extension/push"
    return settings


@router.post("/api/config/extension")
async def set_extension_config(request: Request, settings: ExtensionSettings,
                               _token: str = Depends(verify_admin_token)):
    push_token = settings.push_token
    if settings.regenerate:
        push_token = f"ext_{secrets.token_urlsafe(24)}"
    await db.update_extension_config(
        enabled=settings.enabled, push_token=push_token,
        auto_enable_on_update=settings.auto_enable_on_update,
    )
    return await get_extension_config(request, _token)


@router.get("/api/config/proxy")
async def get_proxy_config(_token: str = Depends(verify_admin_token)):
    return await proxy_manager.get_config()


@router.post("/api/config/proxy")
async def set_proxy_config(request: ProxySettings, _token: str = Depends(verify_admin_token)):
    try:
        result = await proxy_manager.update_config(
            request.enabled, request.proxy_url
        )
    except ValueError as error:
        return _admin_error(400, str(error))
    await db.reload_config_to_memory()
    return result


@router.post("/api/proxy/test")
async def test_proxy(request: ProxyTestSettings, _token: str = Depends(verify_admin_token)):
    try:
        return await proxy_manager.test_proxy(request.proxy_url)
    except ValueError:
        return _admin_error(400, "代理地址格式无效", "invalid_proxy")


@router.get("/api/config/cache")
async def get_cache_config(request: Request, _token: str = Depends(verify_admin_token)):
    effective = config.cache_base_url or str(request.base_url).rstrip("/")
    return {
        "enabled": config.cache_enabled,
        "cache_timeout": config.cache_timeout,
        "cache_base_url": config.cache_base_url,
        "effective_base_url": effective,
        "media_url_prefix": effective + "/tmp/media/",
    }


@router.post("/api/config/cache")
async def set_cache_config(request: Request, settings: CacheSettings,
                           _token: str = Depends(verify_admin_token)):
    await db.update_cache_config(**settings.model_dump(exclude_none=True))
    await db.reload_config_to_memory()
    return await get_cache_config(request, _token)


@router.get("/api/config/debug")
async def get_debug_config(_token: str = Depends(verify_admin_token)):
    return {
        "enabled": config.debug_enabled,
        "log_requests": config.debug_log_requests,
        "log_responses": config.debug_log_responses,
        "mask_token": config.debug_mask_token,
    }


@router.post("/api/config/debug")
async def set_debug_config(request: DebugSettings, _token: str = Depends(verify_admin_token)):
    await db.update_debug_config(**request.model_dump(exclude_none=True))
    await db.reload_config_to_memory()
    return await get_debug_config(_token)


@router.get("/api/config/login")
async def get_login_config(_token: str = Depends(verify_admin_token)):
    return {
        "auto_relogin_enabled": config.auto_relogin_enabled,
        "relogin_cooldown_seconds": config.relogin_cooldown_seconds,
        "login_timeout_seconds": config.login_timeout_seconds,
    }


@router.post("/api/config/login")
async def set_login_config(request: LoginSettings, _token: str = Depends(verify_admin_token)):
    await db.update_login_config(**request.model_dump(exclude_none=True))
    await db.reload_config_to_memory()
    return await get_login_config(_token)


# ---- observability -----------------------------------------------------------
@router.get("/api/stats")
async def get_stats(_token: str = Depends(verify_admin_token)):
    return await db.get_stats_summary()


@router.get("/api/logs")
async def get_logs(limit: int = 50, offset: int = 0,
                   operation: Literal["", "image", "video", "speech"] = "",
                   status: Literal["", "completed", "processing", "failed", "unknown"] = "",
                   query: str = Query("", max_length=200),
                   days: int = Query(0, ge=0, le=30),
                   _token: str = Depends(verify_admin_token)):
    limit = max(1, min(200, limit))
    offset = max(0, offset)
    filters = dict(operation=operation, status=status, query=query.strip(), days=days)
    logs = await db.get_logs(limit, offset, **filters)
    total = await db.count_logs(**filters)
    return {
        "data": [
            {
                "id": log.id, "account_id": log.account_id, "operation": log.operation,
                "generation_id": log.generation_id, "status_code": log.status_code,
                "status_text": log.status_text, "progress": log.progress,
                "duration": log.duration, "created_at": log.created_at,
                "updated_at": log.updated_at, "account_email": log.account_email,
                "state": log.state,
            }
            for log in logs
        ],
        "total": total, "limit": limit, "offset": offset,
        "has_more": offset + len(logs) < total,
    }


@router.get("/api/logs/{log_id}")
async def get_log(log_id: int, _token: str = Depends(verify_admin_token)):
    log = await db.get_log(log_id)
    if log is None:
        return _admin_error(404, "日志不存在", "not_found")
    return {
        "id": log.id, "account_id": log.account_id, "operation": log.operation,
        "generation_id": log.generation_id,
        "request_body": db.mask_payload(log.request_body),
        "response_body": db.mask_payload(log.response_body),
        "status_code": log.status_code, "status_text": log.status_text,
        "progress": log.progress, "duration": log.duration,
        "created_at": log.created_at, "updated_at": log.updated_at,
        "account_email": log.account_email, "state": log.state,
    }


@router.get("/api/logs/{log_id}/content")
async def get_log_content(log_id: int, index: int = Query(0, ge=0, le=3),
                          _token: str = Depends(verify_admin_token)):
    from ..services.vids.gateway import GatewayError
    from ..services.vids.dispatcher import VidsDispatchError

    log = await db.get_log(log_id)
    if log is None or not log.generation_id:
        return _admin_error(404, "日志或生成回执不存在", "not_found")
    owner = await db.get_generation_owner(log.generation_id)
    if owner is None:
        owner = log.account_id
    try:
        path, mime = account_manager.dispatcher.content(owner, log.generation_id, index=index)
    except (GatewayError, VidsDispatchError) as error:
        return _admin_error(error.status, "生成结果尚未完成、已过期或不可用", error.code)
    return FileResponse(path, media_type=mime, headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
    })


@router.delete("/api/logs")
async def clear_logs(_token: str = Depends(verify_admin_token)):
    await db.clear_logs()
    return {"ok": True}


@router.get("/api/system/info")
async def system_info(_token: str = Depends(verify_admin_token)):
    return {
        "version": "1.0.0",
        "python": sys.version.split()[0],
        "accounts_dir": str(config.accounts_dir),
        "tmp_dir": str(BASE_DIR / "tmp"),
        "call_mode": config.call_mode,
    }


@router.get("/health")
async def health():
    """Public readiness snapshot; no credentials required."""
    stats = await db.get_stats_summary()
    return {
        "status": "ok",
        "accounts": {
            "total": stats["total_accounts"],
            "active": stats["active_accounts"],
            "ready": stats["ready_accounts"],
        },
    }
