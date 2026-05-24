from typing import Any

from solvyn.agents.base import BaseAgent
from solvyn.agents.schema import AgentCapability, AgentMetadata

SYSTEM_PROMPT = """You are a System Design Agent. Your role is to:
- Design system architecture from requirements
- Create database schemas
- Design API contracts (OpenAPI-style)
- Recommend tech stacks
- Draw architecture diagrams (Mermaid format)
- Define data flow between components

Always output structured JSON with clear technical specifications."""


class SystemDesignAgent(BaseAgent):
    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            agent_type="system-designer",
            name="System Designer",
            description="Architecture diagrams, tech stack, schema design",
            capabilities=[AgentCapability.SYSTEM_DESIGN, AgentCapability.ARCHITECTURE],
        )

    async def execute(self, input_data: dict[str, Any]) -> dict[str, Any]:
        requirements = input_data.get("requirements", input_data.get("text", ""))
        constraints = input_data.get("constraints", "")

        prompt = f"""Design a system architecture for these requirements:

Requirements: {requirements}
Constraints: {constraints}

Produce:
1. High-level architecture overview
2. Recommended tech stack with justification
3. Database schema (tables, relationships, indexes)
4. API endpoints (method, path, request/response)
5. Data flow diagram (Mermaid format)
6. Key design decisions and trade-offs

Output as JSON with keys: architecture_overview, tech_stack, database_schema, api_endpoints, data_flow_diagram, design_decisions."""

        response = await self.llm_generate(prompt, system_prompt=SYSTEM_PROMPT)
        return {"requirements": requirements, "design": response.content, "provider": response.provider, "model": response.model, "cost": response.cost}
