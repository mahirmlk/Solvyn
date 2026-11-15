"""Run deterministic checks over actual outputs from registered teammate agents.

The dataset intentionally measures response structure and explicit rubric
signals. It is an evaluation aid, not a substitute for human review or sandbox
verification, and its scores must not be presented as a general quality claim.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any, cast

from solvyn.agents.registry import agent_registry

logger = logging.getLogger(__name__)

DATASET_ID = "solvyn-engineering-teammates"
DATASET_VERSION = "1.0.0"


@dataclass(frozen=True)
class EvaluationCaseResult:
    case_id: str
    agent_type: str
    execution_status: str
    passed: bool | None
    score: float | None
    checks: dict[str, bool]
    duration_ms: float
    error_type: str | None = None


@dataclass(frozen=True)
class EvaluationReport:
    run_id: str
    dataset_id: str
    dataset_version: str
    created_at: str
    provider: str | None
    model: str | None
    cases: tuple[EvaluationCaseResult, ...]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "created_at": self.created_at,
            "provider": self.provider,
            "model": self.model,
            "summary": self.summary,
            "cases": [asdict(case) for case in self.cases],
        }


def load_dataset() -> dict[str, Any]:
    """Load and validate the packaged, versioned engineering evaluation set."""
    dataset_path = files("solvyn.eval.datasets").joinpath("engineering_v1.json")
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    if dataset.get("id") != DATASET_ID or dataset.get("version") != DATASET_VERSION:
        raise ValueError("Packaged evaluation dataset identity/version is inconsistent")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Evaluation dataset must contain at least one case")
    case_ids = [case.get("id") for case in cases]
    if any(not isinstance(case_id, str) or not case_id for case_id in case_ids) or len(case_ids) != len(set(case_ids)):
        raise ValueError("Evaluation case IDs must be unique non-empty strings")
    return cast(dict[str, Any], dataset)


def _score_case(
    case: dict[str, Any],
    response: Any,
    duration_ms: float = 0.0,
    error_type: str | None = None,
) -> EvaluationCaseResult:
    if error_type is not None:
        return EvaluationCaseResult(
            case_id=case["id"],
            agent_type=case["agent_type"],
            execution_status="failed",
            passed=None,
            score=None,
            checks={},
            duration_ms=round(duration_ms, 2),
            error_type=error_type,
        )

    checks: dict[str, bool] = {}
    try:
        parsed = json.loads(response) if isinstance(response, str) else response
    except (json.JSONDecodeError, TypeError):
        parsed = None

    required_keys = case.get("required_json_keys", [])
    structured = isinstance(parsed, dict)
    checks["valid_json_object"] = structured
    if structured:
        for key in required_keys:
            checks[f"required_key:{key}"] = key in parsed
    else:
        for key in required_keys:
            checks[f"required_key:{key}"] = False

    searchable_text = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False, default=str)
    searchable_text = searchable_text.casefold()
    for index, alternatives in enumerate(case.get("must_mention_any", [])):
        checks[f"rubric:{index + 1}"] = any(term.casefold() in searchable_text for term in alternatives)
    for term in case.get("must_not_mention", []):
        checks[f"avoid:{term}"] = term.casefold() not in searchable_text

    score = sum(checks.values()) / len(checks) if checks else 0.0
    return EvaluationCaseResult(
        case_id=case["id"],
        agent_type=case["agent_type"],
        execution_status="completed",
        passed=all(checks.values()),
        score=round(score, 4),
        checks=checks,
        duration_ms=round(duration_ms, 2),
        error_type=None,
    )


def score_outputs(
    dataset: dict[str, Any],
    outputs: dict[str, dict[str, Any]],
    *,
    provider: str | None = None,
    model: str | None = None,
    failures: dict[str, str] | None = None,
    durations: dict[str, float] | None = None,
) -> EvaluationReport:
    """Score completed outputs and keep agent/provider failures out of quality scores."""
    cases = dataset["cases"]
    expected_ids = {case["id"] for case in cases}
    failures = failures or {}
    durations = durations or {}
    output_ids = set(outputs) | set(failures)
    if set(outputs) & set(failures):
        raise ValueError("A case cannot have both an output and an invocation failure")
    if output_ids != expected_ids:
        missing = sorted(expected_ids - output_ids)
        extra = sorted(output_ids - expected_ids)
        raise ValueError(f"Output case IDs do not match dataset (missing={missing}, extra={extra})")

    results = tuple(
        _score_case(
            case,
            outputs.get(case["id"], {}).get(case.get("response_field", "output")),
            durations.get(case["id"], 0.0),
            failures.get(case["id"]),
        )
        for case in cases
    )
    roles: dict[str, dict[str, float | int]] = {}
    for result in results:
        role = roles.setdefault(
            result.agent_type,
            {"total": 0, "evaluated": 0, "passed": 0, "execution_failures": 0, "score_sum": 0.0},
        )
        role["total"] += 1
        if result.execution_status == "failed":
            role["execution_failures"] += 1
        else:
            role["evaluated"] += 1
            role["passed"] += int(result.passed is True)
            role["score_sum"] += result.score or 0.0
    role_summaries = {
        role: {
            "total": int(metrics["total"]),
            "evaluated": int(metrics["evaluated"]),
            "execution_failures": int(metrics["execution_failures"]),
            "passed": int(metrics["passed"]),
            "pass_rate": round(int(metrics["passed"]) / int(metrics["evaluated"]), 4) if metrics["evaluated"] else None,
            "mean_score": round(float(metrics["score_sum"]) / int(metrics["evaluated"]), 4) if metrics["evaluated"] else None,
        }
        for role, metrics in sorted(roles.items())
    }
    total = len(results)
    evaluated = sum(result.execution_status == "completed" for result in results)
    passed = sum(result.passed is True for result in results)
    execution_failures = total - evaluated
    return EvaluationReport(
        run_id=str(uuid.uuid4()),
        dataset_id=dataset["id"],
        dataset_version=dataset["version"],
        created_at=datetime.now(UTC).isoformat(),
        provider=provider,
        model=model,
        cases=results,
        summary={
            "total_cases": total,
            "evaluated_cases": evaluated,
            "execution_failures": execution_failures,
            "passed": passed,
            "failed": evaluated - passed,
            "pass_rate": round(passed / evaluated, 4) if evaluated else None,
            "execution_success_rate": round(evaluated / total, 4) if total else 0.0,
            "mean_score": round(sum(result.score or 0.0 for result in results) / evaluated, 4) if evaluated else None,
            "by_agent_type": role_summaries,
        },
    )


async def run_agents(dataset: dict[str, Any], *, tenant_id: str, provider: str | None = None, model: str | None = None) -> EvaluationReport:
    """Execute each packaged case through its registered agent and score the response."""
    outputs: dict[str, dict[str, Any]] = {}
    durations: dict[str, float] = {}
    failures: dict[str, str] = {}
    observed_providers: set[str] = set()
    observed_models: set[str] = set()

    for case in dataset["cases"]:
        agent = agent_registry.create(case["agent_type"], session_id=str(uuid.uuid4()), tenant_id=tenant_id)
        agent._llm_provider = provider
        agent._llm_model = model
        start = asyncio.get_running_loop().time()
        try:
            response = await agent.run(case["input"])
        except Exception as exc:
            # Do not put provider exception text into durable reports; it can
            # contain request fragments or other sensitive diagnostics.
            logger.warning("Evaluation case %s failed (%s)", case["id"], type(exc).__name__)
            failures[case["id"]] = type(exc).__name__
            response = {}
        durations[case["id"]] = (asyncio.get_running_loop().time() - start) * 1000
        provider_name = response.get("provider") if isinstance(response, dict) else None
        model_name = response.get("model") if isinstance(response, dict) else None
        if provider_name:
            observed_providers.add(str(provider_name))
        if model_name:
            observed_models.add(str(model_name))
        if case["id"] not in failures:
            outputs[case["id"]] = response if isinstance(response, dict) else {}

    report = score_outputs(
        dataset,
        outputs,
        provider=next(iter(observed_providers)) if len(observed_providers) == 1 else ("mixed" if observed_providers else provider),
        model=next(iter(observed_models)) if len(observed_models) == 1 else ("mixed" if observed_models else model),
        failures=failures,
        durations=durations,
    )
    return EvaluationReport(
        run_id=report.run_id,
        dataset_id=report.dataset_id,
        dataset_version=report.dataset_version,
        created_at=report.created_at,
        provider=report.provider,
        model=report.model,
        cases=report.cases,
        summary=report.summary,
    )
