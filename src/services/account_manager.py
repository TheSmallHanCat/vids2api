"""Vids account lifecycle: cookie import, session resume, repair, quota and admission stats."""

import asyncio
import hashlib
import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from cryptography.fernet import Fernet

from ..core.config import config
from ..core.logger import debug_logger
from .proxy_manager import normalize_proxy_url
from .vids.client import VidsClient, VidsProtocolError, VidsTransportError, close_session, cookie_rows
from .vids.dispatcher import VidsAccount, VidsProviderDispatcher
from .vids.gateway import GatewayError, VidsGateway
from .vids.session_store import DbSessionStore, SessionStoreError, session_io


class _EditorLinkParser(HTMLParser):
    """Collect anchor targets from the Vids home page."""

    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.extend(value for name, value in attrs if name == "href" and value)


class AccountManagerError(Exception):
    def __init__(self, status: int, code: str, message: str, account_id: int | None = None):
        self.status, self.code = status, code
        self.account_id = account_id
        super().__init__(message)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_cookie_input(raw: str) -> list[dict]:
    """Accept a JSON cookie export (list of cookie objects) or a raw Cookie header string."""
    text = (raw or "").strip()
    if not text:
        raise AccountManagerError(400, "invalid_cookies", "Cookie 输入为空")
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            raise AccountManagerError(400, "invalid_cookies", "Cookie JSON 解析失败") from None
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            data = data["cookies"]
        if not isinstance(data, list) or not data:
            raise AccountManagerError(400, "invalid_cookies", "Cookie JSON 必须是对象数组")
        rows = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name, value = item.get("name"), item.get("value")
            domain = item.get("domain") or ".google.com"
            if not isinstance(name, str) or not isinstance(value, str) or not name:
                continue
            row = {
                "name": name,
                "value": value,
                "domain": domain if domain.startswith(".") else "." + domain,
                "path": item.get("path") or "/",
                "secure": bool(item.get("secure", True)),
                "httpOnly": bool(item.get("httpOnly", False)),
                "hostOnly": False,
            }
            expires = item.get("expirationDate", item.get("expires"))
            if expires is not None and expires != -1:
                row["expires"] = expires
                row["session"] = False
            else:
                row["session"] = True
            rows.append(row)
        if not rows:
            raise AccountManagerError(400, "invalid_cookies", "未解析到有效 Cookie")
        return rows
    rows = []
    for part in text.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or not value:
            continue
        if any(ch in name for ch in "=; \t\r\n\0") or any(ch in value for ch in ";\r\n\0"):
            continue
        rows.append(
            {
                "name": name,
                "value": value,
                "domain": ".google.com",
                "path": "/",
                "secure": True,
                "httpOnly": False,
                "hostOnly": False,
                "session": True,
            }
        )
    if not rows:
        raise AccountManagerError(400, "invalid_cookies", "未解析到有效 Cookie")
    return rows


async def _close_client_session(client) -> None:
    """Close a curl_cffi session, tolerating its idle-close TypeError.

    curl-cffi 0.13 clears the multi handle after the last transfer; closing an
    idle session afterwards raises ``TypeError: initializer for ctype 'void *'
    must be a cdata pointer`` even though the underlying sockets are already
    released. Treat that as a successful close.
    """
    await close_session(client.session)


