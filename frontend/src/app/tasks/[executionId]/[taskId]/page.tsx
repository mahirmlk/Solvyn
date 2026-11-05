"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, TaskDeliveryDiff, TaskDeliveryResult } from "@/lib/api";
import styles from "./page.module.css";

export default function TaskDeliveryReviewPage() {
  const params = useParams<{ executionId: string; taskId: string }>();
  const { executionId, taskId } = params;
  const [preview, setPreview] = useState<TaskDeliveryDiff | null>(null);
  const [result, setResult] = useState<TaskDeliveryResult | null>(null);
  const [loading, setLoading] = useState(true);
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

  async function submitDelivery(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!preview?.can_deliver) return;
    setSubmitting(true);
    setError(null);
    try {
      setResult(await api.deliverTaskWorkspace(executionId, taskId, {
        expected_diff_digest: preview.diff_digest,
        branch_name: branchName,
        commit_message: commitMessage,
        pull_request_title: pullRequestTitle,
        pull_request_body: pullRequestBody,
      }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create the draft pull request.");
    } finally {
      setSubmitting(false);
    }
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

      {loading ? (
        <div className="skeleton" style={{ minHeight: 220 }} />
      ) : preview ? (
        <>
          <section className={`${styles.diffPanel} pearl-pane`} aria-labelledby="diff-heading">
            <div className={styles.sectionHead}>
              <div>
                <span className="eyebrow">{preview.files.length} changed file{preview.files.length === 1 ? "" : "s"}</span>
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
                    <summary><code>{item.path}</code><span>View diff</span></summary>
                    <pre>{item.diff}</pre>
                  </details>
                ))}
              </div>
            ) : (
              <p className={styles.muted}>No file contents have changed from the pinned source.</p>
            )}
            {preview.unsupported_missing_source_paths.length > 0 && (
              <div className={styles.blocker} role="status">
                <strong>Delivery is blocked while selected files are missing.</strong>
                <p>Deletion delivery is not supported. Restore these files in the workspace to continue.</p>
                <ul>{preview.unsupported_missing_source_paths.map((path) => <li key={path}><code>{path}</code></li>)}</ul>
              </div>
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
              <button className="action-button" disabled={!preview.can_deliver || submitting || Boolean(result)} type="submit">
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
            <span className="eyebrow">Draft pull request created</span>
            <h2>{result.pull_request.title || pullRequestTitle}</h2>
            <p><code>{result.branch}</code> · commit <code>{result.commit_sha.slice(0, 12)}</code></p>
          </div>
          <a className="action-button" href={result.pull_request.url} rel="noreferrer" target="_blank">Review on GitHub</a>
        </section>
      )}
    </div>
  );
}
