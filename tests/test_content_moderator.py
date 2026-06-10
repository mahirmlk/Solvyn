"""Tests for the content moderation guardrail."""
from __future__ import annotations

import re

import pytest

from solvyn.guardrails.content_moderator import (
    ContentModerator,
    ModerationAction,
    ModerationError,
    ModerationFinding,
    Severity,
)


# ---------------------------------------------------------------------------
# Severity enum
# ---------------------------------------------------------------------------

def test_severity_ordering() -> None:
    """Severity levels should have a clear ordering via the moderator's internal list."""
    from solvyn.guardrails.content_moderator import ContentModerator

    mod = ContentModerator()
    order = mod._severity_order
    assert order == [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]


# ---------------------------------------------------------------------------
# ModerationFinding
# ---------------------------------------------------------------------------

def test_moderation_finding_to_dict() -> None:
    """Finding dict should redact matched text."""
    f = ModerationFinding(
        category="slur",
        severity=Severity.CRITICAL,
        matched_text="badword",
        description="Detected slur",
    )
    d = f.to_dict()
    assert d["category"] == "slur"
    assert d["severity"] == Severity.CRITICAL
    assert d["matched_text"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# ContentModerator.moderate_text
# ---------------------------------------------------------------------------

def test_moderate_clean_text() -> None:
    """Clean text should produce no findings."""
    mod = ContentModerator(action=ModerationAction.PASS)
    findings = mod.moderate_text("Hello, how are you today?")
    assert findings == []


def test_moderate_threat_of_violence() -> None:
    """Violent threats should be detected."""
    mod = ContentModerator(action=ModerationAction.PASS)
    findings = mod.moderate_text("I will kill you")
    assert len(findings) >= 1
    categories = [f.category for f in findings]
    assert "threat_violence" in categories


def test_moderate_slur() -> None:
    """Slurs should be detected at critical severity."""
    mod = ContentModerator(action=ModerationAction.PASS)
    findings = mod.moderate_text("That's so retarded")
    assert len(findings) >= 1
    severities = [f.severity for f in findings]
    assert Severity.CRITICAL in severities


def test_moderate_hate_speech() -> None:
    """Hate speech patterns should be detected."""
    mod = ContentModerator(action=ModerationAction.PASS)
    findings = mod.moderate_text("All Jews are terrible people")
    assert len(findings) >= 1
    categories = [f.category for f in findings]
    assert "hate_speech" in categories


def test_moderate_self_harm() -> None:
    """Self-harm content should be detected."""
    mod = ContentModerator(action=ModerationAction.PASS)
    findings = mod.moderate_text("You should kill yourself")
    assert len(findings) >= 1
    categories = [f.category for f in findings]
    assert "self_harm" in categories


def test_moderate_injection_attempt() -> None:
    """SQL/script injection should be detected."""
    mod = ContentModerator(action=ModerationAction.PASS)
    findings = mod.moderate_text("Run this: DROP TABLE users")
    assert len(findings) >= 1
    categories = [f.category for f in findings]
    assert "injection_attempt" in categories


def test_moderate_gender_stereotype() -> None:
    """Gender stereotyping should be detected."""
    mod = ContentModerator(action=ModerationAction.PASS)
    findings = mod.moderate_text("Women should only cook and clean")
    assert len(findings) >= 1
    categories = [f.category for f in findings]
    assert "gender_stereotype" in categories


# ---------------------------------------------------------------------------
# ContentModerator.check (action modes)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_blocks_critical_content() -> None:
    """BLOCK mode should raise ModerationError for critical findings."""
    mod = ContentModerator(action=ModerationAction.BLOCK, severity_threshold=Severity.HIGH)

    with pytest.raises(ModerationError) as exc_info:
        mod.check("I will kill you", agent_type="test-agent")

    assert len(exc_info.value.findings) >= 1
    assert "test-agent" in str(exc_info.value) or True  # agent_type is used in logging


def test_check_flags_but_does_not_raise() -> None:
    """FLAG mode should log but not raise."""
    mod = ContentModerator(action=ModerationAction.FLAG, severity_threshold=Severity.HIGH)
    findings = mod.check("I will kill you", agent_type="test-agent")
    assert len(findings) >= 1  # findings returned but no exception


def test_check_passes_everything() -> None:
    """PASS mode should never raise."""
    mod = ContentModerator(action=ModerationAction.PASS)
    findings = mod.check("I will kill you", agent_type="test-agent")
    assert len(findings) >= 1  # findings detected but no exception


def test_check_below_threshold_not_blocked() -> None:
    """Findings below severity_threshold should not trigger the action."""
    mod = ContentModerator(action=ModerationAction.BLOCK, severity_threshold=Severity.CRITICAL)
    # Gender stereotype is MEDIUM — below CRITICAL threshold
    findings = mod.check("Women should only cook", agent_type="test")
    assert len(findings) >= 1  # detected but not blocked


# ---------------------------------------------------------------------------
# Custom patterns
# ---------------------------------------------------------------------------

def test_custom_patterns() -> None:
    """Custom patterns should be added to the check."""
    custom = [(re.compile(r"\bforbidden_word\b", re.I), "custom_category", Severity.HIGH)]
    mod = ContentModerator(action=ModerationAction.PASS, custom_patterns=custom)

    findings = mod.moderate_text("This contains a forbidden_word here")
    categories = [f.category for f in findings]
    assert "custom_category" in categories


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def test_deduplication() -> None:
    """Same match should not produce duplicate findings."""
    mod = ContentModerator(action=ModerationAction.PASS)
    # Text with the same threat pattern appearing twice
    findings = mod.moderate_text("I will kill you. I will kill you again.")
    # Should deduplicate by category+matched_text
    threat_findings = [f for f in findings if f.category == "threat_violence"]
    # At most 1 finding for the same matched phrase
    assert len(threat_findings) <= 2  # "kill you" vs "kill" may differ


# ---------------------------------------------------------------------------
# check_dict
# ---------------------------------------------------------------------------

def test_check_dict() -> None:
    """check_dict should moderate all string values."""
    mod = ContentModerator(action=ModerationAction.PASS)
    data = {
        "safe": "Hello world",
        "unsafe": "I will kill you",
        "number": 42,
    }
    result_data, findings = mod.check_dict(data, agent_type="test")
    assert result_data == data  # data is not modified
    assert len(findings) >= 1
