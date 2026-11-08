"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, WorkflowTaskInfo } from "@/lib/api";
import styles from "@/components/WorkspacePages.module.css";

const TASK_STATUSES = ["all", "pending", "running", "waiting_approval", "review_required", "completed", "failed", "cancelled"];
const STATUS_CLASS: Record<string, string> = {
  completed: "success",
  failed: "error",
  cancelled: "error",
};

export default function TasksPage() {
  const [status, setStatus] = useState("all");
  const [tasks, setTasks] = useState<WorkflowTaskInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadTasks = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.listWorkflowTasks({ limit: 100, ...(status === "all" ? {} : { status }) });
      setTasks(response.tasks);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load workflow tasks.");
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => { void loadTasks(); }, [loadTasks]);

  return (
    <div className="route-page">
      <header className="page-intro">
        <div>
          <span className="eyebrow">Durable execution</span>
          <h1 className="page-title">Tasks</h1>
        </div>
        <label className={styles.taskFilter}>
          <span className="field-label">Status</span>
          <select className="editorial-input" onChange={(event) => setStatus(event.target.value)} value={status}>
            {TASK_STATUSES.map((option) => <option key={option} value={option}>{option.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase())}</option>)}
          </select>
        </label>
      </header>

      {error && (
        <section className={styles.notice} role="alert">
          <p>{error}</p>
          {error.includes("401") && <Link className="action-button secondary" href="/login">Sign in</Link>}
        </section>
      )}

      <section className={styles.groups} aria-label="Workflow tasks">
        {loading ? (
          <div className="skeleton" style={{ minHeight: 180 }} />
        ) : tasks.length === 0 ? (
          <div className="empty-state">No {status === "all" ? "workflow" : status.replaceAll("_", " ")} tasks are recorded yet. Start a run from <Link href="/workflows">Workflows</Link>.</div>
        ) : (
          <div className={styles.taskGrid}>
            {tasks.map((task) => (
              <article className={`${styles.taskCard} pearl-pane`} key={task.task_id}>
                <div className={styles.cardTop}>
                  <span className={`status-pill ${STATUS_CLASS[task.status] || ""}`}>{task.status.replaceAll("_", " ")}</span>
                  <span className={`mono ${styles.slug}`}>{task.assigned_agent || "Unassigned"}</span>
                </div>
                <h2><Link href={`/tasks/${task.execution_id}/${task.task_id}`}>{task.title}</Link></h2>
                {task.description && <p>{task.description}</p>}
                {task.error && <p className={styles.taskError}>{task.error}</p>}
                <dl className={styles.taskMeta}>
                  <div><dt>Execution</dt><dd className="mono">{task.execution_id.slice(0, 8)}</dd></div>
                  <div><dt>Attempts</dt><dd>{task.attempt_count}{task.max_attempts ? ` / ${task.max_attempts}` : ""}</dd></div>
                  <div><dt>Updated</dt><dd>{task.updated_at ? new Date(task.updated_at).toLocaleString() : "—"}</dd></div>
                </dl>
                <Link className="action-button secondary" href={`/tasks/${task.execution_id}/${task.task_id}`}>Open task review</Link>
              </article>
            ))}
          </div>
        )}
      </section>
      {!loading && tasks.length === 100 && <p className={styles.taskLimit}>Showing the 100 most recent matching tasks.</p>}
    </div>
  );
}
