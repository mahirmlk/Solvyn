"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  api,
  VerificationQueueResponse,
  VerificationQueueStatus,
} from "@/lib/api";
import styles from "./page.module.css";

const PAGE_SIZE = 50;
const STATUS_OPTIONS: { value: "all" | VerificationQueueStatus; label: string }[] = [
  { value: "all", label: "All statuses" },
  { value: "pending_approval", label: "Pending approval" },
  { value: "approved", label: "Approved" },
  { value: "running", label: "Running" },
  { value: "interrupted", label: "Interrupted" },
  { value: "stale", label: "Stale" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "dead_letter", label: "Dead letter" },
];

function formatDate(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatStatus(value: string): string {
  return value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

export default function VerificationQueuePage() {
  const [status, setStatus] = useState<"all" | VerificationQueueStatus>("all");
  const [offset, setOffset] = useState(0);
  const [queue, setQueue] = useState<VerificationQueueResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [redrivingId, setRedrivingId] = useState<string | null>(null);
  const [controlUpdatingId, setControlUpdatingId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.getVerificationQueue({
        limit: PAGE_SIZE,
        offset,
        ...(status === "all" ? {} : { status }),
      });
      setQueue(response);
      if (offset > 0 && offset >= response.total) {
        const lastOffset = response.total === 0
          ? 0
          : Math.floor((response.total - 1) / PAGE_SIZE) * PAGE_SIZE;
        setOffset(lastOffset);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load the verification queue.");
      setQueue(null);
    } finally {
      setLoading(false);
    }
  }, [offset, status]);

  useEffect(() => { void refresh(); }, [refresh]);

  function changeStatus(nextStatus: "all" | VerificationQueueStatus) {
    setQueue(null);
    setLoading(true);
    setStatus(nextStatus);
    setOffset(0);
  }

  function changeOffset(nextOffset: number) {
    setQueue(null);
    setLoading(true);
    setOffset(nextOffset);
  }

  async function toggleQueuePause(control: VerificationQueueResponse["controls"][number]) {
    setControlUpdatingId(control.organization_id);
    setActionMessage(null);
    try {
      await api.setVerificationQueuePaused(control.organization_id, !control.paused);
      setActionMessage(control.paused
        ? `New verification claims resumed for ${control.organization_name}.`
        : `New claims paused for ${control.organization_name}. Running verifications will continue.`);
      await refresh();
    } catch (reason) {
      setActionMessage(reason instanceof Error ? reason.message : "Could not update the queue control.");
    } finally {
      setControlUpdatingId(null);
    }
  }

  async function redrive(itemId: string) {
    setRedrivingId(itemId);
    setActionMessage(null);
    try {
      const result = await api.redriveVerification(itemId);
      setActionMessage(
        result.already_redriven
          ? "This dead-letter run already has a redrive request."
          : "A fresh approval request was created. Approve it in Approvals before it can run.",
      );
      await refresh();
    } catch (reason) {
      setActionMessage(reason instanceof Error ? reason.message : "Could not create a redrive request.");
    } finally {
      setRedrivingId(null);
    }
  }

  const counts = queue?.counts ?? {};
  const summary = [
    { label: "Pending approval", value: counts.pending_approval ?? 0 },
    { label: "Approved", value: counts.approved ?? 0 },
    { label: "Running", value: counts.running ?? 0 },
    { label: "Dead letter", value: counts.dead_letter ?? 0 },
  ];
  const hasCurrentPage = queue !== null && offset < queue.total;
  const start = hasCurrentPage ? offset + 1 : 0;
  const end = hasCurrentPage && queue ? Math.min(offset + queue.limit, queue.total) : 0;

  return (
    <div className="route-page">
      <header className="page-intro">
        <div>
          <span className="eyebrow">Worker operations</span>
          <h1 className="page-title">Verification queue</h1>
        </div>
        <button className="action-button secondary" onClick={() => void refresh()} disabled={loading} type="button">
          {loading ? "Refreshing…" : "Refresh queue"}
        </button>
      </header>

      {error && (
        <div className={styles.error} role="alert">
          {error.includes("403")
            ? "This queue is available to organization owners and admins."
            : error.includes("401")
              ? "Sign in with an organization owner or admin account to inspect the queue."
              : error}
          {error.includes("401") && <> <Link href="/login">Sign in</Link></>}
        </div>
      )}
      {actionMessage && <p className={styles.actionMessage} role="status">{actionMessage}</p>}

      <section className={styles.controlSection} aria-labelledby="queue-controls-title">
        <div className={styles.controlIntro}>
          <div>
            <span className="eyebrow">Organization controls</span>
            <h2 id="queue-controls-title">New verification claims</h2>
          </div>
          <p>Pausing blocks new worker claims after the change is saved. Running verifications continue, and approval requests remain available.</p>
        </div>
        <div className={styles.controlList}>
          {queue?.controls.map((control) => (
            <article className={`${styles.controlCard} pearl-pane`} key={control.organization_id}>
              <div>
                <strong>{control.organization_name}</strong>
                <span className={styles.subtle}>{control.paused ? "New claims are paused" : "New claims are allowed"}</span>
              </div>
              <button
                className={`action-button${control.paused ? "" : " secondary"}`}
                disabled={loading || controlUpdatingId !== null}
                onClick={() => void toggleQueuePause(control)}
                type="button"
              >
                {controlUpdatingId === control.organization_id ? "Saving…" : control.paused ? "Resume claims" : "Pause new claims"}
              </button>
            </article>
          ))}
        </div>
      </section>

      <section className={styles.summaryGrid} aria-label="Queue status counts">
        {summary.map((item) => (
          <article className={`${styles.summaryCard} pearl-pane`} key={item.label}>
            <span className={styles.summaryLabel}>{item.label}</span>
            <strong className={styles.summaryValue}>
              {loading && !queue ? "—" : queue ? item.value : "Unavailable"}
            </strong>
          </article>
        ))}
      </section>

      <div className={styles.toolbar}>
        <label className={styles.filter}>
          <span className="field-label">Filter by status</span>
          <select
            className="editorial-input"
            onChange={(event) => changeStatus(event.target.value as "all" | VerificationQueueStatus)}
            value={status}
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </label>
        <span className={styles.paginationLabel}>
          {loading ? "Loading queue…" : queue ? `${queue.total} matching runs` : "Queue unavailable"}
        </span>
      </div>

      <section className={styles.tableShell} aria-label="Verification runs">
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">Task</th>
              <th scope="col">Status</th>
              <th scope="col">Approval</th>
              <th scope="col">Attempts</th>
              <th scope="col">Lease expires</th>
              <th scope="col">Started</th>
              <th scope="col">Queue action</th>
            </tr>
          </thead>
          <tbody>
            {loading && !queue ? (
              <tr><td className={styles.emptyCell} colSpan={7}>Loading queue…</td></tr>
            ) : queue?.items.length ? queue.items.map((item) => (
              <tr key={item.id}>
                <td>
                  {item.execution_id ? (
                    <Link className={styles.taskLink} href={`/tasks/${item.execution_id}/${item.task_id}`}>
                      {item.task_title}
                    </Link>
                  ) : item.task_title}
                  <span className={styles.subtle}>
                    {item.kind} · {item.id.slice(0, 8)}
                    {item.redriven_from_run_id && ` · redriven from ${item.redriven_from_run_id.slice(0, 8)}`}
                  </span>
                </td>
                <td><span className="status-pill">{formatStatus(item.status)}</span></td>
                <td>{formatStatus(item.approval_status)}</td>
                <td>
                  {item.attempt_count} / {item.max_attempts}
                  <span className={styles.subtle}>{item.claimable ? "Eligible for worker claim" : item.organization_paused ? "Organization queue paused" : "Not claimable now"}</span>
                </td>
                <td>{formatDate(item.lease_expires_at)}</td>
                <td>{formatDate(item.started_at)}</td>
                <td>
                  {item.status === "dead_letter" && item.redriven_to_run_id ? (
                    <span className={styles.subtle}>Redriven as {item.redriven_to_run_id.slice(0, 8)}</span>
                  ) : item.status === "dead_letter" ? (
                    <button
                      className="action-button secondary"
                      disabled={loading || redrivingId !== null}
                      onClick={() => void redrive(item.id)}
                      type="button"
                    >{redrivingId === item.id ? "Requesting…" : "Redrive for approval"}</button>
                  ) : "—"}
                </td>
              </tr>
            )) : (
              <tr><td className={styles.emptyCell} colSpan={7}>
                {error ? "Queue data is unavailable." : "No verification runs match this filter."}
              </td></tr>
            )}
          </tbody>
        </table>
        <footer className={styles.pagination}>
          <span className={styles.paginationLabel}>{start}–{end} of {queue?.total ?? 0}</span>
          <div>
            <button
              className="action-button secondary"
              disabled={loading || offset === 0}
              onClick={() => changeOffset(Math.max(0, offset - PAGE_SIZE))}
              type="button"
            >Previous</button>{" "}
            <button
              className="action-button secondary"
              disabled={loading || !queue || offset + PAGE_SIZE >= queue.total}
              onClick={() => changeOffset(offset + PAGE_SIZE)}
              type="button"
            >Next</button>
          </div>
        </footer>
      </section>
    </div>
  );
}
