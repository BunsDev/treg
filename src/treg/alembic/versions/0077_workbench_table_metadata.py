"""Add Workbench team database and table metadata without provisioning remote resources.

Revision ID: 0077
Revises: 0076
"""

from alembic import op
import sqlalchemy as sa

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "teamdatabase",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("turso_database_ref", sa.String(), nullable=False),
        sa.Column("connection_ref", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="provisioning"),
        sa.Column("database_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", name="uq_teamdatabase_org"),
        sa.UniqueConstraint("id", "org_id", name="uq_teamdatabase_id_org"),
        sa.UniqueConstraint("turso_database_ref", name="uq_teamdatabase_turso_ref"),
        sa.CheckConstraint("length(trim(turso_database_ref)) > 0", name="ck_teamdatabase_turso_ref"),
        sa.CheckConstraint("database_generation > 0", name="ck_teamdatabase_generation"),
        sa.CheckConstraint(
            "status != 'ready' OR (connection_ref IS NOT NULL AND length(trim(connection_ref)) > 0)",
            name="ck_teamdatabase_ready_connection",
        ),
    )
    op.create_table(
        "tabledoc",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("team_database_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("sql_name", sa.String(), nullable=False),
        sa.Column("creation_key", sa.String(), nullable=False),
        sa.Column("creation_fingerprint", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=True),
        sa.Column("metadata_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(), nullable=False, server_default="creating"),
        sa.Column("column_metadata", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_tabledoc_org_name"),
        sa.UniqueConstraint("org_id", "creation_key", name="uq_tabledoc_creation_request"),
        sa.UniqueConstraint("team_database_id", "sql_name", name="uq_tabledoc_database_sql_name"),
        sa.ForeignKeyConstraint(
            ["team_database_id", "org_id"], ["teamdatabase.id", "teamdatabase.org_id"],
            name="fk_tabledoc_team_database_owner",
        ),
        sa.CheckConstraint("schema_version IS NULL OR schema_version > 0", name="ck_tabledoc_schema_version"),
        sa.CheckConstraint("metadata_revision > 0", name="ck_tabledoc_metadata_revision"),
        sa.CheckConstraint("status != 'ready' OR schema_version IS NOT NULL", name="ck_tabledoc_ready_schema"),
    )


def downgrade() -> None:
    op.drop_table("tabledoc")
    op.drop_table("teamdatabase")
