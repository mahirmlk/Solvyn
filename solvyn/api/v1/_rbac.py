"""Shared RBAC helpers for Solvyn's API routers.

Every tenant-scoped mutation resolves membership in the organization that
owns the target resource and only then applies role checks. Cross-tenant
ids never reach the resource handlers.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.db.models import Organization, OrganizationMembership
from solvyn.multi_tenant.middleware import tenant_context

OWNER_ADMIN = ("owner", "admin")
MEMBER_OR_ABOVE = ("owner", "admin", "member")
ANY_ROLE = ("owner", "admin", "member", "viewer")


def require_tenant() -> uuid.UUID:
    """Reject unauthenticated callers and callers without a tenant scope."""
    if not tenant_context.is_authenticated or tenant_context.tenant_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return tenant_context.tenant_id


async def require_org_membership(
    db: AsyncSession,
    org_id: uuid.UUID,
    *,
    roles: tuple[str, ...] = ANY_ROLE,
    allow_viewer: bool = True,
) -> OrganizationMembership:
    tenant_id = require_tenant()
    user_id = tenant_context.user_id
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    membership = (
        await db.execute(
            select(OrganizationMembership)
            .join(Organization, Organization.id == OrganizationMembership.organization_id)
            .where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.organization_id == org_id,
                Organization.tenant_id == tenant_id,
                Organization.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    if membership.role not in roles:
        raise HTTPException(status_code=403, detail="Insufficient membership role")
    return membership


def org_role(membership: OrganizationMembership) -> str:
    return str(membership.role)
