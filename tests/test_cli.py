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
