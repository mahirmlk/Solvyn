from __future__ import annotations

import hashlib
import json

from solvyn.task_results import result_metadata


def test_result_metadata_is_stable_and_does_not_copy_result_body() -> None:
    output = {"summary": "  Updated   the authentication flow. ", "content": "private full output"}

    summary, digest = result_metadata(output)

    assert summary == "Updated the authentication flow."
    assert "private full output" not in summary
    canonical = json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert digest == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_result_metadata_bounds_summary() -> None:
    summary, digest = result_metadata({"result_summary": "x" * 900})

    assert len(summary) == 512
    assert len(digest) == 64


def test_result_metadata_uses_safe_fallback_when_no_summary_is_returned() -> None:
    summary, _digest = result_metadata({"content": "not copied to the summary"})

    assert summary == "The assigned teammate returned a structured result."
