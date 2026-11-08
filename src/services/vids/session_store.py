"""Fernet-encrypted session snapshots persisted in the accounts table.

The gateway awaits session_io() around these synchronous primitives so database
lock waits run outside the event loop. Short-lived sqlite3 connections use an
internal lock alongside the async aiosqlite writer. Session payloads are validated
before persistence.
"""

import asyncio
import json
import math
import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from cryptography.fernet import Fernet

MAX_SESSION_BYTES = 1024 * 1024


async def session_io(function, *args, **kwargs):
    """Finish storage work before cancellation can release the account lock."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


class SessionStoreError(RuntimeError):
    """Storage failures never reveal keys, cookies, account URLs or filesystem paths."""


def _payload(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "editor_url", "auth_user", "account_hash", "cookies"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or type(value["auth_user"]) is not int
        or value["auth_user"] < 0
        or not isinstance(value["account_hash"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", value["account_hash"])
    ):
        raise ValueError
    url = value["editor_url"]
    if not isinstance(url, str) or any(char.isspace() or ord(char) < 32 for char in url):
        raise ValueError
    address = urlsplit(url)
    query = parse_qsl(address.query, keep_blank_values=True, strict_parsing=True)
    if (
        address.scheme != "https"
        or address.hostname != "docs.google.com"
        or address.port not in (None, 443)
        or address.username is not None
        or address.password is not None
        or address.fragment
        or not re.fullmatch(r"/videos/(?:u/[0-9]+/)?d/[A-Za-z0-9_-]+/edit", address.path)
        or (query and query != [("authuser", str(value["auth_user"]))])
    ):
        raise ValueError
    cookies = value["cookies"]
    if not isinstance(cookies, list) or not 1 <= len(cookies) <= 256:
        raise ValueError
    rows = []
    allowed = {
        "name", "value", "domain", "path", "secure", "httpOnly", "hostOnly",
        "expires", "expirationDate", "session",
    }
    for cookie in cookies:
        if not isinstance(cookie, dict) or set(cookie) - allowed:
            raise ValueError
        name, secret, domain = (cookie.get(key) for key in ("name", "value", "domain"))
        if (
            not all(isinstance(item, str) and item for item in (name, secret, domain))
            or any(ord(char) <= 32 or ord(char) >= 127 or char in "=;" for char in name)
            or any(ord(char) < 32 or ord(char) >= 127 or char == ";" for char in secret)
            or domain.lower() not in {
                "google.com", ".google.com", "docs.google.com", ".docs.google.com",
            }
        ):
            raise ValueError
        path = cookie.get("path", "/")
        secure, http_only = cookie.get("secure", False), cookie.get("httpOnly", False)
        host_only = cookie.get("hostOnly", not domain.startswith("."))
        expires = cookie.get("expires", cookie.get("expirationDate"))
        session = cookie.get("session", expires in (None, -1))
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or any(ord(char) < 32 for char in path)
            or not all(type(item) is bool for item in (secure, http_only, host_only, session))
            or (
                expires is not None
                and (
                    type(expires) not in (int, float)
                    or not math.isfinite(expires)
                    or (expires < 0 and expires != -1)
                )
            )
            or (not session and expires in (None, -1))
            or (
                "expirationDate" in cookie
                and "expires" in cookie
                and cookie["expirationDate"] != expires
            )
            or (name.startswith("__Secure-") and not secure)
            or (name.startswith("__Host-") and (not secure or not host_only or path != "/"))
        ):
            raise ValueError
        rows.append(
            {
                "name": name,
                "value": secret,
                "domain": domain.lower(),
                "path": path,
                "secure": secure,
                "httpOnly": http_only,
                "hostOnly": host_only,
                "expires": None if expires == -1 else expires,
                "session": session,
            }
        )
    return {
        **value,
        "editor_url": f"https://docs.google.com{address.path}?authuser={value['auth_user']}",
        "cookies": rows,
    }


class DbSessionStore:
    """Synchronous Fernet session snapshots stored in the accounts.session_enc column."""

    def __init__(self, db_path: str | Path, account_id: int, key: bytes | str):
        try:
            self._db_path = str(db_path)
            self._account_id = int(account_id)
            self._cipher = Fernet(key.encode("ascii") if isinstance(key, str) else key)
            self._lock = threading.Lock()
        except Exception:
            raise SessionStoreError("Invalid session encryption configuration") from None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _read(self) -> bytes | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT session_enc FROM accounts WHERE id = ?", (self._account_id,)
            ).fetchone()
        value = row[0] if row else ""
        if not value:
            return None
        encrypted = value.encode("ascii")
        if len(encrypted) > MAX_SESSION_BYTES:
            raise SessionStoreError("Cannot restore encrypted session")
        return encrypted

    def exists(self) -> bool:
        try:
            return self._read() is not None
        except SessionStoreError:
            return True

    def delete(self) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE accounts SET session_enc = '' WHERE id = ?", (self._account_id,)
            )

    def load(self):
        try:
            encrypted = self._read()
            if encrypted is None:
                return None
            return _payload(json.loads(self._cipher.decrypt(encrypted)))
        except SessionStoreError:
            raise
        except Exception:
            raise SessionStoreError("Cannot restore encrypted session") from None

    def save(self, *, editor_url, auth_user, account_hash, cookies):
        try:
            value = _payload(
                {
                    "version": 1,
                    "editor_url": editor_url,
                    "auth_user": auth_user,
                    "account_hash": account_hash,
                    "cookies": cookies,
                }
            )
            encrypted = self._cipher.encrypt(
                json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
            )
            if len(encrypted) > MAX_SESSION_BYTES:
                raise ValueError
            with self._lock, closing(self._connect()) as connection, connection:
                cursor = connection.execute(
                    "UPDATE accounts SET session_enc = ? WHERE id = ?",
                    (encrypted.decode("ascii"), self._account_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Account no longer exists")
                connection.commit()
        except SessionStoreError:
            raise
        except Exception:
            raise SessionStoreError("Cannot persist encrypted session") from None
