import json

from typer.testing import CliRunner

from solvyn.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "Solvyn" in result.stdout


def test_status() -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Solvyn Status" in result.stdout


def test_agents() -> None:
    result = runner.invoke(app, ["agents"])
    assert result.exit_code == 0
    assert "product-manager" in result.stdout


def test_eval_datasets_lists_role_coverage() -> None:
    result = runner.invoke(app, ["eval", "datasets"])
    assert result.exit_code == 0
    assert "solvyn-engineering-teammates" in result.stdout
    assert "security-auditor" in result.stdout


def test_eval_compare_reads_saved_reports_without_provider_or_database(tmp_path) -> None:
    report = {
        "dataset_id": "fixture-dataset",
        "dataset_version": "1.0",
        "provider": "fixture-provider",
        "model": "fixture-model",
        "cases": [
            {
                "case_id": "case.one",
                "agent_type": "qa-engineer",
                "execution_status": "completed",
                "passed": True,
                "score": 1.0,
                "duration_ms": 20,
            }
        ],
    }
    candidate = json.loads(json.dumps(report))
    candidate["cases"][0].update({"passed": False, "score": 0.5, "duration_ms": 900})
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    output_path = tmp_path / "results" / "comparison.json"
    baseline_path.write_text(json.dumps(report), encoding="utf-8")
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "eval",
            "compare",
            "--baseline",
            str(baseline_path),
            "--candidate",
            str(candidate_path),
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.stdout
    comparison = json.loads(output_path.read_text(encoding="utf-8"))
    assert comparison["summary"]["pass_rate_delta"] == -1.0
    assert comparison["cases"][0]["score_delta"] == -0.5


def test_providers() -> None:
    result = runner.invoke(app, ["providers"])
    assert result.exit_code == 0
    assert "OpenRouter" in result.stdout or "Provider" in result.stdout


def test_workflows() -> None:
    result = runner.invoke(app, ["workflows"])
    assert result.exit_code == 0
    assert "feature" in result.stdout


def test_pipeline_unknown_workflow() -> None:
    result = runner.invoke(app, ["pipeline", "nonexistent"])
    assert result.exit_code != 0
    assert "Unknown" in result.stdout


def test_chat_unknown_agent() -> None:
    result = runner.invoke(app, ["chat", "nonexistent"])
    assert result.exit_code != 0
    assert "Unknown" in result.stdout


def test_traces_empty() -> None:
    result = runner.invoke(app, ["traces"])
    assert result.exit_code == 0


def test_list_sessions() -> None:
    result = runner.invoke(app, ["list-sessions"])
    assert result.exit_code == 0
    assert "API server" in result.stdout
