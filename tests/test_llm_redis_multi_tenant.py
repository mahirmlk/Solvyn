import uuid
from unittest.mock import AsyncMock

import pytest

from solvyn.llm.providers.base import LLMResponse
from solvyn.multi_tenant.middleware import tenant_context


# ─── LLM Router ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_llm_router_generate_success() -> None:
    from solvyn.llm.router import llm_router
    mock_provider = AsyncMock()
    mock_provider.generate.return_value = LLMResponse(
        content="specific_response",
        provider="openrouter",
        model="gpt-4o",
        prompt_tokens=5,
        completion_tokens=10,
        total_tokens=15,
        cost=0.0,
    )
    llm_router.providers["openrouter"] = mock_provider
    result = await llm_router.generate(prompt="Hi", provider="openrouter", fallback=False)
    assert result.content == "specific_response"
    assert result.model == "gpt-4o"


@pytest.mark.asyncio
async def test_llm_router_unknown_provider() -> None:
    from solvyn.llm.router import llm_router
    llm_router.cache._local_cache.clear()
    with pytest.raises((ValueError, RuntimeError)):
        await llm_router.generate(prompt="Hi-unique-unknown-provider-test", provider="nonexistent")


@pytest.mark.asyncio
async def test_llm_router_fallback() -> None:
    from solvyn.llm.router import llm_router
    failing = AsyncMock()
    failing.generate.side_effect = ConnectionError("down")
    llm_router.providers["groq"] = failing

    working = AsyncMock()
    working.generate.return_value = LLMResponse(
        content="fallback_response",
        provider="deepseek",
        model="deepseek-chat",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        cost=0.0,
    )
    llm_router.providers["deepseek"] = working

    # Also ensure openrouter (default) won't accidentally succeed
    openrouter_orig = llm_router.providers.get("openrouter")
    llm_router.providers["openrouter"] = AsyncMock()
    llm_router.providers["openrouter"].generate.side_effect = ConnectionError("also down")

    llm_router.cache._local_cache.clear()
    result = await llm_router.generate(prompt="Hi-unique-fallback-test", provider="groq", fallback=True)
    assert result.content == "fallback_response"
    assert result.model == "deepseek-chat"

    # Restore
    if openrouter_orig:
        llm_router.providers["openrouter"] = openrouter_orig


# ─── Multi-Tenant ───────────────────────────────────────────
def test_tenant_context_crud() -> None:
    assert tenant_context.tenant_id is None
    assert tenant_context.tenant_slug is None

    tid = uuid.uuid4()
    tenant_context.set(tid, "alpha")
    assert tenant_context.tenant_id == tid
    assert tenant_context.tenant_slug == "alpha"

    tenant_context.clear()
    assert tenant_context.tenant_id is None
    assert tenant_context.tenant_slug is None


def test_tenant_context_double_set() -> None:
    t1 = uuid.uuid4()
    t2 = uuid.uuid4()
    tenant_context.set(t1, "first")
    tenant_context.set(t2, "second")
    assert tenant_context.tenant_id == t2
    assert tenant_context.tenant_slug == "second"
    tenant_context.clear()
