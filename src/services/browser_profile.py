"""Server-side Chromium profiles for durable Google sessions.

Each account owns a persistent Chromium user-data directory. Interactive
logins run headed on a real (or container virtual) display via patchright
(undetected Playwright fork) and can be fully automated with stored
credentials + TOTP; noVNC remains available as the manual escape hatch for
challenges a bot cannot solve. Credential harvests run headless and feed the
pure-HTTP sessions.
"""

import asyncio
import contextlib
import re
import shutil
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse, urljoin, urlsplit

from ..core.config import config
from ..core.logger import debug_logger

try:  # undetected fork first; plain playwright keeps local development easy
    from patchright.async_api import async_playwright
except ImportError:  # pragma: no cover - depends on host installation
    from playwright.async_api import async_playwright

WATCHDOG_TICK_SECONDS = 2.0
EDITOR_SETTLE_MS = 2500
CAPTCHA_MAX_SOLVES = 2
CAPTCHA_SOLVE_TIMEOUT_SECONDS = 180

HOMEPAGE = "https://docs.google.com/videos/?authuser=0"
LOGIN_URL = (
    "https://accounts.google.com/ServiceLogin"
    "?continue=https%3A%2F%2Fdocs.google.com%2Fvideos%2F%3Fauthuser%3D0"
)
EDITOR_RE = re.compile(r"^/videos/(?:u/\d+/)?d/[A-Za-z0-9_-]+/edit$")
PENDING_DIR_NAME = "_pending_login"

ACTIVE_STATES = ("running", "importing")
TERMINAL_STATES = ("success", "failed", "timeout", "cancelled")


class BrowserProfileError(RuntimeError):
    """Interactive-login lifecycle failure surfaced to the admin API."""


def _chromium_proxy(proxy_url: str) -> dict:
    """Chromium rejects inline credentials in the proxy server URL; split them
    into the dedicated username/password launch options."""
    parsed = urlsplit(proxy_url)
    setting = {"server": f"{parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port or 80}"}
    if parsed.username:
        setting["username"] = unquote(parsed.username)
        setting["password"] = unquote(parsed.password or "")
    return setting


async def recaptcha_challenge(page) -> dict[str, str] | None:
    """Extract the recaptcha anchor sitekey from any frame of the sign-in page."""
    for frame in list(page.frames):
        parsed = urlparse(str(frame.url or ""))
        if parsed.hostname not in {"www.google.com", "www.recaptcha.net"}:
            continue
        if (
            "/recaptcha/enterprise/anchor" not in parsed.path
            and "/recaptcha/api2/anchor" not in parsed.path
        ):
            continue
        query = parse_qs(parsed.query)
        website_key = str((query.get("k") or [""])[0]).strip()
        if website_key:
            return {
                "website_url": page.url,
                "website_key": website_key,
                "enterprise_s": str((query.get("s") or [""])[0]).strip(),
                "enterprise": str("/enterprise/" in parsed.path).lower(),
            }
    return None


_YESCAPTCHA_INJECT = """token => {
  const areas = Array.from(document.querySelectorAll(
    'textarea[name="g-recaptcha-response"],textarea[id^="g-recaptcha-response"]'
  ));
  for (const area of areas) {
    area.value = token;
    area.innerHTML = token;
    area.dispatchEvent(new Event('input', {bubbles: true}));
    area.dispatchEvent(new Event('change', {bubbles: true}));
  }
  const callbacks = [];
  const seen = new Set();
  const visit = (value, depth, key = '') => {
    if (!value || depth > 8 || seen.has(value)) return;
    if (typeof value === 'function') {
      if (key === 'callback') callbacks.push(value);
      return;
    }
    if (typeof value !== 'object') return;
    seen.add(value);
    for (const [childKey, child] of Object.entries(value)) {
      visit(child, depth + 1, childKey);
    }
  };
  visit(window.___grecaptcha_cfg?.clients || {}, 0);
  for (const callback of [...new Set(callbacks)]) {
    try { callback(token); } catch (_) {}
  }
  return areas.length + callbacks.length;
}"""


