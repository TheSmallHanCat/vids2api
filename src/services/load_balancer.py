"""Pool selection for Vids accounts: quota-weighted default mode or ordered polling.

Both modes preserve the receipt-affinity contract — selection only happens for new
idempotency keys; claimed receipts never move between accounts. The default mode
weighs every eligible candidate by its remaining quota for the requested kind, so
consumption spreads in proportion to headroom; a known-zero balance is never picked
while any candidate still has quota. Polling mode keeps strict rotation but also
skips exhausted accounts. The per-account preflight remains the authoritative 429
when the whole pool is exhausted.
"""

import random

from ..core.config import config
from .vids.gateway import GatewayError


class LoadBalancer:
    """Order eligible accounts once per new request; the dispatcher claims the winner."""

    def __init__(self, db):
        self.db = db
        self._rotation: dict[str, int] = {}

    def _remaining_quota(self, token, kind: str) -> int | None:
        """Known remaining balance, or None when the pool has never refreshed it."""
        available = getattr(token, f"quota_{kind}_available", None)
        return available if type(available) is int and available >= 0 else None

    def _weighted_pick(self, kind: str, candidates: list[int], tokens: dict) -> int:
        """Random pick proportional to remaining quota; unknown quota counts as one."""
        weights = []
        for token_id in candidates:
            remaining = self._remaining_quota(tokens[token_id], kind)
            weights.append(float(remaining) if remaining is not None else 1.0)
        if all(weight == 0 for weight in weights):
            return candidates[0]
        threshold = random.uniform(0, sum(weights))
        accumulated = 0.0
        for token_id, weight in zip(candidates, weights):
            accumulated += weight
            if accumulated >= threshold:
                return token_id
        return candidates[-1]

    def _ordered_candidates(self, kind: str, candidates: list[int]) -> list[int]:
        mode = config.call_mode
        if mode == "polling":
            last = self._rotation.get(kind, 0)
            ahead = [cid for cid in candidates if cid > last]
            behind = [cid for cid in candidates if cid <= last]
            return ahead + behind
        # Load-aware default: shuffle first so equally-loaded accounts spread out,
        # the quota weighting below then biases the final pick.
        shuffled = candidates[:]
        random.shuffle(shuffled)
        return shuffled

    async def select(self, kind: str, dispatcher, *, tokens=None) -> int:
        """Return the best eligible account id or raise a GatewayError-style error."""
        if tokens is None:
            try:
                tokens = {token.id: token for token in await self.db.get_accounts()}
            except Exception:
                raise GatewayError(503, "account_lookup_failed", "Vids account admission is unavailable") from None
        candidates = sorted(dispatcher._accounts)
        ordered = self._ordered_candidates(kind, candidates)
        busy = False
        configured = False
        recoverable = None
        qualified: dict[int, object] = {}
        for token_id in ordered:
            try:
                account = dispatcher._account(token_id)
            except GatewayError as error:
                if error.code in {"session_unavailable", "account_mismatch"}:
                    continue
                raise
            token = tokens.get(token_id)
            configured = True
            if not account.gateway._ready:
                continue
            if token is None or not getattr(token, "is_active", False):
                continue
            if kind in {"image", "video", "speech"} and not getattr(token, f"{kind}_enabled", False):
                continue
            preflight = dispatcher._preflight_locks.get(token_id)
            if (
                account.gateway.lock.locked()
                or token_id in dispatcher._pool_pending
                or preflight is not None
                and preflight.locked()
            ):
                busy = True
                continue
            if getattr(account.gateway.client, "initialization_valid", True) is False:
                if dispatcher.repair_session is not None and recoverable is None:
                    recoverable = token_id
                continue
            qualified[token_id] = token
        if not qualified:
            if recoverable is not None:
                self._rotation[kind] = recoverable
                return recoverable
            if busy:
                raise GatewayError(429, "busy", "All eligible Vids accounts are busy")
            if configured:
                raise GatewayError(
                    503, "account_disabled", "No eligible Vids account is available"
                )
            raise GatewayError(503, "session_unavailable", "No Vids account is available")
        with_quota = [
            token_id
            for token_id, token in qualified.items()
            if (self._remaining_quota(token, kind) or 0) > 0
            or self._remaining_quota(token, kind) is None
        ]
        if not with_quota:
            # Whole pool exhausted: keep the head candidate so the per-account
            # preflight reports the authoritative quota_exhausted error.
            with_quota = [next(iter(qualified))]
        if config.call_mode == "polling":
            selected = with_quota[0]
        else:
            selected = self._weighted_pick(kind, with_quota, qualified)
        self._rotation[kind] = selected
        return selected

    async def get_unavailable_reason(self, dispatcher) -> str:
        try:
            await self.select("video", dispatcher)
        except GatewayError as error:
            return error.message
        return ""
