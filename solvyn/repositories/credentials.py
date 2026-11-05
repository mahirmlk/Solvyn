"""Encryption helpers for third-party integration credentials."""

from __future__ import annotations

import uuid

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.config import settings
from solvyn.db.models import Integration, IntegrationCredential


class CredentialEncryptionUnavailableError(RuntimeError):
    """Raised when no usable integration encryption key is configured."""


class GitHubCredentialNotFoundError(RuntimeError):
    """Raised when the organization has no active GitHub credential."""


class IntegrationCredentialVault:
    """Encrypt credentials at rest with Fernet and support key rotation.

    ``keys`` is an ordered list of Fernet keys. The first key encrypts new
    values; all keys can decrypt existing values during a rotation window.
    """

    def __init__(self, keys: str | None = None) -> None:
        configured = settings.integration_encryption_keys if keys is None else keys
        key_values = [value.strip().encode("ascii") for value in configured.split(",") if value.strip()]
        if not key_values:
            raise CredentialEncryptionUnavailableError("GitHub credential encryption is not configured")
        try:
            self._fernet = MultiFernet([Fernet(value) for value in key_values])
        except (TypeError, ValueError) as exc:
            raise CredentialEncryptionUnavailableError("GitHub credential encryption keys are invalid") from exc

    def encrypt(self, plaintext: str) -> bytes:
        if not plaintext:
            raise ValueError("Credential cannot be empty")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise CredentialEncryptionUnavailableError("Credential could not be decrypted with configured keys") from exc


def create_credential_vault() -> IntegrationCredentialVault:
    """Create a vault from the currently configured rotation key set."""
    return IntegrationCredentialVault()


async def load_organization_github_token(db: AsyncSession, organization_id: uuid.UUID) -> str:
    """Decrypt the active GitHub token for a single organization."""
    integration = (
        await db.execute(
            select(Integration).where(
                Integration.organization_id == organization_id,
                Integration.provider == "github",
                Integration.external_id == "organization-token",
                Integration.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        raise GitHubCredentialNotFoundError("GitHub organization integration is not connected")

    credential = (
        await db.execute(select(IntegrationCredential).where(IntegrationCredential.integration_id == integration.id))
    ).scalar_one_or_none()
    if credential is None:
        raise GitHubCredentialNotFoundError("GitHub organization integration is not connected")
    return create_credential_vault().decrypt(credential.ciphertext)
