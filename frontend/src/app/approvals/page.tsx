"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, ApprovalInfo } from "@/lib/api";
import styles from "@/components/WorkspacePages.module.css";

type ApprovalStatus = "pending" | "approved" | "rejected" | "consumed" | "stale";

type VerificationApprovalDetails = {
  execution_id: string;
  task_id: string;
  verification_run_id: string;
  workspace_id: string;
  kind: string;
  command: string[];
  source_digest: string;
};

const STATUS_OPTIONS: ApprovalStatus[] = ["pending", "approved", "rejected", "consumed", "stale"];

function verificationDetails(approval: ApprovalInfo): VerificationApprovalDetails | null {
  if (approval.kind !== "verification_run" || !approval.payload) return null;
  try {
    const value = JSON.parse(approval.payload) as Partial<VerificationApprovalDetails>;
    if (!value.execution_id || !value.task_id || !value.verification_run_id || !value.workspace_id || !Array.isArray(value.command)) return null;
    return value as VerificationApprovalDetails;
  } catch {
    return null;
  }
}

export default function ApprovalsPage() {
  const [status, setStatus] = useState<ApprovalStatus>("pending");
  const [approvals, setApprovals] = useState<ApprovalInfo[]>([]);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [busyApprovalId, setBusyApprovalId] = useState<string | null>(null);
  const [busyRunApprovalId, setBusyRunApprovalId] = useState<string | null>(null);
  const [openHistoryId, setOpenHistoryId] = useState<string | null>(null);
  const [historyLoadingId, setHistoryLoadingId] = useState<string | null>(null);
  const [historyByApproval, setHistoryByApproval] = useState<Record<string, NonNullable<ApprovalInfo["history"]>>>({});
  const [executionMessage, setExecutionMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadApprovals = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.listApprovals(status);
      setApprovals(response.approvals);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load approvals.");
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => { void loadApprovals(); }, [loadApprovals]);

  async function decide(approval: ApprovalInfo, decision: "approve" | "reject") {
    setError(null);
    setBusyApprovalId(approval.id);
    try {
      await api.decideApproval(approval.id, decision, notes[approval.id]?.trim() || "");
      setHistoryByApproval((current) => {
        const next = { ...current };
        delete next[approval.id];
        return next;
      });
      setOpenHistoryId((current) => (current === approval.id ? null : current));
      setNotes((current) => {
        const next = { ...current };
        delete next[approval.id];
        return next;
      });
      await loadApprovals();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : `Could not ${decision} this request.`);
    } finally {
      setBusyApprovalId(null);
    }
  }

  async function executeVerification(approval: ApprovalInfo) {
    const details = verificationDetails(approval);
    if (!details) return;
    setError(null);
    setBusyRunApprovalId(approval.id);
    try {
      const result = await api.executeTaskVerification(details.execution_id, details.task_id, details.verification_run_id);
      setExecutionMessage(`Verification finished with status: ${result.status}.`);
      await loadApprovals();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not run the approved verification.");
    } finally {
      setBusyRunApprovalId(null);
    }
  }

  async function toggleHistory(approvalId: string) {
    if (openHistoryId === approvalId) {
      setOpenHistoryId(null);
      return;
    }
    setOpenHistoryId(approvalId);
    if (historyByApproval[approvalId]) return;
    setHistoryLoadingId(approvalId);
    try {
      const approval = await api.getApproval(approvalId);
      setHistoryByApproval((current) => ({ ...current, [approvalId]: approval.history || [] }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load approval history.");
      setOpenHistoryId(null);
    } finally {
      setHistoryLoadingId(null);
    }
  }

  return (
    <div className="route-page">
      <header className="page-intro">
        <div>
          <span className="eyebrow">Human oversight</span>
          <h1 className="page-title">Approvals</h1>
        </div>
        <label className={styles.approvalFilter}>
          <span className="field-label">View</span>
          <select className="editorial-input" onChange={(event) => setStatus(event.target.value as ApprovalStatus)} value={status}>
            {STATUS_OPTIONS.map((option) => <option key={option} value={option}>{option[0].toUpperCase() + option.slice(1)}</option>)}
          </select>
        </label>
      </header>

      {error && (
        <section className={styles.notice} role="alert">
          <p>{error}</p>
          {error.includes("401") && <Link className="action-button secondary" href="/login">Sign in</Link>}
        </section>
      )}
      {executionMessage && <section className={styles.notice} role="status"><p>{executionMessage}</p></section>}

      <section className={styles.groups} aria-label={`${status} approvals`}>
        {loading ? (
          <div className="skeleton" style={{ minHeight: 180 }} />
        ) : approvals.length === 0 ? (
          <div className="empty-state">No {status} approval requests.</div>
        ) : approvals.map((approval) => (
          <article className={`${styles.approvalCard} pearl-pane`} key={approval.id}>
            {(() => {
              const details = verificationDetails(approval);
              return (
                <>
            <div className={styles.approvalHead}>
              <div>
                <span className="eyebrow">{approval.kind.replaceAll("_", " ")}</span>
                <h2 className={styles.sectionTitle}>{approval.summary}</h2>
              </div>
              <span className={`status-pill ${approval.status === "approved" ? "success" : ""}`}>{approval.status}</span>
            </div>
            <dl className={styles.approvalMeta}>
              <div><dt>Requested by</dt><dd>{approval.requested_by || "Teammate"}</dd></div>
              <div><dt>Created</dt><dd>{approval.created_at ? new Date(approval.created_at).toLocaleString() : "—"}</dd></div>
              {approval.project_id && <div><dt>Project</dt><dd className="mono">{approval.project_id.slice(0, 8)}</dd></div>}
              {approval.teammate_id && <div><dt>Teammate</dt><dd className="mono">{approval.teammate_id.slice(0, 8)}</dd></div>}
              {approval.task_handoff_id && <div><dt>Task handoff</dt><dd className="mono">{approval.task_handoff_id.slice(0, 8)}</dd></div>}
            </dl>
            <button
              className="action-button secondary"
              onClick={() => void toggleHistory(approval.id)}
              type="button"
            >
              {historyLoadingId === approval.id
                ? "Loading history…"
                : openHistoryId === approval.id
                  ? "Hide history"
                  : "View history"}
            </button>
            {openHistoryId === approval.id && (
              <section className={styles.decisionNote} aria-label="Approval transition history">
                <strong>Approval history</strong>
                {(historyByApproval[approval.id] || []).length > 0 ? (
                  <ol>
                    {(historyByApproval[approval.id] || []).map((event, index) => (
                      <li key={`${event.event_type}-${event.occurred_at}-${index}`}>
                        {event.event_type === "created"
                          ? `Requested as ${event.status.replaceAll("_", " ")}`
                          : `${event.previous_status?.replaceAll("_", " ") || "Unknown"} → ${event.status.replaceAll("_", " ")}`}
                        {" · "}{new Date(event.occurred_at).toLocaleString()}
                        {event.actor_user_id && <span> · actor {event.actor_user_id.slice(0, 8)}</span>}
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p>No transition history is available for this approval.</p>
                )}
              </section>
            )}
            {details && (
              <section className={styles.decisionNote} aria-label="Approved verification details">
                <strong>Verification request · {details.kind}</strong>
                <p>Task <code>{details.task_id}</code> · workspace <code>{details.workspace_id}</code></p>
                <p>Workspace digest <code>{details.source_digest}</code></p>
                <p>Exact command <code>{details.command.map((part) => JSON.stringify(part)).join(" ")}</code></p>
              </section>
            )}
            {approval.decision_note && <p className={styles.decisionNote}>Decision note: {approval.decision_note}</p>}
            {details && approval.status === "approved" && (
              <div className={styles.approvalActions}>
                <button className="action-button" disabled={busyRunApprovalId === approval.id} onClick={() => void executeVerification(approval)} type="button">
                  {busyRunApprovalId === approval.id ? "Running approved check…" : "Run approved verification"}
                </button>
                <span className="copy-muted">Execution rechecks that the workspace still matches this approved digest.</span>
              </div>
            )}
            {approval.status === "pending" && (
              <div className={styles.approvalActions}>
                <label className="field">
                  <span className="field-label">Decision note (optional)</span>
                  <textarea className="editorial-input" onChange={(event) => setNotes((current) => ({ ...current, [approval.id]: event.target.value }))} rows={2} value={notes[approval.id] || ""} />
                </label>
                <div className={styles.formFooter}>
                  <span className="copy-muted">Approving or rejecting requires organization owner or admin access.</span>
                  <div className={styles.decisionButtons}>
                    <button className="action-button secondary" disabled={busyApprovalId === approval.id} onClick={() => void decide(approval, "reject")} type="button">{busyApprovalId === approval.id ? "Saving…" : "Reject"}</button>
                    <button className="action-button" disabled={busyApprovalId === approval.id} onClick={() => void decide(approval, "approve")} type="button">{busyApprovalId === approval.id ? "Saving…" : "Approve"}</button>
                  </div>
                </div>
              </div>
            )}
                </>
              );
            })()}
          </article>
        ))}
      </section>
    </div>
  );
}
