from dataclasses import dataclass, field


@dataclass
class WorkflowStep:
    agent_type: str
    input_map: dict[str, str]
    parallel_with: list[str] | None = None
    depends_on: list[str] | None = None
    description: str = ""


@dataclass
class WorkflowTemplate:
    name: str
    description: str
    steps: list[WorkflowStep]
    default_inputs: dict[str, str] = field(default_factory=dict)


feature_workflow = WorkflowTemplate(
    name="feature",
    description="Full feature delivery pipeline: PRD → Design → Code → Test → Review → Docs",
    steps=[
        WorkflowStep(
            agent_type="product-manager",
            input_map={"requirement": "requirement"},
            description="Analyze requirements and generate epics/stories",
        ),
        WorkflowStep(
            agent_type="system-designer",
            input_map={"requirements": "product-manager.output"},
            depends_on=["product-manager"],
            description="Design system architecture from requirements",
        ),
        WorkflowStep(
            agent_type="backend-engineer",
            input_map={"specification": "system-designer.output"},
            parallel_with=["frontend-engineer"],
            depends_on=["system-designer"],
            description="Generate backend code from design spec",
        ),
        WorkflowStep(
            agent_type="frontend-engineer",
            input_map={"design_spec": "system-designer.output"},
            parallel_with=["backend-engineer"],
            depends_on=["system-designer"],
            description="Generate frontend components from design spec",
        ),
        WorkflowStep(
            agent_type="qa-engineer",
            input_map={"code": "backend-engineer.output,frontend-engineer.output"},
            depends_on=["backend-engineer", "frontend-engineer"],
            description="Generate tests for all code",
        ),
        WorkflowStep(
            agent_type="code-reviewer",
            input_map={"code": "backend-engineer.output,frontend-engineer.output"},
            depends_on=["backend-engineer", "frontend-engineer"],
            description="Review all generated code",
        ),
        WorkflowStep(
            agent_type="security-auditor",
            input_map={"code": "backend-engineer.output,frontend-engineer.output"},
            depends_on=["backend-engineer", "frontend-engineer"],
            description="Security audit of all code",
        ),
        WorkflowStep(
            agent_type="devops-engineer",
            input_map={"project_type": "requirement"},
            depends_on=["backend-engineer"],
            description="Generate deployment configuration",
        ),
        WorkflowStep(
            agent_type="documentation-writer",
            input_map={"source": "backend-engineer.output,frontend-engineer.output"},
            depends_on=["backend-engineer", "frontend-engineer"],
            description="Generate documentation",
        ),
    ],
    default_inputs={"requirement": "Enter product requirement here"},
)


bugfix_workflow = WorkflowTemplate(
    name="bugfix",
    description="Bug fix pipeline: Report → Analysis → Fix → Test → Review → Deploy",
    steps=[
        WorkflowStep(
            agent_type="qa-engineer",
            input_map={"code": "bug_report"},
            description="Analyze bug and reproduce steps",
        ),
        WorkflowStep(
            agent_type="backend-engineer",
            input_map={"specification": "qa-engineer.output"},
            depends_on=["qa-engineer"],
            description="Implement bug fix",
        ),
        WorkflowStep(
            agent_type="code-reviewer",
            input_map={"code": "backend-engineer.output,qa-engineer.output"},
            depends_on=["backend-engineer"],
            description="Review the fix",
        ),
        WorkflowStep(
            agent_type="devops-engineer",
            input_map={"project_type": "bug_report"},
            depends_on=["backend-engineer"],
            description="Prepare hotfix deployment",
        ),
    ],
    default_inputs={"bug_report": "Describe the bug and steps to reproduce"},
)


release_workflow = WorkflowTemplate(
    name="release",
    description="Release pipeline: Changelog → Docs → Security → Deploy",
    steps=[
        WorkflowStep(
            agent_type="documentation-writer",
            input_map={"source": "changelog_input", "doc_type": "release_notes"},
            description="Generate release notes from changelog",
        ),
        WorkflowStep(
            agent_type="security-auditor",
            input_map={"code": "codebase_snapshot"},
            parallel_with=["documentation-writer"],
            description="Final security scan before release",
        ),
        WorkflowStep(
            agent_type="devops-engineer",
            input_map={"project_type": "project_info"},
            depends_on=["documentation-writer", "security-auditor"],
            description="Execute production deployment",
        ),
    ],
    default_inputs={"changelog_input": "Recent changes", "codebase_snapshot": "Code to audit", "project_info": "Project description"},
)


WORKFLOW_TEMPLATES: dict[str, WorkflowTemplate] = {
    "feature": feature_workflow,
    "bugfix": bugfix_workflow,
    "release": release_workflow,
}
