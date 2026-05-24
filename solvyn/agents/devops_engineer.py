from typing import Any

from solvyn.agents.base import BaseAgent
from solvyn.agents.schema import AgentCapability, AgentMetadata

SYSTEM_PROMPT = """You are a DevOps Engineer Agent. Your role is to:
- Generate Dockerfiles and Docker Compose configs
- Create Kubernetes manifests (Deployments, Services, Ingress)
- Set up CI/CD pipelines (GitHub Actions, GitLab CI)
- Write Terraform / Pulumi infrastructure code
- Configure monitoring (Prometheus, Grafana) and logging
- Implement backup and disaster recovery strategies
- Follow security best practices (least privilege, secrets management)

Output production-ready configuration files with comments explaining key decisions."""


class DevOpsEngineerAgent(BaseAgent):
    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            agent_type="devops-engineer",
            name="DevOps Engineer",
            description="Docker, k8s, CI/CD, Terraform",
            capabilities=[AgentCapability.DEVOPS],
        )

    async def execute(self, input_data: dict[str, Any]) -> dict[str, Any]:
        project_type = input_data.get("project_type", input_data.get("text", ""))
        language = input_data.get("language", "python")
        deploy_target = input_data.get("deploy_target", "kubernetes")
        ci_platform = input_data.get("ci_platform", "github-actions")

        prompt = f"""Generate deployment configuration for:

Project Type: {project_type}
Language: {language}
Deploy Target: {deploy_target}
CI Platform: {ci_platform}

Include:
1. Dockerfile (multi-stage, optimized layers)
2. Docker Compose for local development
3. Kubernetes manifests (Deployment, Service, Ingress, ConfigMap)
4. CI/CD pipeline configuration
5. Monitoring setup (metrics, health checks)
6. Environment variable management
7. Backup strategy

Output as JSON with keys: dockerfile, docker_compose, k8s_manifests, ci_config, monitoring_config, deployment_instructions."""

        response = await self.llm_generate(prompt, system_prompt=SYSTEM_PROMPT)
        return {"project": project_type, "config": response.content, "provider": response.provider, "model": response.model, "cost": response.cost}
