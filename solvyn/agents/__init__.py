from solvyn.agents.backend_engineer import BackendEngineerAgent
from solvyn.agents.code_reviewer import CodeReviewAgent
from solvyn.agents.devops_engineer import DevOpsEngineerAgent
from solvyn.agents.documentation_writer import DocumentationWriterAgent
from solvyn.agents.frontend_engineer import FrontendEngineerAgent
from solvyn.agents.product_manager import ProductManagerAgent
from solvyn.agents.qa_engineer import QAEngineerAgent
from solvyn.agents.registry import agent_registry
from solvyn.agents.security_auditor import SecurityAuditorAgent
from solvyn.agents.system_designer import SystemDesignAgent

agent_registry.register(BackendEngineerAgent)
agent_registry.register(CodeReviewAgent)
agent_registry.register(DevOpsEngineerAgent)
agent_registry.register(DocumentationWriterAgent)
agent_registry.register(FrontendEngineerAgent)
agent_registry.register(ProductManagerAgent)
agent_registry.register(QAEngineerAgent)
agent_registry.register(SecurityAuditorAgent)
agent_registry.register(SystemDesignAgent)

__all__ = [
    "BackendEngineerAgent",
    "CodeReviewAgent",
    "DevOpsEngineerAgent",
    "DocumentationWriterAgent",
    "FrontendEngineerAgent",
    "ProductManagerAgent",
    "QAEngineerAgent",
    "SecurityAuditorAgent",
    "SystemDesignAgent",
    "agent_registry",
]
