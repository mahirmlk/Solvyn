"""Store verified GitHub workflow failure events for human triage.

Revision ID: 0013_proactive_events
Revises: 0012_integration_credentials
Create Date: 2026-11-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0013_proactive_events"
down_revision: str | None = "0012_integration_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "proactive_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("delivery_id", sa.String(length=128), nullable=False),
        sa.Column("source_event_id", sa.String(length=320), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("repository_name", sa.String(length=255), nullable=False),
        sa.Column("workflow_name", sa.String(length=255), nullable=False),
        sa.Column("conclusion", sa.String(length=50), nullable=False),
        sa.Column("branch", sa.String(length=255), server_default="", nullable=False),
        sa.Column("details_url", sa.String(length=512), server_default="", nullable=False),
        sa.Column("status", sa.String(length=50), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "project_id", "delivery_id", name="uq_proactive_events_project_delivery"),
        sa.UniqueConstraint("organization_id", "project_id", "source_event_id", name="uq_proactive_events_project_source_event"),
    )
    op.create_index("ix_proactive_events_organization_id", "proactive_events", ["organization_id"])
    op.create_index("ix_proactive_events_project_id", "proactive_events", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_proactive_events_project_id", table_name="proactive_events")
    op.drop_index("ix_proactive_events_organization_id", table_name="proactive_events")
    op.drop_table("proactive_events")
