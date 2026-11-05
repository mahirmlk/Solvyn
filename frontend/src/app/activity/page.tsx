"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, AuditEntry } from "@/lib/api";
import styles from "@/components/WorkspacePages.module.css";

export default function ActivityPage() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [action, setAction] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getAuditLogs({ limit: 100 })
      .then((response) => setEntries(response.entries))
      .catch((reason) => setError(reason instanceof Error ? reason.message : "Could not load engineering activity."))
      .finally(() => setLoading(false));
  }, []);

  const actions = useMemo(() => [...new Set(entries.map((entry) => entry.action))].sort(), [entries]);
  const visibleEntries = action ? entries.filter((entry) => entry.action === action) : entries;

  return (
    <div className="route-page">
      <header className="page-intro">
        <div>
          <span className="eyebrow">Engineering history</span>
          <h1 className="page-title">Activity</h1>
        </div>
        <label className={styles.activityFilter}>
          <span className="field-label">Action</span>
          <select className="editorial-input" onChange={(event) => setAction(event.target.value)} value={action}>
            <option value="">All actions</option>
            {actions.map((item) => <option key={item} value={item}>{item.replaceAll("_", " ")}</option>)}
          </select>
        </label>
      </header>

      {error && (
        <section className={styles.notice} role="alert">
          <p>{error}</p>
          {error.includes("401") && <Link className="action-button secondary" href="/login">Sign in</Link>}
        </section>
      )}

      <section className={styles.groups} aria-label="Engineering activity entries">
        {loading ? (
          <div className="skeleton" style={{ minHeight: 180 }} />
        ) : error ? null : visibleEntries.length === 0 ? (
          <div className="empty-state">No audit activity has been recorded for this tenant yet.</div>
        ) : visibleEntries.map((entry, index) => (
          <article className={`${styles.activityCard} pearl-pane`} key={entry.id || `${entry.action}-${entry.timestamp}-${index}`}>
            <div className={styles.activityHeading}>
              <div>
                <span className="eyebrow">{entry.agent_type || "Platform"}</span>
                <h2 className={styles.sectionTitle}>{entry.action.replaceAll("_", " ")}</h2>
              </div>
              <time className={styles.activityTime} dateTime={entry.timestamp || entry.created_at || undefined}>
                {entry.timestamp || entry.created_at ? new Date(entry.timestamp || entry.created_at || "").toLocaleString() : "Time unavailable"}
              </time>
            </div>
            {Object.keys(entry.details || {}).length > 0 && (
              <pre className={styles.activityDetails}>{JSON.stringify(entry.details, null, 2)}</pre>
            )}
          </article>
        ))}
      </section>
      {!loading && visibleEntries.length === 100 && <p className={styles.activityLimit}>Showing the 100 most recent audit entries.</p>}
    </div>
  );
}
