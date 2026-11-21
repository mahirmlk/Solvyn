"""Organization-scoped integration connections."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1._rbac import ANY_ROLE, OWNER_ADMIN, require_org_membership
from solvyn.config import settings
from solvyn.db.models import Integration, IntegrationCredential
from solvyn.db.session import get_session
from solvyn.repositories.github import GitHubRepositoryError
from solvyn.repositories.github_app import GitHubAppClient

router = APIRouter(tags=["integrations"])
GITHUB_CREDENTIAL_EXTERNAL_ID = "organization-token"


async def _get_github_integration(db: AsyncSession, organization_id: uuid.UUID) -> Integration | None:
    result = await db.execute(
        select(Integration).where(
            Integration.organization_id == organization_id,
            Integration.provider == "github",
            Integration.external_id == GITHUB_CREDENTIAL_EXTERNAL_ID,
        )
    )
    return result.scalar_one_or_none()


@router.get("/organizations/{organization_id}/integrations/github")
async def get_github_integration_status(
    organization_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    await require_org_membership(db, organization_id, roles=ANY_ROLE)
    app_integration = (
        await db.execute(
            select(Integration).where(
                Integration.organization_id == organization_id,
                Integration.provider == "github",
                Integration.github_installation_id.is_not(None),
                Integration.is_active.is_(True),
            ).order_by(Integration.created_at.desc())
        )
    ).scalars().first()
    if app_integration is not None:
        return {
            "connected": True,
            "integration_id": str(app_integration.id),
            "installation_id": app_integration.github_installation_id,
            "account_id": app_integration.github_account_id,
            "account_login": app_integration.name.removeprefix("GitHub App (").removesuffix(")"),
            "app_slug": settings.github_app_slug or None,
            "auth_type": "github_app_installation",
        }
    integration = await _get_github_integration(db, organization_id)
    if integration is None or not integration.is_active:
        return {"connected": False, "integration_id": str(integration.id) if integration else None}

    credential = (
        await db.execute(select(IntegrationCredential).where(IntegrationCredential.integration_id == integration.id))
    ).scalar_one_or_none()
    return {
        "connected": False,
        "integration_id": str(integration.id),
        "account_login": credential.account_login if credential else None,
        "updated_at": credential.updated_at.isoformat() if credential and credential.updated_at else None,
        "app_slug": settings.github_app_slug or None,
        "auth_type": None,
        "legacy_token_present": credential is not None,
    }


@router.put("/organizations/{organization_id}/integrations/github")
async def connect_github_integration(
    organization_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    await require_org_membership(db, organization_id, roles=OWNER_ADMIN)
    raise HTTPException(
        status_code=410,
        detail="Personal access tokens are no longer accepted for repository access. Connect a GitHub App installation instead.",
    )


@router.delete("/organizations/{organization_id}/integrations/github")
async def disconnect_github_integration(
    organization_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    await require_org_membership(db, organization_id, roles=OWNER_ADMIN)
    integrations = (
        await db.execute(
            select(Integration).where(
                Integration.organization_id == organization_id,
                Integration.provider == "github",
            )
        )
    ).scalars().all()
    if not integrations:
        return {"connected": False}
    for integration in integrations:
        integration.is_active = False
        credential = (
            await db.execute(select(IntegrationCredential).where(IntegrationCredential.integration_id == integration.id))
        ).scalar_one_or_none()
        if credential is not None:
            await db.delete(credential)
    await db.commit()
    return {"connected": False, "integration_ids": [str(integration.id) for integration in integrations]}


class GitHubInstallationInput(BaseModel):
    installation_id: int = Field(gt=0)


@router.put("/organizations/{organization_id}/integrations/github/installation")
async def connect_github_installation(
    organization_id: uuid.UUID,
    payload: GitHubInstallationInput,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Bind an already-installed GitHub App installation to this organization.

    The caller must be an organization owner/admin. App JWT metadata verification
    prevents callers from binding an arbitrary account or installation ID.
    """
    await require_org_membership(db, organization_id, roles=OWNER_ADMIN)
    try:
        async with GitHubAppClient() as app:
            metadata = await app.installation_metadata(payload.installation_id)
    except GitHubRepositoryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    account_value = metadata.get("account")
    account = account_value if isinstance(account_value, dict) else {}
    account_id = account.get("id")
    login = account.get("login")
    account_type = account.get("type")
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id < 1 or not isinstance(login, str):
        raise HTTPException(status_code=502, detail="GitHub returned invalid installation account metadata")

    existing = (
        await db.execute(
            select(Integration).where(
                Integration.provider == "github",
                Integration.github_installation_id == payload.installation_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing.organization_id != organization_id:
        raise HTTPException(status_code=409, detail="This GitHub installation is already connected to another organization")
    integration = existing or Integration(
        organization_id=organization_id,
        provider="github",
        external_id=f"installation:{payload.installation_id}",
        name=f"GitHub App ({login})",
    )
    integration.github_installation_id = payload.installation_id
    integration.github_account_id = account_id
    integration.github_account_type = account_type if isinstance(account_type, str) else None
    integration.name = f"GitHub App ({login})"
    integration.is_active = True
    if existing is None:
        db.add(integration)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="GitHub installation changed concurrently; retry the request") from exc
    await db.refresh(integration)
    return {
        "connected": True,
        "integration_id": str(integration.id),
        "installation_id": integration.github_installation_id,
        "account_id": integration.github_account_id,
        "account_login": login,
        "account_type": integration.github_account_type,
    }
