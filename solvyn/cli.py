import asyncio
import importlib.metadata
import platform
import uuid

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from solvyn.agents.registry import agent_registry
from solvyn.config import settings
from solvyn.workflows.task_generator import TaskGenerator
from solvyn.workflows.templates import WORKFLOW_TEMPLATES

app = typer.Typer(name="solvyn", help="Solvyn — AI Operating Center for Engineering Teams")
console = Console()

AGENT_TYPES = sorted(agent_registry.list_agents().keys())
AGENT_DESCRIPTIONS = {m.agent_type: m.description for m in agent_registry.list_agents().values()}


def _print_header() -> None:
    from solvyn import __version__

    subtitle = f"v{__version__} — AI Operating Center for Engineering Teams"
    panel = Panel(Text("Solvyn Agent CLI", style="bold cyan"), subtitle=subtitle, border_style="cyan")
    console.print(panel)
    console.print()


def _get_providers_info() -> list[tuple[str, str, str]]:
    def _model(provider: str, model: str) -> str:
        return model if getattr(settings, f"{provider}_api_key") else ""

    return [
        ("OpenRouter", "[OK]" if settings.openrouter_api_key else "[--]", _model("openrouter", settings.llm_default_model)),
        ("Groq", "[OK]" if settings.groq_api_key else "[--]", _model("groq", "llama-3.3-70b-versatile")),
        ("DeepSeek", "[OK]" if settings.deepseek_api_key else "[--]", _model("deepseek", "deepseek-chat")),
    ]


@app.command()
def status() -> None:
    """Show Solvyn system status"""
    table = Table(title="Solvyn Status")
    table.add_column("Component", style="cyan")
    table.add_column("Status", style="green")

    table.add_row("API Server", f"http://{settings.host}:{settings.port}")
    table.add_row("Database", settings.database_url.split("@")[-1] if "@" in settings.database_url else settings.database_url)
    table.add_row("Redis", settings.redis_url)
    table.add_row("Default LLM", f"{settings.llm_default_provider}/{settings.llm_default_model}")

    providers = []
    if settings.openrouter_api_key:
        providers.append("OpenRouter")
    if settings.groq_api_key:
        providers.append("Groq")
    if settings.deepseek_api_key:
        providers.append("DeepSeek")
    table.add_row("LLM Providers", ", ".join(providers) or "None configured")

    console.print(table)


@app.command()
def agents() -> None:
    """List available agent types"""
    agents_list = agent_registry.list_agents()
    table = Table(title=f"Solvyn Agents ({len(agents_list)})")
    table.add_column("#", style="dim")
    table.add_column("Agent", style="cyan")
    table.add_column("Description", style="white")
    table.add_column("Capabilities", style="green")

    for i, (agent_type, meta) in enumerate(sorted(agents_list.items()), 1):
        caps = ", ".join(c.value for c in meta.capabilities)
        table.add_row(str(i), agent_type, meta.description, caps)

    console.print(table)


@app.command()
def chat(agent: str = typer.Argument("product-manager", help="Agent type to chat with")) -> None:
    """Start an interactive chat session with a Solvyn agent"""
    if agent not in AGENT_TYPES:
        console.print(f"[red]Unknown agent: {agent}[/]")
        console.print(f"Available agents: {', '.join(AGENT_TYPES)}")
        raise typer.Exit(1)

    session_id = str(uuid.uuid4())
    tenant_id = settings.default_tenant_id
    console.print(f"[cyan]Starting session [bold]{session_id[:8]}...[/] with {agent}[/]")
    console.print(f"[dim]Agent: {AGENT_DESCRIPTIONS.get(agent, '')}[/]")
    console.print("[yellow]Type 'exit' or 'quit' to end the session.[/]")
    console.print()

    async def _chat_loop() -> None:
        agent_instance = agent_registry.create(agent, session_id=session_id, tenant_id=tenant_id)
        while True:
            user_input = await asyncio.to_thread(console.input, "[bold green]You > [/]")
            if user_input.lower() in ("exit", "quit"):
                break

            console.print(f"[bold cyan]{agent} > [/][italic]Thinking...[/]")
            try:
                result = await agent_instance.run({"text": user_input, "session_id": session_id})
                try:
                    output = next(v for k, v in result.items() if k not in ("provider", "model", "cost", "total_tokens"))
                except StopIteration:
                    output = str(result)
                console.print(f"[bold cyan]{agent} > [/]{output[:2000]}")
                if result.get("cost"):
                    console.print(f"        [dim]cost: ${result['cost']:.6f} | model: {result.get('model', '?')}[/]")
            except Exception as e:
                console.print(f"[red]Error: {e}[/]")
            console.print()

    asyncio.run(_chat_loop())


