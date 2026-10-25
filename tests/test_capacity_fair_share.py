"""Fair share of a provider's concurrency on treg's own keys (domain/capacity/fair_share.py): a free
place is taken at once; when the provider is full, the next free place goes to the waiting team
with the fewest calls in progress; a call that waited too long leaves with `share_busy`, not
charged. Own keys are never gated."""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain.capacity.fair_share import FairShare, ShareBusy, fair_share
from treg.domain.capacity.view import view as capacity_view
from treg.infra import kv, provider_concurrency

from test_marketplace_call import EP, PLATFORM_KEYS, _balance, _fake_relay, platform_on  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh_gate():
    fair_share.reset()
    yield
    fair_share.reset()


# ---- the gate -----------------------------------------------------------------------------------

async def _turns(n: int = 5) -> None:
    """Let the event loop run the woken waiters to completion."""
    for _ in range(n):
        await asyncio.sleep(0)


async def test_a_freed_place_goes_to_the_team_with_the_fewest_calls_in_progress():
    gate = FairShare()
    heavy = [await gate.acquire("p", 1, 3) for _ in range(3)]       # the heavy team fills the provider
    first_heavy = asyncio.create_task(gate.acquire("p", 1, 3))      # it queues first...
    await asyncio.sleep(0)
    light = asyncio.create_task(gate.acquire("p", 2, 3))            # ...a light team queues after it
    await asyncio.sleep(0)
    heavy[0].release()
    await _turns()
    assert light.done() and not first_heavy.done(), "the light team gets the freed place first"
    heavy[1].release()
    slot = await asyncio.wait_for(first_heavy, 1)
    assert slot.waited_ms >= 0
    for s in (heavy[2], light.result(), slot):
        s.release()
    assert gate._pools["p"].active == 0 and not gate._pools["p"].by_team


async def test_a_call_that_waits_too_long_leaves_the_line_and_never_takes_a_place():
    gate = FairShare()
    held = await gate.acquire("p", 1, 1)
    with pytest.raises(ShareBusy) as busy:
        await gate.acquire("p", 1, 1, max_wait_s=0.05)
    assert busy.value.active == 1
    assert not gate._pools["p"].line
    held.release()
    held.release()  # idempotent
    assert gate._pools["p"].active == 0
    assert (await gate.acquire("p", 1, 1)).waited_ms == 0, "a free place is taken at once"


async def test_a_cancelled_waiter_leaks_no_place():
    gate = FairShare()
    held = await gate.acquire("p", 1, 1)
    waiting = asyncio.create_task(gate.acquire("p", 2, 1))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    held.release()
    assert gate._pools["p"].active == 0 and not gate._pools["p"].line


async def test_free_places_are_never_left_empty_while_a_call_waits():
    gate = FairShare()
    held = [await gate.acquire("p", 1, 2) for _ in range(2)]
    waiters = [asyncio.create_task(gate.acquire("p", 2, 4)) for _ in range(2)]  # the limit was raised
    await _turns()
    assert all(w.done() for w in waiters), "a raised limit hands its new places to the line at once"
    for s in held + [w.result() for w in waiters]:
        s.release()


# ---- the cap across processes -------------------------------------------------------------------

class _SharedStore:
    """The slot half of the shared store, in memory; `down` makes every command fail."""

    def __init__(self) -> None:
        self.owners: dict[str, set[str]] = {}
        self.down = False

    async def acquire_slot(self, key, token, ttl_ms, limit):
        if self.down:
            return "unavailable"
        owners = self.owners.setdefault(key, set())
        if len(owners) >= limit:
            return "busy"
        owners.add(token)
        return "acquired"

    async def renew_slot(self, key, token, ttl_ms):
        return "unavailable" if self.down else "renewed" if token in self.owners.get(key, ()) else "lost"

    async def release_slot(self, key, token):
        if self.down:
            return "unavailable"
        owners = self.owners.get(key, set())
        if token not in owners:
            return "not_owned"
        owners.discard(token)
        return "released"

    def held(self, account: str) -> int:
        return len(self.owners.get(f"treg:provider-concurrency:v1:{account}", ()))


