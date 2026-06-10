"""Tests for the embedding service."""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_hash_embedding_provider_produces_deterministic_vectors() -> None:
    """Same input should always produce the same embedding."""
    from solvyn.llm.embeddings import HashEmbeddingProvider

    provider = HashEmbeddingProvider(dimensions=128)
    r1 = await provider.embed("hello world")
    r2 = await provider.embed("hello world")

    assert r1.embedding == r2.embedding
    assert len(r1.embedding) == 128
    assert r1.model == "hash-fallback"


@pytest.mark.asyncio
async def test_hash_embedding_provider_normalised() -> None:
    """Vectors should be unit-normalised for cosine similarity."""
    import math
    from solvyn.llm.embeddings import HashEmbeddingProvider

    provider = HashEmbeddingProvider(dimensions=256)
    response = await provider.embed("test text")

    norm = math.sqrt(sum(v * v for v in response.embedding))
    assert abs(norm - 1.0) < 1e-6


@pytest.mark.asyncio
async def test_hash_embedding_batch() -> None:
    """Batch embedding should return one response per input."""
    from solvyn.llm.embeddings import HashEmbeddingProvider

    provider = HashEmbeddingProvider(dimensions=64)
    texts = ["alpha", "beta", "gamma"]
    results = await provider.embed_batch(texts)

    assert len(results) == 3
    for r in results:
        assert len(r.embedding) == 64


@pytest.mark.asyncio
async def test_hash_embedding_different_texts_different_vectors() -> None:
    """Different inputs should produce different embeddings."""
    from solvyn.llm.embeddings import HashEmbeddingProvider

    provider = HashEmbeddingProvider(dimensions=128)
    r1 = await provider.embed("cats are great")
    r2 = await provider.embed("dogs are loyal")

    assert r1.embedding != r2.embedding


@pytest.mark.asyncio
async def test_embedding_service_falls_back_to_hash() -> None:
    """EmbeddingService should fall back to hash when no API key is set."""
    from solvyn.llm.embeddings import EmbeddingService

    # Patch settings to provide known dimensions (autouse fixture mocks it)
    with patch("solvyn.llm.embeddings.settings") as mock_settings:
        mock_settings.embedding_dimensions = 128
        mock_settings.openrouter_api_key = ""
        mock_settings.embedding_model = "text-embedding-3-small"

        service = EmbeddingService()
        response = await service.embed("test query")

        assert len(response.embedding) == 128
        assert response.model == "hash-fallback"


@pytest.mark.asyncio
async def test_embedding_response_fields() -> None:
    """EmbeddingResponse should store all metadata."""
    from solvyn.llm.embeddings import EmbeddingResponse

    r = EmbeddingResponse(embedding=[0.1, 0.2], model="test-model", prompt_tokens=42)
    assert r.embedding == [0.1, 0.2]
    assert r.model == "test-model"
    assert r.prompt_tokens == 42
