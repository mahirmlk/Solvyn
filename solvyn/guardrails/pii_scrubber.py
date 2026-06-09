from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    "phone": re.compile(r"(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "ip_address": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ABIA|ACCA|ASIA)[A-Z0-9]{16}\b"),
    "aws_secret_key": re.compile(r"(?:aws_secret_access_key|secret_key)['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
    "jwt_token": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "api_key_generic": re.compile(r"(?:api[_-]?key|apikey|api[_-]?secret)['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{16,})", re.IGNORECASE),
    "password_in_config": re.compile(r"(?:password|passwd|pwd)['\"]?\s*[:=]\s*['\"]?([^\s'\"]{6,})", re.IGNORECASE),
    "ipv6": re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"),
}

REDACTION_PLACEHOLDER: dict[str, str] = {
    "email": "[REDACTED_EMAIL]",
    "phone": "[REDACTED_PHONE]",
    "ssn": "[REDACTED_SSN]",
    "credit_card": "[REDACTED_CREDIT_CARD]",
    "ip_address": "[REDACTED_IP]",
    "aws_access_key": "[REDACTED_AWS_KEY]",
    "aws_secret_key": "[REDACTED_AWS_SECRET]",
    "private_key": "[REDACTED_PRIVATE_KEY]",
    "jwt_token": "[REDACTED_JWT]",
    "api_key_generic": "[REDACTED_API_KEY]",
    "password_in_config": "[REDACTED_PASSWORD]",
    "ipv6": "[REDACTED_IPV6]",
}


class PIIScrubber:
    """Scrub PII and secrets from text content before persistence or transmission."""

    def __init__(self, custom_patterns: dict[str, re.Pattern[str]] | None = None) -> None:
        self._patterns = {**PII_PATTERNS}
        if custom_patterns:
            self._patterns.update(custom_patterns)

    def scrub_text(self, text: str) -> tuple[str, list[dict[str, str]]]:
        findings: list[dict[str, str]] = []
        scrubbed = text
        for pii_type, pattern in self._patterns.items():
            matches = list(pattern.finditer(scrubbed))
            if not matches:
                continue
            placeholder = REDACTION_PLACEHOLDER.get(pii_type, f"[REDACTED_{pii_type.upper()}]")
            for match in matches:
                findings.append(
                    {
                        "type": pii_type,
                        "value": "[REDACTED]",
                        "position": str(match.start()),
                    }
                )
            scrubbed = pattern.sub(placeholder, scrubbed)
        return scrubbed, findings

    def scrub_dict(self, data: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
        all_findings: list[dict[str, str]] = []
        scrubbed: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str):
                cleaned, findings = self.scrub_text(value)
                all_findings.extend(findings)
                scrubbed[key] = cleaned
            elif isinstance(value, dict):
                cleaned_sub, findings = self.scrub_dict(value)
                all_findings.extend(findings)
                scrubbed[key] = cleaned_sub
            elif isinstance(value, list):
                cleaned_list, findings = self._scrub_list(value)
                all_findings.extend(findings)
                scrubbed[key] = cleaned_list
            else:
                scrubbed[key] = value
        return scrubbed, all_findings

    def _scrub_list(self, items: list[Any]) -> tuple[list[Any], list[dict[str, str]]]:
        all_findings: list[dict[str, str]] = []
        result: list[Any] = []
        for item in items:
            if isinstance(item, str):
                cleaned, findings = self.scrub_text(item)
                all_findings.extend(findings)
                result.append(cleaned)
            elif isinstance(item, dict):
                cleaned_sub, findings = self.scrub_dict(item)
                all_findings.extend(findings)
                result.append(cleaned_sub)
            else:
                result.append(item)
        return result, all_findings

    def has_pii(self, text: str) -> bool:
        for pattern in self._patterns.values():
            if pattern.search(text):
                return True
        return False


pii_scrubber = PIIScrubber()
