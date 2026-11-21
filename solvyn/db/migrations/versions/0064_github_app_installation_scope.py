"""Bind GitHub App installations and repositories to numeric provider identities.

Revision ID: 0064_github_app_installation_scope
Revises: 0063_uv_lock_dependency_evidence
Create Date: 2026-11-21 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064_github_app_installation_scope"
down_revision: str | None = "0063_uv_lock_dependency_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("integrations", sa.Column("github_installation_id", sa.BigInteger(), nullable=True))
    op.add_column("integrations", sa.Column("github_account_id", sa.BigInteger(), nullable=True))
    op.add_column("integrations", sa.Column("github_account_type", sa.String(length=32), nullable=True))
    op.create_unique_constraint(
        "uq_integrations_github_installation_id", "integrations", ["github_installation_id"]
    )
    op.create_check_constraint(
        "ck_integrations_github_installation_id_positive",
        "integrations",
        "github_installation_id IS NULL OR github_installation_id > 0",
    )
    op.create_check_constraint(
        "ck_integrations_github_account_id_positive",
        "integrations",
        "github_account_id IS NULL OR github_account_id > 0",
    )
    op.create_check_constraint(
        "ck_integrations_github_installation_account_pair",
        "integrations",
        "(github_installation_id IS NULL) = (github_account_id IS NULL)",
    )

    op.add_column("repositories", sa.Column("github_repository_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_repositories_github_repository_id", "repositories", ["github_repository_id"])
    op.create_check_constraint(
        "ck_repositories_github_repository_id_positive",
        "repositories",
        "github_repository_id IS NULL OR github_repository_id > 0",
    )
    op.create_check_constraint(
        "ck_repositories_github_repository_binding",
        "repositories",
        "github_repository_id IS NULL OR (provider = 'github' AND integration_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM integrations
                WHERE github_installation_id IS NOT NULL OR github_account_id IS NOT NULL
            ) OR EXISTS (
                SELECT 1 FROM repositories WHERE github_repository_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'Cannot downgrade GitHub App identity migration while installation/repository bindings exist';
            END IF;
        END
        $$;"""
    )
    op.drop_constraint("ck_repositories_github_repository_binding", "repositories", type_="check")
    op.drop_constraint("ck_repositories_github_repository_id_positive", "repositories", type_="check")
    op.drop_index("ix_repositories_github_repository_id", table_name="repositories")
    op.drop_column("repositories", "github_repository_id")

    op.drop_constraint("ck_integrations_github_account_id_positive", "integrations", type_="check")
    op.drop_constraint("ck_integrations_github_installation_account_pair", "integrations", type_="check")
    op.drop_constraint("ck_integrations_github_installation_id_positive", "integrations", type_="check")
    op.drop_constraint("uq_integrations_github_installation_id", "integrations", type_="unique")
    op.drop_column("integrations", "github_account_type")
    op.drop_column("integrations", "github_account_id")
    op.drop_column("integrations", "github_installation_id")
