"""Vids2API application assembly: singletons, lifespan, routers and static mounts."""

import asyncio
import contextlib
import os
import sys

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

from .api import admin, extension, routes
from .core.config import BASE_DIR, config
from .core.database import Database
from .core.i18n import I18nMiddleware
from .core.logger import debug_logger
from .services.account_manager import AccountManager, AccountManagerError
from .services.browser_profile import BrowserProfileService
from .services.file_cache import FileCache
from .services.generation_logger import GenerationLogger
from .services.keepalive import KeepaliveService
from .services.load_balancer import LoadBalancer
from .services.proxy_manager import ProxyManager
from .services.vids.dispatcher import VidsProviderDispatcher, VidsPoolDispatcher


def _configure_process_runtime():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    no_proxy = os.environ.get("NO_PROXY", "")
    extra = [host for host in ("127.0.0.1", "localhost", "::1") if host not in no_proxy]
    if extra:
        os.environ["NO_PROXY"] = ",".join(filter(None, [no_proxy, *extra]))
    if sys.platform.startswith("win"):
        with contextlib.suppress(Exception):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


_configure_process_runtime()

db = Database()
proxy_manager = ProxyManager(db)
dispatcher = VidsProviderDispatcher(db)
load_balancer = LoadBalancer(db)
account_manager = AccountManager(db, dispatcher, proxy_manager)
dispatcher.repair_session = account_manager.repair_session
dispatcher.ensure_proxy = account_manager.ensure_proxy
dispatcher.load_balancer = load_balancer
pool = VidsPoolDispatcher(dispatcher)
generation_logger = GenerationLogger(db)
file_cache = FileCache()
keepalive = KeepaliveService(account_manager, db)
_resume_task = None


async def _adopt_server_login(editor_url: str, cookies: str, remark: str) -> dict:
    """Import a completed server-browser login, refreshing known identities."""
    login_state = profile_service.status()
    target = login_state.get("account_id")
    login_proxy = login_state.get("login_proxy") or None
    if target is not None:
        # An automated login refreshes its original account only. A deleted
        # account must never be recreated by a late browser callback.
        await account_manager.replace_cookies(int(target), cookies)
        action, account_id = "refreshed", int(target)
    else:
        try:
            result = await account_manager.import_account(
                editor_url=editor_url, cookies=cookies, remark=remark,
                proxy_url=login_proxy or "",
            )
            action, account_id = "imported", result.get("account_id")
        except AccountManagerError as error:
            if error.code != "duplicate_account" or error.account_id is None:
                raise
            await account_manager.replace_cookies(error.account_id, cookies, proxy_url=login_proxy)
            action, account_id = "refreshed", error.account_id
    pending = profile_service.consume_pending_credentials()
    if pending is not None and account_id is not None:
        try:
            await account_manager.save_google_credentials(
                int(account_id), pending.get("email", ""),
                pending.get("password", ""), pending.get("totp", ""),
            )
        except Exception as error:  # noqa: BLE001 - credential storage is best-effort
            debug_logger.log_error("server-login-credentials", error)
        login_proxy = str(pending.get("proxy", "")).strip()
        if login_proxy:
            try:
                # Persist the login proxy so future harvests and auto-relogins
                # reach Google through the same network path.
                await db.update_account_fields(int(account_id), proxy_url=login_proxy)
            except Exception as error:  # noqa: BLE001 - proxy storage is best-effort
                debug_logger.log_error("server-login-proxy", error)
    if target is not None and await db.get_account(int(target)) is None:
        raise AccountManagerError(404, "account_not_found", "账号不存在")
    return {"action": action, "account_id": account_id}


profile_service = BrowserProfileService(BASE_DIR / "data", _adopt_server_login)
account_manager.profile_service = profile_service

routes.set_dependencies(
    logger=generation_logger,
    pool_instance=pool,
    account_manager_instance=account_manager,
    file_cache_instance=file_cache,
)
admin.set_dependencies(account_manager, proxy_manager, db)
extension.set_dependencies(account_manager, db)

STATIC_DIR = BASE_DIR / "static"


async def _startup():
    global _resume_task
    if not db.db_exists():
        await db.init_db(config._data)
    else:
        await db.check_and_migrate_db(config._data)
    await db.reload_config_to_memory()
    config.accounts_dir.mkdir(parents=True, exist_ok=True)
    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    account_manager._key = account_manager.load_or_create_key()
    await account_manager.restore_receipts()
    await db._execute("UPDATE accounts SET status = 'starting' WHERE is_active = 1")
    await file_cache.start_cleanup_loop()

    async def warm_accounts():
        try:
            result = await account_manager.resume_all(restore_receipts=False)
            if result["failed"]:
                debug_logger.log_info("startup", f"session resume failures: {result['failed']}")
        except Exception as error:
            debug_logger.log_error("startup-resume", error)
        keepalive.start()

    _resume_task = asyncio.create_task(warm_accounts(), name="vids2api-resume")


async def _shutdown():
    global _resume_task
    if _resume_task is not None:
        _resume_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _resume_task
        _resume_task = None
    await keepalive.stop()
    await profile_service.shutdown()
    await file_cache.stop_cleanup_loop()
    for account in list(dispatcher._accounts.values()):
        with contextlib.suppress(Exception):
            await account.gateway.close()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        await _startup()
        yield
    finally:
        await _shutdown()


app = FastAPI(title="Vids2API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(I18nMiddleware)
app.include_router(routes.router)
app.include_router(admin.router)
app.include_router(extension.router)

app.mount("/tmp/media", StaticFiles(directory=str(file_cache.cache_dir)), name="tmp_media")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def index():
    return _no_store(FileResponse(STATIC_DIR / "login.html"))


@app.get("/login")
async def login_page():
    return _no_store(FileResponse(STATIC_DIR / "login.html"))


@app.get("/manage")
async def manage_page(request: Request):
    token = request.cookies.get("admin_session", "")
    from .core.auth import is_admin_session_token_valid

    if not is_admin_session_token_valid(token):
        return RedirectResponse("/login", status_code=302)
    return _no_store(FileResponse(STATIC_DIR / "manage.html"))


@app.get("/test")
async def test_page(request: Request):
    token = request.cookies.get("admin_session", "")
    from .core.auth import is_admin_session_token_valid

    if not is_admin_session_token_valid(token):
        return RedirectResponse("/login", status_code=302)
    return _no_store(FileResponse(STATIC_DIR / "test.html"))


@app.get("/api-guide")
async def api_guide_page():
    return _no_store(FileResponse(STATIC_DIR / "api-guide.html"))


@app.exception_handler(404)
async def not_found_handler(_request: Request, _exc):
    return JSONResponse(status_code=404, content={"error": {"message": "Not found"}})
