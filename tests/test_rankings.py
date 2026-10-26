"""/rankings: the boards built from the folded day buckets, their floors, and the served page."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from httpx import AsyncClient

from treg.application import rankings as rankings_app
from treg.domain.catalog import rankings, store
from treg.infra.db import session_maker
from treg.models import EndpointDayStat
from treg.timeutil import utcnow_naive

LAST = date(2026, 10, 25)


def _sync_endpoints(n: int) -> list[dict]:
    """`n` browsable synchronous endpoints from distinct providers."""
    out, providers = [], set()
    for ep in store.load().endpoints:
        if store.browsable(ep) and not ep.get("async") and not ep.get("status") \
                and not ep.get("observed_from") and ep["provider"] not in providers:
            out.append(ep)
            providers.add(ep["provider"])
        if len(out) == n:
            return out
    raise AssertionError("the catalog has too few providers for this test")


def _day(back: int) -> str:
    return (LAST - timedelta(days=back)).isoformat()


def _rows(ep_id: str, per_day: dict[int, int], **kw) -> list[rankings.DayRow]:
    return [rankings.DayRow(endpoint_id=ep_id, day=_day(back), ok=ok, **kw) for back, ok in per_day.items()]


def _week(calls: int, *, back: int = 0) -> dict[int, int]:
    """`calls` spread over the seven days ending `back` days before LAST."""
    days = {back + i: calls // 7 for i in range(7)}
    days[back] += calls - sum(days.values())
    return days


def test_counts_windows_and_change():
    a, b = _sync_endpoints(2)
    rows = (_rows(a["id"], _week(2_000) | _week(1_000, back=7) | {20: 500})
            + _rows(b["id"], _week(700)))
    out = rankings.build(rows, store.load(), LAST)
    assert out["through"] == LAST.isoformat() and out["has_change"]
    week = {r["provider"]: r for r in out["tools"]["7d"]}
    assert week[a["provider"]]["calls"] == 2_000 and week[a["provider"]]["change"] == 100.0
    assert week[b["provider"]]["change"] == "new"
    month = {r["provider"]: r["calls"] for r in out["tools"]["30d"]}
    assert month[a["provider"]] == 3_500 and "change" not in out["tools"]["30d"][0]
    assert out["endpoints"]["7d"][0]["id"] == a["id"]


def test_today_and_unknown_endpoints_are_left_out():
    (a,) = _sync_endpoints(1)
    rows = (_rows(a["id"], {0: 10, -1: 999})
            + [rankings.DayRow(endpoint_id="nobody.knows.this", day=_day(0), ok=5_000)])
    out = rankings.build(rows, store.load(), LAST)
    assert [r["calls"] for r in out["tools"]["7d"]] == [10]


def test_no_change_without_a_full_week_before():
    (a,) = _sync_endpoints(1)
    out = rankings.build(_rows(a["id"], _week(5_000)), store.load(), LAST)
    assert not out["has_change"] and "change" not in out["tools"]["7d"][0]
    assert out["trending"] == {"providers": [], "endpoints": []}


def test_trending_floor_and_new_first():
    a, b, c = _sync_endpoints(3)
    rows = (_rows(a["id"], _week(3_000) | _week(1_000, back=7))     # tripled, over the floor
            + _rows(b["id"], _week(900) | _week(10, back=7))         # 90x, but under the floor
            + _rows(c["id"], _week(1_500) | {13: 0}))                # new this week
    out = rankings.build(rows, store.load(), LAST)
    ids = [r["id"] for r in out["trending"]["endpoints"]]
    assert ids == [c["id"], a["id"]]
    assert out["trending"]["endpoints"][1]["change"] == 200.0


def test_reliable_and_fastest_floors():
    a, b = _sync_endpoints(2)
    rows = (_rows(a["id"], {0: 95}, bad=5, latency_seen=40, latencies=[300] * 40)
            + _rows(b["id"], {0: 50}, bad=0, latency_seen=10, latencies=[50] * 10))
    out = rankings.build(rows, store.load(), LAST)
    assert [(r["provider"], r["ok_rate"]) for r in out["reliable"]] == [(a["provider"], 95.0)]
    assert [(r["id"], r["p50_ms"]) for r in out["fastest"]] == [(a["id"], 300)]


def test_async_jobs_are_not_judged_on_speed_or_reliability():
    ep = next(e for e in store.load().endpoints if store.browsable(e) and e.get("async")
              and not e.get("status") and not e.get("observed_from"))
    rows = _rows(ep["id"], {0: 500}, bad=0, latency_seen=500, latencies=[10] * 50)
    out = rankings.build(rows, store.load(), LAST)
    assert out["reliable"] == [] and out["fastest"] == []
    assert out["tools"]["7d"][0]["calls"] == 500


def test_by_category_shares_add_up():
    a, b = _sync_endpoints(2)
    out = rankings.build(_rows(a["id"], {0: 300}) + _rows(b["id"], {0: 100}), store.load(), LAST)
    for c in out["by_category"]:
        assert round(sum(p["share"] for p in c["providers"])) == 100


async def test_page_serves_the_boards_in_the_html(clients: AsyncClient):
    rankings_app.reset_cache()
    (a,) = _sync_endpoints(1)
    yesterday = (utcnow_naive().date() - timedelta(days=1)).isoformat()
    async with session_maker() as db:
        db.add(EndpointDayStat(endpoint_id=a["id"], day=yesterday, n=40, ok=40, latency_sample=[]))
        await db.commit()
    try:
        r = await clients.get("/rankings")
        assert r.status_code == 200
        assert "{BASE}" not in r.text and "{DATA}" not in r.text
        (block,) = re.findall(r'<script type="application/json" id="rk-data">(.*?)</script>', r.text, re.S)
        data = json.loads(block)
        assert data["through"] == yesterday
        assert data["tools"]["7d"][0] == data["tools"]["7d"][0] | {"provider": a["provider"], "calls": 40}
        assert "endpoints" not in data   # the other boards load when the reader comes near them
        board = await clients.get("/rankings/data/endpoints")
        assert board.status_code == 200
        assert board.json()["endpoints"]["7d"][0]["id"] == a["id"]
        for name, key in (("trending", "trending"), ("categories", "by_category"),
                          ("reliable", "reliable"), ("fastest", "fastest")):
            assert key in (await clients.get(f"/rankings/data/{name}")).json()
        assert (await clients.get("/rankings/data/nothing")).status_code == 404
        assert "/rankings<" in (await clients.get("/sitemap.xml")).text
    finally:
        rankings_app.reset_cache()


async def test_page_with_no_data_still_serves(clients: AsyncClient):
    rankings_app.reset_cache()
    try:
        r = await clients.get("/rankings")
        assert r.status_code == 200
        (block,) = re.findall(r'id="rk-data">(.*?)</script>', r.text, re.S)
        assert json.loads(block)["tools"] == {"7d": [], "30d": []}
    finally:
        rankings_app.reset_cache()


def test_the_chart_follows_each_window_and_its_leaders():
    a, b = _sync_endpoints(2)
    rows = _rows(a["id"], {0: 10, 20: 5_000}) + _rows(b["id"], {0: 90})
    out = rankings.build(rows, store.load(), LAST)
    week, month = out["chart"]["7d"], out["chart"]["30d"]
    assert len(week["days"]) == 7 and week["days"][-1] == LAST.isoformat()
    assert len(month["days"]) == 21   # only the days the buckets reach back to
    assert [s["provider"] for s in week["series"]] == [b["provider"], a["provider"]]
    assert [s["provider"] for s in month["series"]] == [a["provider"], b["provider"]]


def test_categories_are_ordered_by_this_weeks_calls():
    a, b = _sync_endpoints(2)
    out = rankings.build(_rows(a["id"], {0: 10}) + _rows(b["id"], {0: 900}), store.load(), LAST)
    cat = store.load().platforms
    busy = (cat.get(b["platform"]) or {}).get("category") or "Other"
    assert out["categories"][0] == busy
    assert [c["category"] for c in out["by_category"]] == out["categories"]


async def test_the_read_keeps_only_the_first_samples_of_each_day(clients: AsyncClient):
    """Reservoirs are most of the table: the page reads at most SAMPLES_PER_DAY values of each."""
    rankings_app.reset_cache()
    (a,) = _sync_endpoints(1)
    yesterday = (utcnow_naive().date() - timedelta(days=1)).isoformat()
    kept, dropped = [100] * rankings_app.SAMPLES_PER_DAY, [900] * 150
    async with session_maker() as db:
        db.add(EndpointDayStat(endpoint_id=a["id"], day=yesterday, n=200, ok=200,
                               latency_seen=200, latency_sample=kept + dropped))
        await db.commit()
    try:
        fastest = (await clients.get("/rankings/data/fastest")).json()["fastest"]
        assert [(r["id"], r["p50_ms"]) for r in fastest] == [(a["id"], 100)]
    finally:
        rankings_app.reset_cache()
