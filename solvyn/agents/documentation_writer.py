from typing import Any

from solvyn.agents.base import BaseAgent
from solvyn.agents.schema import AgentCapability, AgentMetadata

SYSTEM_PROMPT = """You are a Documentation Agent. Your role is to:
- Generate comprehensive README files
- Write API documentation from code or OpenAPI specs
- Create architecture and developer guides
- Write user guides and onboarding docs
- Generate changelogs and release notes
- Follow the project's existing doc patterns
- Use clear, concise, well-structured Markdown

Every claim must be verifiable from the provided source code. Flag uncertainties."""


class DocumentationWriterAgent(BaseAgent):
    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            agent_type="documentation-writer",
            name="Documentation Writer",
            description="README, API docs, release notes",
            capabilities=[AgentCapability.DOCUMENTATION],
        )

    async def execute(self, input_data: dict[str, Any]) -> dict[str, Any]:
        source = input_data.get("source", input_data.get("text", ""))
        doc_type = input_data.get("doc_type", "readme")
        audience = input_data.get("audience", "developers")

        prompt = f"""Generate {doc_type} documentation for:

Source: {source}
Audience: {audience}

Include:
1. Overview and purpose
2. Getting started / installation
3. Usage examples
4. Configuration reference
5. API reference (if applicable)
6. Contributing guidelines
7. License and credits

Output as JSON with keys: documentation (full markdown), sections (array of section titles), word_count."""

        response = await self.llm_generate(prompt, system_prompt=SYSTEM_PROMPT)
        return {"source": source[:200], "documentation": response.content, "provider": response.provider, "model": response.model, "cost": response.cost}
