---
title: Workbench table storage
status: foundational
sources:
  - src/treg/models.py
  - src/treg/alembic/versions/0077_workbench_table_metadata.py
  - src/treg/domain/governance/teams.py
  - tests/test_workbench_table_schema.py
  - src/treg/infra/workbench_store.py
  - tests/test_workbench_store.py
  - tests/test_workbench_store_lifecycle.py
related:
  - architecture/import-boundaries.md
---

# Workbench table storage

## Management metadata

Revision `0077` adds `TeamDatabase` and `TableDoc` to the existing control-plane database.
It creates empty management tables only: no team is provisioned, no Turso SQL table is created,
and no table API or runtime is enabled. Business rows belong in team storage, not in these records.

`TeamDatabase` is unique per Org and per Turso resource reference. `turso_database_ref` identifies
the fixed target including its provider organization, not just a display name or a bare database
name shared by several accounts. `connection_ref` is an opaque reference to trusted connection
configuration, including separate read-only and read-write credentials; never store tokens here.
Non-null connection references are unique across teams. The resolver must still verify the target
and credential scope: different references could otherwise point to the same remote database.
The mapping starts as `provisioning` with a nullable connection reference. `ready` requires a
nonempty reference; verifying the remote resource and its access remains the writer's responsibility.
`database_generation` starts at one and must stay positive. It fences a replaced/restored database;
it is not a table schema revision.

`TeamDatabase.org_id` and `TableDoc`'s ownership references deliberately have no database foreign
keys. Creating these tables must not acquire foreign-key locks on the existing Org table.
Referential integrity belongs to the application: future management writers must verify that the
Org exists and that a table's database mapping belongs to that Org, including on reassignment.
Data access must resolve both records within the authenticated team's scope; an unchecked database
ID is not authorization. This schema-only change does not implement those writers or access guards.

`TableDoc` keeps a stable Table ID, display and SQL names, creation key and fingerprint, column
metadata, and schema/metadata revisions. Names and creation keys are unique within an Org;
physical SQL names are unique within a database. The table starts as `creating` with no confirmed
schema version or validated database generation. `ready` requires both to be positive.
Metadata revisions also stay positive.
The application writer must compare request fingerprints before resuming a duplicate creation,
verify the physical schema before publishing readiness, and update revisions and timestamps.
These constraints do not implement provisioning, immutable creation operations, or recovery leases.

Table IDs, names and creation identities cannot be empty or space-only. Physical names must match
the lowercase ASCII form `[a-z_][a-z0-9_]*` and cannot start with SQLite's reserved `sqlite_` prefix.
This makes their uniqueness agree with Turso's case-insensitive identifiers; DDL writers must still
quote identifiers. SQLite uses a built-in GLOB check with a NUL guard, PostgreSQL a C-collated regular
expression. Column metadata uses PostgreSQL JSONB and SQLite JSON; its application schema still
needs writer validation.

`TableDoc.database_generation` records the database generation in which the physical table was last
verified. It has no default and stays null until verification; it must not automatically follow
`TeamDatabase.database_generation` on restore. Future data access must require both records to be
ready and their generations to match. Restored tables become eligible only after physical identity
and schema verification updates their recorded generation. Missing or unresolved tables keep their
old generation and remain ineligible. These schema fields do not implement an access guard yet;
the provisioning, restore and data-access writers/readers must enforce that contract when added.

`cascade_delete_org` removes TableDoc records before their TeamDatabase mapping through the common
`ORG_SCOPED_MODELS` list, without relying on database cascades. Direct deletes outside this path can
leave orphaned metadata. This cleans control-plane metadata only. Remote-resource cleanup must be
connected before provisioning is enabled. An application rollback retains these additive tables;
an explicit schema downgrade drops their metadata and does not delete any remote resource.

## Storage adapters

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

