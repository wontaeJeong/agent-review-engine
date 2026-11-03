"""Add audit records and server-defined model profile selection.

Revision ID: 0002_audit_profiles
Revises: 0001_initial
Create Date: 2026-11-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_audit_profiles"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("repository_settings", sa.Column("llm_profile", sa.String(48), nullable=True))
    op.create_table(
        "audit_events",
        sa.Column("fingerprint", sa.String(500), primary_key=True),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("repository", sa.String(250), nullable=False),
        sa.Column("subject", sa.String(400), nullable=False),
        sa.Column("sha", sa.String(64), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_repo_time", "audit_events", ["repository", "created_at"])


def downgrade():
    op.drop_index("ix_audit_repo_time", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_column("repository_settings", "llm_profile")
