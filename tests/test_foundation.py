"""Unit + integration tests for Phase 1 product foundation.

Covers: password/JWT primitives, config fail-fast, tenant-binding auth
(P0-001/P0-003), and the domain model invariants (least-privilege defaults,
secret rejection in knowledge, cross-tenant isolation at the service layer).
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from solvyn.db.models import PermissionLevel

# ---------------------------------------------------------------------------
# Password + JWT primitives
# ---------------------------------------------------------------------------


def test_password_hash_verify_roundtrip() -> None:
    from solvyn.identity.security import hash_password, verify_password

    h = hash_password("correct horse battery")
    assert h != "correct horse battery"
    assert verify_password("correct horse battery", h) is True
    assert verify_password("wrong password", h) is False


def test_password_rejects_short() -> None:
    from solvyn.identity.security import hash_password

    with pytest.raises(ValueError):
        hash_password("short")


def test_jwt_access_roundtrip_and_type_check() -> None:
    from solvyn.identity.security import InvalidTokenError, create_access_token, decode_access_token

    token = create_access_token(subject="00000000-0000-0000-0000-000000000001")
    payload = decode_access_token(token)
    assert payload["sub"] == "00000000-0000-0000-0000-000000000001"
    assert payload["type"] == "access"
    with pytest.raises(InvalidTokenError):
        decode_access_token("not-a-token")


def test_refresh_token_hash_stable() -> None:
    from solvyn.identity.security import hash_token

    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()


# ---------------------------------------------------------------------------
# Tenant-binding auth (P0-001 / P0-003)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_key_rejects_foreign_tenant() -> None:
    from solvyn.multi_tenant.auth import _validate_tenant_api_key

    # Simulate: attacker presents tenant-B's key with tenant-A slug.
    mock_result = type("R", (), {"scalar_one_or_none": lambda self=None: None})()
    from unittest.mock import AsyncMock, patch

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.execute = AsyncMock(return_value=mock_result)
    with patch("solvyn.multi_tenant.auth.async_session_factory", return_value=mock_session):
        result = await _validate_tenant_api_key("tenant-b-key", expected_tenant_id=uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_validate_key_accepts_own_tenant() -> None:
    from solvyn.multi_tenant.auth import _validate_tenant_api_key

    api_key = "own-key-123"
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    tenant_id = uuid.uuid4()

    from unittest.mock import AsyncMock, MagicMock, patch

    mock_tenant = MagicMock()
    mock_tenant.id = tenant_id
    mock_tenant.api_key_hash = key_hash
    mock_tenant.is_active = True

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_tenant

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("solvyn.multi_tenant.auth.async_session_factory", return_value=mock_session):
        result = await _validate_tenant_api_key(api_key, expected_tenant_id=tenant_id)
    assert result is mock_tenant


def test_permission_defaults_are_least_privilege() -> None:
    from solvyn.api.v1.teammates import _DEFAULT_PERMISSIONS

    assert "MERGE_PULL_REQUEST" not in _DEFAULT_PERMISSIONS["Software Engineer"]
    assert "DEPLOY" not in _DEFAULT_PERMISSIONS["Software Engineer"]
    assert PermissionLevel.MERGE_PULL_REQUEST.value not in _DEFAULT_PERMISSIONS["Tech Lead"]
    assert _DEFAULT_PERMISSIONS["Security Engineer"] == [PermissionLevel.READ_ONLY.value]


def test_org_owner_is_not_a_platform_admin() -> None:
    """Organization roles must not grant access to global admin endpoints."""
    from solvyn.db.models import TenantRole
    from solvyn.multi_tenant.auth import _ORG_ROLE_TO_TENANT_ROLE

    assert _ORG_ROLE_TO_TENANT_ROLE["owner"] == TenantRole.USER
    assert _ORG_ROLE_TO_TENANT_ROLE["admin"] == TenantRole.USER


def test_knowledge_secret_screen_rejects() -> None:
    from solvyn.api.v1.teammates import _looks_like_secret

    assert _looks_like_secret("api_key = sk-123") is not None
    assert _looks_like_secret("Architecture: FastAPI + Postgres") is None


# ---------------------------------------------------------------------------
# Config fail-fast (P0-002)
# ---------------------------------------------------------------------------


def test_enforce_prod_secrets_raises_when_missing(monkeypatch) -> None:
    from solvyn.config import Settings, enforce_prod_secrets

    s = Settings(api_key_secret="", jwt_secret="", allow_insecure_dev=False)
    with patch_settings(s):
        with pytest.raises(RuntimeError, match="API_KEY_SECRET"):
            enforce_prod_secrets()
    # dev opt-out never raises
    s2 = Settings(api_key_secret="", jwt_secret="", allow_insecure_dev=True)
    with patch_settings(s2):
        enforce_prod_secrets()


def patch_settings(s):
    from unittest.mock import patch

    return patch("solvyn.config.settings", s)


def test_db_pool_tuning_defaults() -> None:
    from solvyn.config import Settings

    s = Settings(api_key_secret="x", jwt_secret="y")
    assert s.db_pool_size == 20
    assert s.db_max_overflow == 10
    assert s.db_pool_recycle_seconds == 1800


@pytest.mark.asyncio
async def test_require_role_needs_authentication() -> None:
    from fastapi import HTTPException

    from solvyn.multi_tenant.auth import RequireRole
    from solvyn.multi_tenant.middleware import tenant_context

    tenant_context.clear()
    dep = RequireRole("admin")
    from unittest.mock import MagicMock

    with pytest.raises(HTTPException) as exc:
        await dep(MagicMock())
    assert exc.value.status_code == 401
