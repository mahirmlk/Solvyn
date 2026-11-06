const API_BASE = "/api/backend";
let requestQueue: Promise<void> = Promise.resolve();

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const execute = () => performRequest<T>(path, options);
  if (typeof navigator !== "undefined" && "locks" in navigator) {
    return navigator.locks.request("solvyn-api-request", execute);
  }
  const queued = requestQueue.then(execute);
  requestQueue = queued.then(() => undefined, () => undefined);
  return queued;
}

async function performRequest<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null) as { detail?: unknown; error?: string } | null;
    const detail = body?.detail;
    const message = typeof detail === "string"
      ? detail
      : detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string"
        ? detail.message
        : body?.error || res.statusText;
    const recovery = detail && typeof detail === "object" && "recovery" in detail && typeof detail.recovery === "string"
      ? ` ${detail.recovery}`
      : "";
    throw new Error(`API ${res.status}: ${message}${recovery}`);
  }
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
  id?: string;
  action: string;
  agent_type: string;
  timestamp: string;
  details: Record<string, unknown>;
  created_at?: string | null;
}

export interface SessionAnalytics {
  session_id: string;
  totals: { total_tokens: number; total_cost: number; prompt_tokens: number; completion_tokens: number };
  agent_breakdown: { agent_type: string; tokens: number; cost: number; calls: number }[];
}

export interface CurrentUser {
  id: string;
  email: string;
  display_name: string;
  is_active: boolean;
  is_superuser: boolean;
  created_at: string;
}

export interface OrganizationInfo {
  id: string;
  name: string;
  slug: string;
  tenant_id: string;
  is_active: boolean;
  created_at: string;
}

export interface ProjectInfo {
  id: string;
  organization_id: string;
  workspace_id: string | null;
  name: string;
  slug: string;
  description: string;
  instructions: string;
  is_active: boolean;
  created_at: string;
}

export interface TeammateInfo {
  id: string;
  organization_id: string;
  name: string;
  role: string;
  description: string;
  responsibilities: string;
  agent_type: string;
  is_enabled: boolean;
  status: string;
  model_provider: string | null;
  model_name: string | null;
  max_tokens_per_run: number | null;
  max_run_seconds: number | null;
  created_at: string;
  updated_at: string;
}

export interface ApprovalInfo {
  id: string;
  organization_id: string;
  project_id: string | null;
  teammate_id: string | null;
  task_handoff_id: string | null;
  verification_run_id: string | null;
  kind: string;
  summary: string;
  payload: string | null;
  status: string;
  requested_by: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string;
  created_at: string;
  updated_at: string;
}

export interface WorkflowTaskInfo {
  task_id: string;
  execution_id: string;
  session_id: string | null;
  title: string;
  description: string;
  assigned_agent: string | null;
  status: string;
  priority: number;
  attempt_count: number;
  max_attempts: number | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface TaskDeliveryDiff {
  repository_id: string;
  source_commit_sha: string;
  base_branch: string;
  files: { path: string; content: string }[];
  deleted_paths: string[];
  diffs: { path: string; diff: string }[];
  unsupported_missing_source_paths: string[];
  diff_digest: string;
  can_deliver: boolean;
  delivery_history: TaskDeliveryAttempt[];
}

export interface TaskDeliveryAttempt {
  id: string;
  status: "starting" | "branch_created" | "committed" | "pull_request_open" | "unknown";
  diff_digest: string;
  base_branch: string;
  branch: string;
  branch_sha: string | null;
  commit_sha: string | null;
  pull_request_title: string | null;
  pull_request_number: number | null;
  pull_request_url: string | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface TaskDeliveryResult {
  delivery_id: string;
  branch: string;
  commit_sha: string;
  pull_request: { number: number; title: string; url: string; state: string; draft: boolean | null };
  merged: false;
  approved_diff_digest: string;
  replayed: boolean;
}

// Admin panel types -----------------------------------------------------

export interface AdminTenant {
  id: string;
  name: string;
  slug: string;
  role: string;
  is_active: boolean;
  monthly_token_quota: number | null;
  monthly_cost_quota: number | null;
  created_at: string;
  updated_at: string;
  total_tokens: number;
  total_cost: number;
  sessions_count: number;
  api_keys_count: number;
  initial_api_key?: AdminApiKeyCreated;
}

export interface AdminTenantUsage {
  tenant_id: string;
  total_tokens: number;
  total_cost: number;
  prompt_tokens: number;
  completion_tokens: number;
  sessions_count: number;
}

export interface AdminApiKey {
  id: string;
  tenant_id: string;
  name: string;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  is_active: boolean;
  masked: string;
}

export interface AdminApiKeyCreated extends AdminApiKey {
  plaintext_key: string;
}

export const api = {
  health: () => request<HealthStatus>("/health"),

  login: (email: string, password: string) =>
    request<{ authenticated: boolean; expires_in_minutes: number }>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),

