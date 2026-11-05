"""
Squashed baseline: the original 0001_api_keys migration referenced
tenants(id) before tenants existed, so a fresh `alembic upgrade head`
failed on an empty database. 0001 is now a no-op (see
0001_api_keys.py) and ALL schema — including api_keys — is created here,
in dependency order, so upgrade-from-zero works. Existing deployments with
tables created outside Alembic must verify their schema and stamp this revision
instead of replaying the baseline DDL:

    # Make sure required tables already exist, then stamp:
    alembic stamp 0002_phase1_product_foundation

pgvector: memory.embedding uses Vector(1536) on Postgres so the
embeddings path works; on SQLite test paths the type degrades to Text.

Revision ID: 0002_phase1_product_foundation
Revises: 0001_api_keys
Create Date: 2026-11-04 00:00:00.000000
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0002_phase1_product_foundation"
down_revision: str | None = "0001_api_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(col: str, server_default: bool = True) -> sa.Column[datetime]:
    return sa.Column(col, sa.DateTime(timezone=True), server_default=sa.func.now() if server_default else None)


def upgrade() -> None:
    # --- Baseline: pre-migration tables (were created via create_all) ---
    op.create_table(
        "tenants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("api_key_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(50), server_default="user"),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"])

    # The old 0001 migration attempted this before its tenants foreign key
    # existed. Create it here, after tenants, so a clean database can upgrade.
    op.create_table(
        "api_keys",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_hash", sa.String(255), nullable=False),
        sa.Column("key_prefix", sa.String(12), nullable=False),
        _ts("created_at"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])

    op.create_table(
        "sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("title", sa.String(255), server_default=""),
        sa.Column("status", sa.String(50), server_default="active"),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_sessions_tenant_id", "sessions", ["tenant_id"])

    op.create_table(
        "messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        _ts("created_at"),
    )
    op.create_index("ix_messages_session_id", "messages", ["session_id"])

    op.create_table(
        "agent_instances",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("agent_type", sa.String(100), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("config", sa.Text(), nullable=True),
        sa.Column("status", sa.String(50), server_default="idle"),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_agent_instances_tenant_id", "agent_instances", ["tenant_id"])

    op.create_table(
        "memory",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=True),
        sa.Column("agent_type", sa.String(100), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=True),  # pgvector Vector(1536) in prod; Text on sqlite path
        _ts("created_at"),
    )
    op.create_index("ix_memory_tenant_id", "memory", ["tenant_id"])
    op.create_index("ix_memory_key", "memory", ["key"])
    op.create_unique_constraint("uq_memory_tenant_agent_key", "memory", ["tenant_id", "agent_type", "key"])

    op.create_table(
        "artifacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=True),
        sa.Column("agent_type", sa.String(100), nullable=False),
        sa.Column("artifact_type", sa.String(100), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        _ts("created_at"),
    )
    op.create_index("ix_artifacts_tenant_id", "artifacts", ["tenant_id"])

    op.create_table(
        "tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("assigned_agent", sa.String(100), nullable=True),
        sa.Column("status", sa.String(50), server_default="pending"),
        sa.Column("priority", sa.Integer(), server_default="0"),
        sa.Column("parent_task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=True),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_tasks_tenant_id", "tasks", ["tenant_id"])

    op.create_table(
        "execution_graphs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("graph_data", sa.Text(), nullable=False),
        sa.Column("status", sa.String(50), server_default="running"),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_execution_graphs_session_id", "execution_graphs", ["session_id"])

    op.create_table(
        "token_usage",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=True),
        sa.Column("agent_type", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), server_default="0"),
        sa.Column("total_tokens", sa.Integer(), server_default="0"),
        sa.Column("cost", sa.Numeric(12, 6), server_default="0"),
        _ts("created_at"),
    )
    op.create_index("ix_token_usage_tenant_id", "token_usage", ["tenant_id"])

    # --- Phase 1: identity ---
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), server_default=""),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        sa.Column("is_superuser", sa.Boolean(), server_default="false"),
        _ts("created_at"), _ts("updated_at"),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False),
        _ts("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", UUID(as_uuid=True), sa.ForeignKey("refresh_tokens.id"), nullable=True),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"])

    op.create_table(
        "organizations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_organizations_slug", "organizations", ["slug"])
    op.create_index("ix_organizations_tenant_id", "organizations", ["tenant_id"])

    op.create_table(
        "organization_memberships",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("role", sa.String(50), server_default="member"),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_organization_memberships_user_id", "organization_memberships", ["user_id"])
    op.create_index("ix_organization_memberships_organization_id", "organization_memberships", ["organization_id"])
    op.create_unique_constraint("uq_membership_user_org", "organization_memberships", ["user_id", "organization_id"])

    op.create_table(
        "workspaces",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_workspaces_organization_id", "workspaces", ["organization_id"])

    op.create_table(
        "projects",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("instructions", sa.Text(), server_default=""),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_projects_organization_id", "projects", ["organization_id"])
    op.create_index("ix_projects_tenant_id", "projects", ["tenant_id"])
    op.create_unique_constraint("uq_projects_org_slug", "projects", ["organization_id", "slug"])

    op.create_table(
        "project_memberships",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("role", sa.String(50), server_default="member"),
        _ts("created_at"),
    )
    op.create_index("ix_project_memberships_user_id", "project_memberships", ["user_id"])
    op.create_index("ix_project_memberships_project_id", "project_memberships", ["project_id"])
    op.create_unique_constraint("uq_project_membership_user_project", "project_memberships", ["user_id", "project_id"])

    op.create_table(
        "integrations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), server_default=""),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_integrations_organization_id", "integrations", ["organization_id"])
    op.create_index("ix_integrations_external_id", "integrations", ["external_id"])
    op.create_unique_constraint("uq_integrations_org_provider_external", "integrations", ["organization_id", "provider", "external_id"])

    op.create_table(
        "repositories",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("integration_id", UUID(as_uuid=True), sa.ForeignKey("integrations.id"), nullable=True),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(50), server_default="github"),
        sa.Column("default_branch", sa.String(255), server_default="main"),
        sa.Column("url", sa.String(512), server_default=""),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_repositories_project_id", "repositories", ["project_id"])
    op.create_unique_constraint("uq_repositories_project_full_name", "repositories", ["project_id", "full_name"])

    op.create_table(
        "teammate_profiles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("role", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("responsibilities", sa.Text(), server_default=""),
        sa.Column("system_instructions", sa.Text(), server_default=""),
        sa.Column("agent_type", sa.String(100), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default="true"),
        sa.Column("status", sa.String(50), server_default="idle"),
        sa.Column("model_provider", sa.String(50), nullable=True),
        sa.Column("model_name", sa.String(100), nullable=True),
        sa.Column("max_tokens_per_run", sa.Integer(), nullable=True),
        sa.Column("max_run_seconds", sa.Integer(), nullable=True),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_teammate_profiles_organization_id", "teammate_profiles", ["organization_id"])
    op.create_index("ix_teammate_profiles_tenant_id", "teammate_profiles", ["tenant_id"])

    op.create_table(
        "teammate_permissions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("teammate_id", UUID(as_uuid=True), sa.ForeignKey("teammate_profiles.id"), nullable=False),
        sa.Column("permission", sa.String(50), nullable=False),
        sa.Column("granted", sa.Boolean(), server_default="true"),
        _ts("created_at"),
    )
    op.create_index("ix_teammate_permissions_teammate_id", "teammate_permissions", ["teammate_id"])
    op.create_unique_constraint("uq_teammate_permissions_teammate_permission", "teammate_permissions", ["teammate_id", "permission"])

    op.create_table(
        "teammate_project_assignments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("teammate_id", UUID(as_uuid=True), sa.ForeignKey("teammate_profiles.id"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        _ts("created_at"),
    )
    op.create_index("ix_teammate_project_assignments_teammate_id", "teammate_project_assignments", ["teammate_id"])
    op.create_index("ix_teammate_project_assignments_project_id", "teammate_project_assignments", ["project_id"])
    op.create_unique_constraint("uq_teammate_assignment_teammate_project", "teammate_project_assignments", ["teammate_id", "project_id"])

    op.create_table(
        "project_knowledge",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_project_knowledge_project_id", "project_knowledge", ["project_id"])
    op.create_unique_constraint("uq_knowledge_project_category_key", "project_knowledge", ["project_id", "category", "key"])

    op.create_table(
        "approvals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("teammate_id", UUID(as_uuid=True), sa.ForeignKey("teammate_profiles.id"), nullable=True),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("status", sa.String(50), server_default="pending"),
        sa.Column("requested_by", sa.String(255), server_default=""),
        sa.Column("decided_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), server_default=""),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_approvals_organization_id", "approvals", ["organization_id"])


def downgrade() -> None:
    for table in [
        "approvals", "project_knowledge", "teammate_project_assignments", "teammate_permissions",
        "teammate_profiles", "repositories", "integrations", "project_memberships", "projects",
        "workspaces", "organization_memberships", "organizations", "refresh_tokens", "users",
        "api_keys", "token_usage", "execution_graphs", "tasks", "artifacts", "memory", "agent_instances",
        "messages", "sessions", "tenants",
    ]:
        op.drop_table(table)
