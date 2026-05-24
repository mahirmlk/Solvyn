from typing import Any

from solvyn.agents.base import BaseAgent
from solvyn.agents.schema import AgentCapability, AgentMetadata

SYSTEM_PROMPT = """You are a Product Manager Agent. Your role is to:
- Parse product requirements and user stories
- Break down features into epics, stories, and tasks
- Define acceptance criteria
- Create dependency graphs of work items
- Estimate effort and priority

Always output structured JSON with clear task breakdowns."""


class ProductManagerAgent(BaseAgent):
    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            agent_type="product-manager",
            name="Product Manager",
            description="Parse PRDs, generate epics and user stories",
            capabilities=[AgentCapability.PRODUCT_MANAGEMENT],
        )

    async def execute(self, input_data: dict[str, Any]) -> dict[str, Any]:
        requirement = input_data.get("requirement", input_data.get("text", ""))
        context = input_data.get("context", "")

        prompt = f"""Analyze this product requirement and produce:
1. A concise product overview
2. 3-5 epics with descriptions
3. User stories for each epic
4. Acceptance criteria for priority stories
5. Task dependency graph

Requirement: {requirement}
Context: {context}

Output as JSON with keys: overview, epics (array of {{name, description, stories}}), dependencies."""

        response = await self.llm_generate(prompt, system_prompt=SYSTEM_PROMPT)
        return {"requirement": requirement, "analysis": response.content, "provider": response.provider, "model": response.model, "cost": response.cost}
