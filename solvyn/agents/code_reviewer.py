from typing import Any

from solvyn.agents.base import BaseAgent
from solvyn.agents.schema import AgentCapability, AgentMetadata

SYSTEM_PROMPT = """You are a Code Review Agent. Your role is to:
- Review code for correctness, performance, security, and style
- Identify bugs, anti-patterns, and code smells
- Check type safety and error handling
- Verify tests are adequate
- Suggest concrete improvements with code examples
- Categorize findings as Issues (must fix), Suggestions (consider), or Notes

Be thorough but constructive. Always reference specific lines or patterns."""


class CodeReviewAgent(BaseAgent):
    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            agent_type="code-reviewer",
            name="Code Reviewer",
            description="Static analysis, style, correctness verification",
            capabilities=[AgentCapability.CODE_REVIEW],
        )

    async def execute(self, input_data: dict[str, Any]) -> dict[str, Any]:
        code = input_data.get("code", input_data.get("text", ""))
        language = input_data.get("language", "python")
        review_focus = input_data.get("focus", "correctness,performance,security,style")

        prompt = f"""Review this {language} code:

```{language}
{code}
```

Focus areas: {review_focus}

Categorize findings as:
- 🔴 ISSUES (must fix — bugs, security, correctness)
- 🟡 SUGGESTIONS (should consider — performance, maintainability)
- 🔵 NOTES (observations, style preferences)

For each finding include: line reference, severity, explanation, and suggested fix.

Output as JSON with keys: issues (array), suggestions (array), notes (array), overall_score (1-10), summary."""

        response = await self.llm_generate(prompt, system_prompt=SYSTEM_PROMPT)
        return {"code_reviewed": code[:200], "review": response.content, "provider": response.provider, "model": response.model, "cost": response.cost}
