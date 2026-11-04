"""Content moderation guardrail for filtering toxic, biased, and harmful content.

Provides multi-layered content filtering:
1. **Keyword filter** — blocks obvious toxic patterns (configurable word lists)
2. **Pattern detector** — regex-based detection for slurs, hate speech patterns
3. **Severity classification** — categorises findings by severity (low / medium / high / critical)
4. **Action modes** — can *block* (raise error), *flag* (log + annotate), or *pass* content through

The moderator is designed to be called as a guardrail in the agent pipeline
before content is persisted or forwarded to downstream systems.
"""
from __future__ import annotations

import logging
import re
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Severity levels
# ---------------------------------------------------------------------------

class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ModerationAction(StrEnum):
    BLOCK = "block"      # Raise ModerationError
    FLAG = "flag"        # Log warning + annotate, but allow through
    PASS = "pass"        # Do nothing


# ---------------------------------------------------------------------------
# Moderation finding
# ---------------------------------------------------------------------------

class ModerationFinding:
    """A single moderation finding."""

    def __init__(
        self,
        category: str,
        severity: Severity,
        matched_text: str,
        pattern: str = "",
        description: str = "",
    ) -> None:
        self.category = category
        self.severity = severity
        self.matched_text = matched_text
        self.pattern = pattern
        self.description = description

    def to_dict(self) -> dict[str, str]:
        return {
            "category": self.category,
            "severity": self.severity,
            "matched_text": "[REDACTED]",
            "pattern": self.pattern,
            "description": self.description,
        }


# ---------------------------------------------------------------------------
# Moderation error
# ---------------------------------------------------------------------------

class ModerationError(Exception):
    """Raised when content is blocked by the moderator."""

    def __init__(self, findings: list[ModerationFinding], content_preview: str = "") -> None:
        self.findings = findings
        self.content_preview = content_preview[:200]
        summary = "; ".join(f"{f.category} ({f.severity})" for f in findings)
        super().__init__(f"Content blocked by moderation: {summary}")


# ---------------------------------------------------------------------------
# Toxic pattern definitions
# ---------------------------------------------------------------------------

# Patterns mapped to (category, severity)
TOXIC_PATTERNS: list[tuple[re.Pattern[str], str, Severity]] = [
    # Direct threats of violence
    (re.compile(r"\b(?:kill|murder|assassinate|execute)\s+(?:you|him|her|them|everyone)\b", re.I), "threat_violence", Severity.CRITICAL),
    (re.compile(r"\b(?:i(?:'ll| will)\s+(?:kill|murder|destroy|end))\b", re.I), "threat_violence", Severity.CRITICAL),
    # Slurs and dehumanising language (abbreviated for demonstration)
    (re.compile(r"\b(?:nigg[a-z]*|fagg[a-z]*|retard[a-z]*|kike[a-z]*|spic[a-z]*)\b", re.I), "slur", Severity.CRITICAL),
    # Sexually explicit / non-consensual
    (re.compile(r"\b(?:rape|rapist|raping)\b", re.I), "sexual_violence", Severity.HIGH),
    # Self-harm encouragement
    (re.compile(r"\b(?:suicide|kill\s+yourself|kys)\b", re.I), "self_harm", Severity.HIGH),
    # Hate speech patterns
    (re.compile(r"\b(?:all\s+(?:jews|muslims|christians|blacks|whites|asians|gays|lesbians)\s+(?:are|should|deserve))\b", re.I), "hate_speech", Severity.HIGH),
    # Doxxing patterns
    (re.compile(r"\b(?:\d{3}[-.]?\d{3}[-.]?\d{4}.*(?:address|live|home|phone))\b", re.I), "doxxing", Severity.HIGH),
    # Malicious code injection attempts in agent outputs
    (re.compile(r"(?:DROP\s+TABLE|DELETE\s+FROM|<script>|javascript:)", re.I), "injection_attempt", Severity.MEDIUM),
]

