"""Compare saved evaluation reports without calling an LLM or database."""

from __future__ import annotations

import math
from typing import Any, TypedDict


class EvaluationMetrics(TypedDict):
    total_cases: int
    evaluated_cases: int
    execution_failures: int
    passed: int
    pass_rate: float | None
    mean_score: float | None


def _report_cases(report: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    raw_cases = report.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError(f"{label} report must contain a cases list")
    if not raw_cases:
        raise ValueError(f"{label} report must contain at least one case")

    cases: dict[str, dict[str, Any]] = {}
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError(f"{label} report cases must be objects")
        case_id = raw_case.get("case_id")
        agent_type = raw_case.get("agent_type")
        status = raw_case.get("execution_status")
        passed = raw_case.get("passed")
        score = raw_case.get("score")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"{label} report case IDs must be non-empty strings")
        if case_id in cases:
            raise ValueError(f"{label} report contains duplicate case ID: {case_id}")
        if not isinstance(agent_type, str) or not agent_type:
            raise ValueError(f"{label} report case {case_id} must have an agent_type")
        if not isinstance(status, str) or status not in {"completed", "failed"}:
            raise ValueError(f"{label} report case {case_id} has an invalid execution_status")
        if status == "completed":
            if not isinstance(passed, bool):
                raise ValueError(f"{label} completed case {case_id} must have a boolean passed value")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise ValueError(f"{label} completed case {case_id} must have a finite numeric score")
            if score < 0 or score > 1:
                raise ValueError(f"{label} completed case {case_id} score must be between 0 and 1")
            if not math.isfinite(score):
                raise ValueError(f"{label} completed case {case_id} must have a finite numeric score")
        elif passed is not None or score is not None:
            raise ValueError(f"{label} failed case {case_id} must not have a pass value or score")
        cases[case_id] = raw_case
    return cases


def _metrics(cases: list[dict[str, Any]]) -> EvaluationMetrics:
    evaluated = [case for case in cases if case["execution_status"] == "completed"]
    passed = sum(case["passed"] is True for case in evaluated)
    return {
        "total_cases": len(cases),
        "evaluated_cases": len(evaluated),
        "execution_failures": len(cases) - len(evaluated),
        "passed": passed,
        "pass_rate": round(passed / len(evaluated), 4) if evaluated else None,
        "mean_score": round(sum(float(case["score"]) for case in evaluated) / len(evaluated), 4)
        if evaluated
        else None,
    }


