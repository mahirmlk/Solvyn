"""Shared Dependabot alert normalization for webhook and scheduled sources."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from solvyn.repositories.github import validate_full_name


def _bounded_text(value: Any, *, limit: int, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = "".join(character for character in value if unicodedata.category(character) != "Cc").strip()
    return cleaned[:limit] if cleaned else fallback


def dependabot_alert_signal(
    repository_name: str,
    alert: dict[str, Any],
    *,
    action: str = "created",
) -> dict[str, Any] | None:
    """Normalize a GitHub Dependabot alert to the webhook signal shape.

    The source event ID intentionally matches ``dependabot_alert`` webhook
    normalization so polling and delivery retries converge on one inbox item.
    """
    if not isinstance(alert, dict) or not action:
        return None
    action_key = action.lower()
    state = alert.get("state")
    if action_key in {"fixed", "resolved"}:
        lifecycle_state = "fixed"
    elif action_key in {"closed_by_user", "dismissed", "resolved_by_user", "auto_dismissed"}:
        lifecycle_state = "dismissed"
    elif isinstance(state, str) and state.lower() in {"open", "reopened"}:
        lifecycle_state = "open"
    elif isinstance(state, str) and state.lower() in {"dismissed", "fixed", "auto_dismissed"}:
        lifecycle_state = "dismissed" if state.lower() == "auto_dismissed" else state.lower()
    else:
        return None
    try:
        owner, repo = validate_full_name(repository_name)
    except Exception:
        return None
    owner_repo = f"{owner}/{repo}"

    alert_id = alert.get("number", alert.get("id"))
    if isinstance(alert_id, bool) or not isinstance(alert_id, int) or alert_id < 1:
        return None

    updated_at_value = alert.get("updated_at")
    if not isinstance(updated_at_value, str):
        return None
    try:
        provider_updated_at = datetime.fromisoformat(updated_at_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if provider_updated_at.tzinfo is None or provider_updated_at.utcoffset() is None:
        return None
    provider_updated_at = provider_updated_at.astimezone(UTC)

    rule_value = alert.get("rule")
    rule = rule_value if isinstance(rule_value, dict) else {}
    advisory_value = alert.get("security_advisory")
    advisory = advisory_value if isinstance(advisory_value, dict) else {}
    vulnerabilities = advisory.get("vulnerabilities")
    vulnerability = vulnerabilities[0] if isinstance(vulnerabilities, list) and vulnerabilities and isinstance(vulnerabilities[0], dict) else {}
    package_value = vulnerability.get("package")
    package = package_value if isinstance(package_value, dict) else {}
    dependency_value = alert.get("dependency")
    dependency = dependency_value if isinstance(dependency_value, dict) else {}
    dependency_package_value = dependency.get("package")
    dependency_package = dependency_package_value if isinstance(dependency_package_value, dict) else {}

    severity = advisory.get("severity") if isinstance(advisory.get("severity"), str) else ""
    if not severity:
        severity = (
            _bounded_text(alert.get("security_severity_level"), limit=50)
            or _bounded_text(rule.get("security_severity_level"), limit=50)
            or _bounded_text(vulnerability.get("severity"), limit=50, fallback="unknown")
        )
    summary = (
        _bounded_text(advisory.get("summary"), limit=255)
        or _bounded_text(rule.get("description"), limit=255)
        or _bounded_text(rule.get("name"), limit=255)
        or _bounded_text(package.get("name"), limit=255)
        or _bounded_text(dependency_package.get("name"), limit=255)
        or f"Dependabot Alert #{alert_id}"
    )
    details_url_value = alert.get("html_url")
    if isinstance(details_url_value, str):
        parsed_url = urlparse(details_url_value)
        details_url = (
            details_url_value[:512]
            if parsed_url.scheme == "https" and parsed_url.hostname == "github.com"
            else ""
        )
    else:
        details_url = ""
    branch = _bounded_text(alert.get("ref"), limit=255)
    if branch.startswith("refs/heads/"):
        branch = branch[len("refs/heads/") :]
    # Preserve the incoming action in the event while making open/reopened
    # deliveries and polling share the canonical created-event dedupe key.
    return {
        "repository_name": owner_repo[:255],
        "source_event_id": f"dependabot_alert:{owner_repo.lower()}:{alert_id}:created"[:320],
        "workflow_name": summary,
        "conclusion": severity[:50],
        "branch": branch,
        "details_url": details_url,
        "action": action[:100],
        "alert_number": alert_id,
        "lifecycle_state": lifecycle_state,
        "provider_updated_at": provider_updated_at,
        "notify": lifecycle_state == "open",
    }
