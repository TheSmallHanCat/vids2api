"""Periodic session keepalive for the account pool.

Idle accounts still need traffic so Google keeps rotating short-lived
cookies; every probe lets the gateway checkpoint the rotated cookies back
into the encrypted session store. The loop also retries active accounts
that are not registered after a transient startup failure.
"""

import asyncio
import time

from ..core.config import config
from ..core.logger import debug_logger
from .account_manager import AccountManagerError

WAKEUP_SECONDS = 30.0


class KeepaliveService:
    def __init__(self, account_manager, db):
        self.account_manager = account_manager
        self.db = db
        self._task: asyncio.Task | None = None
        self._last_run = 0.0
        self._stopping = False

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="vids2api-keepalive")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    async def _loop(self) -> None:
        while not self._stopping:
            interval = config.keepalive_interval * 60
            if config.keepalive_enabled and time.monotonic() - self._last_run >= interval:
                self._last_run = time.monotonic()
                try:
                    await self.run_once()
                except Exception as error:  # noqa: BLE001 - the loop must survive
                    debug_logger.log_error("keepalive", error)
            await asyncio.sleep(WAKEUP_SECONDS)

    # ---- one round ---------------------------------------------------------
    async def run_once(self) -> dict:
        """Probe every running account and retry unregistered active ones."""
        summary = {
            "probed": 0,
            "ready": 0,
            "failed": 0,
            "busy": 0,
            "resumed": 0,
            "recover_failed": 0,
            "repaired": 0,
            "quota_refreshed": 0,
        }
        for account_id, registered in list(self.account_manager.dispatcher._accounts.items()):
            account = await self.db.get_account(account_id)
            if account is None:
                continue
            # Automatically banned accounts keep their probe cycle: once the
            # session recovers (or auto re-login fixes it) the account returns
            # to the pool on its own. Manual disables stay untouched.
            auto_banned = account.ban_reason.startswith("consecutive_errors:")
            if not account.is_active and not auto_banned:
                continue
            if registered.gateway.lock.locked():
                summary["busy"] += 1
                continue
            summary["probed"] += 1
            try:
                outcome = await self.account_manager.test_account(account_id)
            except AccountManagerError as error:
                summary["probed"] -= 1
                debug_logger.log_info("keepalive", f"account {account_id} probe skipped: {error.code}")
                continue
            if outcome.get("ready"):
                summary["ready"] += 1
                if account.status != "ready" or account.last_error:
                    await self.db._execute(
                        "UPDATE accounts SET status = 'ready', last_error = '' WHERE id = ? "
                        "AND (is_active = 1 OR ban_reason GLOB 'consecutive_errors:*')",
                        (account_id,),
                    )
                if not account.is_active and auto_banned:
                    if await self.db.unban_account(account_id, automatic_only=True):
                        summary["unbanned"] = summary.get("unbanned", 0) + 1
                        debug_logger.log_info(
                            "keepalive", f"account {account_id}: consecutive-error ban lifted"
                        )
                try:
                    await self.account_manager.refresh_quota(account_id)
                    summary["quota_refreshed"] += 1
                except Exception as error:  # noqa: BLE001 - refresh is best-effort
                    debug_logger.log_info(
                        "keepalive", f"account {account_id} quota refresh failed: {error}"
                    )
            else:
                summary["failed"] += 1
                reason = str(outcome.get("error", ""))[:500]
                repaired = False
                try:
                    repaired = await self.account_manager.repair_session(account_id)
                except Exception as error:  # noqa: BLE001 - repair is best-effort
                    debug_logger.log_info("keepalive", f"account {account_id} repair failed: {error}")
                if repaired:
                    summary["repaired"] += 1
                    debug_logger.log_info("keepalive", f"account {account_id} session repaired")
                    continue
                await self.db.update_account_fields(
                    account_id, status="session_error", last_error=reason
                )
                debug_logger.log_info("keepalive", f"account {account_id} session_error: {reason}")
        await self._retry_unregistered(summary)
        return summary

    async def _retry_unregistered(self, summary: dict) -> None:
        try:
            accounts = await self.db.get_accounts()
        except Exception as error:  # noqa: BLE001 - probing must not crash the loop
            debug_logger.log_error("keepalive-list", error)
            return
        for account in accounts:
            auto_banned = account.ban_reason.startswith("consecutive_errors:")
            if ((not account.is_active and not auto_banned)
                    or account.id in self.account_manager.dispatcher._accounts):
                continue
            try:
                await self.account_manager.start_account(account.id)
                summary["resumed"] += 1
            except Exception as error:  # noqa: BLE001 - try the remaining accounts
                summary["recover_failed"] += 1
                debug_logger.log_error(
                    "keepalive-resume", f"account {account.id}: {error}"
                )
