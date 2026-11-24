from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _yaml(path: str) -> dict[str, Any]:
    value = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_prometheus_scrapes_the_compose_api_and_loads_alert_rules() -> None:
    config = _yaml("ops/prometheus.yml")

    assert config["rule_files"] == ["/etc/prometheus/solvyn-alerts.yml"]
    scrape_jobs = config["scrape_configs"]
    assert scrape_jobs == [
        {
            "job_name": "solvyn-api",
            "metrics_path": "/metrics",
            "static_configs": [{"targets": ["api:8000"]}],
        }
    ]
    assert config["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"] == ["alertmanager:9093"]


def test_alert_rules_only_use_exported_http_metrics_and_prometheus_up() -> None:
    metrics_source = (ROOT / "solvyn/observability/metrics.py").read_text(encoding="utf-8")
    registered = set(re.findall(r'(?:Counter|Gauge|Histogram)\(\s*"(solvyn_[^"]+)"', metrics_source))
    rules = _yaml("monitoring/prometheus-alerts.yml")["groups"]
    expressions = [rule["expr"] for group in rules for rule in group["rules"]]
    used = {name for expression in expressions for name in re.findall(r"\b(?:solvyn_[a-zA-Z0-9_]+|up)\b", expression)}

    assert used <= registered | {"up"}
    assert used == {"solvyn_http_requests_total", "up"}


def test_alertmanager_webhook_secret_is_read_from_a_file() -> None:
    config = _yaml("ops/alertmanager.yml")
    receivers = config["receivers"]
    assert len(receivers) == 1
    webhook = receivers[0]["webhook_configs"][0]

    assert webhook["url_file"] == "/run/secrets/alertmanager_webhook_url"
    assert "url" not in webhook
    assert webhook["send_resolved"] is True


def test_monitoring_overlay_is_loopback_only_and_uses_the_production_network() -> None:
    overlay = _yaml("ops/monitoring.compose.yml")
    services = overlay["services"]

    assert services["prometheus"]["image"] == "prom/prometheus:v2.54.1"
    assert services["alertmanager"]["image"] == "prom/alertmanager:v0.27.0"
    assert services["prometheus"]["ports"] == ["127.0.0.1:${PROMETHEUS_PORT:-9090}:9090"]
    assert services["alertmanager"]["ports"] == ["127.0.0.1:${ALERTMANAGER_PORT:-9093}:9093"]
    webhook_mounts = services["alertmanager"]["volumes"]
    assert any("${ALERTMANAGER_WEBHOOK_URL_FILE:?" in mount for mount in webhook_mounts)
    assert overlay["networks"]["solvyn"] == {"external": True, "name": "solvyn_default"}