@pytest.fixture
def shared(monkeypatch):
    store = _SharedStore()
    monkeypatch.setattr(kv, "configured", lambda: True)
    monkeypatch.setattr(kv, "store", lambda: store)
    return store


async def test_the_cap_holds_across_processes(shared):
    one, two = FairShare(), FairShare()                   # two server processes, one account
    held = [await one.acquire("p", 1, 2), await two.acquire("p", 2, 2)]
    assert shared.held("p") == 2
    third = asyncio.create_task(two.acquire("p", 3, 2, max_wait_s=1))
    await asyncio.sleep(0.05)
    assert not third.done(), "a place free in its own process is not enough when the account is full"
    held[0].release()                                      # a place frees in the OTHER process
    slot = await asyncio.wait_for(third, 1)
    assert slot.waited_ms > 0 and shared.held("p") == 2
    for s in (held[1], slot):
        s.release()
    await _turns()
    assert shared.held("p") == 0
    assert one._pools["p"].active == 0 and two._pools["p"].active == 0


async def test_a_full_account_elsewhere_refuses_with_share_busy_and_keeps_no_place(shared):
    one, two = FairShare(), FairShare()
    held = await one.acquire("p", 1, 1)
    with pytest.raises(ShareBusy):
        await two.acquire("p", 2, 1, max_wait_s=0.05)
    assert two._pools["p"].active == 0 and shared.held("p") == 1
    held.release()
    await _turns()
    assert shared.held("p") == 0


async def test_a_call_cancelled_while_the_account_is_full_returns_both_places(shared):
    one, two = FairShare(), FairShare()
    held = await one.acquire("p", 1, 1)
    waiting = asyncio.create_task(two.acquire("p", 2, 1, max_wait_s=1))
    await asyncio.sleep(0.05)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    held.release()
    await _turns()
    assert two._pools["p"].active == 0 and shared.held("p") == 0


async def test_a_failing_store_falls_back_to_the_process_count(shared):
    shared.down = True
    one, two = FairShare(), FairShare()
    held = [await one.acquire("p", 1, 1), await two.acquire("p", 2, 1)]
    assert all(s.lease is None for s in held), "each process enforces the cap on its own"
    for s in held:
        s.release()


async def test_no_shared_store_takes_no_lease():
    assert not kv.configured()
    slot = await FairShare().acquire("p", 1, 1)
    assert slot.lease is None and slot.waited_ms == 0
    slot.release()
    assert await provider_concurrency.acquire("p", 1, wait_s=0) is None


# ---- the call path ------------------------------------------------------------------------------

@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(capacity_view, "concurrency", lambda provider: 1 if provider == "tikhub" else 0)
    monkeypatch.setenv("TREG_FAIR_SHARE_WAIT_S", "0.2")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_a_full_provider_refuses_with_share_busy_and_charges_nothing(
    clients: AsyncClient, platform_on, gated, monkeypatch,
):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    other_team = await fair_share.acquire("tikhub", -1, 1)   # another team holds the only place
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 429, r.text
    assert "share_busy" in r.text and "not charged" in r.text
    assert await _balance(clients) == before
    other_team.release()
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    assert fair_share._pools["tikhub"].active == 0, "the place is returned after the answer"


async def test_own_key_calls_are_never_gated(clients: AsyncClient, platform_on, gated, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})  # tier 2
    other_team = await fair_share.acquire("tikhub", -1, 1)
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    other_team.release()


async def test_a_provider_error_still_returns_the_place(clients: AsyncClient, platform_on, gated, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(0, raises=__import__("httpx").ConnectError("down")))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code >= 500
    assert fair_share._pools["tikhub"].active == 0
