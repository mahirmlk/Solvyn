interface Node {
  id: string;
  label: string;
  agent?: string;
  status: "pending" | "running" | "success" | "error";
}

interface Edge {
  from: string;
  to: string;
}

interface Props {
  nodes: Node[];
  edges: Edge[];
  compact?: boolean;
}

const STATUS_LABELS: Record<Node["status"], string> = {
  pending: "Pending",
  running: "Running",
  success: "Complete",
  error: "Error",
};

export function ExecutionGraph({ nodes, edges, compact }: Props) {
  if (!nodes || nodes.length === 0) {
    return <div className="empty-state dark-empty">No execution data yet.</div>;
  }

  return (
    <div className="execution-list">
      {edges.length > 0 && (
        <div className="edge-summary">
          {edges.map((edge) => `${edge.from} / ${edge.to}`).join("  |  ")}
        </div>
      )}
      {nodes.map((node, index) => (
        <div className="execution-node" key={node.id}>
          <span className={`execution-number ${node.status}`}>{String(index + 1).padStart(2, "0")}</span>
          <div>
            <p className="node-title">{node.label}</p>
            <p className="node-copy">
              {node.agent || "Agent"} / {STATUS_LABELS[node.status]}
            </p>
          </div>
          {node.status === "running" && !compact && <span className="spinner" aria-label="Running" />}
        </div>
      ))}
    </div>
  );
}
