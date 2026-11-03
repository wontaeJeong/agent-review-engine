"""Initial durable review-engine schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "jobs",
        sa.Column("key", sa.String(400), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("repository", sa.String(250), nullable=False),
        sa.Column("sha", sa.String(64), nullable=False),
        sa.Column("spec", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("next_run", sa.DateTime(timezone=True), nullable=False),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_jobs_status_next_run", "jobs", ["status", "next_run"])
    op.create_table(
        "deliveries", sa.Column("id", sa.String(150), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "repository_settings", sa.Column("repository", sa.String(250), primary_key=True),
        sa.Column("jev_enabled", sa.Boolean(), nullable=True),
        sa.Column("approval_mode", sa.String(24), nullable=True),
        sa.Column("auto_enqueue", sa.Boolean(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("repository_settings")
    op.drop_table("deliveries")
    op.drop_index("ix_jobs_status_next_run", table_name="jobs")
    op.drop_table("jobs")
