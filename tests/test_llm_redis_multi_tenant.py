import uuid
from unittest.mock import AsyncMock

import pytest

from solvyn.multi_tenant.middleware import tenant_context


# ─── LLM Router ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_llm_router_generate_success() -> None:
    from solvyn.llm.router import llm_router
    mock_provider = AsyncMock()
    mock_provider.generate.return_value = {
        "content": "specific_response",
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
    }
    llm_router.providers["openrouter"] = mock_provider
    result = await llm_router.generate(prompt="Hi", provider="openrouter", fallback=False)
    assert result["content"] == "specific_response"
    assert result["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_llm_router_unknown_provider() -> None:
    from solvyn.llm.router import llm_router
    with pytest.raises(ValueError, match="Unknown provider"):
        await llm_router.generate(prompt="Hi", provider="nonexistent")


@pytest.mark.asyncio
async def test_llm_router_fallback() -> None:
    from solvyn.llm.router import llm_router
    failing = AsyncMock()
    failing.generate.side_effect = RuntimeError("down")
    llm_router.providers["groq"] = failing

    working = AsyncMock()
    working.generate.return_value = {"content": "fallback_response", "model": "deepseek-chat", "usage": {}}
    llm_router.providers["deepseek"] = working

    # Also ensure openrouter (default) won't accidentally succeed
    openrouter_orig = llm_router.providers.get("openrouter")
    llm_router.providers["openrouter"] = AsyncMock()
    llm_router.providers["openrouter"].generate.side_effect = RuntimeError("also down")

    result = await llm_router.generate(prompt="Hi", provider="groq", fallback=True)
    assert result["content"] == "fallback_response"
    assert result["model"] == "deepseek-chat"

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
