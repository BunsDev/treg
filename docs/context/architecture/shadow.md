---
title: Shadow provider — a candidate asked the same question as served calls, after the fact, and compared
status: building
sources:
  - src/treg/domain/catalog/shadow.py
  - src/treg/application/shadow.py
  - src/treg/alembic/versions/0086_shadow_pairs.py
  - tests/test_shadow.py
related:
  - architecture/catalog.md
  - architecture/search-experiment.md
---

# Shadow provider

A way to measure a candidate provider on real traffic before any customer sees it: how often it
answers, how often its answer agrees with the one treg served, how fast it is and what it would
cost. Nothing about the candidate is in the public catalog; it is described only by the private
`shadow_provider` setting, and an empty setting turns the whole thing off.

## How it runs

- `treg-worker shadow collect` (a cron) reads new `CallRecord` rows after its cursor
  (`ShadowCursor`, one row). The first run only records the newest call id, so history is never read.
- A call is considered when its endpoint has a routing adapter and its capability is named in the
  setting. Calls are sampled per capability (`sample`, default all) by a salted hash of the call id.
- The served request and answer come from the archive, through the same reader the arena collector
  uses (`application.arena_insights._evidence`). A team that opted out of the archive is therefore
  never paired.
- `domain.catalog.shadow.request_identity` reads the served request back into the contract's fields
  through the served adapter's plain `in` map (fields set only by expressions cannot be read back).
  The candidate's adapter, from the setting, then builds its own request; an identity it does not
  accept is counted as not mappable and never sent.
- The candidate is called from the worker with its own key (`shadow_provider_key`), at most
  `shadow_concurrency` at once, `shadow_per_second` per second, each limited to `shadow_timeout_s`.
  No hold, no settle, no team balance: the customer's call finished long before.
- Both answers go through their adapters to the contract's fields; `compare` records, per shared
  scalar field, whether they agree (domains by host, names by similarity after removing accents and
  legal suffixes, other fields by normalized equality). Optional `count` and `with_email` output
  keys give list sizes, so "found" and "found with an email" are reported apart.

## What is stored

`ShadowPair`, one row per pair: capability, served endpoint, a salted hash of the identity, found on
each side, list counts, the per-field yes/no map, the candidate's status, time and billed units (from
`units_header`), and the served call's charge. No names, emails, phone numbers or answer bodies. Each
run deletes rows older than `shadow_retention_days`.

## The report

`GET /admin/shadow?days=N` (superadmin) and `treg-worker shadow report`: per capability and served
endpoint, the pair count, the pairs the candidate answered (a refusal, a timeout or a 5xx is no
answer: it counts in the error rate and stays out of every hit rate), each side's hit rate, the candidate's hit rate with an email, its error
rate, p50 and p95 time, per-field agreement, the candidate's units per 1,000 pairs and the served
cost per 1,000 calls.

## The setting

```json
{"base_url": "https://candidate.example", "units_header": "x-units",
 "sample": {"people.search": 0.05},
 "capabilities": {"companies.enrich": {"method": "GET", "path": "/company",
   "accepts": [["domain"]], "in": {"domain": "queryParams.domain"},
   "out": {"name": "data.name", "domain": "data.domain", "industry": "data.industry"},
   "miss": "data == null"}}}
```

The adapter keys are the routing YAML's (`accepts`, `in`, `in_expr`, `const`, `out`, `miss`), and
`count_with(rows, 'path')` counts the rows carrying a value at a path.
