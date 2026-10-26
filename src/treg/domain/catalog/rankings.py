"""The public /rankings boards, computed from the folded per-endpoint, per-day buckets.

Pure: the caller reads the `EndpointDayStat` rows (`application.rankings`) and passes them in with
the catalog and the last complete UTC day; nothing here touches the database or the audit table.

What counts is a successful call (`ok`, a 2xx the provider answered), the same word the catalog's
reliability numbers use, so a cheap search and an expensive render weigh the same.

The usage boards (most used tools, most called endpoints, the daily chart, top by category) have
no floor on purpose: they list every tool with calls, so a tool only one team uses shows that
team's volume, though never the team. The three boards that rate or compare have floors, so a
small base cannot make a large percentage:

- trending ranks only rows with at least `TRENDING_MIN_CALLS` successful calls this week;
- most reliable ranks tools (all their endpoints together) with at least `RELIABLE_MIN_DECIDED`
  decided calls (2xx plus 5xx, the denominator `stats.publish` uses; a 4xx is the caller's mistake);
- fastest ranks only endpoints with at least `FASTEST_MIN_TIMED` timed successful calls.

Jobs that finish later (`async` endpoints) are left out of reliability and speed: their bucket's
duration is the submit, not the job, and their success may still be reconciled.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import stats, store

TRENDING_MIN_CALLS = 1_000
RELIABLE_MIN_DECIDED = 100
FASTEST_MIN_TIMED = 20
NEW_FIRST = 5            # trending lists at most this many brand-new rows before the risers
TOP = 20                 # rows per board, and per category on a filterable board: 10, then 10 more
CHART_SERIES = 10        # providers drawn by name on the daily chart; the rest is "Others"
WINDOWS = {"7d": 7, "30d": 30}


@dataclass
class DayRow:
    """One `EndpointDayStat` row, reduced to what the boards read."""
    endpoint_id: str
    day: str
    ok: int = 0
    bad: int = 0
    latency_seen: int = 0
    latencies: list[int] = field(default_factory=list)


def _days(last: date, count: int, *, back: int = 0) -> tuple[str, str]:
    end = last - timedelta(days=back)
    return (end - timedelta(days=count - 1)).isoformat(), end.isoformat()


def _change(cur: int, prev: int) -> float | str | None:
    """Percent change against the window before, rounded; "new" when there was nothing before."""
    if prev <= 0:
        return "new" if cur > 0 else None
    return round((cur - prev) * 100 / prev, 1)


def _keep_per_category(rows: list[dict], top: int = TOP) -> list[dict]:
    """The first `top` rows overall plus the first `top` of each category, in the given order, so
    a filter on a small category still has a full board without shipping every row."""
    seen: dict[str, int] = defaultdict(int)
    out = []
    for i, row in enumerate(rows):
        seen[row["category"]] += 1
        if i < top or seen[row["category"]] <= top:
            out.append(row)
    return out


def build(rows: list[DayRow], cat: store.Catalog, last_day: date) -> dict:
    """Every board the page shows, through `last_day` (the newest complete UTC day)."""
    eps: dict[str, dict] = {}
    for ep in cat.endpoints:
        if store.browsable(ep) and not ep.get("status"):
            eps[ep["id"]] = ep
    since = {i: str(ep.get("observed_from") or "") for i, ep in eps.items()}
    rows = [r for r in rows if r.endpoint_id in eps and r.day <= last_day.isoformat()
            and r.day >= since[r.endpoint_id]]
    oldest = min((r.day for r in rows), default=None)

    def category(ep: dict) -> str:
        return (cat.platforms.get(ep["platform"]) or {}).get("category") or "Other"

    def label(provider: str) -> str:
        return store.provider_label(provider)

    def ep_view(ep_id: str) -> dict:
        ep = eps[ep_id]
        return {"id": ep_id, "name": ep.get("name") or ep_id, "provider": ep["provider"],
                "label": label(ep["provider"]), "category": category(ep)}

    def window_sum(start: str, end: str) -> tuple[dict[str, int], dict[str, int]]:
        by_ep: dict[str, int] = defaultdict(int)
        by_provider: dict[str, int] = defaultdict(int)
        for r in rows:
            if start <= r.day <= end and r.ok:
                by_ep[r.endpoint_id] += r.ok
                by_provider[eps[r.endpoint_id]["provider"]] += r.ok
        return by_ep, by_provider

    # A change is shown only when the window before lies wholly inside the kept buckets.
    prev7 = _days(last_day, 7, back=7)
    has_prev7 = oldest is not None and oldest <= prev7[0]
    cur7_ep, cur7_pr = window_sum(*_days(last_day, 7))
    prev7_ep, prev7_pr = window_sum(*prev7)

    provider_eps: dict[str, set[str]] = defaultdict(set)
    provider_cats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    tools: dict[str, list[dict]] = {}
    endpoints: dict[str, list[dict]] = {}
    for key, length in WINDOWS.items():
        by_ep, by_pr = (cur7_ep, cur7_pr) if length == 7 else window_sum(*_days(last_day, length))
        if length == 30:
            for ep_id, calls in by_ep.items():
                provider_eps[eps[ep_id]["provider"]].add(ep_id)
                provider_cats[eps[ep_id]["provider"]][category(eps[ep_id])] += calls
        ep_rows = []
        for ep_id, calls in sorted(by_ep.items(), key=lambda kv: (-kv[1], kv[0])):
            row = ep_view(ep_id) | {"calls": calls}
            if length == 7 and has_prev7:
                row["change"] = _change(calls, prev7_ep.get(ep_id, 0))
            ep_rows.append(row)
        endpoints[key] = _keep_per_category(ep_rows)
        tool_rows = []
        for provider, calls in sorted(by_pr.items(), key=lambda kv: (-kv[1], kv[0])):
            row = {"provider": provider, "label": label(provider), "calls": calls}
            if length == 7 and has_prev7:
                row["change"] = _change(calls, prev7_pr.get(provider, 0))
            tool_rows.append(row)
        tools[key] = tool_rows[:TOP]
    for key in WINDOWS:
        for row in tools[key]:
            cats = provider_cats.get(row["provider"]) or {}
            row["category"] = max(cats, key=lambda c: (cats[c], c)) if cats else ""
            row["endpoints"] = len(provider_eps.get(row["provider"], ()))

    # Trending: the week against the week before, among rows past the floor.
    def risers(cur: dict[str, int], prev: dict[str, int], view) -> list[dict]:
        if not has_prev7:
            return []
        fresh, grown = [], []
        for k, calls in cur.items():
            if calls < TRENDING_MIN_CALLS:
                continue
            before = prev.get(k, 0)
            row = view(k) | {"calls": calls, "prev": before, "change": _change(calls, before)}
            (fresh if before == 0 else grown).append(row)
        fresh.sort(key=lambda r: (-r["calls"], str(r.get("id") or r.get("provider"))))
        grown = [r for r in grown if r["change"] > 0]
        grown.sort(key=lambda r: (-r["change"], -r["calls"]))
        return (fresh[:NEW_FIRST] + grown)[:TOP]

    trending = {
        "providers": risers(cur7_pr, prev7_pr, lambda p: {"provider": p, "label": label(p)}),
        "endpoints": risers(cur7_ep, prev7_ep, ep_view),
    }

    # Top by category: each category's providers by their share of its successful calls this week.
    cat_pr: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for ep_id, calls in cur7_ep.items():
        cat_pr[category(eps[ep_id])][eps[ep_id]["provider"]] += calls
    by_category = []
    for name, providers in cat_pr.items():
        total = sum(providers.values())
        ranked = sorted(providers.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        by_category.append({"category": name, "calls": total, "providers": [
            {"provider": p, "label": label(p), "calls": c, "share": round(c * 100 / total, 1)}
            for p, c in ranked]})
    by_category.sort(key=lambda c: (-c["calls"], c["category"]))

    # Reliability and speed over the same seven days, synchronous endpoints only. Reliability is
    # per tool: a provider error (5xx) is mostly the provider's own systems, whatever was called.
    # Speed stays per endpoint: it depends on the job, and one tool's jobs differ.
    start7, end7 = _days(last_day, 7)
    tallies = stats.merged(
        (r.endpoint_id, stats.Tally(ok=r.ok, bad=r.bad, latency_seen=r.latency_seen,
                                    latencies=list(r.latencies)))
        for r in rows if start7 <= r.day <= end7 and not eps[r.endpoint_id].get("async"))
    reliable, fastest = [], []
    by_tool: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])   # ok, decided, endpoints
    for ep_id, t in tallies.items():
        if t.ok + t.bad:
            acc = by_tool[eps[ep_id]["provider"]]
            acc[0] += t.ok; acc[1] += t.ok + t.bad; acc[2] += 1
    for provider, (ok, decided, n_eps) in by_tool.items():
        if decided >= RELIABLE_MIN_DECIDED:
            cats = provider_cats.get(provider) or {}
            reliable.append({"provider": provider, "label": label(provider),
                             "category": max(cats, key=lambda c: (cats[c], c)) if cats else "",
                             "endpoints": n_eps, "ok_rate": round(ok * 100 / decided, 2), "decided": decided})
    for ep_id, t in tallies.items():
        if t.latency_seen >= FASTEST_MIN_TIMED and len(t.latencies) >= stats.MIN_SAMPLES:
            p50 = t.percentile(0.50)
            if p50 is not None:
                fastest.append(ep_view(ep_id) | {"p50_ms": p50, "timed": t.latency_seen})
    reliable.sort(key=lambda r: (-r["ok_rate"], -r["decided"], r["provider"]))
    fastest.sort(key=lambda r: (r["p50_ms"], -r["timed"], r["id"]))

    # The daily charts, one per window: that window's leaders by name (the same tools, in the same
    # order, as its list), everything else summed as "Others".
    per_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        if r.ok:
            per_day[r.day][eps[r.endpoint_id]["provider"]] += r.ok
    chart = {}
    for key, length in WINDOWS.items():
        days = [(last_day - timedelta(days=i)).isoformat() for i in range(length - 1, -1, -1)]
        days = [d for d in days if oldest is not None and d >= oldest]
        leaders = [r["provider"] for r in tools[key][:CHART_SERIES]]
        series = [{"provider": p, "label": label(p), "values": [per_day[d].get(p, 0) for d in days]}
                  for p in leaders]
        others = [sum(v for p, v in per_day[d].items() if p not in leaders) for d in days]
        if any(others):
            series.append({"provider": "", "label": "Others", "values": others})
        chart[key] = {"days": days, "series": series}

    # Every category with calls in the kept days, busiest this week first: the order of the chips
    # and of every category menu on the page.
    week_calls: dict[str, int] = defaultdict(int)
    for ep_id, calls in cur7_ep.items():
        week_calls[category(eps[ep_id])] += calls
    seen = {category(eps[r.endpoint_id]) for r in rows} | set(week_calls)
    categories = sorted(seen, key=lambda c: (-week_calls.get(c, 0), c))
    return {
        "through": last_day.isoformat(),
        "has_change": has_prev7,
        "floors": {"trending": TRENDING_MIN_CALLS, "reliable": RELIABLE_MIN_DECIDED,
                   "fastest": FASTEST_MIN_TIMED},
        "categories": categories,
        "chart": chart,
        "tools": tools,
        "endpoints": endpoints,
        "trending": trending,
        "by_category": by_category,
        "reliable": _keep_per_category(reliable),
        "fastest": _keep_per_category(fastest),
    }
