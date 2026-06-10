from __future__ import annotations

import pytest

from solvyn.guardrails.pii_scrubber import PIIScrubber, pii_scrubber
from solvyn.guardrails.output_validator import OutputValidator, output_validator


class TestPIIScrubber:
    def test_scrub_email(self) -> None:
        scrubbed, findings = pii_scrubber.scrub_text("Contact me at john@example.com")
        assert "[REDACTED_EMAIL]" in scrubbed
        assert len(findings) == 1
        assert findings[0]["type"] == "email"

    def test_scrub_phone(self) -> None:
        scrubbed, findings = pii_scrubber.scrub_text("Call 555-123-4567")
        assert "[REDACTED_PHONE]" in scrubbed

    def test_scrub_ssn(self) -> None:
        scrubbed, findings = pii_scrubber.scrub_text("SSN: 123-45-6789")
        assert "[REDACTED_SSN]" in scrubbed

    def test_scrub_aws_key(self) -> None:
        scrubbed, findings = pii_scrubber.scrub_text("Key: AKIAIOSFODNN7EXAMPLE")
        assert "[REDACTED_AWS_KEY]" in scrubbed

    def test_scrub_private_key(self) -> None:
        scrubbed, findings = pii_scrubber.scrub_text("-----BEGIN RSA PRIVATE KEY-----")
        assert "[REDACTED_PRIVATE_KEY]" in scrubbed

    def test_scrub_jwt(self) -> None:
        text = "Token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        scrubbed, findings = pii_scrubber.scrub_text(text)
        assert "[REDACTED_JWT]" in scrubbed

    def test_scrub_dict(self) -> None:
        data = {"config": "password=secret123", "host": "localhost"}
        scrubbed, findings = pii_scrubber.scrub_dict(data)
        assert "[REDACTED_PASSWORD]" in scrubbed["config"]

    def test_no_pii_clean_text(self) -> None:
        assert not pii_scrubber.has_pii("This is a clean sentence about dogs.")

    def test_has_pii(self) -> None:
        assert pii_scrubber.has_pii("Email: test@test.com")


class TestOutputValidator:
    def test_validate_json(self) -> None:
        result = output_validator.validate_json_output('{"key": "value"}')
        assert result["key"] == "value"

    def test_validate_json_block(self) -> None:
        content = 'Some text\n```json\n{"a": 1}\n```\nMore text'
        result = output_validator.validate_json_output(content)
        assert result["a"] == 1

    def test_validate_invalid_json(self) -> None:
        result = output_validator.validate_json_output("not json at all", agent_type="test")
        assert "_error" in result
        assert result["_agent_type"] == "test"

    def test_validate_with_schema(self) -> None:
        result = output_validator.validate_with_schema('{"name": "test", "type": "api"}', ["name", "type"])
        assert result["name"] == "test"
        assert "_validation_errors" not in result

    def test_validate_with_missing_keys(self) -> None:
        result = output_validator.validate_with_schema('{"name": "test"}', ["name", "type"])
        assert "_validation_errors" in result

    def test_scrub_and_validate(self) -> None:
        content = '{"email": "test@test.com", "data": "ok"}'
        result = output_validator.scrub_and_validate(content)
        assert "[REDACTED_EMAIL]" in result.get("email", "")
        assert "_pii_findings" in result

    def test_sanitize_text_output(self) -> None:
        text, findings = output_validator.sanitize_text_output("Send to john@example.com")
        assert "[REDACTED_EMAIL]" in text

    def test_chain_validate(self) -> None:
        result = output_validator.chain_validate('{"a": 1, "b": 2}', required_keys=["a", "b"])
        assert result["a"] == 1
        assert "_validation_errors" not in result

    def test_chain_validate_missing(self) -> None:
        result = output_validator.chain_validate('{"a": 1}', required_keys=["a", "b"])
        assert "_validation_errors" in result
