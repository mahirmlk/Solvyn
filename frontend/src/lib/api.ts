const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";
const API_KEY = process.env.NEXT_PUBLIC_API_KEY || "";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(API_KEY ? { "X-API-Key": API_KEY } : {}),
      ...options?.headers,
    },
    ...options,
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  return res.json();
}

export interface HealthStatus {
  status: string;
  service: string;
}

export interface AgentInfo {
  type: string;
  name: string;
  description: string;
  status: string;
  capabilities: string[];
}

export interface WorkflowInfo {
  name: string;
  description: string;
  steps: number;
}

export interface WorkflowDetail {
  name: string;
  description: string;
  steps: StepInfo[];
  default_inputs: Record<string, string>;
}

export interface StepInfo {
  agent_type: string;
  description: string;
  depends_on: string[] | null;
  parallel_with: string[] | null;
}

export interface SessionInfo {
  id: string;
  title: string;
  status: string;
  created_at: string;
}

export interface WorkflowResult {
  workflow: string;
  session_id: string;
  status: string;
  nodes: { agent_type: string; status: string; error: string | null }[];
}

export interface TaskGenResult {
  tasks: Task[];
  graph: { nodes: { id: string; agent_type: string; title: string }[]; edges: { source: string; target: string }[] };
}

export interface Task {
  title: string;
  description: string;
  acceptance_criteria: string[];
  epic: string;
  priority: string;
  estimated_hours: number;
}

export interface TraceInfo {
  id: string;
  name: string;
  agent_type: string;
  session_id: string;
  spans: SpanInfo[];
}

export interface SpanInfo {
  name: string;
  type: string;
  duration_ms: number;
  token_count: number;
  model: string;
  provider: string;
  error: string | null;
}

export interface AuditEntry {
  action: string;
  agent_type: string;
  timestamp: string;
  details: Record<string, unknown>;
}

export interface SessionAnalytics {
  session_id: string;
  totals: { total_tokens: number; total_cost: number; prompt_tokens: number; completion_tokens: number };
  agent_breakdown: { agent_type: string; tokens: number; cost: number; calls: number }[];
}

export const api = {
  health: () => request<HealthStatus>("/health"),

  listAgents: () => request<{ agents: AgentInfo[] }>("/workflows/agents"),

  listWorkflows: () => request<{ workflows: WorkflowInfo[] }>("/workflows/"),

  getWorkflow: (name: string) => request<WorkflowDetail>(`/workflows/${name}`),

  runWorkflow: (name: string, inputs: Record<string, string>) =>
    request<WorkflowResult>(`/workflows/${name}/run`, { method: "POST", body: JSON.stringify(inputs) }),

  generateTasks: (specification: string) =>
    request<TaskGenResult>("/workflows/generate-tasks", { method: "POST", body: JSON.stringify({ specification }) }),

  listSessions: () => request<{ sessions: SessionInfo[] }>("/sessions/"),

  createSession: () => request<SessionInfo>("/sessions/", { method: "POST" }),

  listTraces: (sessionId?: string) =>
    request<{ traces: TraceInfo[] }>(`/observability/traces${sessionId ? `?session_id=${sessionId}` : ""}`),

  getAuditLogs: (params?: { action?: string; agent_type?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.action) q.set("action", params.action);
    if (params?.agent_type) q.set("agent_type", params.agent_type);
    if (params?.limit) q.set("limit", String(params.limit));
    return request<{ entries: AuditEntry[] }>(`/observability/audit?${q}`);
  },

  getSessionAnalytics: (sessionId: string) => request<SessionAnalytics>(`/observability/analytics/session/${sessionId}`),
};
