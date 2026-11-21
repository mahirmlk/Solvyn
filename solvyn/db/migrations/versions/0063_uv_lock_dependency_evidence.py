"""Persist dependency-install provenance for sandbox verification runs.

Revision ID: 0063_uv_lock_dependency_evidence
Revises: 0062_github_work_item_lifecycle
Create Date: 2026-11-21 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063_uv_lock_dependency_evidence"
down_revision: str | None = "0062_github_work_item_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    columns = (
        sa.Column("dependency_lock_sha256", sa.String(length=64), nullable=True),
        sa.Column("dependency_wheelhouse_sha256", sa.String(length=64), nullable=True),
        sa.Column("dependency_runtime_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("dependency_uv_version", sa.String(length=32), nullable=True),
        sa.Column("dependency_pip_version", sa.String(length=32), nullable=True),
        sa.Column("dependency_package_count", sa.Integer(), nullable=True),
        sa.Column("dependency_wheelhouse_bytes", sa.Integer(), nullable=True),
    )
    for column in columns:
        op.add_column("verification_runs", column)
    op.create_check_constraint(
        "ck_verification_runs_dependency_evidence_pair",
        "verification_runs",
        """(
            dependency_lock_sha256 IS NULL
            AND dependency_wheelhouse_sha256 IS NULL
            AND dependency_runtime_fingerprint IS NULL
            AND dependency_uv_version IS NULL
            AND dependency_pip_version IS NULL
            AND dependency_package_count IS NULL
            AND dependency_wheelhouse_bytes IS NULL
        ) OR (
            dependency_lock_sha256 IS NOT NULL
            AND length(dependency_lock_sha256) = 64
            AND dependency_wheelhouse_sha256 IS NOT NULL
            AND length(dependency_wheelhouse_sha256) = 64
            AND dependency_runtime_fingerprint IS NOT NULL
            AND length(dependency_runtime_fingerprint) = 64
            AND dependency_uv_version IS NOT NULL
            AND dependency_pip_version IS NOT NULL
            AND dependency_package_count > 0
            AND dependency_wheelhouse_bytes > 0
        )""",
    )


def downgrade() -> None:
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM verification_runs
                WHERE dependency_lock_sha256 IS NOT NULL
                   OR dependency_wheelhouse_sha256 IS NOT NULL
                   OR dependency_runtime_fingerprint IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'dependency installation evidence exists; export it before downgrading';
            END IF;
        END;
        $$"""
    )
    op.drop_constraint("ck_verification_runs_dependency_evidence_pair", "verification_runs", type_="check")
    for name in (
        "dependency_package_count",
        "dependency_wheelhouse_bytes",
        "dependency_pip_version",
        "dependency_uv_version",
        "dependency_runtime_fingerprint",
        "dependency_wheelhouse_sha256",
        "dependency_lock_sha256",
    ):
        op.drop_column("verification_runs", name)
