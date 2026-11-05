"""Organization-scoped integration connections."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1._rbac import ANY_ROLE, OWNER_ADMIN, require_org_membership
from solvyn.db.models import Integration, IntegrationCredential
from solvyn.db.session import get_session
from solvyn.multi_tenant.middleware import tenant_context
from solvyn.repositories.credentials import CredentialEncryptionUnavailableError, create_credential_vault
from solvyn.repositories.github import GitHubClient, GitHubRepositoryError

router = APIRouter(tags=["integrations"])
GITHUB_CREDENTIAL_EXTERNAL_ID = "organization-token"


class GitHubCredentialInput(BaseModel):
    access_token: SecretStr


def _encryption_unavailable(exc: CredentialEncryptionUnavailableError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


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
    integration = await _get_github_integration(db, organization_id)
    if integration is None or not integration.is_active:
        return {"connected": False, "integration_id": str(integration.id) if integration else None}

    credential = (
        await db.execute(select(IntegrationCredential).where(IntegrationCredential.integration_id == integration.id))
    ).scalar_one_or_none()
    return {
        "connected": credential is not None,
        "integration_id": str(integration.id),
        "account_login": credential.account_login if credential else None,
        "updated_at": credential.updated_at.isoformat() if credential and credential.updated_at else None,
    }


@router.put("/organizations/{organization_id}/integrations/github")
async def connect_github_integration(
    organization_id: uuid.UUID,
    payload: GitHubCredentialInput,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    await require_org_membership(db, organization_id, roles=OWNER_ADMIN)
    token = payload.access_token.get_secret_value().strip()
    if not 20 <= len(token) <= 512:
        raise HTTPException(status_code=422, detail="GitHub access token length is invalid")
    try:
        vault = create_credential_vault()
    except CredentialEncryptionUnavailableError as exc:
        raise _encryption_unavailable(exc) from exc

    try:
        async with GitHubClient(token=token) as github:
            identity = await github.authenticated_identity()
    except GitHubRepositoryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    integration = await _get_github_integration(db, organization_id)
    if integration is None:
        integration = Integration(
            organization_id=organization_id,
            provider="github",
            external_id=GITHUB_CREDENTIAL_EXTERNAL_ID,
            name="GitHub",
        )
        db.add(integration)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(status_code=409, detail="GitHub integration changed concurrently; retry the request") from exc
    else:
        integration.is_active = True

    credential = (
        await db.execute(select(IntegrationCredential).where(IntegrationCredential.integration_id == integration.id))
    ).scalar_one_or_none()
    encrypted_token = vault.encrypt(token)
    if credential is None:
        credential = IntegrationCredential(
            integration_id=integration.id,
            ciphertext=encrypted_token,
            account_login=identity["login"],
            updated_by=tenant_context.user_id,
        )
        db.add(credential)
    else:
        credential.ciphertext = encrypted_token
        credential.account_login = identity["login"]
        credential.updated_by = tenant_context.user_id
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="GitHub credential changed concurrently; retry the request") from exc
    await db.refresh(integration)
    await db.refresh(credential)
    return {
        "connected": True,
        "integration_id": str(integration.id),
        "account_login": credential.account_login,
        "updated_at": credential.updated_at.isoformat() if credential.updated_at else None,
    }


@router.delete("/organizations/{organization_id}/integrations/github")
async def disconnect_github_integration(
    organization_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    await require_org_membership(db, organization_id, roles=OWNER_ADMIN)
    integration = await _get_github_integration(db, organization_id)
    if integration is None:
        return {"connected": False}
    credential = (
        await db.execute(select(IntegrationCredential).where(IntegrationCredential.integration_id == integration.id))
    ).scalar_one_or_none()
    if credential is not None:
        await db.delete(credential)
    integration.is_active = False
    await db.commit()
    return {"connected": False, "integration_id": str(integration.id)}
