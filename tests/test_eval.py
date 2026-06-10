from __future__ import annotations

import json

import pytest

from solvyn.eval.evaluator import AgentEvaluator, EvalResult, agent_evaluator


class TestEvalResult:
    def test_to_dict(self) -> None:
        result = EvalResult(agent_type="pm", test_name="schema", passed=True, score=0.9)
        d = result.to_dict()
        assert d["agent_type"] == "pm"
        assert d["passed"] is True
        assert d["score"] == 0.9


class TestAgentEvaluator:
    def test_evaluate_json_valid(self) -> None:
        evaluator = AgentEvaluator()
        output = json.dumps({"name": "test", "type": "api"})
        result = evaluator.evaluate_json_output("pm", output, ["name", "type"])
        assert result.passed is True
        assert result.score == 1.0

    def test_evaluate_json_missing_keys(self) -> None:
        evaluator = AgentEvaluator()
        output = json.dumps({"name": "test"})
        result = evaluator.evaluate_json_output("pm", output, ["name", "type"])
        assert result.passed is False
        assert result.score < 1.0

    def test_evaluate_json_invalid(self) -> None:
        evaluator = AgentEvaluator()
        result = evaluator.evaluate_json_output("pm", "not json", ["key"])
        assert result.passed is False
        assert result.score == 0.0

    def test_evaluate_length_ok(self) -> None:
        evaluator = AgentEvaluator()
        result = evaluator.evaluate_length("pm", "hello world", min_length=1, max_length=100)
        assert result.passed is True

    def test_evaluate_length_too_short(self) -> None:
        evaluator = AgentEvaluator()
        result = evaluator.evaluate_length("pm", "", min_length=5, max_length=100)
        assert result.passed is False

    def test_evaluate_no_hallucination_clean(self) -> None:
        evaluator = AgentEvaluator()
        result = evaluator.evaluate_no_hallucination("pm", "This is fine", ["forbidden"])
        assert result.passed is True

    def test_evaluate_no_hallucination_found(self) -> None:
        evaluator = AgentEvaluator()
        result = evaluator.evaluate_no_hallucination("pm", "This contains forbidden content", ["forbidden"])
        assert result.passed is False

    def test_evaluate_code_quality_clean(self) -> None:
        evaluator = AgentEvaluator()
        result = evaluator.evaluate_code_quality("dev", "def hello(): pass")
        assert result.passed is True

    def test_evaluate_code_quality_issues(self) -> None:
        evaluator = AgentEvaluator()
        result = evaluator.evaluate_code_quality("dev", "eval('bad')\nexcept:\n  pass")
        assert result.passed is False
        assert result.metrics["issue_count"] > 0

    def test_evaluate_code_quality_empty(self) -> None:
        evaluator = AgentEvaluator()
        result = evaluator.evaluate_code_quality("dev", "")
        assert result.passed is False

    def test_summarize(self) -> None:
        evaluator = AgentEvaluator()
        results = [
            EvalResult(passed=True, score=1.0),
            EvalResult(passed=True, score=0.8),
            EvalResult(passed=False, score=0.3),
        ]
        summary = evaluator.summarize(results)
        assert summary["total_tests"] == 3
        assert summary["passed"] == 2
        assert summary["pass_rate"] == pytest.approx(2 / 3, rel=0.01)


class TestGlobalEvaluator:
    def test_global_exists(self) -> None:
        assert agent_evaluator is not None
