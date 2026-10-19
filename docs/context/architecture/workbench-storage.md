---
title: Workbench table storage adapters
status: foundational
sources:
  - src/treg/infra/workbench_store.py
  - tests/test_workbench_store.py
related:
  - architecture/import-boundaries.md
---

# Workbench table storage adapters

`infra.workbench_store` is a standalone infrastructure boundary, not yet wired into product
routes or bootstrap. `TursoStore` uses SQL over HTTP; `SQLiteStore` uses a file-backed database
for local development and contract tests. Both expose `read` and an async `transaction` context
whose `execute` returns `StoreResult` rows and affected-row counts. Existing PostgreSQL storage
is unchanged.

## Caller boundary

The caller supplies trusted SQL and a server-selected database location; this module does not
authorize teams, validate user table definitions or provision databases. Bind values through
positional parameters. Database credentials and these adapters belong on the Treg server, not
inside user scripts. Do not hold a PostgreSQL connection while awaiting remote storage.

`read` enables SQLite's query-only mode. A transaction owns its connection and commits only on
successful context exit. Application errors, cancellation and failed statements cause rollback;
catching a statement error inside the context does not make the transaction committable again.
Await all operations before leaving the context. The context owns BEGIN, COMMIT and ROLLBACK;
callers must not issue transaction-control statements themselves.

## Remote failures

`TursoStore` uses the [Turso HTTP protocol](https://docs.turso.tech/sdk/http/reference), rotating
the connection baton after each response and closing the stream after use. Transaction deadlines
default to four seconds, below the provider's documented five-second window. Keep batches short
and do not perform unrelated I/O inside a transaction. The local adapter checks the same deadline,
but cancellation does not immediately stop work already queued on SQLite's background thread.

There are no automatic retries. A lost, malformed or cancelled COMMIT response raises
`StoreError("outcome_unknown")`: the write may already be durable. The caller must resolve that
uncertainty before replaying a write; durable write receipts are outside this module. Other errors
expose stable codes, not SQL values, database tokens or remote error bodies.

Configured remote URLs must use HTTPS (including normalized `libsql://` and `turso://` URLs).
HTTP redirects are disabled and response `base_url` values must match the configured origin;
credentials are never forwarded to a server-selected different host. Deployments requiring
cross-origin routing need an explicit trusted routing policy before use.

## Verification boundary

`test_workbench_store.py` runs the same storage contract against file-backed SQLite and an HTTP
protocol fixture backed by real SQLite transactions. It exercises atomic rollback, parameter
binding, conditional writes, cancellation and lost COMMIT responses. These tests do not replace
integration testing against a configured Turso Cloud database.