async def solve_recaptcha(
    page,
    api_key: str,
    *,
    base_url: str = "https://api.yescaptcha.com",
    timeout_seconds: float = CAPTCHA_SOLVE_TIMEOUT_SECONDS,
) -> bool:
    """Solve the page's recaptcha via YesCaptcha and inject the response token."""
    from curl_cffi.requests import AsyncSession

    challenge = await recaptcha_challenge(page)
    if challenge is None:
        return False
    task: dict = {
        "type": (
            "RecaptchaV2EnterpriseTaskProxyless"
            if challenge["enterprise"] == "true"
            else "RecaptchaV2TaskProxyless"
        ),
        "websiteURL": challenge["website_url"],
        "websiteKey": challenge["website_key"],
    }
    if challenge["enterprise_s"]:
        task["enterprisePayload"] = {"s": challenge["enterprise_s"]}
    async with AsyncSession(timeout=30) as session:
        created = (
            await session.post(
                base_url.rstrip("/") + "/createTask",
                json={"clientKey": api_key, "task": task, "softID": "33424"},
            )
        ).json()
        task_id = created.get("taskId") if isinstance(created, dict) else None
        if not task_id:
            code = str(created.get("errorCode") if isinstance(created, dict) else "unknown")[:120]
            debug_logger.log_info("server-login-captcha", f"createTask failed: {code}")
            return False
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            result = (
                await session.post(
                    base_url.rstrip("/") + "/getTaskResult",
                    json={"clientKey": api_key, "taskId": task_id},
                )
            ).json()
            if isinstance(result, dict) and result.get("errorId") not in (None, 0):
                code = str(result.get("errorCode") or "unknown")[:120]
                debug_logger.log_info("server-login-captcha", f"getTaskResult failed: {code}")
                return False
            if not isinstance(result, dict) or result.get("status") != "ready":
                continue
            token = (result.get("solution") or {}).get("gRecaptchaResponse")
            if not token:
                return False
            applied = await page.evaluate(_YESCAPTCHA_INJECT, token)
            if not applied:
                return False
            await page.wait_for_timeout(1000)
            nxt = page.get_by_text(
                re.compile(r"^Next$|^Verify$|^Continue$|^下一步$|^验证$|^继续$", re.IGNORECASE)
            ).first
            with contextlib.suppress(Exception):
                if await nxt.is_visible():
                    await nxt.click(no_wait_after=True)
            return True
    return False


class _EditorLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.links.append(value)


def totp_code(secret: str, period: int = 30, digits: int = 6) -> str:
    """RFC 6238 TOTP (SHA-1) from a Base32 secret."""
    import base64
    import hashlib
    import hmac
    import struct

    clean = (secret or "").strip().replace(" ", "").upper()
    padding = "=" * (-len(clean) % 8)
    key = base64.b32decode(clean + padding)
    counter = int(time.time()) // period
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def _format_cookie(raw: dict) -> dict:
    """Convert a Playwright cookie record into the canonical storage row."""
    domain = raw.get("domain") or ".google.com"
    row = {
        "name": raw.get("name", ""),
        "value": raw.get("value", ""),
        "domain": domain if domain.startswith(".") else "." + domain,
        "path": raw.get("path") or "/",
        "secure": bool(raw.get("secure", True)),
        "httpOnly": bool(raw.get("httpOnly", False)),
        "hostOnly": False,
    }
    expires = raw.get("expires")
    if expires is not None and expires != -1:
        row["expires"] = expires
        row["session"] = False
    else:
        row["session"] = True
    return row