# Lower-severity bias patterns (often unintentional in LLM output)
BIAS_PATTERNS: list[tuple[re.Pattern[str], str, Severity]] = [
    # Gender stereotyping
    (
        re.compile(r"\b(?:women|girls?)\s+(?:should|must|need to|ought to)\s+(?:only|just|merely)\s+(?:cook|clean|stay|serve)\b", re.I),
        "gender_stereotype",
        Severity.MEDIUM,
    ),
    # Racial stereotyping
    (
        re.compile(r"\b(?:all|most|typical)\s+(?:black|white|asian|hispanic|latino)\s+(?:people|men|women)\s+(?:are|do|have)\b", re.I),
        "racial_stereotype",
        Severity.MEDIUM,
    ),
    # Age discrimination
    (re.compile(r"\b(?:too\s+old|too\s+young|age\s+doesn'?t\s+matter)\b", re.I), "age_bias", Severity.LOW),
]


# ---------------------------------------------------------------------------
# ContentModerator
# ---------------------------------------------------------------------------

class ContentModerator:
    """Multi-layered content moderation guardrail.

    Parameters
    ----------
    action : ModerationAction
        What to do when content violates rules.  Default is ``BLOCK``.
    custom_patterns : list of (re.Pattern, category, severity) tuples
        Additional patterns to check beyond the built-in ones.
    severity_threshold : Severity
        Minimum severity that triggers the configured *action*.
        Findings below this threshold are always logged but not blocked.
    """

    def __init__(
        self,
        action: ModerationAction = ModerationAction.BLOCK,
        custom_patterns: list[tuple[re.Pattern[str], str, Severity]] | None = None,
        severity_threshold: Severity = Severity.HIGH,
    ) -> None:
        self.action = action
        self.severity_threshold = severity_threshold
        self._patterns: list[tuple[re.Pattern[str], str, Severity]] = [
            *TOXIC_PATTERNS,
            *BIAS_PATTERNS,
        ]
        if custom_patterns:
            self._patterns.extend(custom_patterns)

        self._severity_order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]

    def _meets_threshold(self, severity: Severity) -> bool:
        return self._severity_order.index(severity) >= self._severity_order.index(self.severity_threshold)

    def moderate_text(self, text: str) -> list[ModerationFinding]:
        """Scan text and return all findings (regardless of action mode)."""
        findings: list[ModerationFinding] = []
        seen: set[str] = set()

        for pattern, category, severity in self._patterns:
            for match in pattern.finditer(text):
                matched = match.group(0)
                dedup_key = f"{category}:{matched.lower()}"
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                findings.append(
                    ModerationFinding(
                        category=category,
                        severity=severity,
                        matched_text=matched,
                        pattern=pattern.pattern,
                        description=f"Detected {category} content",
                    )
                )

        return findings

    def check(self, text: str, agent_type: str = "") -> list[ModerationFinding]:
        """Run moderation and apply the configured action.

        Returns the list of findings.  If action is ``BLOCK`` and
        threshold-severity findings exist, raises ``ModerationError``.
        """
        findings = self.moderate_text(text)

        threshold_findings = [f for f in findings if self._meets_threshold(f.severity)]

        if threshold_findings:
            for f in threshold_findings:
                logger.warning(
                    "Content moderation finding [%s] in %s: %s",
                    f.severity,
                    agent_type or "unknown",
                    f.category,
                )

            if self.action == ModerationAction.BLOCK:
                raise ModerationError(threshold_findings, text)
            elif self.action == ModerationAction.FLAG:
                logger.warning(
                    "Content flagged (not blocked) by moderation in %s: %d findings",
                    agent_type,
                    len(threshold_findings),
                )

        return findings

    def check_dict(
        self,
        data: dict[str, Any],
        agent_type: str = "",
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        """Moderate all string values in a dict.

        Returns (data, findings_dicts) where findings_dicts is the list of
        finding dicts for logging / storage.
        """
        all_findings: list[ModerationFinding] = []

        for key, value in data.items():
            if isinstance(value, str):
                findings = self.check(value, agent_type)
                all_findings.extend(findings)

        findings_dicts = [f.to_dict() for f in all_findings]
        return data, findings_dicts


# Module-level singleton with default config
content_moderator = ContentModerator()
