"""Record the public repository revision seeded into task workspaces.

Revision ID: 0015_task_workspace_repository_source
Revises: 0014_maintenance_schedules
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0015_task_workspace_repository_source"
down_revision: str | None = "0014_maintenance_schedules"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("task_workspaces", sa.Column("repository_id", UUID(as_uuid=True), nullable=True))
    op.add_column("task_workspaces", sa.Column("source_commit_sha", sa.String(length=64), nullable=True))
    op.add_column("task_workspaces", sa.Column("source_paths", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_task_workspaces_repository_id_repositories",
        "task_workspaces",
        "repositories",
        ["repository_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_task_workspaces_repository_id", "task_workspaces", ["repository_id"])


def downgrade() -> None:
    op.drop_index("ix_task_workspaces_repository_id", table_name="task_workspaces")
    op.drop_constraint("fk_task_workspaces_repository_id_repositories", "task_workspaces", type_="foreignkey")
    op.drop_column("task_workspaces", "source_paths")
    op.drop_column("task_workspaces", "source_commit_sha")
    op.drop_column("task_workspaces", "repository_id")
