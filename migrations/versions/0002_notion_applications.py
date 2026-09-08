"""Read-only Notion snapshots and observed application history; no seed data."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notion_applications",
        sa.Column("page_id", sa.String(36), primary_key=True),
        sa.Column("data_source_id", sa.String(36), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("max_observed_stage", sa.Integer(), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("missing", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "application_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("page_id", sa.String(36), sa.ForeignKey("notion_applications.page_id"), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_application_events_page_id", "application_events", ["page_id"])
    op.create_table(
        "application_links",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("page_id", sa.String(36), sa.ForeignKey("notion_applications.page_id"), nullable=False),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=False),
        sa.UniqueConstraint("page_id", "job_id"),
    )
    for table in ("notion_applications", "application_events", "application_links"):
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))


def downgrade():
    op.drop_table("application_links")
    op.drop_table("application_events")
    op.drop_table("notion_applications")
