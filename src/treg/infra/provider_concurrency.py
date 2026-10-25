"""Calls in progress on one treg-owned provider account, counted across every server process.

One sorted set per account in the shared store (`infra/kv.py`), on the same lease scripts as org
concurrency: each call owns a member that expires unless renewed, so a crashed process frees its
places within one lease. A lease bounds abandoned occupancy; it cannot stop a request already sent.
No shared store (`TREG_KV_URL` unset) or a store fault reads as `None`: the caller keeps only its
in-process count, which is exact for one process.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid

from . import kv

log = logging.getLogger("treg.provider_concurrency")
_LEASE_S = 30
_FIRST_POLL_S = 0.02
_MAX_POLL_S = 0.2
_pending: set[asyncio.Task] = set()


class Busy(Exception):
    """Every place on the account stayed taken, across all processes, for the whole wait."""


def _background(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


class Lease:
    """One place on one account. `release()` is sync and idempotent; the store write runs behind."""

    def __init__(self, store: kv.Store, account: str) -> None:
        self.store = store
        self.key = f"treg:provider-concurrency:v1:{account}"
        self.token = uuid.uuid4().hex
        self.ttl_ms = _LEASE_S * 1000
        self.renewal: asyncio.Task | None = None
        self.waited = False  # the account was full when first asked
        self._released = False

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(_LEASE_S / 3)
            outcome = await self.store.renew_slot(self.key, self.token, self.ttl_ms)
            if outcome == "renewed":
                continue
            if outcome == "lost":  # expired while the store was unreachable; never recreate it
                log.warning("provider_concurrency lease_lost key=%s; the call continues", self.key)
                return

    async def _release(self, *, quiet: bool = False) -> None:
        if await self.store.release_slot(self.key, self.token) == "unavailable" and not quiet:
            log.warning("provider_concurrency release_unavailable key=%s; expires by lease", self.key)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.renewal is not None:
            self.renewal.cancel()
        _background(self._release())


async def _drop_after(attempt: asyncio.Future, lease: Lease) -> None:
    """The write may land after the caller left: wait for it, then drop only this token."""
    await asyncio.gather(attempt, return_exceptions=True)
    await lease._release()


async def acquire(account: str, cap: int, *, wait_s: float) -> Lease | None:
    """Take a place on `account` among `cap` shared by every process, polling for at most
    `wait_s`. `None` when no shared store answers; `Busy` when the wait ran out."""
    if not kv.configured():
        return None
    try:
        store = kv.store()
    except Exception:  # noqa: BLE001 - a store that cannot initialize reads as unavailable
        return None
    lease = Lease(store, account)
    deadline = time.monotonic() + wait_s
    poll_s = _FIRST_POLL_S
    while True:
        attempt = asyncio.ensure_future(store.acquire_slot(lease.key, lease.token, lease.ttl_ms, cap))
        try:
            outcome = await asyncio.shield(attempt)
        except asyncio.CancelledError:
            _background(_drop_after(attempt, lease))
            raise
        if outcome == "acquired":
            lease.renewal = asyncio.get_running_loop().create_task(lease._renew())
            return lease
        if outcome != "busy":
            # A lost reply may have acquired: drop only this token. The store is failing, so quietly.
            _background(lease._release(quiet=True))
            return None
        lease.waited = True
        left = deadline - time.monotonic()
        if left <= 0:
            raise Busy(account)
        await asyncio.sleep(min(left, poll_s * random.uniform(0.5, 1.0)))
        poll_s = min(poll_s * 2, _MAX_POLL_S)