class AccountManager:
    """Owns gateway construction and DB synchronization for the account pool."""

    def __init__(self, db, dispatcher: VidsProviderDispatcher, proxy_manager=None, profile_service=None):
        self.db = db
        self.dispatcher = dispatcher
        self.proxy_manager = proxy_manager
        self.profile_service = profile_service
        self._key: bytes | None = None
        self._lifecycle_locks: dict[int, asyncio.Lock] = {}
        self._repair_locks: dict[int, asyncio.Lock] = {}

    # ---- encryption key ---------------------------------------------------
    def load_or_create_key(self) -> bytes:
        path = config.session_key_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            value = path.read_bytes()
            Fernet(value)
            return value
        import os

        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            key = Fernet.generate_key()
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        return key

    @property
    def key(self) -> bytes:
        if self._key is None:
            self._key = self.load_or_create_key()
        return self._key

    def _session_store(self, account_id: int) -> DbSessionStore:
        return DbSessionStore(self.db.db_path, account_id, self.key)

    def account_root(self, account_id: int) -> Path:
        return config.accounts_dir / str(int(account_id))

    async def _resolve_proxy(self, proxy_url: str | None) -> str | None:
        if proxy_url:
            return normalize_proxy_url(proxy_url)
        if self.proxy_manager is not None:
            return await self.proxy_manager.get_effective_proxy()
        return None

    async def effective_proxy(self) -> str | None:
        return await self._resolve_proxy(None)

    async def _account_proxy(self, account_id: int):
        account = await self.db.get_account(account_id)
        if account is None:
            raise AccountManagerError(404, "account_not_found", "账号不存在")
        return await self._resolve_proxy(account.proxy_url or None)

    async def ensure_proxy(self, account_id: int):
        registered = self.dispatcher._accounts.get(account_id)
        if registered is None:
            return
        gateway = registered.gateway
        try:
            async with gateway.lock:
                proxy = await self._account_proxy(account_id)
                if (self.dispatcher._accounts.get(account_id) is not registered
                        or getattr(gateway, "_closed", False)):
                    raise GatewayError(503, "account_disabled", "Vids account is unavailable")
                if gateway.client.proxy != proxy:
                    await gateway._replace_cookies(cookie_rows(gateway.client.session.cookies.jar), proxy=proxy)
        except AccountManagerError:
            raise GatewayError(503, "account_disabled", "Vids account is unavailable") from None
        except (VidsProtocolError, VidsTransportError, ValueError):
            raise GatewayError(503, "proxy_unavailable", "Vids proxy session could not be verified") from None

    @staticmethod
    async def _bootstrap_client(editor_url, rows, proxy):
        client = VidsClient(editor_url, rows, proxy=proxy)
        try:
            await client.bootstrap()
        except BaseException:
            await _close_client_session(client)
            raise
        return client

    # ---- google login credentials -----------------------------------------
    def _encrypt(self, value: str) -> str:
        return Fernet(self.key).encrypt(value.encode()).decode()

    def _decrypt(self, value: str) -> str:
        return Fernet(self.key).decrypt(value.encode()).decode()

    async def save_google_credentials(
        self, account_id: int, email: str, password: str, totp_secret: str = ""
    ) -> None:
        saved = await self.db.save_google_credentials(
            int(account_id),
            email,
            self._encrypt(password),
            self._encrypt(totp_secret) if totp_secret else "",
        )
        if not saved:
            raise AccountManagerError(404, "account_not_found", "账号不存在")

    async def get_google_credentials(self, account_id: int) -> dict | None:
        stored = await self.db.get_google_credentials(account_id)
        if stored is None or not stored["password_enc"]:
            return None
        return {
            "email": stored["email"],
            "password": self._decrypt(stored["password_enc"]),
            "totp": self._decrypt(stored["totp_enc"]) if stored["totp_enc"] else "",
        }

    async def discover_editor_url(self, cookies: str) -> str | None:
        """Locate an existing Vids document URL from the account home page.

        Returns None when the session is invalid or the account owns no
        Vids document yet; callers decide how to create one.
        """
        from curl_cffi.requests import AsyncSession

        from .vids.client import DOCS_ORIGIN, cookie_jar, parse_flags

        rows = parse_cookie_input(cookies)
        proxy = await self._resolve_proxy(None)
        home = f"{DOCS_ORIGIN}/videos/?authuser=0"
        editor_re = re.compile(r"^/videos/(?:u/\d+/)?d/[A-Za-z0-9_-]+/edit$")
        async with AsyncSession(
            cookies=cookie_jar(rows),
            proxy=proxy,
            timeout=45,
            trust_env=False,
        ) as session:
            response = await session.get(home)
        final = urlsplit(str(response.url))
        if response.status_code != 200 or final.hostname != "docs.google.com":
            return None
        if "@" not in str(parse_flags(response.text).get("docs-hue", "")):
            return None
        parser = _EditorLinkParser()
        parser.feed(response.text)
        for href in parser.links:
            address = urlsplit(urljoin(home, href))
            if address.hostname == "docs.google.com" and editor_re.fullmatch(address.path):
                return f"{DOCS_ORIGIN}{address.path}?authuser=0"
        return None

    # ---- import -----------------------------------------------------------
    async def import_account(
        self,
        *,
        editor_url: str,
        cookies: str,
        remark: str = "",
        proxy_url: str = "",
        image_enabled: bool = True,
        video_enabled: bool = True,
        speech_enabled: bool = True,
    ) -> dict:
        rows = parse_cookie_input(cookies)
        proxy = await self._resolve_proxy(proxy_url or None)
        client = None
        try:
            client = await self._bootstrap_client(editor_url, rows, proxy)
        except GatewayError as error:
            if client is not None:
                await _close_client_session(client)
            raise AccountManagerError(error.status, error.code, str(error)) from None
        except ValueError as error:
            if client is not None:
                await _close_client_session(client)
            raise AccountManagerError(400, "invalid_editor_url", str(error)) from None
        except Exception as error:
            if client is not None:
                await _close_client_session(client)
            debug_logger.log_error("import", error)
            raise AccountManagerError(
                502, "bootstrap_failed", "上游编辑器初始化失败，请检查 Cookie 与网络"
            ) from None
        account_id = None
        registered = False
        try:
            identity = client.flags.get("docs-hue")
            if not isinstance(identity, str) or "@" not in identity:
                raise AccountManagerError(
                    400, "identity_missing", "编辑器会话缺少账号身份（docs-hue），Cookie 可能已失效"
                )
            identity_hash = hashlib.sha256(identity.encode()).hexdigest()
            existing = await self.db.get_account_by_identity(identity_hash)
            if existing is not None:
                raise AccountManagerError(
                    409,
                    "duplicate_account",
                    f"该账号已存在（#{existing.id} {existing.email}）",
                    account_id=existing.id,
                )
            try:
                account_id = await self.db.add_account(
                    identity_hash=identity_hash,
                    editor_url=client.editor_url,
                    auth_user=client.auth_user,
                    email=identity,
                    session_enc="",
                    remark=remark,
                    proxy_url=proxy_url,
                )
            except sqlite3.IntegrityError:
                existing = await self.db.get_account_by_identity(identity_hash)
                if existing is None:
                    raise
                raise AccountManagerError(
                    409, "duplicate_account", f"该账号已存在（#{existing.id} {existing.email}）",
                    account_id=existing.id,
                ) from None
            await self.db.update_account_fields(
                account_id,
                image_enabled=image_enabled,
                video_enabled=video_enabled,
                speech_enabled=speech_enabled,
            )
            gateway = VidsGateway(
                client,
                self.account_root(account_id),
                session_store=self._session_store(account_id),
                proxy=proxy,
                expected_account_hash=identity_hash,
                expected_editor_url=client.editor_url,
            )
            try:
                await gateway.start()
            except Exception as error:
                debug_logger.log_error("import", error)
                raise AccountManagerError(
                    502, "start_failed", "账号会话启动失败，请检查 Cookie 有效性与配额"
                ) from None
            try:
                self.dispatcher.register(VidsAccount(account_id, gateway, identity_hash))
            except BaseException as error:
                await gateway.close()
                if isinstance(error, ValueError):
                    raise AccountManagerError(409, "duplicate_account", str(error)) from None
                raise
            registered = True
            await self.db.update_account_fields(account_id, status="ready", last_error="")
            await self.refresh_quota(account_id)
            return {
                "account_id": account_id,
                "email": identity,
                "editor_url": client.editor_url,
                "state": "ready",
            }
        finally:
            if not registered:
                try:
                    await _close_client_session(client)
                except Exception as error:
                    debug_logger.log_error("import-close", error)
                if account_id is not None:
                    try:
                        await self.db.delete_account(account_id)
                    except Exception as error:
                        debug_logger.log_error("import-cleanup", error)

    # ---- startup recovery ---------------------------------------------------
    async def restore_receipts(self, accounts=None):
        """Restore local ownership before any account performs network activity."""
        accounts = await self.db.get_accounts() if accounts is None else accounts
        roots = {account.id: self.account_root(account.id) for account in accounts}
        self.dispatcher.unavailable_receipt_roots.update(
            {account_id: root for account_id, root in roots.items() if root.is_dir()}
        )

        def read_receipts():
            receipts = []
            for account_id, root in roots.items():
                for path in root.glob("vids_*.json"):
                    if not re.fullmatch(r"vids_[a-f0-9]{64}", path.stem):
                        continue
                    try:
                        record = json.loads(path.read_text(encoding="utf-8"))
                        if not isinstance(record, dict) or record.get("id") != path.stem:
                            record = {}
                    except (OSError, ValueError):
                        record = {}
                    receipts.append((path.stem, account_id, record))
            return receipts

        receipts = await asyncio.to_thread(read_receipts)
        await self.db.index_legacy_receipts([
            (generation_id, account_id,
             "success" if record.get("status") == "completed" else
             "error" if record.get("status") in {"failed", "no_result", "specification_mismatch"} else "")
            for generation_id, account_id, record in receipts
        ])
        claims = await self.db.get_generation_claims()
        self.dispatcher._claims.update(
            {generation_id.removeprefix("vids_"): owner for generation_id, owner in claims.items()}
        )
        for generation_id, account_id, record in receipts:
            if claims[generation_id] != account_id:
                raise AccountManagerError(409, "account_affinity_conflict", "收据账号归属冲突")
        self.dispatcher._receipt_index_ready = True
        pending = await self.db._fetchall("SELECT generation_id FROM generation_claims WHERE outcome = ''")
        pending_ids = {row["generation_id"] for row in pending}
        for generation_id, account_id, record in receipts:
            if generation_id in pending_ids:
                await self.record_receipt(account_id, record)

    async def resume_all(self, *, restore_receipts=True) -> dict:
        succeeded, failed = [], []
        accounts = await self.db.get_accounts()
        if restore_receipts:
            await self.restore_receipts(accounts)
        semaphore = asyncio.Semaphore(4)

        async def resume(account):
            if not account.is_active and not account.ban_reason.startswith("consecutive_errors:"):
                return
            try:
                async with semaphore:
                    await self._resume_account(account)
                succeeded.append(account.id)
            except Exception as error:
                debug_logger.log_error("resume", f"account {account.id}: {error}")
                await self.db.update_account_fields(
                    account.id, status="session_error", last_error=str(error)[:500]
                )
                root = self.account_root(account.id)
                if root.exists():
                    self.dispatcher.unavailable_receipt_roots[account.id] = root
                failed.append(account.id)
        await asyncio.gather(*(resume(account) for account in accounts))
        return {"resumed": succeeded, "failed": failed}

    async def _resume_account(self, account) -> VidsGateway:
        async with self._lifecycle_locks.setdefault(account.id, asyncio.Lock()):
            registered = self.dispatcher._accounts.get(account.id)
            if registered is not None:
                return registered.gateway
            return await self._resume_locked(account)

    async def _resume_locked(self, account) -> VidsGateway:
        gateway = VidsGateway(
            None,
            self.account_root(account.id),
            session_store=self._session_store(account.id),
            resume=True,
            proxy=await self._resolve_proxy(account.proxy_url or None),
            expected_account_hash=account.identity_hash,
            expected_editor_url=account.editor_url,
        )
        await gateway.start()
        try:
            self.dispatcher.register(VidsAccount(account.id, gateway, account.identity_hash))
        except BaseException:
            await gateway.close()
            raise
        await self.db.update_account_fields(account.id, status="ready", last_error="")
        return gateway

    async def start_account(self, account_id: int) -> dict:
        """Retry a session_error account from its stored encrypted session."""
        account = await self.db.get_account(account_id)
        if account is None:
            raise AccountManagerError(404, "account_not_found", "账号不存在")
        if account.id in self.dispatcher._accounts:
            raise AccountManagerError(409, "already_started", "账号已在运行中")
        try:
            await self._resume_account(account)
        except AccountManagerError:
            raise
        except Exception as error:
            debug_logger.log_error("start", f"account {account_id}: {error}")
            await self.db.update_account_fields(
                account_id, status="session_error", last_error=str(error)[:500]
            )
            raise AccountManagerError(
                502, "start_failed", "会话恢复失败，请尝试更换 Cookie"
            ) from None
        await self.refresh_quota(account_id)
        return {"account_id": account_id, "state": "ready"}

    # ---- removal / switches -------------------------------------------------
    async def remove_account(self, account_id: int) -> dict:
        async with self._lifecycle_locks.setdefault(account_id, asyncio.Lock()):
            return await self._remove_account(account_id)

    async def _remove_account(self, account_id: int) -> dict:
        account = await self.db.get_account(account_id)
        if account is None:
            raise AccountManagerError(404, "account_not_found", "账号不存在")
        registered = self.dispatcher._accounts.get(account_id)
        root = self.dispatcher.unregister(account_id)
        if root is None:
            root = self.account_root(account_id)
        if registered is not None:
            try:
                await registered.gateway.close()
            except Exception as error:
                debug_logger.log_error("remove", error)
        self.dispatcher.unavailable_receipt_roots.pop(account_id, None)
        await self.db.delete_account(account_id)
        if Path(root).exists():
            await asyncio.to_thread(shutil.rmtree, root)
        if self.profile_service is not None:
            await self.profile_service.remove_profile(account_id)
        return {"account_id": account_id, "removed": True}

    async def set_account_enabled(self, account_id: int, enabled: bool) -> dict:
        account = await self.db.get_account(account_id)
        if account is None:
            raise AccountManagerError(404, "account_not_found", "账号不存在")
        if enabled:
            await self.db.unban_account(account_id)
            await self.db.update_account_fields(
                account_id, is_active=True, ban_reason="", status="ready"
            )
        else:
            await self.db.update_account_fields(
                account_id, is_active=False, status="disabled", ban_reason="", banned_at=None
            )
        return {"account_id": account_id, "enabled": enabled}

    async def update_account(
        self, account_id: int, *, remark=None, proxy_url=None, image_enabled=None,
        video_enabled=None, speech_enabled=None,
    ) -> dict:
        account = await self.db.get_account(account_id)
        if account is None:
            raise AccountManagerError(404, "account_not_found", "账号不存在")
        fields = {}
        if remark is not None:
            fields["remark"] = str(remark)[:500]
        if proxy_url is not None:
            try:
                fields["proxy_url"] = normalize_proxy_url(str(proxy_url).strip())
            except ValueError:
                raise AccountManagerError(400, "invalid_proxy", "代理地址格式无效") from None
        if image_enabled is not None:
            fields["image_enabled"] = bool(image_enabled)
        if video_enabled is not None:
            fields["video_enabled"] = bool(video_enabled)
        if speech_enabled is not None:
            fields["speech_enabled"] = bool(speech_enabled)
        if fields:
            await self.db.update_account_fields(account_id, **fields)
        return {"account_id": account_id, "updated": True}

    # ---- cookies & quota ------------------------------------------------------
    async def replace_cookies(self, account_id: int, cookies: str, *, proxy_url=None) -> dict:
        registered = self.dispatcher._accounts.get(account_id)
        if registered is None:
            return await self.replace_account_session(account_id, cookies, proxy_url=proxy_url)
        rows = parse_cookie_input(cookies)
        try:
            result = await registered.gateway.replace_cookies(
                rows, proxy_resolver=(lambda: self._account_proxy(account_id)) if proxy_url is None
                else (lambda: self._resolve_proxy(proxy_url or None)),
            )
        except AccountManagerError:
            raise
        except GatewayError as error:
            await self.db.update_account_fields(
                account_id, status="session_error", last_error=error.code
            )
            raise AccountManagerError(error.status, error.code, str(error)) from None
        except Exception as error:
            debug_logger.log_error("replace-cookies", error)
            raise AccountManagerError(502, "replace_failed", "更换 Cookie 失败") from None
        fields = {} if proxy_url is None else {"proxy_url": normalize_proxy_url(proxy_url)}
        await self.db.update_account_fields(account_id, status="ready", last_error="", **fields)
        await self.refresh_quota(account_id)
        return {"account_id": account_id, "updated": True, "detail": result}

    async def replace_account_session(self, account_id: int, cookies: str, *, proxy_url=None) -> dict:
        """Rebuild a NOT-running account's gateway directly from fresh cookies.

        Used when a credential refresh targets an account whose previous
        session died before the process registered it (boot-time resume
        failure): the stored snapshot is unusable, so the gateway is built
        from the freshly supplied cookies instead.
        """
        async with self._lifecycle_locks.setdefault(account_id, asyncio.Lock()):
            return await self._replace_account_session(account_id, cookies, proxy_url=proxy_url)

    async def _replace_account_session(self, account_id: int, cookies: str, *, proxy_url=None) -> dict:
        account = await self.db.get_account(account_id)
        if account is None:
            raise AccountManagerError(404, "account_not_found", "账号不存在")
        if account.id in self.dispatcher._accounts:
            return await self.replace_cookies(account_id, cookies, proxy_url=proxy_url)
        rows = parse_cookie_input(cookies)
        proxy = await self._resolve_proxy((account.proxy_url if proxy_url is None else proxy_url) or None)
        client = None
        try:
            client = await self._bootstrap_client(account.editor_url, rows, proxy)
        except GatewayError as error:
            if client is not None:
                await _close_client_session(client)
            raise AccountManagerError(error.status, error.code, str(error)) from None
        except ValueError as error:
            if client is not None:
                await _close_client_session(client)
            raise AccountManagerError(400, "invalid_editor_url", str(error)) from None
        except Exception as error:
            if client is not None:
                await _close_client_session(client)
            debug_logger.log_error("replace-session", error)
            raise AccountManagerError(
                502, "bootstrap_failed", "上游编辑器初始化失败，请检查 Cookie 与网络"
            ) from None
        identity = client.flags.get("docs-hue")
        identity_hash = hashlib.sha256(str(identity).encode()).hexdigest()
        if identity_hash != account.identity_hash:
            await _close_client_session(client)
            raise AccountManagerError(
                400, "identity_mismatch", "新 Cookie 的账号身份与该账号不一致"
            )
        store = self._session_store(account_id)
        gateway = VidsGateway(
            client,
            self.account_root(account_id),
            session_store=store,
            proxy=proxy,
            expected_account_hash=account.identity_hash,
            expected_editor_url=client.editor_url,
            replace_session=True,
        )
        try:
            await gateway.start()
        except Exception as error:
            await _close_client_session(client)
            debug_logger.log_error("replace-session", error)
            raise AccountManagerError(
                502, "start_failed", "账号会话启动失败，请检查 Cookie 有效性与配额"
            ) from None
        try:
            self.dispatcher.register(VidsAccount(account_id, gateway, account.identity_hash))
        except ValueError as error:
            await gateway.close()
            raise AccountManagerError(409, "duplicate_account", str(error)) from None
        fields = {} if proxy_url is None else {"proxy_url": normalize_proxy_url(proxy_url)}
        await self.db.update_account_fields(account_id, status="ready", last_error="", **fields)
        await self.refresh_quota(account_id)
        return {"account_id": account_id, "updated": True}

    async def refresh_quota(self, account_id: int, kinds=("image", "video", "speech")) -> dict:
        registered = self.dispatcher._accounts.get(account_id)
        if registered is None:
            raise AccountManagerError(404, "account_not_found", "账号未在运行")
        await self.ensure_proxy(account_id)
        fields = {}
        results = {}
        for kind in kinds:
            try:
                quota = await registered.gateway.quota(kind)
            except Exception as error:
                debug_logger.log_error("quota", f"account {account_id} {kind}: {error}")
                continue
            prefix = f"quota_{kind}"
            fields.update(
                {
                    f"{prefix}_total": quota["total"],
                    f"{prefix}_available": quota["available"],
                    f"{prefix}_checked_at": _now(),
                }
            )
            results[kind] = quota
        if fields:
            await self.db.update_account_fields(account_id, **fields)
        return results

    async def repair_session(self, account_id: int) -> bool:
        """Dispatcher callback: rebuild the client from the stored session snapshot,
        falling back to a headless harvest from the server-side browser profile."""
        async with self._repair_locks.setdefault(account_id, asyncio.Lock()):
            return await self._repair_session(account_id)

    async def _repair_session(self, account_id: int) -> bool:
        registered = self.dispatcher._accounts.get(account_id)
        if registered is None:
            raise AccountManagerError(404, "account_not_found", "账号未在运行")
        store = self._session_store(account_id)
        snapshot_error = None
        try:
            saved = await session_io(store.load)
        except SessionStoreError:
            saved = None
        account = await self.db.get_account(account_id)
        if account is None:
            raise AccountManagerError(404, "account_not_found", "账号不存在")
        proxy = await self._resolve_proxy(account.proxy_url or None)
        if saved is not None:
            try:
                await registered.gateway.replace_cookies(
                    saved["cookies"], proxy_resolver=lambda: self._account_proxy(account_id),
                )
                await self.db.update_account_fields(account_id, status="ready", last_error="")
                return True
            except (GatewayError, VidsProtocolError, VidsTransportError, ValueError) as error:
                snapshot_error = self._session_error(error)
        rows = await self._harvest_profile(account_id)
        if rows:
            try:
                await registered.gateway.replace_cookies(
                    rows, proxy_resolver=lambda: self._account_proxy(account_id),
                )
                debug_logger.log_info("repair", f"account {account_id}: session rebuilt from browser profile")
                await self.db.update_account_fields(account_id, status="ready", last_error="")
                return True
            except (GatewayError, VidsProtocolError, VidsTransportError, ValueError) as error:
                snapshot_error = snapshot_error or self._session_error(error)
        if self.profile_service is not None and config.auto_relogin_enabled:
            credentials = await self.get_google_credentials(account_id)
            if credentials is not None:
                # BrowserProfileService owns the cooldown and does not consume
                # an attempt while another login is using the browser.
                try:
                    if await self.profile_service.auto_relogin(account_id, credentials, proxy):
                        debug_logger.log_info(
                            "repair", f"account {account_id}: session rebuilt via automated re-login"
                        )
                        await self.db.update_account_fields(account_id, status="ready", last_error="")
                        return True
                except Exception as error:  # noqa: BLE001 - re-login is best-effort
                    debug_logger.log_error("auto-relogin", f"account {account_id}: {error}")
        if snapshot_error is not None:
            await self.db.update_account_fields(
                account_id, status="session_error", last_error=snapshot_error.code
            )
            raise snapshot_error
        await self.db.update_account_fields(
            account_id, status="session_error", last_error="session_missing"
        )
        raise AccountManagerError(
            503, "session_missing", "没有已保存的会话，且浏览器 Profile 不可用"
        )

    @staticmethod
    def _session_error(error):
        if isinstance(error, GatewayError):
            return error
        return AccountManagerError(503, "session_unavailable", "会话验证失败，请重新同步登录状态")

    async def record_receipt(self, account_id: int, record: dict):
        if record.get("operation") == "insert" or record.get("kind") not in {"image", "video", "speech"}:
            return
        generation_id = record.get("id", "")
        if not isinstance(generation_id, str) or not re.fullmatch(r"vids_[a-f0-9]{64}", generation_id):
            return
        status = record.get("status")
        if status not in {"completed", "failed", "no_result", "specification_mismatch"}:
            return
        error = str(record.get("error") or "")
        settled = await self.db.settle_generation(
            generation_id, account_id, record["kind"], status == "completed", error,
            ban_threshold=config.error_ban_threshold,
        )
        return settled

    async def _harvest_profile(self, account_id: int) -> list[dict] | None:
        if self.profile_service is None:
            return None
        account = await self.db.get_account(account_id)
        if account is None or not getattr(account, "editor_url", ""):
            return None
        proxy = await self._resolve_proxy(account.proxy_url or None)
        try:
            return await self.profile_service.harvest(account_id, account.editor_url, proxy)
        except Exception as error:  # noqa: BLE001 - harvest is best-effort
            debug_logger.log_error("profile-harvest", f"account {account_id}: {error}")
            return None

    # ---- stats ----------------------------------------------------------------
    async def record_success(self, account_id: int, kind: str):
        await self.db.record_account_result(account_id, kind, True)
        await self.db.touch_account_used(account_id, kind)
        try:
            await self.db.consume_account_quota(account_id, kind)
        except Exception as error:  # noqa: BLE001 - accounting must not fail the result
            debug_logger.log_error("quota-decrement", f"account {account_id} {kind}: {error}")
        # A paid success proves the session works; lift any automatic
        # consecutive-error ban so the account serves new traffic immediately.
        account = await self.db.get_account(account_id)
        if account is not None and not account.is_active and account.ban_reason.startswith(
            "consecutive_errors:"
        ):
            await self.db.unban_account(account_id)
            debug_logger.log_info("unban", f"account {account_id}: success lifted auto ban")

    async def record_error(self, account_id: int, kind: str, code: str = ""):
        # Transport-grade losses (acceptance unknown) describe the network, not
        # the account; they must not feed the consecutive-error ban.
        if code.startswith("acceptance_unknown"):
            await self.db.update_account_fields(account_id, last_error=code[:500])
            return
        await self.db.record_account_result(account_id, kind, False)
        await self.db.update_account_fields(account_id, last_error=code[:500])
        account = await self.db.get_account(account_id)
        if account is not None and account.consecutive_error_count >= config.error_ban_threshold:
            await self.db.ban_account(account_id, f"consecutive_errors:{code or 'unknown'}")

    async def test_account(self, account_id: int) -> dict:
        """Lightweight readiness probe: identity check plus a quota read."""
        registered = self.dispatcher._accounts.get(account_id)
        if registered is None:
            raise AccountManagerError(404, "account_not_found", "账号未在运行")
        try:
            await self.ensure_proxy(account_id)
            quota = await registered.gateway.quota("video")
        except GatewayError as error:
            return {"account_id": account_id, "ready": False, "error": error.code}
        except Exception as error:
            return {"account_id": account_id, "ready": False, "error": str(error)[:200]}
        return {"account_id": account_id, "ready": True, "quota": quota}
