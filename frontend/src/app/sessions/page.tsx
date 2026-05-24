"use client";

import { useEffect, useState } from "react";
import { api, SessionInfo } from "@/lib/api";

const STATUS_CLASS: Record<string, string> = {
  active: "success",
  completed: "success",
  failed: "error",
};

export default function SessionsPage() {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api.listSessions()
      .then((response) => {
        setSessions(response.sessions);
        setLoaded(true);
      })
      .catch(() => setLoaded(true));
  }, []);

  return (
    <div className="route-page">
      <header className="page-intro">
        <div>
          <span className="eyebrow">Execution Archive</span>
          <h1 className="page-title">Sessions</h1>
        </div>
        <span className="status-pill">{loaded ? `${sessions.length} Records` : "Loading Ledger"}</span>
      </header>

      <section className="ledger-panel" aria-label="Execution sessions">
        <div className="ledger-header">
          <span>Identifier</span>
          <span>Assignment</span>
          <span>Status</span>
          <span>Created</span>
        </div>

        {!loaded ? (
          <div style={{ padding: 20 }}>
            {[1, 2, 3].map((item) => <div className="skeleton" key={item} style={{ marginBottom: 8 }} />)}
          </div>
        ) : sessions.length === 0 ? (
          <div className="empty-state">No session records have been logged.</div>
        ) : (
          sessions.map((session) => (
            <div className="ledger-row" key={session.id}>
              <span className="ledger-id mono">{session.id.slice(0, 8)}</span>
              <span className="ledger-title">{session.title}</span>
              <span className={`status-pill ${STATUS_CLASS[session.status] || ""}`}>
                <span className="dot" />
                {session.status}
              </span>
              <span className="ledger-date">
                {new Date(session.created_at).toLocaleString(undefined, {
                  month: "short",
                  day: "numeric",
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </span>
            </div>
          ))
        )}
      </section>
    </div>
  );
}
