"""Embedding service for generating vector embeddings from text.

Uses OpenAI-compatible embedding APIs (text-embedding-3-small) with a
deterministic hash-based fallback when no API key is configured.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import httpx

from solvyn.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Embedding response dataclass
# ---------------------------------------------------------------------------


class EmbeddingResponse:
    """Response from an embedding API call."""

    def __init__(
        self,
        embedding: list[float],
        model: str,
        prompt_tokens: int = 0,
    ) -> None:
        self.embedding = embedding
        self.model = model
        self.prompt_tokens = prompt_tokens


# ---------------------------------------------------------------------------
# Embedding providers
# ---------------------------------------------------------------------------


class OpenAIEmbeddingProvider:
    """Embedding provider using OpenAI-compatible APIs.

    Works with OpenAI, OpenRouter, and any OpenAI-compatible endpoint.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        model: str = "text-embedding-3-small",
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def embed(self, text: str) -> EmbeddingResponse:
        """Generate an embedding for the given text."""
        if not self.api_key:
            raise ValueError("No API key configured for embedding provider")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "input": text,
                    "model": self.model,
                },
            )
            response.raise_for_status()
            data = response.json()

            embedding_data = data["data"][0]
            return EmbeddingResponse(
                embedding=embedding_data["embedding"],
                model=data.get("model", self.model),
                prompt_tokens=data.get("usage", {}).get("prompt_tokens", 0),
            )

    async def embed_batch(self, texts: list[str]) -> list[EmbeddingResponse]:
        """Generate embeddings for multiple texts in a single API call."""
        if not self.api_key:
            raise ValueError("No API key configured for embedding provider")

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "input": texts,
                    "model": self.model,
                },
            )
            response.raise_for_status()
            data = response.json()

            return [
                EmbeddingResponse(
                    embedding=item["embedding"],
                    model=data.get("model", self.model),
                    prompt_tokens=data.get("usage", {}).get("prompt_tokens", 0),
                )
                for item in data["data"]
            ]


class HashEmbeddingProvider:
    """Deterministic hash-based embedding provider.

    Used as a fallback when no API key is configured. Produces
    deterministic but semantically meaningless embeddings suitable for
    testing and development only.
    """

    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions

    async def embed(self, text: str) -> EmbeddingResponse:
        embedding = self._text_to_vector(text)
        return EmbeddingResponse(embedding=embedding, model="hash-fallback", prompt_tokens=0)

    async def embed_batch(self, texts: list[str]) -> list[EmbeddingResponse]:
        return [await self.embed(t) for t in texts]

    def _text_to_vector(self, text: str) -> list[float]:
        """Convert text to a deterministic float vector using SHA-512 hashing."""
        hash_bytes = hashlib.sha512(text.encode("utf-8")).digest()
        vector: list[float] = []
        for i in range(self.dimensions):
            byte_index = i % len(hash_bytes)
            # Map byte to [-1.0, 1.0] range for cosine-similarity-friendly vectors
            vector.append((hash_bytes[byte_index] / 127.5) - 1.0)
        # Normalise to unit vector for consistent cosine similarity
        norm = sum(v * v for v in vector) ** 0.5
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector


# ---------------------------------------------------------------------------
# Unified embedding service
# ---------------------------------------------------------------------------


class EmbeddingService:
    """Unified embedding service with provider fallback.

    Automatically selects the best available provider:
    1. OpenAI-compatible API (if API key is configured)
    2. Hash-based fallback (development/testing)
    """

    def __init__(self) -> None:
        self._openai_provider: OpenAIEmbeddingProvider | None = None
        self._hash_provider = HashEmbeddingProvider(dimensions=settings.embedding_dimensions)
        self._init_providers()

    def _init_providers(self) -> None:
        # Try to use the OpenRouter or OpenAI API key for embeddings
        api_key = settings.openrouter_api_key or ""
        base_url = "https://openrouter.ai/api/v1" if settings.openrouter_api_key else "https://api.openai.com/v1"

        if api_key:
            self._openai_provider = OpenAIEmbeddingProvider(
                api_key=api_key,
                base_url=base_url,
                model=settings.embedding_model,
            )
            logger.info("Embedding service initialized with API provider (model=%s)", settings.embedding_model)
        else:
            logger.warning(
                "No embedding API key configured. Using hash-based fallback. "
                "Set OPENROUTER_API_KEY for real embeddings."
            )

    async def embed(self, text: str) -> EmbeddingResponse:
        """Generate an embedding for the given text.

        Uses the API provider if available, otherwise falls back to hash-based.
        """
        if self._openai_provider is not None:
            try:
                return await self._openai_provider.embed(text)
            except Exception as e:
                logger.warning("Embedding API call failed, falling back to hash: %s", e)

        return await self._hash_provider.embed(text)

    async def embed_batch(self, texts: list[str]) -> list[EmbeddingResponse]:
        """Generate embeddings for multiple texts."""
        if self._openai_provider is not None:
            try:
                return await self._openai_provider.embed_batch(texts)
            except Exception as e:
                logger.warning("Embedding batch API call failed, falling back to hash: %s", e)

        return await self._hash_provider.embed_batch(texts)

    async def embed_with_cache(
        self,
        text: str,
        cache_key: str | None = None,
    ) -> EmbeddingResponse:
        """Generate an embedding with optional Redis caching.

        Args:
            text: The text to embed.
            cache_key: Optional cache key. If None, a SHA256 hash of the text is used.
        """
        key = cache_key or hashlib.sha256(text.encode()).hexdigest()

        # Try Redis cache first
        try:
            from solvyn.message_bus.redis import message_bus

            if message_bus.redis is not None:
                cached = await message_bus.redis.get(f"embedding:{key}")
                if cached:
                    data = json.loads(cached)
                    return EmbeddingResponse(
                        embedding=data["embedding"],
                        model=data.get("model", "cached"),
                        prompt_tokens=0,
                    )
        except Exception:
            pass

        # Generate fresh embedding
        response = await self.embed(text)

        # Cache in Redis
        try:
            from solvyn.message_bus.redis import message_bus

            if message_bus.redis is not None:
                cache_data = json.dumps({
                    "embedding": response.embedding,
                    "model": response.model,
                })
                await message_bus.redis.setex(
                    f"embedding:{key}",
                    settings.llm_cache_ttl,
                    cache_data,
                )
        except Exception:
            pass

        return response


# Module-level singleton
embedding_service = EmbeddingService()
