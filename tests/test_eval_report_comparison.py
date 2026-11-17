from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from solvyn.eval.report_comparison import compare_reports


def _report() -> dict[str, Any]:
    return {
        "run_id": "baseline-run",
        "dataset_id": "engineering",
        "dataset_version": "1.0",
        "created_at": "2026-01-01T00:00:00+00:00",
        "provider": "fixture-provider",
        "model": "fixture-model",
        "cases": [
            {
                "case_id": "design.one",
                "agent_type": "system-designer",
                "execution_status": "completed",
                "passed": True,
                "score": 1.0,
                "duration_ms": 12,
            },
            {
                "case_id": "design.two",
                "agent_type": "system-designer",
                "execution_status": "completed",
                "passed": False,
                "score": 0.5,
                "duration_ms": 17,
            },
            {
                "case_id": "backend.one",
                "agent_type": "backend-engineer",
                "execution_status": "failed",
                "passed": None,
                "score": None,
                "duration_ms": 0,
            },
        ],
    }


def test_compare_reports_calculates_case_role_and_repeatability_metrics() -> None:
    baseline = _report()
    candidate = deepcopy(baseline)
    candidate.update({"run_id": "candidate-run", "created_at": "2026-02-01T00:00:00+00:00"})
    candidate["cases"][0].update({"passed": False, "score": 0.75, "duration_ms": 999})
    candidate["cases"][1].update({"passed": True, "score": 0.75})

    result = compare_reports(baseline, candidate)

    assert result["summary"]["baseline"]["pass_rate"] == 0.5
    assert result["summary"]["candidate"]["pass_rate"] == 0.5
    assert result["summary"]["pass_rate_delta"] == 0.0
    assert result["summary"]["comparable_cases"] == 2
    assert result["summary"]["pass_consistent_cases"] == 0
    assert result["summary"]["pass_consistency_rate"] == 0.0
    assert result["summary"]["mean_absolute_score_delta"] == 0.25
    assert result["by_agent_type"]["system-designer"]["mean_score_delta"] == 0.0
    assert result["cases"][0]["score_delta"] == -0.25
    assert result["cases"][0]["pass_delta"] == -1
    assert result["cases"][2]["score_delta"] is None


def test_compare_reports_keeps_execution_failures_out_of_quality_metrics() -> None:
    baseline = _report()
    candidate = deepcopy(baseline)
    candidate["cases"][2].update(
        {"execution_status": "completed", "passed": True, "score": 1.0, "error_type": None}
    )

    result = compare_reports(baseline, candidate)

    assert result["summary"]["baseline"]["execution_failures"] == 1
    assert result["summary"]["candidate"]["execution_failures"] == 0
    assert result["summary"]["comparable_cases"] == 2
    assert result["cases"][2]["score_delta"] is None
    assert result["by_agent_type"]["backend-engineer"]["pass_consistency_rate"] is None


def test_report_deltas_use_the_same_completed_cases_and_report_failure_changes() -> None:
    baseline = _report()
    candidate = deepcopy(baseline)
    candidate["cases"][2].update(
        {"execution_status": "completed", "passed": False, "score": 0.25, "error_type": None}
    )

    result = compare_reports(baseline, candidate)

    assert result["summary"]["baseline"]["pass_rate"] == 0.5
    assert result["summary"]["candidate"]["pass_rate"] == pytest.approx(0.3333)
    assert result["summary"]["pass_rate_delta"] == 0.0
    assert result["summary"]["mean_score_delta"] == 0.0
    assert result["summary"]["execution_failure_delta"] == -1
    assert result["summary"]["execution_success_rate_delta"] == pytest.approx(0.3333)


def test_compare_reports_requires_single_provider_model_and_bounded_scores() -> None:
    baseline = _report()
    candidate = deepcopy(baseline)
    baseline["provider"] = None
    candidate["provider"] = None
    with pytest.raises(ValueError, match="identify one provider"):
        compare_reports(baseline, candidate)

    mixed_provider = _report()
    mixed_provider["provider"] = "mixed"
    baseline_with_mixed_provider = _report()
    baseline_with_mixed_provider["provider"] = "mixed"
    with pytest.raises(ValueError, match="identify one provider"):
        compare_reports(baseline_with_mixed_provider, mixed_provider)

    invalid_score = _report()
    invalid_score["cases"][0]["score"] = 1.01
    with pytest.raises(ValueError, match="between 0 and 1"):
        compare_reports(_report(), invalid_score)

    huge_score = _report()
    huge_score["cases"][0]["score"] = 10**1000
    with pytest.raises(ValueError, match="between 0 and 1"):
        compare_reports(_report(), huge_score)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"dataset_version": "2.0"}, "dataset_version"),
        ({"provider": "other-provider"}, "provider"),
        ({"model": "other-model"}, "model"),
    ],
)
def test_compare_reports_rejects_incompatible_identity(change: dict[str, Any], message: str) -> None:
    baseline = _report()
    candidate = {**baseline, **change}

    with pytest.raises(ValueError, match=message):
        compare_reports(baseline, candidate)


def test_compare_reports_rejects_case_set_or_role_mismatch() -> None:
    baseline = _report()
    missing_case = deepcopy(baseline)
    missing_case["cases"].pop()
    with pytest.raises(ValueError, match="same case IDs"):
        compare_reports(baseline, missing_case)

    different_role = deepcopy(baseline)
    different_role["cases"][0]["agent_type"] = "qa-engineer"
    with pytest.raises(ValueError, match="agent_type"):
        compare_reports(baseline, different_role)


def test_compare_reports_rejects_malformed_scores_and_duplicate_case_ids() -> None:
    baseline = _report()
    bad_score = deepcopy(baseline)
    bad_score["cases"][0]["score"] = float("nan")
    with pytest.raises(ValueError, match="finite numeric score"):
        compare_reports(baseline, bad_score)

    duplicate = deepcopy(baseline)
    duplicate["cases"][1]["case_id"] = duplicate["cases"][0]["case_id"]
    with pytest.raises(ValueError, match="duplicate case ID"):
        compare_reports(baseline, duplicate)

    empty = deepcopy(baseline)
    empty["cases"] = []
    with pytest.raises(ValueError, match="at least one case"):
        compare_reports(_report(), empty)

    malformed_status = deepcopy(baseline)
    malformed_status["cases"][0]["execution_status"] = []
    with pytest.raises(ValueError, match="invalid execution_status"):
        compare_reports(_report(), malformed_status)
