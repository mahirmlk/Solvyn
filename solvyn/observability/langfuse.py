"""LangFuse LLM observability integration.

Provides a thin wrapper around the LangFuse client that:

* Initializes from existing settings (``langfuse_public_key``,
  ``langfuse_secret_key``, ``langfuse_host``) without adding new env vars.
* Gracefully no-ops when the package is missing or credentials are
  unconfigured — never breaks the LLM call path.
* Exposes ``trace_llm_call`` as the primary integration point for LLM
  generations.
* Provides ``flush`` / ``shutdown`` lifecycle hooks for FastAPI startup /
  shutdown.
* Exposes a lower-level ``record_event`` helper used by
  :class:`solvyn.observability.tracing.TracingManager` to optionally mirror
  in-memory traces to LangFuse.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from solvyn.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional langfuse import — must never crash if the package is missing.
# ---------------------------------------------------------------------------
try:
    from langfuse import Langfuse as _LangfuseClient
    _LANGFUSE_AVAILABLE = True
    _LANGFUSE_IMPORT_ERROR: Exception | None = None
except ImportError as _exc:  # pragma: no cover - depends on environment
    _LangfuseClient = None  # type: ignore[assignment,misc]
    _LANGFUSE_AVAILABLE = False
    _LANGFUSE_IMPORT_ERROR = _exc


# ---------------------------------------------------------------------------
# Module-level singleton state.
# ---------------------------------------------------------------------------
_client: Any = None
_enabled: bool = False
_last_init_keys: tuple[str, str, str] | None = None


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------
def is_available() -> bool:
    """Return True if the ``langfuse`` package is importable."""
    return _LANGFUSE_AVAILABLE


def is_enabled() -> bool:
    """Return True if the client is configured and ready to record traces."""
    return _enabled and _client is not None


def init_langfuse(
    public_key: str | None = None,
    secret_key: str | None = None,
    host: str | None = None,
) -> bool:
    """Initialize the global LangFuse client.

    Credentials are read from arguments first and then from
    :class:`solvyn.config.Settings`. Returns ``True`` when the client was
    successfully configured, otherwise ``False`` (the module is left in a
    no-op state).
    """
    global _client, _enabled, _last_init_keys

    if not _LANGFUSE_AVAILABLE:
        logger.info(
            "LangFuse package not installed; tracing is a no-op. "
            "Install with `pip install langfuse` to enable."
        )
        _client = None
        _enabled = False
        _last_init_keys = None
        return False

    pk = public_key if public_key is not None else settings.langfuse_public_key
    sk = secret_key if secret_key is not None else settings.langfuse_secret_key
    h = host if host is not None else settings.langfuse_host

    if not pk or not sk:
        logger.info(
            "LangFuse credentials not configured (LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY); "
            "tracing is a no-op."
        )
        _client = None
        _enabled = False
        _last_init_keys = None
        return False

    try:
        client = _LangfuseClient(public_key=pk, secret_key=sk, host=h)
    except Exception as exc:  # noqa: BLE001 — defensive: any client error is a no-op
        logger.warning("Failed to initialize LangFuse client: %s", exc)
        _client = None
        _enabled = False
        _last_init_keys = None
        return False

    _client = client
    _enabled = True
    _last_init_keys = (pk, sk, h)
    logger.info("LangFuse initialized: host=%s", h)
    return True


def get_client() -> Any:
    """Return the underlying LangFuse client, or ``None`` if not initialized."""
    return _client


def set_client_for_tests(client: Any, enabled: bool = True) -> None:
    """Inject a client directly. Intended for unit tests only."""
    global _client, _enabled, _last_init_keys
    _client = client
    _enabled = enabled
    _last_init_keys = ("test", "test", "test") if enabled else None


def reset_for_tests() -> None:
    """Reset module-level state. Intended for unit tests only."""
    global _client, _enabled, _last_init_keys
    _client = None
    _enabled = False
    _last_init_keys = None


# ---------------------------------------------------------------------------
# Tracing primitives
# ---------------------------------------------------------------------------
@contextmanager
def trace_llm_call(
    name: str = "llm_call",
    *,
    model: str = "",
    provider: str = "",
    input_data: Any = None,
    metadata: dict[str, Any] | None = None,
    tenant_id: str = "default",
    session_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Context manager that records an LLM call as a LangFuse generation.

    The yielded ``record`` dict can be populated by the caller with:
    ``output`` (the model response or text), ``prompt_tokens``,
    ``completion_tokens``, ``total_tokens``, and ``cost`` (USD).

    When LangFuse is not configured the context manager is a safe no-op
    and ``record`` is still yielded, so callers can treat the API
    uniformly.
    """
    record: dict[str, Any] = {
        "name": name,
        "model": model,
        "provider": provider,
        "input": input_data,
        "output": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost": 0.0,
        "duration_ms": 0.0,
        "error": None,
        "tenant_id": tenant_id,
        "session_id": session_id,
    }

    if not is_enabled() or _client is None:
        yield record
        return

    merged_metadata: dict[str, Any] = {
        "provider": provider,
        "tenant_id": tenant_id,
    }
    if session_id:
        merged_metadata["session_id"] = session_id
    if metadata:
        merged_metadata.update(metadata)

    start = time.perf_counter()
    gen: Any = None
    error: BaseException | None = None
    try:
        start_kwargs: dict[str, Any] = {
            "as_type": "generation",
            "name": name,
            "input": input_data,
            "metadata": merged_metadata,
        }
        if model:
            start_kwargs["model"] = model
        gen = _client.start_observation(**start_kwargs)
    except Exception as exc:  # noqa: BLE001 — must not break LLM calls
        logger.warning("LangFuse start_observation failed: %s", exc)
        yield record
        return

    try:
        yield record
    except BaseException as exc:  # noqa: BLE001 — re-raised after recording
        error = exc
        record["error"] = str(exc)
        raise
    finally:
        record["duration_ms"] = (time.perf_counter() - start) * 1000.0
        _finalize_observation(gen, record, error)


