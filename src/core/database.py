"""aiosqlite persistence: accounts pool, stats, request logs and single-row config tables.

Configuration tables are seeded from the TOML file on first boot only; afterwards
they are the runtime source of truth and hot-reload into the in-memory config.
"""

import asyncio
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiosqlite

from ..core.config import BASE_DIR, config
from .settings import CacheSettings, DebugSettings, GenerationSettings, LoginSettings, PoolSettings
from ..core.models import (
    Account,
    RequestLog,
)

DB_PATH = BASE_DIR / "data" / "vids2api.db"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Database:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = asyncio.Lock()

    # ---- connection -------------------------------------------------------
    def _connect_async(self):
        return aiosqlite.connect(str(self.db_path), timeout=30)

    async def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        async with self._write_lock:
            async with self._connect_async() as db:
                await db.execute("PRAGMA busy_timeout=30000")
                cursor = await db.execute(sql, params)
                await db.commit()
                return cursor

    async def _fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        async with self._connect_async() as db:
            await db.execute("PRAGMA busy_timeout=30000")
            async with db.execute(sql, params) as cursor:
                cursor.row_factory = sqlite3.Row
                return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        async with self._connect_async() as db:
            await db.execute("PRAGMA busy_timeout=30000")
            async with db.execute(sql, params) as cursor:
                cursor.row_factory = sqlite3.Row
                return list(await cursor.fetchall())

    def db_exists(self) -> bool:
        return self.db_path.exists()

    # ---- schema -----------------------------------------------------------
    async def init_db(self, config_dict: dict | None = None):
        seed = config_dict or {}
        async with self._connect_async() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await self._create_tables(db)
            await self._ensure_config_rows(db, seed)
            await db.commit()

    async def check_and_migrate_db(self, config_dict: dict | None = None):
        seed = config_dict or {}
        async with self._connect_async() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await self._create_tables(db)
            await self._migrate_columns(db)
            await self._ensure_config_rows(db, seed, overwrite=False)
            await db.commit()

    async def _create_tables(self, db):
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL DEFAULT '',
                identity_hash TEXT NOT NULL UNIQUE,
                editor_url TEXT NOT NULL,
                auth_user INTEGER NOT NULL DEFAULT 0,
                session_enc TEXT NOT NULL DEFAULT '',
                remark TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                image_enabled INTEGER NOT NULL DEFAULT 1,
                video_enabled INTEGER NOT NULL DEFAULT 1,
                speech_enabled INTEGER NOT NULL DEFAULT 1,
                proxy_url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'starting',
                last_error TEXT NOT NULL DEFAULT '',
                use_count INTEGER NOT NULL DEFAULT 0,
                image_count INTEGER NOT NULL DEFAULT 0,
                video_count INTEGER NOT NULL DEFAULT 0,
                speech_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                consecutive_error_count INTEGER NOT NULL DEFAULT 0,
                quota_image_total INTEGER,
                quota_image_available INTEGER,
                quota_image_checked_at TEXT,
                quota_video_total INTEGER,
                quota_video_available INTEGER,
                quota_video_checked_at TEXT,
                quota_speech_total INTEGER,
                quota_speech_available INTEGER,
                quota_speech_checked_at TEXT,
                created_at TEXT NOT NULL DEFAULT '',
                last_used_at TEXT,
                banned_at TEXT,
                ban_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts(is_active, last_used_at);
            CREATE TABLE IF NOT EXISTS account_stats (
                account_id INTEGER PRIMARY KEY,
                today_date TEXT NOT NULL DEFAULT '',
                today_image INTEGER NOT NULL DEFAULT 0,
                today_video INTEGER NOT NULL DEFAULT 0,
                today_speech INTEGER NOT NULL DEFAULT 0,
                today_error INTEGER NOT NULL DEFAULT 0,
                total_image INTEGER NOT NULL DEFAULT 0,
                total_video INTEGER NOT NULL DEFAULT 0,
                total_speech INTEGER NOT NULL DEFAULT 0,
                total_error INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                operation TEXT NOT NULL DEFAULT '',
                generation_id TEXT,
                request_body TEXT NOT NULL DEFAULT '',
                response_body TEXT NOT NULL DEFAULT '',
                status_code INTEGER,
                status_text TEXT NOT NULL DEFAULT '',
                progress INTEGER NOT NULL DEFAULT 0,
                duration REAL,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_request_logs_created
                ON request_logs(created_at DESC);
            CREATE TABLE IF NOT EXISTS generation_claims (
                generation_id TEXT PRIMARY KEY,
                account_id INTEGER NOT NULL,
                outcome TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL DEFAULT 'admin',
                password TEXT NOT NULL DEFAULT 'admin',
                api_key TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS generation_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                image_timeout INTEGER NOT NULL DEFAULT 300,
                speech_timeout INTEGER NOT NULL DEFAULT 300,
                video_timeout INTEGER NOT NULL DEFAULT 1500,
                max_retries INTEGER NOT NULL DEFAULT 3
            );
            CREATE TABLE IF NOT EXISTS pool_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                call_mode TEXT NOT NULL DEFAULT 'default',
                quota_preflight INTEGER NOT NULL DEFAULT 1,
                error_ban_threshold INTEGER NOT NULL DEFAULT 5,
                keepalive_enabled INTEGER NOT NULL DEFAULT 1,
                keepalive_interval INTEGER NOT NULL DEFAULT 10,
                yescaptcha_api_key TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS proxy_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                proxy_url TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS cache_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 1,
                cache_timeout INTEGER NOT NULL DEFAULT 7200,
                cache_base_url TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS debug_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                log_requests INTEGER NOT NULL DEFAULT 0,
                log_responses INTEGER NOT NULL DEFAULT 0,
                mask_token INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS extension_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                push_token TEXT NOT NULL DEFAULT '',
                auto_enable_on_update INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS login_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                auto_relogin_enabled INTEGER NOT NULL DEFAULT 1,
                relogin_cooldown_seconds INTEGER NOT NULL DEFAULT 3600,
                login_timeout_seconds INTEGER NOT NULL DEFAULT 600
            );
            CREATE TABLE IF NOT EXISTS google_credentials (
                account_id INTEGER PRIMARY KEY,
                email TEXT NOT NULL DEFAULT '',
                password_enc TEXT NOT NULL DEFAULT '',
                totp_enc TEXT NOT NULL DEFAULT '',
                updated_at TEXT
            );
            """
        )

    async def _migrate_columns(self, db):
        """Add columns introduced after first boot; existing values are preserved."""
        migrations = {
            "extension_config": {
                "auto_enable_on_update": "INTEGER NOT NULL DEFAULT 1",
            },
            "accounts": {
                "speech_enabled": "INTEGER NOT NULL DEFAULT 1",
                "speech_count": "INTEGER NOT NULL DEFAULT 0",
                "quota_speech_total": "INTEGER",
                "quota_speech_available": "INTEGER",
                "quota_speech_checked_at": "TEXT",
                "proxy_url": "TEXT NOT NULL DEFAULT ''",
                "ban_reason": "TEXT NOT NULL DEFAULT ''",
                "banned_at": "TEXT",
            },
            "request_logs": {
                "generation_id": "TEXT",
            },
            "generation_config": {
                "speech_timeout": "INTEGER NOT NULL DEFAULT 300",
            },
            "account_stats": {
                "today_speech": "INTEGER NOT NULL DEFAULT 0",
                "total_speech": "INTEGER NOT NULL DEFAULT 0",
            },
            "pool_config": {
                "keepalive_enabled": "INTEGER NOT NULL DEFAULT 1",
                "keepalive_interval": "INTEGER NOT NULL DEFAULT 10",
                "yescaptcha_api_key": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, columns in migrations.items():
            async with db.execute(f"PRAGMA table_info({table})") as cursor:
                existing = {row[1] for row in await cursor.fetchall()}
            for column, definition in columns.items():
                if column not in existing:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def _ensure_config_rows(self, db, seed: dict, overwrite: bool = True):
        generation = seed.get("generation", {})
        pool = seed.get("pool", {})
        proxy = seed.get("proxy", {})
        cache = seed.get("cache", {})
        debug = seed.get("debug", {})
        admin = seed.get("admin", {})
        login = seed.get("login", {})
        api_key = seed.get("global", {}).get("api_key", "")
        defaults = {
            "login_config": (
                "INSERT OR IGNORE INTO login_config "
                "(id, auto_relogin_enabled, relogin_cooldown_seconds, login_timeout_seconds) "
                "VALUES (1, ?, ?, ?)",
                (1 if login.get("auto_relogin_enabled", True) else 0,
                 int(login.get("relogin_cooldown_seconds", 3600)),
                 int(login.get("login_timeout_seconds", 600))),
            ),
            "admin_config": (
                "INSERT OR IGNORE INTO admin_config (id, username, password, api_key) "
                "VALUES (1, ?, ?, ?)",
                (
                    str(admin.get("username", "admin")),
                    str(admin.get("password", "admin")),
                    str(api_key or "sk-vids2api-default-key"),
                ),
            ),
            "generation_config": (
                "INSERT OR IGNORE INTO generation_config "
                "(id, image_timeout, speech_timeout, video_timeout, max_retries) "
                "VALUES (1, ?, ?, ?, ?)",
                (
                    int(generation.get("image_timeout", 300)),
                    int(generation.get("speech_timeout", 300)),
                    int(generation.get("video_timeout", 1500)),
                    int(generation.get("max_retries", 3)),
                ),
            ),
            "pool_config": (
                "INSERT OR IGNORE INTO pool_config (id, call_mode, quota_preflight, "
                "error_ban_threshold, keepalive_enabled, keepalive_interval) "
                "VALUES (1, ?, ?, ?, ?, ?)",
                (
                    str(pool.get("call_mode", "default")),
                    1 if pool.get("quota_preflight", True) else 0,
                    int(pool.get("error_ban_threshold", 5)),
                    1 if pool.get("keepalive_enabled", True) else 0,
                    int(pool.get("keepalive_interval", 10)),
                ),
            ),
            "proxy_config": (
                "INSERT OR IGNORE INTO proxy_config (id, enabled, proxy_url) VALUES (1, ?, ?)",
                (1 if proxy.get("enabled", False) else 0, str(proxy.get("proxy_url", ""))),
            ),
            "cache_config": (
                "INSERT OR IGNORE INTO cache_config (id, enabled, cache_timeout, cache_base_url) "
                "VALUES (1, ?, ?, ?)",
                (1 if cache.get("enabled", True) else 0, int(cache.get("cache_timeout", 7200)),
                 str(cache.get("cache_base_url", ""))),
            ),
            "debug_config": (
                "INSERT OR IGNORE INTO debug_config (id, enabled, log_requests, log_responses, "
                "mask_token) VALUES (1, ?, ?, ?, ?)",
                (1 if debug.get("enabled", False) else 0,
                 1 if debug.get("log_requests", False) else 0,
                 1 if debug.get("log_responses", False) else 0,
                 1 if debug.get("mask_token", True) else 0),
            ),
            "extension_config": (
                "INSERT OR IGNORE INTO extension_config (id, enabled, push_token) "
                "VALUES (1, 0, ?)",
                (f"ext_{secrets.token_urlsafe(24)}",),
            ),
        }
        for _, (sql, params) in defaults.items():
            await db.execute(sql, params)

    # ---- config hot reload ------------------------------------------------
    async def reload_config_to_memory(self):
        row = await self._fetchone("SELECT * FROM login_config WHERE id = 1")
        if row:
            config.set_login(
                auto_relogin_enabled=bool(row["auto_relogin_enabled"]),
                relogin_cooldown_seconds=row["relogin_cooldown_seconds"],
                login_timeout_seconds=row["login_timeout_seconds"],
            )
        row = await self._fetchone("SELECT * FROM admin_config WHERE id = 1")
        if row:
            config.set_admin_credentials(row["username"], row["password"])
            if row["api_key"]:
                config.set_api_key(row["api_key"])
        row = await self._fetchone("SELECT * FROM generation_config WHERE id = 1")
        if row:
            config.set_generation(
                image_timeout=row["image_timeout"],
                speech_timeout=row["speech_timeout"],
                video_timeout=row["video_timeout"],
                max_retries=row["max_retries"],
            )
        row = await self._fetchone("SELECT * FROM pool_config WHERE id = 1")
        if row:
            config.set_pool(
                call_mode=row["call_mode"],
                quota_preflight=bool(row["quota_preflight"]),
                error_ban_threshold=row["error_ban_threshold"],
                keepalive_enabled=bool(row["keepalive_enabled"]),
                keepalive_interval=row["keepalive_interval"],
                yescaptcha_api_key=str(row["yescaptcha_api_key"] or ""),
            )
        row = await self._fetchone("SELECT * FROM proxy_config WHERE id = 1")
        if row:
            config._data.setdefault("proxy", {}).update(
                enabled=bool(row["enabled"]), proxy_url=row["proxy_url"]
            )
        row = await self._fetchone("SELECT * FROM cache_config WHERE id = 1")
        if row:
            config.set_cache(
                enabled=bool(row["enabled"]),
                cache_timeout=row["cache_timeout"],
                cache_base_url=row["cache_base_url"],
            )
        row = await self._fetchone("SELECT * FROM debug_config WHERE id = 1")
        if row:
            config.set_debug(
                enabled=bool(row["enabled"]),
                log_requests=bool(row["log_requests"]),
                log_responses=bool(row["log_responses"]),
                mask_token=bool(row["mask_token"]),
            )

    # ---- accounts ---------------------------------------------------------
    @staticmethod
    def _account_from_row(row) -> Account:
        return Account(
            id=row["id"],
            email=row["email"],
            identity_hash=row["identity_hash"],
            editor_url=row["editor_url"],
            auth_user=row["auth_user"],
            remark=row["remark"],
            is_active=bool(row["is_active"]),
            image_enabled=bool(row["image_enabled"]),
            video_enabled=bool(row["video_enabled"]),
            speech_enabled=bool(row["speech_enabled"]),
            proxy_url=row["proxy_url"],
            status=row["status"],
            last_error=row["last_error"],
            use_count=row["use_count"],
            image_count=row["image_count"],
            video_count=row["video_count"],
            speech_count=row["speech_count"],
            error_count=row["error_count"],
            consecutive_error_count=row["consecutive_error_count"],
            quota_image_total=row["quota_image_total"],
            quota_image_available=row["quota_image_available"],
            quota_image_checked_at=row["quota_image_checked_at"],
            quota_video_total=row["quota_video_total"],
            quota_video_available=row["quota_video_available"],
            quota_video_checked_at=row["quota_video_checked_at"],
            quota_speech_total=row["quota_speech_total"],
            quota_speech_available=row["quota_speech_available"],
            quota_speech_checked_at=row["quota_speech_checked_at"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            banned_at=row["banned_at"],
            ban_reason=row["ban_reason"],
        )

    async def get_account(self, account_id: int) -> Account | None:
        row = await self._fetchone("SELECT * FROM accounts WHERE id = ?", (int(account_id),))
        return self._account_from_row(row) if row else None

    async def get_account_by_identity(self, identity_hash: str) -> Account | None:
        row = await self._fetchone(
            "SELECT * FROM accounts WHERE identity_hash = ?", (identity_hash,)
        )
        return self._account_from_row(row) if row else None

    async def get_account_session(self, account_id: int) -> str:
        row = await self._fetchone(
            "SELECT session_enc FROM accounts WHERE id = ?", (int(account_id),)
        )
        return row["session_enc"] if row else ""

    async def get_accounts(self, active_only: bool = False) -> list[Account]:
        sql = "SELECT * FROM accounts"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY id"
        rows = await self._fetchall(sql)
        return [self._account_from_row(row) for row in rows]

    async def add_account(self, *, identity_hash: str, editor_url: str, auth_user: int,
                          email: str, session_enc: str, remark: str, proxy_url: str) -> int:
        async with self._write_lock, self._connect_async() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "INSERT INTO accounts (email, identity_hash, editor_url, auth_user, session_enc, "
                "remark, proxy_url, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'starting', ?)",
                (email, identity_hash, editor_url, int(auth_user), session_enc, remark,
                 proxy_url, _now()),
            )
            account_id = cursor.lastrowid
            await db.execute(
                "INSERT INTO account_stats (account_id, today_date) VALUES (?, ?)",
                (account_id, _today()),
            )
            await db.commit()
        return account_id

    async def update_account_fields(self, account_id: int, **fields):
        if not fields:
            return
        allowed = {
            "email", "remark", "is_active", "image_enabled", "video_enabled",
            "speech_enabled", "proxy_url", "status", "last_error", "session_enc",
            "quota_image_total", "quota_image_available", "quota_image_checked_at",
            "quota_video_total", "quota_video_available", "quota_video_checked_at",
            "quota_speech_total", "quota_speech_available", "quota_speech_checked_at",
            "consecutive_error_count", "banned_at", "ban_reason",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        sets = ", ".join(f"{key} = ?" for key in updates)
        await self._execute(
            f"UPDATE accounts SET {sets} WHERE id = ?",
            (*updates.values(), int(account_id)),
        )

    async def delete_account(self, account_id: int):
        async with self._write_lock, self._connect_async() as db:
            await db.execute("BEGIN IMMEDIATE")
            for table, column in (("accounts", "id"), ("account_stats", "account_id"),
                                  ("google_credentials", "account_id")):
                await db.execute(f"DELETE FROM {table} WHERE {column} = ?", (int(account_id),))
            # Claims remain as tombstones: deleting an account must not allow
            # a previously used key to purchase the same generation again.
            await db.commit()

    async def get_generation_claims(self) -> dict[str, int]:
        rows = await self._fetchall("SELECT generation_id, account_id FROM generation_claims")
        return {row["generation_id"]: row["account_id"] for row in rows}

    async def get_generation_owner(self, generation_id: str) -> int | None:
        row = await self._fetchone(
            "SELECT account_id FROM generation_claims WHERE generation_id = ?", (generation_id,),
        )
        return int(row["account_id"]) if row is not None else None

    async def claim_generation(self, generation_id: str, account_id: int) -> int:
        async with self._write_lock, self._connect_async() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "INSERT OR IGNORE INTO generation_claims (generation_id, account_id, created_at) "
                "VALUES (?, ?, ?)", (generation_id, int(account_id), _now()),
            )
            async with db.execute(
                "SELECT account_id FROM generation_claims WHERE generation_id = ?", (generation_id,)
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()
            return int(row[0])

    async def index_legacy_receipts(self, receipts: list[tuple[str, int, str]]):
        """Import old receipt ownership without settling historical usage again."""
        if not receipts:
            return
        async with self._write_lock, self._connect_async() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.executemany(
                "INSERT OR IGNORE INTO generation_claims "
                "(generation_id, account_id, outcome, created_at) VALUES (?, ?, ?, ?)",
                [(generation_id, int(account_id), outcome, _now())
                 for generation_id, account_id, outcome in receipts],
            )
            await db.commit()

    async def _record_result(self, db, account_id: int, kind: str, success: bool,
                             *, account_fault=True):
        if kind not in self.QUOTA_KINDS:
            raise ValueError("Unsupported Vids quota kind")
        today = _today()
        await db.execute(
            "INSERT INTO account_stats (account_id, today_date) VALUES (?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET "
            "today_image = CASE WHEN today_date = excluded.today_date THEN today_image ELSE 0 END, "
            "today_video = CASE WHEN today_date = excluded.today_date THEN today_video ELSE 0 END, "
            "today_speech = CASE WHEN today_date = excluded.today_date THEN today_speech ELSE 0 END, "
            "today_error = CASE WHEN today_date = excluded.today_date THEN today_error ELSE 0 END, "
            "today_date = excluded.today_date", (int(account_id), today),
        )
        counter = kind if success else "error"
        await db.execute(
            f"UPDATE account_stats SET today_{counter} = today_{counter} + 1, "
            f"total_{counter} = total_{counter} + 1 WHERE account_id = ?", (int(account_id),),
        )
        if success:
            await db.execute(
                "UPDATE accounts SET consecutive_error_count = 0 WHERE id = ?", (int(account_id),)
            )
        elif account_fault:
            await db.execute(
                "UPDATE accounts SET consecutive_error_count = consecutive_error_count + 1, "
                "error_count = error_count + 1 WHERE id = ?", (int(account_id),),
            )

    async def settle_generation(self, generation_id: str, account_id: int,
                                kind: str, success: bool, error_code: str = "",
                                *, ban_threshold: int = 5) -> bool:
        """Settle one durable receipt and its counters in a single transaction."""
        async with self._write_lock, self._connect_async() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT id FROM accounts WHERE id = ?", (int(account_id),)) as cursor:
                if await cursor.fetchone() is None:
                    return False
            await db.execute(
                "INSERT OR IGNORE INTO generation_claims (generation_id, account_id, created_at) "
                "VALUES (?, ?, ?)", (generation_id, int(account_id), _now()),
            )
            cursor = await db.execute(
                "UPDATE generation_claims SET outcome = ? "
                "WHERE generation_id = ? AND account_id = ? AND outcome = ''",
                ("success" if success else "error", generation_id, int(account_id)),
            )
            if cursor.rowcount != 1:
                await db.commit()
                return False
            account_fault = error_code in {"upstream_http_401", "upstream_http_403", "upstream_http_429"}
            await self._record_result(db, account_id, kind, success, account_fault=account_fault)
            if success:
                await db.execute(
                    f"UPDATE accounts SET use_count = use_count + 1, {kind}_count = {kind}_count + 1, "
                    f"last_used_at = ?, quota_{kind}_available = "
                    f"CASE WHEN quota_{kind}_available IS NULL THEN NULL "
                    f"ELSE MAX(quota_{kind}_available - 1, 0) END WHERE id = ?",
                    (_now(), int(account_id)),
                )
                await db.execute(
                    "UPDATE accounts SET is_active = 1, status = 'ready', banned_at = NULL, "
                    "ban_reason = '', last_error = '' WHERE id = ? "
                    "AND ban_reason GLOB 'consecutive_errors:*'", (int(account_id),),
                )
            else:
                await db.execute(
                    "UPDATE accounts SET last_error = ? WHERE id = ?", (error_code[:500], int(account_id))
                )
                if account_fault:
                    reason = f"consecutive_errors:{error_code}"
                    await db.execute(
                        "UPDATE accounts SET is_active = 0, status = 'disabled', banned_at = ?, "
                        "ban_reason = ?, last_error = ? WHERE id = ? AND consecutive_error_count >= ? "
                        "AND (is_active = 1 OR ban_reason GLOB 'consecutive_errors:*')",
                        (_now(), reason, reason, int(account_id), int(ban_threshold)),
                    )
            await db.commit()
            return True

    async def touch_account_used(self, account_id: int, kind: str):
        column = {"image": "image_count", "video": "video_count", "speech": "speech_count"}.get(
            kind
        )
        await self._execute(
            "UPDATE accounts SET use_count = use_count + 1, last_used_at = ?, "
            f"{column} = {column} + 1 WHERE id = ?",
            (_now(), int(account_id)),
        )

    QUOTA_KINDS = ("image", "video", "speech")

    async def consume_account_quota(self, account_id: int, kind: str):
        """Optimistic local decrement so dispatch weighting reflects in-flight usage.

        The periodic keepalive refresh re-syncs this against Google's real numbers.
        """
        if kind not in self.QUOTA_KINDS:
            raise ValueError("Unsupported Vids quota kind")
        await self._execute(
            f"UPDATE accounts SET quota_{kind}_available = MAX(quota_{kind}_available - 1, 0) "
            f"WHERE id = ? AND quota_{kind}_available IS NOT NULL",
            (int(account_id),),
        )

    async def record_account_result(self, account_id: int, kind: str, success: bool):
        async with self._write_lock, self._connect_async() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._record_result(db, account_id, kind, success)
            await db.commit()

    async def ban_account(self, account_id: int, reason: str):
        await self._execute(
            "UPDATE accounts SET is_active = 0, status = 'disabled', banned_at = ?, "
            "ban_reason = ?, last_error = ? WHERE id = ?",
            (_now(), reason, reason, int(account_id)),
        )

    async def unban_account(self, account_id: int, *, automatic_only=False):
        condition = " AND ban_reason GLOB 'consecutive_errors:*'" if automatic_only else ""
        cursor = await self._execute(
            "UPDATE accounts SET is_active = 1, banned_at = NULL, ban_reason = '', "
            "consecutive_error_count = 0, status = 'ready', last_error = '' WHERE id = ?" + condition,
            (int(account_id),),
        )
        return cursor.rowcount == 1

    # ---- request logs -----------------------------------------------------
    async def add_request_log(self, *, account_id: int | None, operation: str,
                              generation_id: str | None = None, request_body: str = "",
                              status_text: str = "", progress: int = 0) -> int:
        now = _now()
        cursor = await self._execute(
            "INSERT INTO request_logs (account_id, operation, generation_id, request_body, "
            "status_text, progress, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, operation, generation_id, request_body[:65535], status_text,
             int(progress), now, now),
        )
        return cursor.lastrowid

    async def update_request_log(self, log_id: int, *, response_body: str | None = None,
                                 status_code: int | None = None, status_text: str | None = None,
                                 progress: int | None = None, duration: float | None = None,
                                 account_id: int | None = None):
        fields = {}
        if response_body is not None:
            fields["response_body"] = response_body[:65535]
        if status_code is not None:
            fields["status_code"] = int(status_code)
        if status_text is not None:
            fields["status_text"] = status_text
        if progress is not None:
            fields["progress"] = int(progress)
        if duration is not None:
            fields["duration"] = float(duration)
        if account_id is not None:
            fields["account_id"] = int(account_id)
        if not fields:
            return
        fields["updated_at"] = _now()
        sets = ", ".join(f"{key} = ?" for key in fields)
        await self._execute(
            f"UPDATE request_logs SET {sets} WHERE id = ?", (*fields.values(), int(log_id))
        )

    _LOG_STATE_SQL = """CASE
        WHEN l.status_text IN ('failed', 'download_failed', 'cancelled')
             OR l.status_code >= 400 THEN 'failed'
        WHEN l.status_text = 'unknown' THEN 'unknown'
        WHEN l.status_code = 202 THEN 'processing'
        WHEN l.status_text = 'completed'
             OR (l.status_code >= 200 AND l.status_code < 300 AND l.status_code != 202)
             THEN 'completed'
        ELSE 'processing' END"""

    @classmethod
    def _log_filters(cls, operation="", status="", query="", days=0):
        conditions, params = [], []
        if operation:
            conditions.append("l.operation = ?")
            params.append(operation)
        if status:
            conditions.append(f"({cls._LOG_STATE_SQL}) = ?")
            params.append(status)
        if query:
            conditions.append(
                "(instr(lower(COALESCE(l.generation_id, '')), lower(?)) > 0 "
                "OR instr(lower(COALESCE(a.email, '')), lower(?)) > 0 "
                "OR CAST(l.account_id AS TEXT) = ?)"
            )
            params.extend((query, query, query.removeprefix("#")))
        if days:
            conditions.append("l.created_at >= ?")
            params.append((datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
        return (" WHERE " + " AND ".join(conditions) if conditions else ""), tuple(params)

    async def get_logs(self, limit: int = 50, offset: int = 0, *,
                       operation="", status="", query="", days=0) -> list[RequestLog]:
        where, params = self._log_filters(operation, status, query, days)
        rows = await self._fetchall(
            "SELECT l.id, l.account_id, l.operation, l.generation_id, l.status_code, "
            "l.status_text, l.progress, l.duration, l.created_at, l.updated_at, "
            f"a.email AS account_email, {self._LOG_STATE_SQL} AS state "
            "FROM request_logs l LEFT JOIN accounts a ON a.id = l.account_id" + where +
            " ORDER BY l.id DESC LIMIT ? OFFSET ?",
            (*params, int(limit), int(offset)),
        )
        return [RequestLog.model_validate(dict(row)) for row in rows]

    async def count_logs(self, *, operation="", status="", query="", days=0) -> int:
        where, params = self._log_filters(operation, status, query, days)
        row = await self._fetchone(
            "SELECT COUNT(*) AS total FROM request_logs l "
            "LEFT JOIN accounts a ON a.id = l.account_id" + where, params,
        )
        return row["total"]

    async def get_log(self, log_id: int) -> RequestLog | None:
        row = await self._fetchone(
            f"SELECT l.*, a.email AS account_email, {self._LOG_STATE_SQL} AS state "
            "FROM request_logs l LEFT JOIN accounts a ON a.id = l.account_id WHERE l.id = ?",
            (int(log_id),),
        )
        if not row:
            return None
        return RequestLog.model_validate(dict(row))

    async def clear_logs(self):
        await self._execute("DELETE FROM request_logs")

    # ---- single-row config tables ------------------------------------------
    async def _get_config_row(self, table: str) -> dict | None:
        row = await self._fetchone(f"SELECT * FROM {table} WHERE id = 1")
        return dict(row) if row else None

    async def get_admin_config(self) -> dict | None:
        return await self._get_config_row("admin_config")

    async def update_admin_credentials(self, username: str, password: str):
        await self._execute(
            "UPDATE admin_config SET username = ?, password = ? WHERE id = 1",
            (username, password),
        )

    async def update_api_key(self, api_key: str):
        await self._execute("UPDATE admin_config SET api_key = ? WHERE id = 1", (api_key,))

    async def update_generation_config(self, **fields):
        fields = GenerationSettings.model_validate(fields).model_dump(exclude_none=True)
        allowed = {"image_timeout", "speech_timeout", "video_timeout", "max_retries"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return
        sets = ", ".join(f"{k} = ?" for k in updates)
        await self._execute(
            f"UPDATE generation_config SET {sets} WHERE id = 1", tuple(updates.values())
        )

    async def update_pool_config(self, **fields):
        fields = PoolSettings.model_validate(fields).model_dump(exclude_none=True)
        if "call_mode" in fields and fields["call_mode"] not in {"default", "polling"}:
            fields.pop("call_mode")
        allowed = {
            "call_mode",
            "quota_preflight",
            "error_ban_threshold",
            "keepalive_enabled",
            "keepalive_interval",
            "yescaptcha_api_key",
        }
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "yescaptcha_api_key" in updates:
            updates["yescaptcha_api_key"] = str(updates["yescaptcha_api_key"]).strip()
        if not updates:
            return
        if "quota_preflight" in updates:
            updates["quota_preflight"] = 1 if updates["quota_preflight"] else 0
        if "keepalive_enabled" in updates:
            updates["keepalive_enabled"] = 1 if updates["keepalive_enabled"] else 0
        if "keepalive_interval" in updates:
            updates["keepalive_interval"] = max(5, int(updates["keepalive_interval"]))
        if "quota_preflight" in updates:
            updates["quota_preflight"] = 1 if updates["quota_preflight"] else 0
        sets = ", ".join(f"{k} = ?" for k in updates)
        await self._execute(
            f"UPDATE pool_config SET {sets} WHERE id = 1", tuple(updates.values())
        )

    async def update_proxy_config(self, enabled: bool, proxy_url: str):
        await self._execute(
            "UPDATE proxy_config SET enabled = ?, proxy_url = ? WHERE id = 1",
            (1 if enabled else 0, proxy_url),
        )

    async def update_cache_config(self, **fields):
        fields = CacheSettings.model_validate(fields).model_dump(exclude_none=True)
        allowed = {"enabled", "cache_timeout", "cache_base_url"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0
        sets = ", ".join(f"{k} = ?" for k in updates)
        await self._execute(f"UPDATE cache_config SET {sets} WHERE id = 1",
                            tuple(updates.values()))

    async def update_debug_config(self, **fields):
        fields = DebugSettings.model_validate(fields).model_dump(exclude_none=True)
        allowed = {"enabled", "log_requests", "log_responses", "mask_token"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return
        for key in list(updates):
            updates[key] = 1 if updates[key] else 0
        sets = ", ".join(f"{k} = ?" for k in updates)
        await self._execute(f"UPDATE debug_config SET {sets} WHERE id = 1",
                            tuple(updates.values()))

    async def get_extension_config(self) -> dict:
        row = await self._fetchone("SELECT * FROM extension_config WHERE id = 1")
        if row is None:
            return {"enabled": False, "push_token": "", "auto_enable_on_update": True}
        return {"enabled": bool(row["enabled"]), "push_token": row["push_token"] or "",
                "auto_enable_on_update": bool(row["auto_enable_on_update"])}
    async def update_extension_config(
        self, enabled: bool | None = None, push_token: str | None = None,
        auto_enable_on_update: bool | None = None,
    ):
        if enabled is None and push_token is None and auto_enable_on_update is None:
            return
        parts, params = [], []
        if enabled is not None:
            parts.append("enabled = ?")
            params.append(1 if enabled else 0)
        if push_token is not None:
            parts.append("push_token = ?")
            params.append(push_token)
        if auto_enable_on_update is not None:
            parts.append("auto_enable_on_update = ?")
            params.append(1 if auto_enable_on_update else 0)
        params.append(1)
        await self._execute(
            f"UPDATE extension_config SET {', '.join(parts)} WHERE id = ?", tuple(params)
        )

    async def update_login_config(self, **fields):
        fields = LoginSettings.model_validate(fields).model_dump(exclude_none=True)
        if not fields:
            return
        if "auto_relogin_enabled" in fields:
            fields["auto_relogin_enabled"] = int(fields["auto_relogin_enabled"])
        assignments = ", ".join(f"{key} = ?" for key in fields)
        await self._execute(f"UPDATE login_config SET {assignments} WHERE id = 1", tuple(fields.values()))

    async def save_google_credentials(
        self, account_id: int, email: str, password_enc: str, totp_enc: str
    ) -> bool:
        cursor = await self._execute(
            """
            INSERT INTO google_credentials (account_id, email, password_enc, totp_enc, updated_at)
            SELECT id, ?, ?, ?, ? FROM accounts WHERE id = ?
            ON CONFLICT(account_id) DO UPDATE SET
                email = excluded.email,
                password_enc = excluded.password_enc,
                totp_enc = excluded.totp_enc,
                updated_at = excluded.updated_at
            """,
            (email, password_enc, totp_enc, _now(), int(account_id)),
        )
        return cursor.rowcount > 0

    async def get_google_credentials(self, account_id: int) -> dict | None:
        row = await self._fetchone(
            "SELECT account_id, email, password_enc, totp_enc FROM google_credentials "
            "WHERE account_id = ?",
            (int(account_id),),
        )
        if row is None:
            return None
        return {
            "account_id": row["account_id"],
            "email": row["email"] or "",
            "password_enc": row["password_enc"] or "",
            "totp_enc": row["totp_enc"] or "",
        }

    # ---- dashboard --------------------------------------------------------
    async def get_stats_summary(self) -> dict:
        accounts = await self._fetchall(
            "SELECT is_active, status FROM accounts"
        )
        total = len(accounts)
        active = sum(1 for row in accounts if row["is_active"])
        ready = sum(
            1 for row in accounts if row["is_active"] and row["status"] == "ready"
        )
        today = _today()
        row = await self._fetchone(
            "SELECT SUM(today_image) AS ti, SUM(today_video) AS tv, SUM(today_speech) AS ts, "
            "SUM(today_error) AS te FROM account_stats WHERE today_date = ?",
            (today,),
        )
        totals = await self._fetchone(
            "SELECT SUM(total_image) AS ti, SUM(total_video) AS tv, SUM(total_speech) AS ts, "
            "SUM(total_error) AS te FROM account_stats"
        )
        log_count_row = await self._fetchone("SELECT COUNT(*) AS c FROM request_logs")

        def _pair(row_a, row_b):
            return {
                "today": int(row_a["ti"] or 0) + int(row_a["tv"] or 0)
                + int(row_a["ts"] or 0),
                "today_errors": int(row_a["te"] or 0),
                "total": int(row_b["ti"] or 0) + int(row_b["tv"] or 0)
                + int(row_b["ts"] or 0),
                "total_errors": int(row_b["te"] or 0),
            }

        return {
            "total_accounts": total,
            "active_accounts": active,
            "ready_accounts": ready,
            "generation": _pair(row, totals),
            "request_logs": int(log_count_row["c"] or 0) if log_count_row else 0,
        }

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def mask_payload(body: str) -> str:
        """Keep admin details readable without exposing credentials or inline media."""
        sensitive = re.compile(
            r"cookie|authorization|password|passwd|secret|credential|session|"
            r"token|api.?key|totp|base64|b64_json|signature", re.I,
        )

        def clean_text(text):
            if text.startswith("data:"):
                return f"<masked:{len(text)} chars>"

            def clean_url(match):
                try:
                    parts = urlsplit(match.group())
                    host = parts.netloc.rsplit("@", 1)[-1]
                    query = urlencode([
                        (key, "<masked>" if sensitive.search(key) or "sid" in key.lower()
                         or key.lower() in {"code", "state", "auth", "key"} else value)
                        for key, value in parse_qsl(parts.query, keep_blank_values=True)
                    ])
                    return urlunsplit((parts.scheme, host, parts.path, query, ""))
                except ValueError:
                    return "<masked URL>"

            text = re.sub(r"https?://[^\s<>\"']+", clean_url, text)
            text = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer <masked>", text)
            return text if len(text) <= 4096 else text[:4096] + "… <truncated>"

        def scrub(value, depth=0):
            if depth > 12:
                return "<nested payload>"
            if isinstance(value, dict):
                return {
                    key: (f"<masked:{len(str(item))} chars>" if sensitive.search(key)
                          else scrub(item, depth + 1))
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [scrub(item, depth + 1) for item in value]
            return clean_text(value) if isinstance(value, str) else value

        try:
            value = json.loads(body)
        except (ValueError, TypeError, RecursionError):
            text = str(body or "")
            return "<unavailable: malformed payload>" if text.lstrip().startswith(("{", "[")) else clean_text(text)
        return json.dumps(scrub(value), ensure_ascii=False)
