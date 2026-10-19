from datetime import datetime
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

from treg.domain.governance.teams import cascade_delete_org
from treg.infra import db
from treg.models import Org, TableDoc, TeamDatabase


migration = import_module("treg.alembic.versions.0077_workbench_table_metadata")
MODELS = (TeamDatabase, TableDoc)
NOW = datetime(2026, 1, 1)


def _apply(connection, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, direction)()


def _database(team_id=1, **overrides):
    return {
        "id": team_id, "org_id": team_id,
        "turso_database_ref": f"test-organization/team-{team_id}",
        "created_at": NOW, "updated_at": NOW, **overrides,
    }


def _table(org_id=1, name="leads", **overrides):
    return {
        "id": f"tbl_{org_id}_{name}", "org_id": org_id, "team_database_id": org_id,
        "name": name, "sql_name": f"data_{name}", "creation_key": f"create-{name}",
        "creation_fingerprint": "table-fingerprint",
        "column_metadata": [{"id": "name", "name": "Name", "type": "text"}],
        "created_by": "jeremy@example.test", "created_at": NOW, "updated_at": NOW, **overrides,
    }


@pytest.fixture(params=["model", "migration"])
async def store_connection(request):
    await db.reset_db()
    async with db._engine.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            await connection.execute(sa.text("PRAGMA foreign_keys=ON"))
            await connection.commit()
        transaction = await connection.begin()
        try:
            for org_id in (1, 2):
                await connection.execute(Org.__table__.insert().values(
                    id=org_id, name=f"Workbench {org_id}", slug=f"workbench-{org_id}", balance_micro=123,
                ))
            if request.param == "migration":
                for model in reversed(MODELS):
                    await connection.run_sync(model.__table__.drop)
                await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
            yield connection
        finally:
            await transaction.rollback()
            if sqlite:
                await connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
                await connection.commit()


async def test_empty_upgrade_matches_models_and_preserves_existing_teams(store_connection):
    connection = store_connection
    names = {model.__tablename__ for model in MODELS}
    diff = await connection.run_sync(lambda sync: compare_metadata(
        MigrationContext.configure(sync, opts={
            "compare_server_default": True,
            "include_object": lambda obj, name, kind, reflected, other: kind != "table" or name in names,
        }), SQLModel.metadata,
    ))
    assert diff == []
    for model in MODELS:
        assert (await connection.execute(sa.select(sa.func.count()).select_from(model))).scalar_one() == 0
    assert (await connection.execute(sa.select(Org.id, Org.balance_micro).order_by(Org.id))).all() == [(1, 123), (2, 123)]


@pytest.mark.parametrize("overrides", [
    {"org_id": 1}, {"turso_database_ref": "test-organization/team-1"}, {"org_id": 999},
])
async def test_database_identity_and_ownership_are_unique(store_connection, overrides):
    table = TeamDatabase.__table__
    await store_connection.execute(table.insert().values(**_database()))
    with pytest.raises(sa.exc.IntegrityError):
        async with store_connection.begin_nested():
            await store_connection.execute(table.insert().values(**_database(2, **overrides)))
    await store_connection.execute(table.insert().values(**_database(2)))
    assert (await store_connection.execute(sa.select(table.c.org_id).order_by(table.c.org_id))).scalars().all() == [1, 2]


@pytest.mark.parametrize("duplicate", ["name", "sql_name", "creation_key"])
async def test_table_identities_are_unique_within_their_team(store_connection, duplicate):
    for org_id in (1, 2):
        await store_connection.execute(TeamDatabase.__table__.insert().values(**_database(org_id)))
        await store_connection.execute(TableDoc.__table__.insert().values(**_table(org_id)))
    candidate = {**_table(name="other"), duplicate: _table()[duplicate]}
    with pytest.raises(sa.exc.IntegrityError):
        async with store_connection.begin_nested():
            await store_connection.execute(TableDoc.__table__.insert().values(**candidate))
    await store_connection.execute(TableDoc.__table__.insert().values(**_table(name="other")))
    assert (await store_connection.execute(sa.select(sa.func.count()).select_from(TableDoc))).scalar_one() == 3


@pytest.mark.parametrize("update", [False, True])
@pytest.mark.parametrize("owner", [{"org_id": 2}, {"team_database_id": 2}])
async def test_table_cannot_reference_another_teams_database(store_connection, update, owner):
    table = TableDoc.__table__
    for org_id in (1, 2):
        await store_connection.execute(TeamDatabase.__table__.insert().values(**_database(org_id)))
    if update:
        await store_connection.execute(table.insert().values(**_table()))
        statement = table.update().where(table.c.id == _table()["id"]).values(**owner)
    else:
        statement = table.insert().values(**{**_table(), **owner})
    with pytest.raises(sa.exc.IntegrityError):
        async with store_connection.begin_nested():
            await store_connection.execute(statement)
    if not update:
        await store_connection.execute(table.insert().values(**_table()))
    assert (await store_connection.execute(sa.select(table.c.org_id, table.c.team_database_id))).one() == (1, 1)


