from __future__ import annotations

import json
from typing import Any

import pytest

from solvyn.eval.benchmark import DATASET_ID, DATASET_VERSION, load_dataset, run_agents, score_outputs


def _passing_output(case: dict[str, Any]) -> dict[str, str]:
    keys = case["required_json_keys"]
    body: dict[str, object] = {str(key): "reviewed" for key in keys}
    required_terms = case["must_mention_any"]
    for index, alternatives in enumerate(required_terms):
        body[f"rubric_{index}"] = alternatives[0]
    return {str(case["response_field"]): json.dumps(body)}


def test_packaged_dataset_is_versioned_and_covers_initial_roles() -> None:
    dataset = load_dataset()

    assert dataset["id"] == DATASET_ID
    assert dataset["version"] == DATASET_VERSION
    expected_roles = {
        "system-designer",
        "backend-engineer",
        "qa-engineer",
        "security-auditor",
    }
    assert len(dataset["cases"]) == 12
    assert {case["agent_type"] for case in dataset["cases"]} == expected_roles
    assert len({case["id"] for case in dataset["cases"]}) == len(dataset["cases"])

    cases_by_role = {
        role: [case for case in dataset["cases"] if case["agent_type"] == role]
        for role in expected_roles
    }
    assert all(len(cases) == 3 for cases in cases_by_role.values())
    for cases in cases_by_role.values():
        tags = {tag for case in cases for tag in case.get("tags", [])}
        assert "boundary" in tags
        assert "adversarial" in tags


def test_scoring_reports_case_and_role_reliability_without_raw_outputs() -> None:
    dataset = load_dataset()
    outputs = {case["id"]: _passing_output(case) for case in dataset["cases"]}

    report = score_outputs(dataset, outputs, provider="test-provider", model="fixture-model")
    serialized = report.to_dict()

    assert report.summary["total_cases"] == len(dataset["cases"])
    assert report.summary["passed"] == len(dataset["cases"])
    assert report.summary["pass_rate"] == 1.0
    assert report.summary["by_agent_type"]["qa-engineer"]["pass_rate"] == 1.0
    assert report.provider == "test-provider"
    assert report.model == "fixture-model"
    assert all(case["passed"] for case in serialized["cases"])
    assert all("raw_output" not in case for case in serialized["cases"])


def test_scoring_fails_closed_for_missing_or_extra_case_outputs() -> None:
    dataset = load_dataset()
    first_case = dataset["cases"][0]

    with pytest.raises(ValueError, match="missing="):
        score_outputs(dataset, {first_case["id"]: _passing_output(first_case)})

    complete_outputs = {case["id"]: _passing_output(case) for case in dataset["cases"]}
    complete_outputs["unreviewed.case"] = {}
    with pytest.raises(ValueError, match="extra="):
        score_outputs(dataset, complete_outputs)


def test_invalid_structured_output_is_recorded_as_a_failed_case() -> None:
    dataset = load_dataset()
    outputs = {case["id"]: _passing_output(case) for case in dataset["cases"]}
    outputs[dataset["cases"][0]["id"]] = {dataset["cases"][0]["response_field"]: "[]"}

    report = score_outputs(dataset, outputs)

    result = report.cases[0]
    assert not result.passed
    assert not result.checks["valid_json_object"]
    assert result.checks["required_key:architecture_overview"] is False


def test_agent_execution_failure_is_not_counted_as_a_quality_failure() -> None:
    dataset = load_dataset()
    cases = dataset["cases"]
    failed_case = cases[0]
    outputs = {case["id"]: _passing_output(case) for case in cases[1:]}

    report = score_outputs(dataset, outputs, failures={failed_case["id"]: "ProviderTimeout"})

    failed_result = report.cases[0]
    assert failed_result.execution_status == "failed"
    assert failed_result.passed is None
    assert failed_result.score is None
    assert failed_result.error_type == "ProviderTimeout"
    assert report.summary["execution_failures"] == 1
    assert report.summary["evaluated_cases"] == len(cases) - 1
    assert report.summary["failed"] == 0
    assert report.summary["pass_rate"] == 1.0
    assert report.summary["execution_success_rate"] == (len(cases) - 1) / len(cases)


@pytest.mark.asyncio
async def test_run_agents_persists_provider_failure_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    dataset = load_dataset()
    class FakeAgent:
        def __init__(self, agent_type: str) -> None:
            self.agent_type = agent_type
            self._llm_provider: str | None = None
            self._llm_model: str | None = None

        async def run(self, input_data: dict[str, Any]) -> dict[str, Any]:
            if input_data == dataset["cases"][0]["input"]:
                raise TimeoutError("sensitive provider request details")
            matching_case = next(
                case
                for case in dataset["cases"]
                if case["agent_type"] == self.agent_type and case["input"] == input_data
            )
            result: dict[str, Any] = _passing_output(matching_case)
            result.update({"provider": "fake-provider", "model": "fake-model"})
            return result

    monkeypatch.setattr(
        "solvyn.eval.benchmark.agent_registry.create",
        lambda agent_type, **_kwargs: FakeAgent(agent_type),
    )

    report = await run_agents(dataset, tenant_id="00000000-0000-0000-0000-000000000001")

    failed_case = report.cases[0]
    assert failed_case.execution_status == "failed"
    assert failed_case.error_type == "TimeoutError"
    assert "sensitive" not in str(report.to_dict())
    assert report.summary["execution_failures"] == 1
    assert report.summary["failed"] == 0
    assert report.summary["evaluated_cases"] == len(dataset["cases"]) - 1
