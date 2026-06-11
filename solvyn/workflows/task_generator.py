import json
import logging
from typing import Any

from solvyn.agents.registry import agent_registry

logger = logging.getLogger(__name__)


class TaskGenerator:
    async def generate_from_spec(self, specification: str, tenant_id: str = "default") -> dict[str, Any]:
        pm_agent = agent_registry.create("product-manager", tenant_id=tenant_id)
        result = await pm_agent.run({"text": specification})

        tasks = self._parse_tasks(result.get("analysis", result.get("output", result.get("content", ""))))
        return {"specification": specification, "tasks": tasks, "raw_output": result}

    def _parse_tasks(self, raw: str) -> list[dict[str, Any]]:
        tasks = []
        try:
            parsed = json.loads(raw)
            epics = parsed.get("epics", [])
            for epic in epics:
                stories = epic.get("stories", [])
                for story in stories:
                    tasks.append(
                        {
                            "title": story.get("title", epic.get("name", "Untitled")),
                            "description": story.get("description", ""),
                            "acceptance_criteria": story.get("acceptance_criteria", []),
                            "epic": epic.get("name", ""),
                            "priority": story.get("priority", "medium"),
                            "estimated_hours": story.get("estimated_hours", 0),
                        }
                    )
        except (json.JSONDecodeError, TypeError, AttributeError):
            lines = raw.strip().split("\n")
            for line in lines:
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("```"):
                    tasks.append({"title": line[:100], "description": line, "priority": "medium"})
        return tasks

    def build_workflow_graph(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        nodes = []
        edges = []
        agent_suggestions = {
            "api": "backend-engineer",
            "database": "backend-engineer",
            "ui": "frontend-engineer",
            "frontend": "frontend-engineer",
            "test": "qa-engineer",
            "deploy": "devops-engineer",
            "devops": "devops-engineer",
            "docker": "devops-engineer",
            "doc": "documentation-writer",
            "docs": "documentation-writer",
            "auth": "security-auditor",
            "security": "security-auditor",
            "review": "code-reviewer",
            "design": "system-designer",
            "architecture": "system-designer",
            "product": "product-manager",
            "prd": "product-manager",
        }

        for i, task in enumerate(tasks):
            title_lower = task.get("title", "").lower()
            agent_type = "backend-engineer"
            for keyword, suggested_agent in agent_suggestions.items():
                if keyword in title_lower:
                    agent_type = suggested_agent
                    break

            node_id = f"task_{i}"
            nodes.append({"id": node_id, "agent_type": agent_type, "title": task.get("title", ""), "status": "pending"})

            # Create sequential edges: task_i depends on task_{i-1}
            if i > 0:
                edges.append({"source": f"task_{i - 1}", "target": node_id})

        return {"nodes": nodes, "edges": edges, "task_count": len(tasks)}
