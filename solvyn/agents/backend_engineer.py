from typing import Any

from solvyn.agents.base import BaseAgent
from solvyn.agents.schema import AgentCapability, AgentMetadata

SYSTEM_PROMPT = """You are a Backend Engineer Agent. Your role is to:
- Generate production-ready backend code (FastAPI/Python preferred)
- Implement API endpoints, database models, and business logic
- Write Alembic migrations
- Add input validation and error handling
- Follow SOLID principles and clean architecture

Always output code with proper imports, type hints, and docstrings."""


class BackendEngineerAgent(BaseAgent):
    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            agent_type="backend-engineer",
            name="Backend Engineer",
            description="Generate FastAPI endpoints, models, business logic",
            capabilities=[AgentCapability.CODE_GENERATION],
        )

    async def execute(self, input_data: dict[str, Any]) -> dict[str, Any]:
        specification = input_data.get("specification", input_data.get("text", ""))
        language = input_data.get("language", "python")
        framework = input_data.get("framework", "fastapi")

        prompt = f"""Generate backend code for:

Specification: {specification}
Language: {language}
Framework: {framework}

Include:
1. Database models (SQLAlchemy)
2. API endpoints with request/response schemas
3. Service layer with business logic
4. Input validation
5. Error handling
6. Unit test examples

Output as JSON with keys: models_code, api_code, service_code, tests_code, instructions."""

        response = await self.llm_generate(prompt, system_prompt=SYSTEM_PROMPT)
        return {"specification": specification, "code": response.content, "provider": response.provider, "model": response.model, "cost": response.cost}
