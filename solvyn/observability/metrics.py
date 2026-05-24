import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from prometheus_client import Counter, Gauge, Histogram, generate_latest

http_requests_total = Counter("solvyn_http_requests_total", "Total HTTP requests", ["method", "endpoint", "status"])
http_request_duration_seconds = Histogram("solvyn_http_request_duration_seconds", "HTTP request duration", ["method", "endpoint"])
llm_calls_total = Counter("solvyn_llm_calls_total", "Total LLM API calls", ["provider", "model", "status"])
agent_executions_total = Counter("solvyn_agent_executions_total", "Total agent executions", ["agent_type", "status"])
active_agents_gauge = Gauge("solvyn_active_agents", "Number of currently active agents")
workflow_runs_total = Counter("solvyn_workflow_runs_total", "Total workflow runs", ["workflow_name", "status"])
memory_operations_total = Counter("solvyn_memory_operations_total", "Total memory operations", ["operation", "agent_type"])
token_usage_total = Counter("solvyn_token_usage_total", "Total tokens consumed", ["provider", "model"])


class MetricsMiddleware:
    async def __call__(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        endpoint = request.url.path
        http_requests_total.labels(method=request.method, endpoint=endpoint, status=response.status_code).inc()
        http_request_duration_seconds.labels(method=request.method, endpoint=endpoint).observe(duration)

        return response


class Metrics:
    def track_llm_call(self, provider: str, model: str, success: bool) -> None:
        llm_calls_total.labels(provider=provider, model=model, status="ok" if success else "error").inc()

    def track_agent_execution(self, agent_type: str, success: bool) -> None:
        agent_executions_total.labels(agent_type=agent_type, status="completed" if success else "failed").inc()

    def track_workflow_run(self, workflow_name: str, success: bool) -> None:
        workflow_runs_total.labels(workflow_name=workflow_name, status="completed" if success else "failed").inc()

    def track_memory_op(self, operation: str, agent_type: str) -> None:
        memory_operations_total.labels(operation=operation, agent_type=agent_type).inc()

    def track_tokens(self, provider: str, model: str, tokens: int) -> None:
        token_usage_total.labels(provider=provider, model=model).inc(tokens)

    def set_active_agents(self, count: int) -> None:
        active_agents_gauge.set(count)

    def generate(self) -> bytes:
        return generate_latest()


metrics = Metrics()
