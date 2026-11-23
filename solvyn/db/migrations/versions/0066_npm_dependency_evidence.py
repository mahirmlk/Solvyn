"""Persist honest, platform-bound npm dependency evidence.

Revision ID: 0066_npm_dependency_evidence
Revises: 0065_dependabot_alert_lifecycle
Create Date: 2026-11-23 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066_npm_dependency_evidence"
down_revision: str | None = "0065_dependabot_alert_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

_COLUMNS = (
    sa.Column("dependency_ecosystem", sa.String(length=16), nullable=True),
    sa.Column("dependency_lockfile_sha256", sa.String(length=64), nullable=True),
    sa.Column("dependency_artifact_sha256", sa.String(length=64), nullable=True),
    sa.Column("dependency_artifact_bytes", sa.Integer(), nullable=True),
    sa.Column("dependency_artifact_package_count", sa.Integer(), nullable=True),
    sa.Column("dependency_environment_fingerprint", sa.String(length=64), nullable=True),
    sa.Column("dependency_runtime_version", sa.String(length=32), nullable=True),
    sa.Column("dependency_installer_version", sa.String(length=32), nullable=True),
    sa.Column("dependency_runtime_platform", sa.String(length=16), nullable=True),
    sa.Column("dependency_runtime_architecture", sa.String(length=16), nullable=True),
)

_CHECK = """(
    dependency_ecosystem IS NULL
    AND dependency_lockfile_sha256 IS NULL
    AND dependency_artifact_sha256 IS NULL
    AND dependency_artifact_bytes IS NULL
    AND dependency_artifact_package_count IS NULL
    AND dependency_environment_fingerprint IS NULL
    AND dependency_runtime_version IS NULL
    AND dependency_installer_version IS NULL
    AND dependency_runtime_platform IS NULL
    AND dependency_runtime_architecture IS NULL
    AND (dependency_lock_sha256 IS NULL OR dependency_wheelhouse_sha256 IS NOT NULL)
) OR (
    dependency_ecosystem = 'npm'
    AND dependency_lockfile_sha256 IS NOT NULL
    AND length(dependency_lockfile_sha256) = 64
    AND dependency_artifact_sha256 IS NOT NULL
    AND length(dependency_artifact_sha256) = 64
    AND dependency_artifact_bytes IS NOT NULL
    AND dependency_artifact_bytes > 0
    AND dependency_artifact_package_count IS NOT NULL
    AND dependency_artifact_package_count > 0
    AND dependency_environment_fingerprint IS NOT NULL
    AND length(dependency_environment_fingerprint) = 64
    AND dependency_runtime_version IS NOT NULL
    AND dependency_installer_version IS NOT NULL
    AND dependency_runtime_platform IS NOT NULL
    AND dependency_runtime_platform = 'linux'
    AND dependency_runtime_architecture IS NOT NULL
    AND dependency_runtime_architecture IN ('x64', 'arm64')
    AND dependency_lock_sha256 IS NULL
    AND dependency_wheelhouse_sha256 IS NULL
    AND dependency_runtime_fingerprint IS NULL
    AND dependency_uv_version IS NULL
    AND dependency_pip_version IS NULL
    AND dependency_package_count IS NULL
    AND dependency_wheelhouse_bytes IS NULL
)"""


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("verification_runs", column)
    op.create_check_constraint(
        "ck_verification_runs_generic_dependency_evidence",
        "verification_runs",
        _CHECK,
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM verification_runs WHERE dependency_ecosystem IS NOT NULL) THEN
                RAISE EXCEPTION 'npm dependency evidence exists; export it before downgrading';
            END IF;
        END $$"""
    )
    op.drop_constraint("ck_verification_runs_generic_dependency_evidence", "verification_runs", type_="check")
    for column in reversed(_COLUMNS):
        op.drop_column("verification_runs", column.name)