@app.command()
def run(
    task: str = typer.Option("", help="Task description"),
    agent: str = typer.Option("product-manager", help="Primary agent for this task"),
    output: str = typer.Option("", help="Output file for results"),
) -> None:
    """Run a single task with a Solvyn agent (non-interactive)"""
    session_id = str(uuid.uuid4())
    tenant_id = settings.default_tenant_id

    if agent not in AGENT_TYPES:
        console.print(f"[red]Unknown agent: {agent}[/]")
        raise typer.Exit(1)

    console.print(f"[cyan]Running task with agent '{agent}'...[/]")
    console.print(f"  [dim]session: {session_id}[/]")

    async def _run_task() -> None:
        agent_instance = agent_registry.create(agent, session_id=session_id, tenant_id=tenant_id)
        console.print("[yellow]Processing...[/]")
        try:
            result = await agent_instance.run({"text": task})
            try:
                output_text = next(v for k, v in result.items() if k not in ("provider", "model", "cost", "total_tokens"))
            except StopIteration:
                output_text = str(result)
            console.print(f"[green]{output_text[:2000]}[/]")
            if result.get("cost"):
                console.print(f"        [dim]cost: ${result['cost']:.6f} | model: {result.get('model', '?')} | tokens: {result.get('total_tokens', '?')}[/]")

            if output:
                with open(output, "w") as f:
                    f.write(output_text)
                console.print(f"[dim]Result written to {output}[/]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/]")

    asyncio.run(_run_task())


@app.command()
def workflows() -> None:
    """List available workflow templates"""
    table = Table(title="Solvyn Workflows")
    table.add_column("Name", style="cyan")
    table.add_column("Description", style="white")
    table.add_column("Steps")

    for name, tmpl in WORKFLOW_TEMPLATES.items():
        steps_desc = " → ".join(s.agent_type for s in tmpl.steps)
        table.add_row(name, tmpl.description, steps_desc)

    console.print(table)


@app.command()
def pipeline(
    workflow: str = typer.Argument("feature", help="Workflow template name"),
    input_text: str = typer.Option("", "--input", "-i", help="Input text for the workflow"),
) -> None:
    """Run a workflow pipeline (feature/bugfix/release)"""
    if workflow not in WORKFLOW_TEMPLATES:
        console.print(f"[red]Unknown workflow: {workflow}[/]")
        console.print(f"Available: {', '.join(WORKFLOW_TEMPLATES.keys())}")
        raise typer.Exit(1)

    session_id = str(uuid.uuid4())
    tenant_id = settings.default_tenant_id
    tmpl = WORKFLOW_TEMPLATES[workflow]

    console.print(f"[cyan]Starting {workflow} workflow[/]")
    console.print(f"  [dim]session: {session_id}[/]")
    console.print(f"  [dim]steps: {len(tmpl.steps)}[/]")
    console.print()

    async def _run_pipeline() -> None:
        from solvyn.workflows.executor import workflow_executor

        inputs = {**tmpl.default_inputs}
        if input_text:
            inputs["requirement"] = input_text
            inputs["bug_report"] = input_text

        console.print("[yellow]Executing workflow...[/]")
        try:
            result = await workflow_executor.run(
                template_name=workflow,
                inputs=inputs,
                session_id=session_id,
                tenant_id=tenant_id,
            )

            table = Table(title=f"Workflow '{workflow}' Results")
            table.add_column("Step", style="cyan")
            table.add_column("Agent", style="green")
            table.add_column("Status")
            table.add_column("Error", style="red")

            for node in result.nodes:
                table.add_row(node.id[:8], node.agent_type, node.status, node.error or "")

            console.print(table)
            console.print(f"[green]Workflow status: {result.status}[/]")
        except Exception as e:
            console.print(f"[red]Workflow failed: {e}[/]")

    asyncio.run(_run_pipeline())


