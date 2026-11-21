from __future__ import annotations

import runpy
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_github_app_migration_adds_numeric_identity_pair_constraints() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "solvyn"
        / "db"
        / "migrations"
        / "versions"
        / "0064_github_app_installation_scope.py"
    )
    migration = runpy.run_path(str(migration_path))
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    migration["upgrade"].__globals__["op"] = Operations(context)

    migration["upgrade"]()

    sql = output.getvalue()
    assert migration["revision"] == "0064_github_app_installation_scope"
    assert migration["down_revision"] == "0063_uv_lock_dependency_evidence"
    assert "ck_integrations_github_installation_account_pair" in sql
    assert "(github_installation_id IS NULL) = (github_account_id IS NULL)" in sql
    assert "ck_repositories_github_repository_binding" in sql
    assert "github_repository_id IS NULL OR (provider = 'github' AND integration_id IS NOT NULL)" in sql