def compare_reports(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Compare two reports for the same dataset and model identity.

    Run IDs, timestamps, and durations are intentionally ignored. Only the
    stored case scores and pass outcomes contribute to the comparison.
    """
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        raise ValueError("Baseline and candidate reports must be JSON objects")
    for key in ("dataset_id", "dataset_version", "provider", "model"):
        if baseline.get(key) != candidate.get(key):
            raise ValueError(f"Reports must have the same {key}")
    if not isinstance(baseline.get("dataset_id"), str) or not baseline["dataset_id"]:
        raise ValueError("Reports must identify a dataset")
    if not isinstance(baseline.get("dataset_version"), str) or not baseline["dataset_version"]:
        raise ValueError("Reports must identify a dataset version")
    for key in ("provider", "model"):
        identity = baseline.get(key)
        if not isinstance(identity, str) or not identity.strip() or identity.casefold() == "mixed":
            raise ValueError(f"Reports must identify one {key}")

    baseline_cases = _report_cases(baseline, "Baseline")
    candidate_cases = _report_cases(candidate, "Candidate")
    if set(baseline_cases) != set(candidate_cases):
        missing = sorted(set(baseline_cases) - set(candidate_cases))
        extra = sorted(set(candidate_cases) - set(baseline_cases))
        raise ValueError(f"Reports must contain the same case IDs (missing={missing}, extra={extra})")

    comparisons: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    # Keep the baseline report's case order so the comparison is easy to line
    # up with the original artifact. The candidate is keyed by ID above, so
    # its input order does not affect matching.
    for case_id in baseline_cases:
        before = baseline_cases[case_id]
        after = candidate_cases[case_id]
        if before["agent_type"] != after["agent_type"]:
            raise ValueError(f"Reports disagree on agent_type for case {case_id}")
        before_done = before["execution_status"] == "completed"
        after_done = after["execution_status"] == "completed"
        both_done = before_done and after_done
        pass_delta = int(after["passed"]) - int(before["passed"]) if both_done else None
        score_delta = round(float(after["score"]) - float(before["score"]), 4) if both_done else None
        comparisons.append(
            {
                "case_id": case_id,
                "agent_type": before["agent_type"],
                "baseline_status": before["execution_status"],
                "candidate_status": after["execution_status"],
                "baseline_passed": before["passed"],
                "candidate_passed": after["passed"],
                "pass_delta": pass_delta,
                "baseline_score": before["score"],
                "candidate_score": after["score"],
                "score_delta": score_delta,
            }
        )
        grouped.setdefault(before["agent_type"], []).append({"baseline": before, "candidate": after})

    comparable = [case for case in comparisons if case["score_delta"] is not None]
    consistent = sum(case["pass_delta"] == 0 for case in comparable)
    score_deltas = [float(case["score_delta"]) for case in comparable]
    baseline_metrics = _metrics(list(baseline_cases.values()))
    candidate_metrics = _metrics(list(candidate_cases.values()))
    role_metrics: dict[str, Any] = {}
    for role, role_cases in sorted(grouped.items()):
        before_metrics = _metrics([pair["baseline"] for pair in role_cases])
        after_metrics = _metrics([pair["candidate"] for pair in role_cases])
        role_comparable = [case for case in comparisons if case["agent_type"] == role and case["score_delta"] is not None]
        role_consistent = sum(case["pass_delta"] == 0 for case in role_comparable)
        role_metrics[role] = {
            "baseline": before_metrics,
            "candidate": after_metrics,
            "pass_rate_delta": round(sum(case["pass_delta"] for case in role_comparable) / len(role_comparable), 4)
            if role_comparable
            else None,
            "mean_score_delta": round(
                sum(float(case["score_delta"]) for case in role_comparable) / len(role_comparable), 4
            )
            if role_comparable
            else None,
            "comparable_cases": len(role_comparable),
            "pass_consistency_rate": round(role_consistent / len(role_comparable), 4) if role_comparable else None,
        }

    return {
        "dataset_id": baseline["dataset_id"],
        "dataset_version": baseline["dataset_version"],
        "provider": baseline.get("provider"),
        "model": baseline.get("model"),
        "summary": {
            "baseline": baseline_metrics,
            "candidate": candidate_metrics,
            "pass_rate_delta": round(sum(case["pass_delta"] for case in comparable) / len(comparable), 4)
            if comparable
            else None,
            "mean_score_delta": round(sum(score_deltas) / len(score_deltas), 4) if score_deltas else None,
            "execution_failure_delta": candidate_metrics["execution_failures"] - baseline_metrics["execution_failures"],
            "execution_success_rate_delta": round(
                candidate_metrics["evaluated_cases"] / candidate_metrics["total_cases"]
                - baseline_metrics["evaluated_cases"] / baseline_metrics["total_cases"],
                4,
            )
            if baseline_metrics["total_cases"] and candidate_metrics["total_cases"]
            else None,
            "comparable_cases": len(comparable),
            "pass_consistent_cases": consistent,
            "pass_consistency_rate": round(consistent / len(comparable), 4) if comparable else None,
            "mean_absolute_score_delta": round(sum(abs(delta) for delta in score_deltas) / len(score_deltas), 4)
            if score_deltas
            else None,
        },
        "by_agent_type": role_metrics,
        "cases": comparisons,
    }