def _finalize_observation(gen: Any, record: dict[str, Any], error: BaseException | None) -> None:
    """Update a LangFuse observation with the collected record values."""
    if gen is None:
        return
    usage_details: dict[str, int] | None = None
    pt = int(record.get("prompt_tokens") or 0)
    ct = int(record.get("completion_tokens") or 0)
    tt = int(record.get("total_tokens") or 0)
    if pt or ct or tt:
        usage_details = {"input": pt, "output": ct, "total": tt}

    cost_details: dict[str, float] | None = None
    cost = record.get("cost")
    if cost:
        cost_details = {"total": float(cost)}

    update_kwargs: dict[str, Any] = {
        "output": record.get("output"),
    }
    if usage_details is not None:
        update_kwargs["usage_details"] = usage_details
    if cost_details is not None:
        update_kwargs["cost_details"] = cost_details
    if error is not None:
        update_kwargs["level"] = "ERROR"
        update_kwargs["status_message"] = str(error)
    try:
        gen.update(**update_kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LangFuse finalize observation failed: %s", exc)
    try:
        gen.end()
    except Exception as exc:  # noqa: BLE001
        logger.warning("LangFuse end observation failed: %s", exc)


def record_event(
    name: str,
    *,
    as_type: str = "span",
    input_data: Any = None,
    output_data: Any = None,
    metadata: dict[str, Any] | None = None,
    tenant_id: str = "default",
    session_id: str | None = None,
    model: str = "",
    usage_details: dict[str, int] | None = None,
    cost_details: dict[str, float] | None = None,
    error: str | None = None,
) -> None:
    """Best-effort helper to record a single observation to LangFuse.

    Used by :class:`solvyn.observability.tracing.TracingManager` to mirror
    in-memory traces. Safe to call when LangFuse is not configured.
    """
    if not is_enabled() or _client is None:
        return

    merged_metadata: dict[str, Any] = {"tenant_id": tenant_id}
    if session_id:
        merged_metadata["session_id"] = session_id
    if metadata:
        merged_metadata.update(metadata)

    try:
        start_kwargs: dict[str, Any] = {
            "as_type": as_type,
            "name": name,
            "input": input_data,
            "metadata": merged_metadata,
        }
        if model:
            start_kwargs["model"] = model
        gen = _client.start_observation(**start_kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LangFuse record_event start failed: %s", exc)
        return

    try:
        update_kwargs: dict[str, Any] = {"output": output_data}
        if usage_details is not None:
            update_kwargs["usage_details"] = usage_details
        if cost_details is not None:
            update_kwargs["cost_details"] = cost_details
        if error is not None:
            update_kwargs["level"] = "ERROR"
            update_kwargs["status_message"] = error
        gen.update(**update_kwargs)
        gen.end()
    except Exception as exc:  # noqa: BLE001
        logger.warning("LangFuse record_event finalize failed: %s", exc)


async def flush() -> None:
    """Flush any pending events to the LangFuse backend.

    Safe to call when LangFuse is not configured.
    """
    if _client is None:
        return
    try:
        _client.flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("LangFuse flush failed: %s", exc)


async def shutdown() -> None:
    """Flush pending events and reset module state.

    Safe to call when LangFuse is not configured.
    """
    global _client, _enabled
    if _client is not None:
        try:
            _client.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning("LangFuse shutdown failed: %s", exc)
    _client = None
    _enabled = False
