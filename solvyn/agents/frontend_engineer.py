from typing import Any

from solvyn.agents.base import BaseAgent
from solvyn.agents.schema import AgentCapability, AgentMetadata

SYSTEM_PROMPT = """You are a Frontend Engineer Agent. Your role is to:
- Generate React/TypeScript components
- Implement UI from design specs
- Create API integration hooks
- Set up state management
- Write responsive, accessible markup
- Follow component-driven development patterns

Always output production-ready code with TypeScript types and proper imports."""


class FrontendEngineerAgent(BaseAgent):
    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            agent_type="frontend-engineer",
            name="Frontend Engineer",
            description="React components, hooks, state management",
            capabilities=[AgentCapability.CODE_GENERATION],
        )

    async def execute(self, input_data: dict[str, Any]) -> dict[str, Any]:
        design_spec = input_data.get("design_spec", input_data.get("text", ""))
        framework = input_data.get("framework", "react")
        api_endpoints = input_data.get("api_endpoints", [])

        prompt = f"""Generate frontend code for:

Design Specification: {design_spec}
Framework: {framework}
API Endpoints: {api_endpoints}

Include:
1. Component tree and hierarchy
2. Main components with TypeScript types
3. API integration hooks
4. State management setup
5. Styling approach
6. Test examples

Output as JSON with keys: components_code, hooks_code, styles_code, tests_code, instructions."""

        response = await self.llm_generate(prompt, system_prompt=SYSTEM_PROMPT)
        return {"design_spec": design_spec, "code": response.content, "provider": response.provider, "model": response.model, "cost": response.cost}