@pytest.mark.parametrize("invalid", [
    {"turso_database_ref": None}, {"turso_database_ref": ""}, {"turso_database_ref": " "},
    {"database_generation": 0}, {"database_generation": -1},
    {"status": "ready"}, {"status": "ready", "connection_ref": ""},
    {"status": "ready", "connection_ref": " "},
])
async def test_database_requires_identity_generation_and_ready_connection(store_connection, invalid):
    table = TeamDatabase.__table__
    await store_connection.execute(table.insert().values(**_database()))
    with pytest.raises(sa.exc.IntegrityError):
        async with store_connection.begin_nested():
            await store_connection.execute(table.update().values(**invalid))
    row = (await store_connection.execute(sa.select(table))).mappings().one()
    assert row["status"] == "provisioning" and row["connection_ref"] is None
    assert row["database_generation"] == 1


@pytest.mark.parametrize("invalid", [
    {"schema_version": 0}, {"schema_version": -1},
    {"metadata_revision": 0}, {"metadata_revision": -1},
    {"column_metadata": None}, {"status": "ready"},
])
async def test_table_revisions_and_ready_schema_are_validated(store_connection, invalid):
    table = TableDoc.__table__
    await store_connection.execute(TeamDatabase.__table__.insert().values(**_database()))
    await store_connection.execute(table.insert().values(**_table()))
    with pytest.raises(sa.exc.IntegrityError):
        async with store_connection.begin_nested():
            await store_connection.execute(table.update().values(**invalid))
    row = (await store_connection.execute(sa.select(table))).mappings().one()
    assert row["status"] == "creating" and row["schema_version"] is None
    assert row["metadata_revision"] == 1 and row["column_metadata"] == _table()["column_metadata"]


async def test_database_and_table_can_be_promoted_from_pending_to_ready(store_connection):
    connection = store_connection
    database, table = (model.__table__ for model in MODELS)
    await connection.execute(database.insert().values(**_database()))
    await connection.execute(table.insert().values(**_table()))
    await connection.execute(database.update().values(status="ready", connection_ref="test-connection", database_generation=2))
    await connection.execute(table.update().values(status="ready", schema_version=1, metadata_revision=2))
    database_row = (await connection.execute(sa.select(database))).mappings().one()
    table_row = (await connection.execute(sa.select(table))).mappings().one()
    assert database_row["status"] == "ready" and database_row["database_generation"] == 2
    assert table_row["status"] == "ready" and table_row["schema_version"] == 1
    assert table_row["metadata_revision"] == 2
    assert table_row["created_at"] == table_row["updated_at"] == NOW
    assert table_row["creation_fingerprint"] == "table-fingerprint"


async def test_database_defaults_apply_without_model_insert_defaults(store_connection):
    connection = store_connection
    tables = await connection.run_sync(lambda sync: {
        model: sa.Table(model.__tablename__, sa.MetaData(), autoload_with=sync) for model in MODELS
    })
    await connection.execute(tables[TeamDatabase].insert().values(**_database()))
    await connection.execute(tables[TableDoc].insert().values(**_table()))
    database_row = (await connection.execute(sa.select(tables[TeamDatabase]))).mappings().one()
    table_row = (await connection.execute(sa.select(tables[TableDoc]))).mappings().one()
    assert database_row["status"] == "provisioning" and database_row["database_generation"] == 1
    assert database_row["connection_ref"] is None
    assert table_row["status"] == "creating" and table_row["metadata_revision"] == 1
    assert table_row["schema_version"] is None


async def test_team_deletion_removes_only_its_database_and_table_metadata(store_connection):
    connection = store_connection
    for org_id in (1, 2):
        await connection.execute(TeamDatabase.__table__.insert().values(**_database(org_id)))
        await connection.execute(TableDoc.__table__.insert().values(**_table(org_id)))
    async with AsyncSession(bind=connection, expire_on_commit=False) as session:
        org = await session.get(Org, 1)
        await cascade_delete_org(org, session)
        await session.flush()
    for model in MODELS:
        assert (await connection.execute(sa.select(model.org_id))).scalars().all() == [2]
    assert (await connection.execute(sa.select(Org.id, Org.balance_micro))).all() == [(2, 123)]


async def test_downgrade_preserves_teams_and_allows_a_fresh_upgrade(store_connection):
    connection = store_connection
    await connection.execute(TeamDatabase.__table__.insert().values(**_database()))
    await connection.execute(TableDoc.__table__.insert().values(**_table()))
    await connection.run_sync(lambda sync: _apply(sync, "downgrade"))
    names = await connection.run_sync(lambda sync: sa.inspect(sync).get_table_names())
    assert all(model.__tablename__ not in names for model in MODELS)
    assert (await connection.execute(sa.select(Org.id, Org.balance_micro).order_by(Org.id))).all() == [(1, 123), (2, 123)]
    await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
    for model in MODELS:
        assert (await connection.execute(sa.select(sa.func.count()).select_from(model))).scalar_one() == 0
