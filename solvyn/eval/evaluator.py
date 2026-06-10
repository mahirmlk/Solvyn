from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class EvalResult:
    """Result of a single evaluation run."""

    agent_type: str = ""
    test_name: str = ""
    passed: bool = False
    score: float = 0.0
    max_score: float = 1.0
    details: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_type": self.agent_type,
            "test_name": self.test_name,
            "passed": self.passed,
            "score": self.score,
            "max_score": self.max_score,
            "details": self.details,
            "metrics": self.metrics,
            "duration_ms": self.duration_ms,
        }


class AgentEvaluator:
    """Framework for evaluating agent output quality against ground truth."""

    def __init__(self) -> None:
        self._test_suites: dict[str, list[dict[str, Any]]] = {}

    def register_test_suite(self, agent_type: str, tests: list[dict[str, Any]]) -> None:
        self._test_suites[agent_type] = tests
        logger.info(f"Registered {len(tests)} tests for agent '{agent_type}'")

    def evaluate_json_output(self, agent_type: str, output: str, required_keys: list[str], test_name: str = "json_schema") -> EvalResult:
        start = time.monotonic()
        try:
            parsed = json.loads(output) if isinstance(output, str) else output
        except (json.JSONDecodeError, TypeError):
            return EvalResult(
                agent_type=agent_type,
                test_name=test_name,
                passed=False,
                score=0.0,
                details="Output is not valid JSON",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        missing = [k for k in required_keys if k not in parsed]
        score = (len(required_keys) - len(missing)) / len(required_keys) if required_keys else 1.0
        return EvalResult(
            agent_type=agent_type,
            test_name=test_name,
            passed=len(missing) == 0,
            score=score,
            max_score=1.0,
            details=f"Missing keys: {missing}" if missing else "All required keys present",
            metrics={"missing_keys": missing, "found_keys": [k for k in required_keys if k in parsed]},
            duration_ms=(time.monotonic() - start) * 1000,
        )

    def evaluate_length(self, agent_type: str, output: str, min_length: int = 1, max_length: int = 10000, test_name: str = "length_check") -> EvalResult:
        start = time.monotonic()
        length = len(output)
        passed = min_length <= length <= max_length
        score = 1.0 if passed else max(0.0, 1.0 - abs(length - (min_length + max_length) / 2) / ((max_length - min_length) / 2))
        return EvalResult(
            agent_type=agent_type,
            test_name=test_name,
            passed=passed,
            score=score,
            max_score=1.0,
            details=f"Length {length} (expected {min_length}-{max_length})",
            metrics={"length": length, "min_length": min_length, "max_length": max_length},
            duration_ms=(time.monotonic() - start) * 1000,
        )

    def evaluate_no_hallucination(self, agent_type: str, output: str, forbidden_phrases: list[str], test_name: str = "no_hallucination") -> EvalResult:
        start = time.monotonic()
        found = [p for p in forbidden_phrases if p.lower() in output.lower()]
        score = 1.0 if not found else max(0.0, 1.0 - len(found) / len(forbidden_phrases))
        return EvalResult(
            agent_type=agent_type,
            test_name=test_name,
            passed=len(found) == 0,
            score=score,
            max_score=1.0,
            details=f"Found forbidden phrases: {found}" if found else "No hallucinated content detected",
            metrics={"forbidden_found": found, "forbidden_total": len(forbidden_phrases)},
            duration_ms=(time.monotonic() - start) * 1000,
        )

    def evaluate_code_quality(self, agent_type: str, code: str, test_name: str = "code_quality") -> EvalResult:
        start = time.monotonic()
        issues: list[str] = []
        score = 1.0

        if not code.strip():
            return EvalResult(
                agent_type=agent_type,
                test_name=test_name,
                passed=False,
                score=0.0,
                details="Empty code output",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        if len(code) > 10000:
            issues.append("Code exceeds 10000 characters")
            score -= 0.2

        if "TODO" in code or "FIXME" in code or "HACK" in code:
            issues.append("Contains unresolved TODO/FIXME/HACK markers")
            score -= 0.1

        if "password" in code.lower() and "=" in code:
            issues.append("Potential hardcoded password detected")
            score -= 0.3

        if "except:" in code or "except Exception" in code:
            issues.append("Broad exception handling detected")
            score -= 0.1

        if "eval(" in code or "exec(" in code:
            issues.append("Use of eval/exec detected (security risk)")
            score -= 0.3

        if "SELECT" in code.upper() and "%" in code:
            issues.append("Potential SQL injection: string formatting in SQL query")
            score -= 0.3

        return EvalResult(
            agent_type=agent_type,
            test_name=test_name,
            passed=len(issues) == 0,
            score=max(0.0, score),
            max_score=1.0,
            details=f"Issues: {issues}" if issues else "Code quality checks passed",
            metrics={"issues": issues, "issue_count": len(issues)},
            duration_ms=(time.monotonic() - start) * 1000,
        )

    def run_test_suite(self, agent_type: str, outputs: list[dict[str, Any]]) -> list[EvalResult]:
        results: list[EvalResult] = []
        for test in self._test_suites.get(agent_type, []):
            test_name = test.get("name", "unnamed")
            test_type = test.get("type", "json_schema")

            output = ""
            for o in outputs:
                if o.get("test_name") == test_name:
                    output = o.get("output", "")
                    break

            if test_type == "json_schema":
                result = self.evaluate_json_output(agent_type, output, test.get("required_keys", []), test_name)
            elif test_type == "length":
                result = self.evaluate_length(agent_type, output, test.get("min_length", 1), test.get("max_length", 10000), test_name)
            elif test_type == "no_hallucination":
                result = self.evaluate_no_hallucination(agent_type, output, test.get("forbidden_phrases", []), test_name)
            elif test_type == "code_quality":
                result = self.evaluate_code_quality(agent_type, output, test_name)
            else:
                result = EvalResult(agent_type=agent_type, test_name=test_name, details=f"Unknown test type: {test_type}")

            results.append(result)

        return results

    def summarize(self, results: list[EvalResult]) -> dict[str, Any]:
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        avg_score = sum(r.score for r in results) / total if total > 0 else 0.0
        return {
            "total_tests": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": passed / total if total > 0 else 0.0,
            "avg_score": avg_score,
        }


agent_evaluator = AgentEvaluator()
