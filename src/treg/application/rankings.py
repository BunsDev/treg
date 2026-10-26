"""The /rankings page's boards: one bounded read of the folded day buckets, kept for an hour.

The read is `EndpointDayStat` only (the `treg-worker catalog stats` read model, about thirty days
of one row per endpoint per day), never `callrecord`, and in two parts sized to what the boards use.
The counts of every kept day come without the latency reservoirs, which are most of the table; the
reservoirs come only for the seven days "Fastest" ranks, and only their first `SAMPLES_PER_DAY`
values. Every position of a reservoir is a uniform pick of that day's durations, so a prefix is one
too, and `stats.Tally` weights each kept value by `latency_seen` over what was kept. Reading every
row's full reservoir into one web process would cost hundreds of megabytes, once an hour, on every
process. The boards change once a day, so each process
keeps its answer for `TTL_S`; a read costs tens of milliseconds, so a few concurrent misses at
expiry simply read twice. A failed read is not cached.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

from sqlalchemy import cast, func, literal_column, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.catalog import rankings, store
from ..domain.catalog.stats import WINDOW_DAYS
from ..models import EndpointDayStat, EndpointStatCursor
from ..timeutil import utcnow_naive

TTL_S = 3600
SAMPLES_PER_DAY = 50   # latency values read per endpoint per day; the median needs no more

_cache: tuple[float, dict] | None = None


def _last_complete_day(watermark, today: date) -> date:
    """The newest UTC day the worker has folded in full: the day before both today and the day
    of the last call it consumed (a worker that stopped mid-day leaves that day partial)."""
    last = today - timedelta(days=1)
    if watermark is not None:
        last = min(last, watermark.date() - timedelta(days=1))
    return last


async def _read(db: AsyncSession) -> dict:
    today = utcnow_naive().date()
    cursor = await db.get(EndpointStatCursor, "callrecord")
    last_day = _last_complete_day(cursor.watermark if cursor else None, today)
    since = (last_day - timedelta(days=WINDOW_DAYS)).isoformat()
    end = last_day.isoformat()
    counts = await db.execute(select(
        EndpointDayStat.endpoint_id, EndpointDayStat.day, EndpointDayStat.ok, EndpointDayStat.bad,
        EndpointDayStat.latency_seen,
    ).where(EndpointDayStat.day >= since, EndpointDayStat.day <= end))
    rows = {(e, d): rankings.DayRow(endpoint_id=e, day=d, ok=ok or 0, bad=bad or 0, latency_seen=seen or 0)
            for e, d, ok, bad, seen in counts.all()}
    sample = EndpointDayStat.latency_sample
    if db.get_bind().dialect.name == "postgresql":
        # Cut each reservoir in the database, so the rest never crosses the wire.
        sample = func.jsonb_path_query_array(cast(EndpointDayStat.latency_sample, JSONB),
                                             literal_column(f"'$[0 to {int(SAMPLES_PER_DAY) - 1}]'::jsonpath"), type_=JSONB)
    week = (last_day - timedelta(days=6)).isoformat()
    samples = await db.execute(select(EndpointDayStat.endpoint_id, EndpointDayStat.day, sample).where(
        EndpointDayStat.day >= week, EndpointDayStat.day <= end, EndpointDayStat.latency_seen > 0))
    for e, d, values in samples.all():
        if (e, d) in rows:
            rows[e, d].latencies = list(values or [])[:SAMPLES_PER_DAY]
    await db.close()   # release the connection before the in-memory work
    rows = list(rows.values())
    return rankings.build(rows, store.load(), last_day)


async def boards(db: AsyncSession) -> dict:
    """The boards, from this process's cache when it is younger than `TTL_S`."""
    global _cache
    if _cache and time.monotonic() - _cache[0] < TTL_S:
        return _cache[1]
    body = await _read(db)
    _cache = (time.monotonic(), body)
    return body


def reset_cache() -> None:
    """Tests: forget the cached boards."""
    global _cache
    _cache = None
