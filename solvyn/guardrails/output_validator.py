from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from solvyn.guardrails.pii_scrubber import pii_scrubber

logger = logging.getLogger(__name__)

JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


class OutputValidator:
    """Validate and sanitize agent outputs before they are passed downstream."""

    def __init__(self) -> None:
        self._validators: dict[str, Callable[..., Any]] = {}

    def register_validator(self, output_type: str, validator: Callable[..., Any]) -> None:
        self._validators[output_type] = validator

    def validate_json_output(self, content: str, agent_type: str = "") -> dict[str, Any]:
        extracted = self._extract_json(content)
        if extracted is None:
            logger.warning(f"Agent '{agent_type}' produced invalid JSON output")
            return {"_error": "Invalid JSON output", "_raw": content[:200], "_agent_type": agent_type}
        return extracted

    def validate_with_schema(self, content: str, required_keys: list[str], agent_type: str = "") -> dict[str, Any]:
        parsed = self.validate_json_output(content, agent_type)
        if "_error" in parsed:
            return parsed
        missing = [k for k in required_keys if k not in parsed]
        if missing:
            logger.warning(f"Agent '{agent_type}' output missing keys: {missing}")
            parsed["_validation_errors"] = [f"Missing key: {k}" for k in missing]
        return parsed

    def scrub_and_validate(self, content: str, agent_type: str = "") -> dict[str, Any]:
        scrubbed_text, pii_findings = pii_scrubber.scrub_text(content)
        if pii_findings:
            logger.warning(f"PII found in agent '{agent_type}' output: {len(pii_findings)} instances scrubbed")
        parsed = self.validate_json_output(scrubbed_text, agent_type)
        parsed["_pii_findings"] = pii_findings
        return parsed

    def sanitize_text_output(self, content: str, agent_type: str = "") -> tuple[str, list[dict[str, str]]]:
        scrubbed, findings = pii_scrubber.scrub_text(content)
        if findings:
            logger.warning(f"PII scrubbed from {agent_type} output: {len(findings)} items")
        return scrubbed, findings

    def _extract_json(self, content: str) -> dict[str, Any] | None:
        content = content.strip()
        match = JSON_BLOCK_PATTERN.search(content)
        if match:
            content = match.group(1).strip()
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return parsed
            return {"_value": parsed}
        except json.JSONDecodeError:
            return None

    def chain_validate(self, content: str, agent_type: str = "", required_keys: list[str] | None = None) -> dict[str, Any]:
        result = self.scrub_and_validate(content, agent_type)
        if "_error" in result:
            return result
        if required_keys:
            missing = [k for k in required_keys if k not in result]
            if missing:
                result["_validation_errors"] = [f"Missing key: {k}" for k in missing]
        return result


output_validator = OutputValidator()