class BrowserProfileService:
    """Manages per-account profiles, the interactive login session and harvests."""

    def __init__(self, data_dir: Path, import_handler) -> None:
        self._dir = data_dir / "browser_profiles"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._import_handler = import_handler
        self._state: dict = {"state": "idle"}
        self._lock = asyncio.Lock()
        self._context = None
        self._playwright = None
        self._watchdog: asyncio.Task | None = None
        self._harvest_locks: dict[int, asyncio.Lock] = {}
        self._relogin_attempts: dict[int, float] = {}

    # ---- status -----------------------------------------------------------
    _STATUS_SECRET_KEYS = ("login_password", "login_totp", "pending_credentials")

    def status(self) -> dict:
        """Polling snapshot; credential-bearing fields never leave the process."""
        return {k: v for k, v in self._state.items() if k not in self._STATUS_SECRET_KEYS}

    def profile_exists(self, account_id: int) -> bool:
        return (self._dir / str(int(account_id))).exists()

    async def remove_profile(self, account_id: int) -> None:
        async with self._lock:
            if self._state.get("account_id") == int(account_id) and self._state.get("state") in ACTIVE_STATES:
                await self._finish("cancelled", "")
            async with self._harvest_locks.setdefault(int(account_id), asyncio.Lock()):
                profile = self._dir / str(int(account_id))
                if profile.is_symlink():
                    profile.unlink()
                elif profile.exists():
                    await asyncio.to_thread(shutil.rmtree, profile)

    def _vnc_password(self) -> str | None:
        path = self._dir.parent / "vnc_secret"
        with contextlib.suppress(OSError):
            return path.read_text(encoding="utf-8").strip() or None
        return None

    def consume_pending_credentials(self) -> dict | None:
        payload = self._state.get("pending_credentials")
        if isinstance(payload, dict):
            self._state.pop("pending_credentials", None)
            return payload
        return None

    # ---- interactive login -------------------------------------------------
    async def start_login(
        self,
        remark: str = "",
        proxy_url: str | None = None,
        credentials: dict | None = None,
        persist_credentials: bool = False,
        target_account_id: int | None = None,
    ) -> dict:
        async with self._lock:
            if self._state.get("state") in ACTIVE_STATES:
                raise BrowserProfileError("已有登录会话进行中，请先取消或等待完成")
            await self._teardown_browser()
            pending = self._dir / PENDING_DIR_NAME
            if pending.exists():
                shutil.rmtree(pending, ignore_errors=True)
            self._state = {
                "state": "running",
                "account_id": target_account_id,
                "started_at": int(time.time()),
                "remark": remark,
                "automated": bool(credentials and credentials.get("password")),
                "login_email": (credentials or {}).get("email", ""),
                "login_password": (credentials or {}).get("password", ""),
                "login_totp": (credentials or {}).get("totp", ""),
                "login_proxy": proxy_url or "",
                "captcha_solves": 0,
            }
            try:
                self._playwright = await async_playwright().start()
                kwargs: dict = {
                    "headless": False,
                    "args": ["--no-sandbox", "--disable-dev-shm-usage"],
                    "no_viewport": True,
                    "locale": "zh-CN",
                    "timezone_id": "Asia/Shanghai",
                }
                if proxy_url:
                    kwargs["proxy"] = _chromium_proxy(proxy_url)
                self._context = await self._playwright.chromium.launch_persistent_context(
                    str(pending), **kwargs
                )
                page = self._context.pages[0] if self._context.pages else await self._context.new_page()
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as error:  # noqa: BLE001 - report launch problems verbatim
                debug_logger.log_error("server-login", error)
                await self._teardown_browser()
                self._state = {"state": "failed", "error": f"浏览器启动失败: {error}"}
                raise BrowserProfileError(f"浏览器启动失败: {error}") from None
            self._watchdog = asyncio.create_task(
                self._watch_login(page, credentials, persist_credentials),
                name="server-login-watchdog",
            )
        password = self._vnc_password()
        mode = "remote" if password and not sys.platform.startswith("win") else "local"
        info = {"state": "running", "mode": mode, "automated": self._state.get("automated", False)}
        if password:
            info["vnc_password"] = password
            info["vnc_port"] = 8690
        return info

    async def cancel_login(self) -> None:
        async with self._lock:
            if self._state.get("state") not in ACTIVE_STATES:
                raise BrowserProfileError("没有进行中的登录会话")
            await self._finish("cancelled", "")

    async def auto_relogin(self, account_id: int, credentials: dict, proxy_url: str | None) -> bool:
        """Fully automated re-login from stored credentials; no human interaction."""
        if not credentials or not credentials.get("password"):
            return False
        if not config.auto_relogin_enabled:
            return False
        if self._state.get("state") in ACTIVE_STATES:
            return False
        last = self._relogin_attempts.get(int(account_id), 0.0)
        if time.monotonic() - last < config.relogin_cooldown_seconds:
            return False
        self._relogin_attempts[int(account_id)] = time.monotonic()
        try:
            await self.start_login(
                remark=f"auto-relogin #{account_id}",
                proxy_url=proxy_url,
                credentials=credentials,
                persist_credentials=False,
                target_account_id=int(account_id),
            )
        except BrowserProfileError as error:
            debug_logger.log_info("auto-relogin", f"account {account_id}: {error}")
            return False
        deadline = time.monotonic() + config.login_timeout_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(3.0)
            state = self._state.get("state")
            if state == "success":
                return True
            if state in TERMINAL_STATES:
                return False
        return False

    async def _watch_login(self, page, credentials: dict | None, persist: bool) -> None:
        deadline = time.monotonic() + config.login_timeout_seconds
        try:
            while time.monotonic() < deadline:
                if self._state.get("state") != "running":
                    return
                url = page.url
                host = ""
                with contextlib.suppress(Exception):
                    host = urlsplit(url).hostname or ""
                if host == "docs.google.com":
                    await self._complete_login(page, persist)
                    return
                if credentials and credentials.get("password"):
                    await self._drive_login(page, credentials)
                if "/challenge/" in url:
                    # The form driver attempts an automated solve first; only
                    # surface the manual hint when no solver is configured or
                    # the attempt budget is exhausted.
                    if (
                        not config.yescaptcha_api_key
                        or self._state.get("captcha_solves", 0) >= CAPTCHA_MAX_SOLVES
                    ):
                        self._state["hint"] = "manual_challenge"
                self._state["url"] = url[:300]
                await asyncio.sleep(WATCHDOG_TICK_SECONDS)
            await self._finish("timeout", f"登录超时（{config.login_timeout_seconds} 秒），请重试")
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - watchdog must finish cleanly
            debug_logger.log_error("server-login", error)
            await self._finish("failed", str(error))

    async def _drive_login(self, page, credentials: dict) -> None:
        """Advance Google's sign-in form when automation inputs are available."""
        async def visible(selector: str) -> bool:
            locator = page.locator(selector)
            if not await locator.count():
                return False
            try:
                return await locator.first.is_visible()
            except Exception:  # noqa: BLE001 - stale node between navigations
                return False

        try:
            # reCAPTCHA interstitial: solve it via YesCaptcha when configured.
            if (
                config.yescaptcha_api_key
                and self._state.get("captcha_solves", 0) < CAPTCHA_MAX_SOLVES
                and time.monotonic() >= self._state.get("captcha_wait_until", 0.0)
            ):
                challenge = await recaptcha_challenge(page)
                if challenge is not None:
                    self._state["captcha_status"] = "solving"
                    try:
                        solved = await solve_recaptcha(page, config.yescaptcha_api_key)
                    except Exception as error:  # noqa: BLE001 - external solver
                        debug_logger.log_info("server-login-captcha", f"solve error: {error}")
                        solved = False
                    if solved:
                        self._state["captcha_solves"] = self._state.get("captcha_solves", 0) + 1
                        self._state["captcha_status"] = "solved"
                        self._state["captcha_wait_until"] = time.monotonic() + 30.0
                        self._state.pop("hint", None)
                    else:
                        self._state["captcha_status"] = "failed"
                    return
            if await visible("#identifierId"):
                email_input = page.locator("#identifierId")
                if not await email_input.first.input_value():
                    await email_input.first.fill(credentials["email"], timeout=4000)
                nxt = page.locator("#identifierNext")
                if await nxt.first.is_visible():
                    await nxt.first.click(timeout=4000)
                return
            if await visible("input[name='Passwd']"):
                password_input = page.locator("input[name='Passwd']")
                if not await password_input.first.input_value():
                    await password_input.first.fill(credentials["password"], timeout=4000)
                nxt = page.locator("#passwordNext")
                if await nxt.first.is_visible():
                    await nxt.first.click(timeout=4000)
                return
            if await visible("#totpPin"):
                totp_input = page.locator("#totpPin")
                code = totp_code(credentials.get("totp", ""))
                if code:
                    await totp_input.first.fill(code, timeout=4000)
                    nxt = page.locator("#totpNext")
                    if await nxt.first.is_visible():
                        await nxt.first.click(timeout=4000)
        except Exception as error:  # noqa: BLE001 - transient DOM states are expected
            debug_logger.log_info("server-login", f"form drive waiting: {error}")

    async def _complete_login(self, page, persist: bool) -> None:
        self._state["state"] = "importing"
        self._state.pop("hint", None)
        try:
            await page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(EDITOR_SETTLE_MS)
            content = await page.content()
            identity = str(_parse_flags(content).get("docs-hue", ""))
            editor_url = self._pick_editor(content)
            if not editor_url:
                await page.goto("https://vids.new", wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(4000)
                final = urlsplit(page.url)
                if final.hostname == "docs.google.com" and EDITOR_RE.fullmatch(final.path or ""):
                    editor_url = f"https://docs.google.com{final.path}?authuser=0"
            cookies = await self._context.cookies()
            rows = [_format_cookie(c) for c in cookies if "google.com" in (c.get("domain") or "")]
            if not editor_url or "@" not in identity:
                await self._finish("failed", "登录后未能定位 Vids 工作区，请确认该 Google 账号可用 Vids")
                return
            await self._teardown_browser()
            import json as _json

            if persist and self._state.get("automated"):
                # Attach before the import handler runs: _adopt_server_login
                # consumes and persists (encrypted) them while we still hold
                # the browser remark/state; success status stays credential-free.
                self._state["pending_credentials"] = {
                    "email": self._state.get("login_email", ""),
                    "password": self._state.get("login_password", ""),
                    "totp": self._state.get("login_totp", ""),
                    "proxy": self._state.get("login_proxy", ""),
                }
            result = await self._import_handler(
                editor_url, _json.dumps(rows), self._state.get("remark", "")
            )
            account_id = result.get("account_id")
            success = {
                "state": "success",
                "account_id": account_id,
                "action": result.get("action", "imported"),
                "email": identity,
                "finished_at": int(time.time()),
            }
            self._state = success
            pending = self._dir / PENDING_DIR_NAME
            if account_id is not None and pending.exists():
                target = self._dir / str(int(account_id))
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                pending.rename(target)
        except Exception as error:  # noqa: BLE001 - surface import failures in status
            debug_logger.log_error("server-login", error)
            await self._finish("failed", f"导入失败: {error}")

    def _pick_editor(self, content: str) -> str:
        parser = _EditorLinkParser()
        parser.feed(content)
        for href in parser.links:
            address = urlsplit(urljoin(HOMEPAGE, href))
            if address.hostname == "docs.google.com" and EDITOR_RE.fullmatch(address.path or ""):
                return f"https://docs.google.com{address.path}?authuser=0"
        return ""

    async def _finish(self, state: str, error: str) -> None:
        payload = {"state": state, "finished_at": int(time.time())}
        if error:
            payload["error"] = error
        self._state = payload
        await self._teardown_browser()

    # ---- harvest ----------------------------------------------------------
    async def harvest(
        self, account_id: int, editor_url: str, proxy_url: str | None
    ) -> list[dict] | None:
        """Cold-open the account profile headless and collect fresh cookies.

        Returns None when no profile exists or the profile session was
        rejected upstream, which tells callers the profile needs a fresh
        interactive login.
        """
        profile = self._dir / str(int(account_id))
        if not profile.exists():
            return None
        lock = self._harvest_locks.setdefault(int(account_id), asyncio.Lock())
        async with lock:
            pw = await async_playwright().start()
            try:
                kwargs: dict = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
                if proxy_url:
                    kwargs["proxy"] = _chromium_proxy(proxy_url)
                context = await pw.chromium.launch_persistent_context(str(profile), **kwargs)
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.goto(editor_url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(EDITOR_SETTLE_MS)
                    final = urlsplit(page.url)
                    if final.hostname != "docs.google.com" or not EDITOR_RE.fullmatch(final.path or ""):
                        return None
                    if "@" not in str(_parse_flags(await page.content()).get("docs-hue", "")):
                        return None
                    cookies = await context.cookies()
                    return [_format_cookie(c) for c in cookies if "google.com" in (c.get("domain") or "")]
                finally:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await context.close()
            finally:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pw.stop()

    # ---- lifecycle --------------------------------------------------------
    async def shutdown(self) -> None:
        with contextlib.suppress(BrowserProfileError):
            await self._teardown_browser()

    async def _teardown_browser(self) -> None:
        watchdog = self._watchdog
        if watchdog is not asyncio.current_task():
            self._watchdog = None
        context, self._context = self._context, None
        playwright, self._playwright = self._playwright, None
        current = asyncio.current_task()
        if watchdog is not None and watchdog is not current and not watchdog.done():
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watchdog
        if context is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await context.close()
        if playwright is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await playwright.stop()


def _parse_flags(text: str) -> dict:
    from .vids.client import parse_flags

    return parse_flags(text)
