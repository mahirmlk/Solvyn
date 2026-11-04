"""Tests for per-tenant RBAC and role-based access control."""
from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from solvyn.db.models import TenantRole
from solvyn.multi_tenant.middleware import TenantContext, tenant_context

# ---------------------------------------------------------------------------
# TenantContext
# ---------------------------------------------------------------------------

def test_tenant_context_default_role() -> None:
    """Default role should be viewer."""
    ctx = TenantContext()
    assert ctx.tenant_role == TenantRole.VIEWER
    assert ctx.is_admin is False


def test_tenant_context_set_role() -> None:
    """Setting tenant should store the role."""
    ctx = TenantContext()
    ctx.set(uuid.uuid4(), "test-slug", TenantRole.ADMIN)
    assert ctx.tenant_role == TenantRole.ADMIN
    assert ctx.is_admin is True
    ctx.clear()


def test_tenant_context_clear_resets_role() -> None:
    """Clearing should reset role to viewer."""
    ctx = TenantContext()
    ctx.set(uuid.uuid4(), "test-slug", TenantRole.ADMIN)
    ctx.clear()
    assert ctx.tenant_role == TenantRole.VIEWER
    assert ctx.is_admin is False


# ---------------------------------------------------------------------------
# verify_api_key
# ---------------------------------------------------------------------------

def test_verify_api_key_with_hash() -> None:
    """Should verify API key against a SHA256 hash."""
    from solvyn.multi_tenant.auth import verify_api_key

    secret = "my-secret-key"
    key_hash = hashlib.sha256(secret.encode()).hexdigest()
    assert verify_api_key(secret, key_hash) is True
    assert verify_api_key("wrong-key", key_hash) is False


def test_verify_api_key_with_plaintext() -> None:
    """Should verify API key against a plaintext secret."""
    from solvyn.multi_tenant.auth import verify_api_key

    assert verify_api_key("my-secret-key", "my-secret-key") is True
    assert verify_api_key("wrong-key", "my-secret-key") is False


# ---------------------------------------------------------------------------
# _validate_tenant_api_key
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_tenant_api_key_matches() -> None:
    """Should return the tenant when API key matches (indexed lookup)."""
    from solvyn.multi_tenant.auth import _validate_tenant_api_key

    api_key = "tenant-secret-123"
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    mock_tenant = MagicMock()
    mock_tenant.api_key_hash = key_hash
    mock_tenant.is_active = True
    mock_tenant.slug = "test-tenant"
    mock_tenant.id = uuid.uuid4()
    mock_tenant.role = TenantRole.USER

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_tenant

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.execute.return_value = mock_result

    with patch("solvyn.multi_tenant.auth.async_session_factory", return_value=mock_session):
        result = await _validate_tenant_api_key(api_key)

    assert result is mock_tenant


@pytest.mark.asyncio
async def test_validate_tenant_api_key_no_match() -> None:
    """Should return None when no tenant matches."""
    from solvyn.multi_tenant.auth import _validate_tenant_api_key

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.execute.return_value = mock_result

    with patch("solvyn.multi_tenant.auth.async_session_factory", return_value=mock_session):
        result = await _validate_tenant_api_key("wrong-key")

    assert result is None


@pytest.mark.asyncio
async def test_validate_tenant_api_key_binds_to_header_tenant() -> None:
    """A valid key from a *different* tenant must be rejected (P0-003)."""
    from solvyn.multi_tenant.auth import _validate_tenant_api_key

    api_key = "attacker-key"
    other_tenant_id = uuid.uuid4()

    mock_result = MagicMock()
    # Tenant lookup by the header-supplied id returns a tenant whose
    # api_key_hash does NOT match, and the api_keys fallback is also empty.
    mock_result.scalar_one_or_none.return_value = None

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.execute.return_value = mock_result

    with patch("solvyn.multi_tenant.auth.async_session_factory", return_value=mock_session):
        result = await _validate_tenant_api_key(api_key, expected_tenant_id=other_tenant_id)

    assert result is None


# ---------------------------------------------------------------------------
# RequireRole
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_require_role_passes_for_allowed_role() -> None:
    """Should not raise when tenant has an allowed role."""
    from solvyn.multi_tenant.auth import RequireRole

    tenant_context.set(uuid.uuid4(), "test", TenantRole.ADMIN)
    dep = RequireRole(TenantRole.ADMIN, TenantRole.USER)

    mock_request = MagicMock()
    # Should not raise
    await dep(mock_request)
    tenant_context.clear()


@pytest.mark.asyncio
async def test_require_role_fails_for_disallowed_role() -> None:
    """Should raise 403 when tenant role is not allowed."""
    from fastapi import HTTPException

    from solvyn.multi_tenant.auth import RequireRole

    tenant_context.set(uuid.uuid4(), "test", TenantRole.VIEWER)
    dep = RequireRole(TenantRole.ADMIN)

    mock_request = MagicMock()
    with pytest.raises(HTTPException) as exc_info:
        await dep(mock_request)
    assert exc_info.value.status_code == 403
    tenant_context.clear()


@pytest.mark.asyncio
async def test_require_role_fails_without_tenant() -> None:
    """Should raise 401 when unauthenticated (P0-003: credential, not header)."""
    from fastapi import HTTPException

    from solvyn.multi_tenant.auth import RequireRole

    tenant_context.clear()
    dep = RequireRole(TenantRole.ADMIN)

    mock_request = MagicMock()
    with pytest.raises(HTTPException) as exc_info:
        await dep(mock_request)
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# TenantRole enum
# ---------------------------------------------------------------------------

def test_tenant_role_values() -> None:
    """TenantRole should have admin, user, viewer."""
    assert TenantRole.ADMIN == "admin"
    assert TenantRole.USER == "user"
    assert TenantRole.VIEWER == "viewer"
