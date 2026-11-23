"""Protect the explicit staging and production deployment gates."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci-cd.yml"


def test_main_push_builds_but_staging_requires_manual_opt_in() -> None:
    config = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    jobs = config["jobs"]
    dispatch_inputs = config["on"]["workflow_dispatch"]["inputs"]

    assert dispatch_inputs["deploy_staging"]["default"] == "false"
    assert dispatch_inputs["deploy_staging"]["type"] == "boolean"
    assert jobs["build"]["needs"] == ["lint", "test", "frontend-audit", "tracing-smoke"]
    api_image_step = next(
        step for step in jobs["build"]["steps"] if step.get("name") == "Build and push API image"
    )
    assert api_image_step["with"]["push"] == "${{ github.ref == 'refs/heads/main' }}"
    staging = jobs["deploy-staging"]
    assert staging["needs"] == ["build"]
    assert staging["if"] == (
        "github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch' && inputs.deploy_staging"
    )


def test_production_still_requires_successful_opted_in_staging() -> None:
    config = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    production = config["jobs"]["deploy-prod"]

    assert production["needs"] == ["build", "deploy-staging"]
    assert production["if"] == (
        "github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch' "
        "&& inputs.deploy_staging && inputs.deploy_production"
    )