@app.command()
def generate_tasks(
    spec: str = typer.Option("", "--spec", "-s", help="Product specification text"),
) -> None:
    """Generate tasks from a product specification"""
    if not spec:
        console.print("[yellow]No specification provided. Use --spec to provide one.[/]")
        return

    async def _generate() -> None:
        generator = TaskGenerator()
        result = await generator.generate_from_spec(spec)
        tasks = result["tasks"]
        graph = generator.build_workflow_graph(tasks)

        console.print(f"[green]Generated {len(tasks)} tasks[/]")

        table = Table(title="Generated Tasks")
        table.add_column("#")
        table.add_column("Title", style="cyan")
        table.add_column("Priority")
        table.add_column("Suggested Agent", style="green")

        for i, task in enumerate(tasks[:20]):
            title = task.get("title", "")[:60]
            priority = task.get("priority", "medium")
            agent = graph["nodes"][i]["agent_type"] if i < len(graph["nodes"]) else "?"
            table.add_row(str(i + 1), title, priority, agent)

        console.print(table)

        if len(tasks) > 20:
            console.print(f"[dim]... and {len(tasks) - 20} more tasks[/]")

    asyncio.run(_generate())


@app.command()
def traces(limit: int = typer.Option(10, "--limit", "-l", help="Number of traces to show")) -> None:
    """Show recent execution traces"""
    from solvyn.observability.tracing import tracing_manager

    traces_list = tracing_manager.list_traces(limit)
    if not traces_list:
        console.print("[yellow]No traces recorded yet.[/]")
        return

    table = Table(title=f"Recent Traces ({len(traces_list)})")
    table.add_column("ID", style="dim")
    table.add_column("Name", style="cyan")
    table.add_column("Agent")
    table.add_column("Spans")
    table.add_column("Session")

    for t in traces_list:
        table.add_row(t["id"][:12], t["name"], t["agent_type"], str(len(t["spans"])), (t.get("session_id") or "?")[:8])

    console.print(table)


@app.command()
def audit(
    action: str = typer.Option("", "--action", "-a", help="Filter by action type"),
    agent: str = typer.Option("", "--agent", help="Filter by agent type"),
    limit: int = typer.Option(10, "--limit", "-l", help="Number of entries"),
) -> None:
    """Show audit log entries"""

    async def _show_audit() -> None:
        from solvyn.observability.audit import audit_logger

        entries = await audit_logger.search(action=action or None, agent_type=agent or None, limit=limit)
        if not entries:
            console.print("[yellow]No audit entries found.[/]")
            return

        table = Table(title=f"Audit Log ({len(entries)} entries)")
        table.add_column("Time", style="dim")
        table.add_column("Action", style="cyan")
        table.add_column("Agent")
        table.add_column("Details")

        for e in entries:
            ts = (e.get("timestamp") or "?")[:19]
            details = str(e.get("details", {}))[:60]
            table.add_row(ts, e.get("action", "?"), e.get("agent_type", "?"), details)

        console.print(table)

    asyncio.run(_show_audit())


@app.command(name="list-sessions")
def list_sessions() -> None:
    """List recent sessions (requires API server)"""
    console.print("[yellow]Session listing requires database connection.[/]")
    console.print("[dim]Start the API server: solvyn-serve[/]")
    console.print("[dim]Then check: GET /api/v1/sessions[/]")


@app.command()
def providers() -> None:
    """Show configured LLM providers and their status"""
    table = Table(title="LLM Providers")
    table.add_column("Provider", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Default Model")
    table.add_column("API Key", style="dim")

    for name, status, model in _get_providers_info():
        key_status = "Configured" if "[OK]" in status else "Missing"
        table.add_row(name, status, model, key_status)

    console.print(table)


@app.command()
def version() -> None:
    """Show Solvyn version and system info"""
    from solvyn import __version__

    info = Table.grid(padding=(0, 2))
    info.add_column(style="cyan")
    info.add_column(style="white")
    info.add_row("Solvyn", f"v{__version__}")
    info.add_row("Python", platform.python_version())
    info.add_row("Platform", platform.platform())
    info.add_row("Host", f"{settings.host}:{settings.port}")
    console.print(info)
    console.print()

    try:
        deps = ["fastapi", "uvicorn", "pydantic", "sqlalchemy", "alembic", "redis", "typer", "httpx"]
        dep_table = Table(title="Core Dependencies")
        dep_table.add_column("Package", style="cyan")
        dep_table.add_column("Version")
        for dep in deps:
            ver = importlib.metadata.version(dep)
            dep_table.add_row(dep, ver)
        console.print(dep_table)
    except importlib.metadata.PackageNotFoundError:
        pass


if __name__ == "__main__":
    app()
