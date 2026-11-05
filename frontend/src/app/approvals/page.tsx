"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, ApprovalInfo } from "@/lib/api";
import styles from "@/components/WorkspacePages.module.css";

type ApprovalStatus = "pending" | "approved" | "rejected";

const STATUS_OPTIONS: ApprovalStatus[] = ["pending", "approved", "rejected"];

export default function ApprovalsPage() {
  const [status, setStatus] = useState<ApprovalStatus>("pending");
  const [approvals, setApprovals] = useState<ApprovalInfo[]>([]);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [busyApprovalId, setBusyApprovalId] = useState<string | null>(null);
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

      <section className={styles.groups} aria-label={`${status} approvals`}>
        {loading ? (
          <div className="skeleton" style={{ minHeight: 180 }} />
        ) : approvals.length === 0 ? (
          <div className="empty-state">No {status} approval requests.</div>
        ) : approvals.map((approval) => (
          <article className={`${styles.approvalCard} pearl-pane`} key={approval.id}>
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
            {approval.decision_note && <p className={styles.decisionNote}>Decision note: {approval.decision_note}</p>}
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
          </article>
        ))}
      </section>
    </div>
  );
}
