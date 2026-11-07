"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, TaskActivityEvent, TaskDeliveryDiff, TaskDeliveryResult } from "@/lib/api";
import styles from "./page.module.css";

export default function TaskDeliveryReviewPage() {
  const params = useParams<{ executionId: string; taskId: string }>();
  const { executionId, taskId } = params;
  const [preview, setPreview] = useState<TaskDeliveryDiff | null>(null);
  const [result, setResult] = useState<TaskDeliveryResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [activity, setActivity] = useState<TaskActivityEvent[]>([]);
  const [activityLoading, setActivityLoading] = useState(true);
  const [activityError, setActivityError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [branchName, setBranchName] = useState(`solvyn/task-${taskId.slice(0, 8)}`);
  const [commitMessage, setCommitMessage] = useState("Implement delegated task changes");
  const [pullRequestTitle, setPullRequestTitle] = useState("Implement delegated task changes");
  const [pullRequestBody, setPullRequestBody] = useState("Created from a reviewed Solvyn task workspace.");

  const loadPreview = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setPreview(await api.getTaskDeliveryPreview(executionId, taskId));
      setResult(null);
    } catch (reason) {
      setPreview(null);
      setError(reason instanceof Error ? reason.message : "Could not load the task delivery preview.");
    } finally {
      setLoading(false);
    }
  }, [executionId, taskId]);

  useEffect(() => { void loadPreview(); }, [loadPreview]);

  const loadActivity = useCallback(async () => {
    setActivityLoading(true);
    setActivityError(null);
    try {
      const feed = await api.getTaskActivity(executionId, taskId);
      setActivity(feed.activity);
    } catch (reason) {
      setActivityError(reason instanceof Error ? reason.message : "Could not load task activity.");
    } finally {
      setActivityLoading(false);
    }
  }, [executionId, taskId]);

  useEffect(() => { void loadActivity(); }, [loadActivity]);

  async function submitDelivery(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!preview?.can_deliver) return;
    setSubmitting(true);
    setError(null);
    try {
      const delivery = await api.deliverTaskWorkspace(executionId, taskId, {
        expected_diff_digest: preview.diff_digest,
        branch_name: branchName,
        commit_message: commitMessage,
        pull_request_title: pullRequestTitle,
        pull_request_body: pullRequestBody,
      });
      setResult(delivery);
      setPreview(await api.getTaskDeliveryPreview(executionId, taskId));
      void loadActivity();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create the draft pull request.");
      try {
        setPreview(await api.getTaskDeliveryPreview(executionId, taskId));
        void loadActivity();
      } catch {
        // Keep the delivery error visible if history cannot be refreshed.
      }
    } finally {
      setSubmitting(false);
    }
  }

  const currentAttempt = preview?.delivery_history.find((delivery) => delivery.diff_digest === preview.diff_digest);

  function formatActivityTime(timestamp: string | null): string {
    if (!timestamp) return "Time unavailable";
    const date = new Date(timestamp);
    return Number.isNaN(date.getTime()) ? "Time unavailable" : date.toLocaleString();
  }

  return (
    <div className="route-page" aria-busy={loading || submitting}>
      <header className="page-intro">
        <div>
          <span className="eyebrow">Human review · GitHub delivery</span>
          <h1 className="page-title">Review task changes</h1>
          <p className={styles.intro}>Inspect the workspace diff before creating a draft pull request.</p>
        </div>
        <Link className="action-button secondary" href="/tasks">Back to tasks</Link>
      </header>

      <section className={`${styles.source} pearl-pane`} aria-label="Task source">
        <div>
          <span className="field-label">Execution</span>
          <code>{executionId}</code>
        </div>
        <div>
          <span className="field-label">Task</span>
          <code>{taskId}</code>
        </div>
        {preview && (
          <>
            <div>
              <span className="field-label">Pinned source</span>
              <code>{preview.source_commit_sha}</code>
            </div>
            <div>
              <span className="field-label">Pull request base</span>
              <code>{preview.base_branch}</code>
            </div>
          </>
        )}
      </section>

      {error && <section className={styles.notice} role="alert"><p>{error}</p></section>}

      <section className={`${styles.diffPanel} pearl-pane`} aria-labelledby="activity-heading" aria-busy={activityLoading}>
        <div className={styles.sectionHead}>
          <div>
            <span className="eyebrow">Task record</span>
            <h2 id="activity-heading">Activity</h2>
          </div>
          <button className="action-button secondary" onClick={() => void loadActivity()} type="button" disabled={activityLoading}>
            {activityLoading ? "Refreshing…" : "Refresh activity"}
          </button>
        </div>
        {activityError && <p className={styles.activityError} role="alert">{activityError}</p>}
        {activityLoading && activity.length === 0 ? (
          <div className="skeleton" style={{ minHeight: 120 }} />
        ) : activity.length ? (
          <ol className={styles.activityList}>
            {activity.map((event) => {
              const pullRequest = event.details.pull_request_status;
              return (
                <li className={styles.activityItem} key={event.id}>
                  <div className={styles.activityMeta}>
                    <span className={styles.activityType}>{event.type.replaceAll("_", " ")}</span>
                    <span className="status-pill">{event.status.replaceAll("_", " ")}</span>
                    <time dateTime={event.timestamp ?? undefined}>{formatActivityTime(event.timestamp)}</time>
                  </div>
                  <strong className={styles.activitySummary}>{event.summary}</strong>
                  {event.type === "tool" && event.details.path && <p className={styles.muted}>Path <code>{event.details.path}</code></p>}
                  {event.type === "verification" && event.details.exit_code !== undefined && (
                    <p className={styles.muted}>Exit code {event.details.exit_code}{event.details.timed_out ? " · timed out" : ""}</p>
                  )}
                  {event.type === "delivery" && event.details.branch && <p className={styles.muted}>Branch <code>{event.details.branch}</code></p>}
                  {event.type === "handoff" && event.details.reason && <p className={styles.muted}>{event.details.reason}</p>}
                  {event.type === "approval" && event.details.decision_note && <p className={styles.muted}>{event.details.decision_note}</p>}
                  {event.type === "orchestration_transition" && event.details.payload?.text && (
                    <p className={styles.muted}>{event.details.payload.text}</p>
                  )}
                  {event.type === "orchestration_transition" && event.details.payload?.previous_text && (
                    <p className={styles.muted}>Previously: {event.details.payload.previous_text}</p>
                  )}
                  {event.type === "orchestration_transition" && event.details.payload?.decision_note && (
                    <p className={styles.muted}>{event.details.payload.decision_note}</p>
                  )}
                  {event.type === "orchestration_transition" && event.details.payload?.previous_decision_note && (
                    <p className={styles.muted}>Previous decision note: {event.details.payload.previous_decision_note}</p>
                  )}
                  {event.type === "orchestration_transition" && event.details.payload?.depends_on_task_id && (
                    <p className={styles.muted}>Depends on task <code>{event.details.payload.depends_on_task_id}</code></p>
                  )}
                  {event.type === "orchestration_transition" && event.details.payload?.previous_depends_on_task_id && (
                    <p className={styles.muted}>Previously depended on task <code>{event.details.payload.previous_depends_on_task_id}</code></p>
                  )}
                  {event.type === "delivery" && pullRequest && (
                    <p className={styles.muted}>
                      {pullRequest.availability === "current" && pullRequest.review_status
                        ? `GitHub snapshot: ${pullRequest.merged ? "merged" : pullRequest.state}${pullRequest.draft ? " · draft" : ""} · review ${pullRequest.review_status.state} · ${pullRequest.review_status.approvals} approval(s) · ${pullRequest.review_status.changes_requested} change request(s)${pullRequest.review_status.history_truncated ? " · review history incomplete" : ""}`
                        : pullRequest.availability === "not_refreshed"
                          ? "Current GitHub status was not refreshed because this task has many pull requests."
                          : "Current GitHub status is unavailable; the saved delivery record is still shown."}
                    </p>
                  )}
                </li>
              );
            })}
          </ol>
        ) : activityError ? null : (
          <p className={styles.muted}>No activity has been recorded for this task yet.</p>
        )}
      </section>

      {loading ? (
        <div className="skeleton" style={{ minHeight: 220 }} />
      ) : preview ? (
        <>
          <section className={`${styles.diffPanel} pearl-pane`} aria-labelledby="diff-heading">
            <div className={styles.sectionHead}>
              <div>
                <span className="eyebrow">{preview.files.length + preview.deleted_paths.length} changed file{preview.files.length + preview.deleted_paths.length === 1 ? "" : "s"}</span>
                <h2 id="diff-heading">Workspace diff</h2>
              </div>
              <button className="action-button secondary" onClick={() => void loadPreview()} type="button">
                Refresh preview
              </button>
            </div>
            <p className={styles.digest}>Approval digest <code>{preview.diff_digest}</code></p>
            {preview.diffs.length ? (
              <div className={styles.fileList}>
                {preview.diffs.map((item) => (
                  <details className={styles.file} key={item.path} open>
                    <summary><code>{item.path}</code><span>{preview.deleted_paths.includes(item.path) ? "Delete" : "View diff"}</span></summary>
                    <pre>{item.diff}</pre>
                  </details>
                ))}
              </div>
            ) : (
              <p className={styles.muted}>No file contents have changed from the pinned source.</p>
            )}
            {preview.unsupported_missing_source_paths.length > 0 && (
              <div className={styles.blocker} role="status">
                <strong>Some missing selected files cannot be safely deleted.</strong>
                <p>Only selected regular, non-executable text files from the pinned source can be deleted.</p>
                <ul>{preview.unsupported_missing_source_paths.map((path) => <li key={path}><code>{path}</code></li>)}</ul>
              </div>
            )}
          </section>

          <section className={`${styles.diffPanel} pearl-pane`} aria-labelledby="history-heading">
            <div className={styles.sectionHead}>
              <div>
                <span className="eyebrow">Durable delivery record</span>
                <h2 id="history-heading">Delivery history</h2>
              </div>
              <span className="status-pill">{preview.delivery_history.length} attempt{preview.delivery_history.length === 1 ? "" : "s"}</span>
            </div>
            {preview.delivery_history.length ? (
              <div className={styles.fileList}>
                {preview.delivery_history.map((delivery) => (
                  <article className={styles.file} key={delivery.id}>
                    <div className={styles.sectionHead}>
                      <strong>{delivery.status === "pull_request_open" ? "pull request created (recorded)" : delivery.status.replaceAll("_", " ")}</strong>
                      <time dateTime={delivery.created_at ?? undefined}>{delivery.created_at ? new Date(delivery.created_at).toLocaleString() : "Time unavailable"}</time>
                    </div>
                    <p className={styles.digest}>Digest <code>{delivery.diff_digest}</code></p>
                    <p className={styles.muted}>Branch <code>{delivery.branch}</code>{delivery.commit_sha ? <> · commit <code>{delivery.commit_sha.slice(0, 12)}</code></> : null}</p>
                    {delivery.pull_request_title && <p>{delivery.pull_request_title}</p>}
                    {delivery.status === "pull_request_open" && <p className={styles.muted}>This records the state when created. Current GitHub status and reviews are not refreshed here.</p>}
                    {delivery.error && <p className={styles.blocker}>{delivery.error}</p>}
                    {delivery.pull_request_url && <a href={delivery.pull_request_url} rel="noreferrer" target="_blank">Open pull request #{delivery.pull_request_number}</a>}
                    {delivery.status === "unknown" && <p className={styles.muted}>Check GitHub for this branch and pull request before retrying.</p>}
                  </article>
                ))}
              </div>
            ) : (
              <p className={styles.muted}>No delivery attempts have been recorded for this task.</p>
            )}
          </section>

          <section className={`${styles.deliveryPanel} pearl-pane`} aria-labelledby="delivery-heading">
            <div className={styles.sectionHead}>
              <div>
                <span className="eyebrow">Owner/admin approval</span>
                <h2 id="delivery-heading">Create a draft pull request</h2>
              </div>
              <span className="status-pill">Draft only</span>
            </div>
            <p className={styles.muted}>
              Submitting confirms this exact diff. Solvyn will create a branch from the pinned commit and leave the pull request in draft for human review.
            </p>
            {currentAttempt && (
              <div className={styles.blocker} role="status">
                <strong>This exact diff already has a delivery record: {currentAttempt.status.replaceAll("_", " ")}.</strong>
                <p>Review the history above. Solvyn blocks a second delivery attempt until the recorded outcome is reconciled.</p>
              </div>
            )}
            <form className={styles.form} onSubmit={submitDelivery}>
              <label>
                <span className="field-label">Branch name</span>
                <input className="editorial-input" maxLength={240} onChange={(event) => setBranchName(event.target.value)} required value={branchName} />
              </label>
              <label>
                <span className="field-label">Commit message</span>
                <input className="editorial-input" maxLength={2000} onChange={(event) => setCommitMessage(event.target.value)} required value={commitMessage} />
              </label>
              <label>
                <span className="field-label">Pull request title</span>
                <input className="editorial-input" maxLength={256} onChange={(event) => setPullRequestTitle(event.target.value)} required value={pullRequestTitle} />
              </label>
              <label>
                <span className="field-label">Pull request description</span>
                <textarea className="editorial-input" maxLength={20000} onChange={(event) => setPullRequestBody(event.target.value)} rows={4} value={pullRequestBody} />
              </label>
              <button className="action-button" disabled={!preview.can_deliver || submitting || Boolean(result) || Boolean(currentAttempt)} type="submit">
                {submitting ? "Creating draft PR…" : "Approve diff and create draft PR"}
              </button>
            </form>
            {!preview.can_deliver && preview.unsupported_missing_source_paths.length === 0 && (
              <p className={styles.muted}>Make at least one supported workspace change to enable delivery.</p>
            )}
          </section>
        </>
      ) : (
        <section className={`${styles.blocker} pearl-pane`} role="status">
          <strong>A repository delivery preview is unavailable.</strong>
          <p>Check that this task has an approved project handoff and a repository-backed workspace, and that your organization account has owner/admin access.</p>
          <button className="action-button secondary" onClick={() => void loadPreview()} type="button">Try again</button>
        </section>
      )}

      {result && (
        <section className={styles.success} role="status">
          <div>
            <span className="eyebrow">{result.replayed ? "Previously recorded delivery" : "Draft pull request created"}</span>
            <h2>{result.pull_request.title || pullRequestTitle}</h2>
            <p><code>{result.branch}</code> · commit <code>{result.commit_sha.slice(0, 12)}</code></p>
            {result.replayed && <p className={styles.muted}>This is the saved creation result; the pull request may have changed since it was recorded.</p>}
          </div>
          <a className="action-button" href={result.pull_request.url} rel="noreferrer" target="_blank">Review on GitHub</a>
        </section>
      )}
    </div>
  );
}
