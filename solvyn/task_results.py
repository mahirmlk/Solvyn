"""Build bounded, content-minimal metadata for immutable task outcomes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

_SUMMARY_LIMIT = 512
_SUMMARY_FIELDS = ("summary", "result_summary", "outcome_summary")


def result_metadata(output: Mapping[str, Any]) -> tuple[str, str]:
    """Return a bounded summary and stable digest without copying the full result."""
    summary = next(
        (value.strip() for key in _SUMMARY_FIELDS if isinstance((value := output.get(key)), str) and value.strip()),
        "The assigned teammate returned a structured result.",
    )
    summary = " ".join(summary.split())[:_SUMMARY_LIMIT]
    canonical_output = json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical_output.encode("utf-8")).hexdigest()
    return summary, digest