`TursoStore` requires separate database-scoped credentials for reads and writes. Provision
`read_auth_token` with Turso's `--read-only` permission; the service enforces the read boundary.
The caller must supply tokens for the same database and rotate them before expiry. Reads never
fall back to the write token. Do not rely on `PRAGMA query_only = ON` for remote reads: Turso Cloud
can reject that statement, and a pipeline can still execute subsequent statements after an error.

`SQLiteStore.read` enables query-only mode and opens the file with `mode=ro`, so reading a missing
database fails without creating an empty file. Read and write connections use the configured
SQLite busy timeout. A transaction owns its connection and commits only on
successful context exit. Errors and cancellation before COMMIT cause rollback; catching a
statement error inside the context does not make the transaction committable again. A failed
rollback preserves the original error.
Await all operations before leaving the context. The context owns BEGIN, COMMIT and ROLLBACK;
callers must not issue transaction-control statements themselves.

## Remote failures

`TursoStore` uses the [Turso HTTP protocol](https://docs.turso.tech/sdk/http/reference), rotating
the connection baton after each response and closing the stream after use. Transaction deadlines
default to four seconds, below the provider's documented five-second window. The timeout must be
greater than 0.5 seconds and less than five. Both adapters refuse to send COMMIT with less than
0.5 seconds remaining, returning `transaction_expired` and rolling back. This reserve reduces
avoidable uncertainty; it does not guarantee COMMIT will finish before the deadline. Keep batches
short and do not perform unrelated I/O inside a transaction. The local adapter checks the same deadline,
but cancellation does not immediately stop work already queued on SQLite's background thread.

There are no automatic retries. A transport failure or unreadable COMMIT result raises
`StoreError("outcome_unknown")` and sets `tx.outcome_unknown`: the write may already be durable.
Cancellation during COMMIT sets the same flag but propagates `CancelledError`, preserving
`asyncio.timeout` and `TaskGroup` behavior. The caller must resolve that uncertainty before replaying
a write; durable write receipts are outside this module. An explicit COMMIT statement error keeps
its stable error code (for example, `constraint_violation` for a deferred foreign key failure).
An acknowledged COMMIT result is not changed by a subsequent close error. Other errors expose
stable codes, not SQL values, database tokens or remote error bodies.

Configured remote URLs must use HTTPS (including normalized `libsql://` and `turso://` URLs).
HTTP redirects are disabled and response `base_url` values must match the configured origin;
hostnames are normalized and the default HTTPS port is omitted before comparison. Database
credentials are never forwarded to a server-selected different host. Deployments requiring
cross-origin routing need an explicit trusted routing policy before use.

Each read or transaction owns one HTTP client; requests within a transaction reuse that client.
Cross-operation pooling is deferred until bootstrap owns client creation and shutdown.
Remote BLOB results accept both padded and unpadded standard Base64: Turso Cloud can omit the
padding. Invalid Base64 still fails as `invalid_response`.

## Verification boundary

`test_workbench_table_schema.py` exercises constraints, the absence of foreign keys, pending/ready
transitions, team deletion, and upgrade/downgrade behavior against both model-created and migrated
schemas. A PostgreSQL regression holds an Org update open while upgrading the Workbench schema,
so adding a foreign key that blocks the migration fails the test. PostgreSQL CI runs
the same tests; the full Alembic drift check also compares the deployed schema with the models.

`test_workbench_store.py` and `test_workbench_store_lifecycle.py` run the same storage contract against
file-backed SQLite and an HTTP protocol fixture backed by real SQLite transactions. They exercise
atomic rollback, parameter
binding, conditional writes, cancellation and lost COMMIT responses. These tests do not replace
integration testing against a configured Turso Cloud database. Before business integration, verify
the provisioned read-only token on the selected cloud engine: a SELECT must succeed and a mutation
through `read` must fail without changing data. Also verify transaction rollback and explicit
COMMIT errors. Test credentials must stay outside repository files and logs.
