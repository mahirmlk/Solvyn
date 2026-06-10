from typing import Any

from solvyn.agents.base import BaseAgent
from solvyn.agents.schema import AgentCapability, AgentMetadata

SYSTEM_PROMPT = """You are a Security Auditor Agent. Your role is to:
- Scan code for OWASP Top 10 vulnerabilities
- Detect hardcoded secrets, API keys, and credentials
- Check authentication and authorization patterns
- Identify injection risks (SQL, XSS, command injection)
- Review dependency usage for known vulnerabilities
- Audit data handling for GDPR/HIPAA/PCI compliance
- Check for insecure deserialization, SSRF, path traversal

Assign severity: Critical, High, Medium, Low, Info. Always suggest specific fixes."""


class SecurityAuditorAgent(BaseAgent):
    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            agent_type="security-auditor",
            name="Security Auditor",
            description="Vulnerability scanning, OWASP, secrets detection",
            capabilities=[AgentCapability.SECURITY],
        )

    async def execute(self, input_data: dict[str, Any]) -> dict[str, Any]:
        code = input_data.get("code", input_data.get("text", ""))
        scan_type = input_data.get("scan_type", "full")
        compliance = input_data.get("compliance", "")

        prompt = f"""Perform a security audit on the following code:

Code:
```{input_data.get("language", "python")}
{code}
```

Scan Type: {scan_type}
Compliance Standards: {compliance or "None specified"}

Check for:
1. Hardcoded secrets / credentials
2. SQL / NoSQL injection vectors
3. XSS vulnerabilities
4. Authentication / authorization flaws
5. Insecure data handling
6. Path traversal / SSRF
7. Insecure deserialization
8. Dependency vulnerabilities
9. Compliance violations

Output as JSON with keys: critical_findings, high_findings, medium_findings, low_findings, compliance_issues, overall_risk_score, summary."""

        response = await self.llm_generate(prompt, system_prompt=SYSTEM_PROMPT)
        return {"code_audited": code[:200], "audit": response.content, "provider": response.provider, "model": response.model, "cost": response.cost}