  logout: () => request<{ ok: boolean }>("/auth/logout", { method: "POST", body: JSON.stringify({}) }),

  me: () => request<CurrentUser>("/auth/me"),

  listOrganizations: () => request<{ organizations: OrganizationInfo[]; count: number }>("/organizations"),

  listProjects: (organizationId: string) =>
    request<{ projects: ProjectInfo[]; count: number }>(`/organizations/${organizationId}/projects`),

  createProject: (input: { organization_id: string; name: string; slug: string; description: string }) =>
    request<ProjectInfo>("/projects", { method: "POST", body: JSON.stringify(input) }),

  listTeammates: (organizationId: string) =>
    request<{ teammates: TeammateInfo[]; count: number }>(`/organizations/${organizationId}/teammates`),

  createTeammate: (input: { organization_id: string; name: string; role: string; description: string; responsibilities: string; project_ids: string[] }) =>
    request<TeammateInfo>("/teammates", { method: "POST", body: JSON.stringify(input) }),

  updateTeammate: (teammateId: string, input: { is_enabled: boolean }) =>
    request<TeammateInfo>(`/teammates/${teammateId}`, { method: "PATCH", body: JSON.stringify(input) }),

  runTeammate: (teammateId: string, projectId: string, task: string) =>
    request<{ teammate_id: string; project_id: string; output: unknown }>(`/teammates/${teammateId}/run`, {
      method: "POST",
      body: JSON.stringify({ project_id: projectId, input_data: { task } }),
    }),

  listApprovals: (status: "pending" | "approved" | "rejected" | "consumed" | "stale" = "pending") =>
    request<{ approvals: ApprovalInfo[]; count: number }>(`/approvals?status=${status}`),

  decideApproval: (approvalId: string, decision: "approve" | "reject", note: string) =>
    request<ApprovalInfo>(`/approvals/${approvalId}/${decision}`, { method: "POST", body: JSON.stringify({ note }) }),

  executeTaskVerification: (executionId: string, taskId: string, verificationRunId: string) =>
    request<{ id: string; status: string; kind: string; stdout: string; stderr: string }>(
      `/workflows/executions/${executionId}/tasks/${taskId}/verification-runs/${verificationRunId}/execute`,
      { method: "POST" },
    ),

  listWorkflowTasks: (params?: { limit?: number; status?: string }) => {
    const query = new URLSearchParams();
    if (params?.limit) query.set("limit", String(params.limit));
    if (params?.status) query.set("status", params.status);
    return request<{ tasks: WorkflowTaskInfo[]; count: number }>(`/workflows/tasks?${query}`);
  },

  getTaskDeliveryPreview: (executionId: string, taskId: string) =>
    request<TaskDeliveryDiff>(`/workflows/executions/${executionId}/tasks/${taskId}/delivery-preview`),

  deliverTaskWorkspace: (
    executionId: string,
    taskId: string,
    input: {
      expected_diff_digest: string;
      branch_name: string;
      commit_message: string;
      pull_request_title: string;
      pull_request_body: string;
    },
  ) => request<TaskDeliveryResult>(`/workflows/executions/${executionId}/tasks/${taskId}/deliver`, {
    method: "POST",
    body: JSON.stringify(input),
  }),

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

  // -------------------------------------------------------------------
  // Admin — tenant & API key management
  // -------------------------------------------------------------------

  listAdminTenants: () => request<{ tenants: AdminTenant[]; count: number }>("/admin/tenants"),

  getAdminTenantUsage: (tenantId: string) => request<AdminTenantUsage>(`/admin/tenants/${tenantId}/usage`),

  createAdminTenant: (input: { name: string; slug: string; role?: string; is_active?: boolean; monthly_token_quota?: number; monthly_cost_quota?: number }) =>
    request<AdminTenant>("/admin/tenants", { method: "POST", body: JSON.stringify(input) }),

  updateAdminTenant: (tenantId: string, input: { name?: string; role?: string; is_active?: boolean; monthly_token_quota?: number; monthly_cost_quota?: number }) =>
    request<AdminTenant>(`/admin/tenants/${tenantId}`, { method: "PATCH", body: JSON.stringify(input) }),

  deleteAdminTenant: (tenantId: string) => request<{ deleted: boolean; tenant_id: string; is_active: boolean }>(`/admin/tenants/${tenantId}`, { method: "DELETE" }),

  listAdminApiKeys: (tenantId: string) => request<{ api_keys: AdminApiKey[]; count: number }>(`/admin/tenants/${tenantId}/api-keys`),

  createAdminApiKey: (tenantId: string, name: string) =>
    request<AdminApiKeyCreated>(`/admin/tenants/${tenantId}/api-keys`, { method: "POST", body: JSON.stringify({ name }) }),

  revokeAdminApiKey: (keyId: string) => request<{ revoked: boolean; key_id: string; tenant_id: string; revoked_at: string | null }>(`/admin/api-keys/${keyId}`, { method: "DELETE" }),
};
