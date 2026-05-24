from typing import Any

from solvyn.agents.base import BaseAgent
from solvyn.agents.schema import AgentCapability, AgentMetadata

SYSTEM_PROMPT = """You are a QA/Test Engineer Agent. Your role is to:
- Generate comprehensive test suites (unit, integration, E2E)
- Identify edge cases and boundary conditions
- Follow the project's existing test patterns
- Write tests that are deterministic and maintainable
- Analyze code for untestable patterns
- Suggest test coverage improvements

Always output runnable tests with assertions covering happy and error paths."""


class QAEngineerAgent(BaseAgent):
    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            agent_type="qa-engineer",
            name="QA Engineer",
            description="Unit, integration, and E2E tests",
            capabilities=[AgentCapability.TESTING],
        )

    async def execute(self, input_data: dict[str, Any]) -> dict[str, Any]:
        code_to_test = input_data.get("code", input_data.get("text", ""))
        test_framework = input_data.get("test_framework", "pytest")
        coverage_target = input_data.get("coverage_target", 80)

        prompt = f"""Generate tests for the following code:

Code:
{code_to_test}

Test Framework: {test_framework}
Coverage Target: {coverage_target}%

Include:
1. Unit tests for all functions/classes
2. Edge case tests (empty inputs, boundary values, error states)
3. Integration tests if applicable
4. Test fixtures and setup
5. Mocks for external dependencies
6. Coverage analysis of what's tested vs untested

Output as JSON with keys: unit_tests, integration_tests, fixtures, coverage_analysis, edge_cases_found."""

        response = await self.llm_generate(prompt, system_prompt=SYSTEM_PROMPT)
        return {"code_tested": code_to_test[:200], "tests": response.content, "provider": response.provider, "model": response.model, "cost": response.cost}
