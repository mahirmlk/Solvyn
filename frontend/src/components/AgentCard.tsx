interface AgentCardProps {
  name: string;
  description: string;
  status: "idle" | "running" | "error" | "success";
  capabilities: string[];
}

const STATUS_LABELS: Record<AgentCardProps["status"], { label: string; className: string }> = {
  idle: { label: "Idle", className: "" },
  running: { label: "Running", className: "success" },
  error: { label: "Error", className: "error" },
  success: { label: "Ready", className: "success" },
};

export function AgentCard({ name, description, status, capabilities }: AgentCardProps) {
  const state = STATUS_LABELS[status] || STATUS_LABELS.idle;
  const marker = name.replace(/\s+/g, "").slice(0, 2).toUpperCase();

  return (
    <article className="agent-card">
      <div className="agent-card-top">
        <span className="agent-index">{marker}</span>
        <span className={`status-pill ${state.className}`}>
          <span className={`dot ${status === "running" ? "busy" : ""}`} />
          {state.label}
        </span>
      </div>
      <h3 className="agent-name">{name}</h3>
      <p className="agent-description">{description}</p>
      <div className="tag-row">
        {capabilities.slice(0, 3).map((capability) => (
          <span className="tag" key={capability}>
            {capability}
          </span>
        ))}
      </div>
    </article>
  );
}
