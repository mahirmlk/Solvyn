"use client";

import { useEffect, useState } from "react";
import { api, AuditEntry, TraceInfo } from "@/lib/api";

const METRICS = [
  { label: "HTTP Requests", metric: "solvyn_http_requests_total", number: "01" },
  { label: "LLM Calls", metric: "solvyn_llm_calls_total", number: "02" },
  { label: "Agent Runs", metric: "solvyn_agent_executions_total", number: "03" },
  { label: "Workflow Runs", metric: "solvyn_workflow_runs_total", number: "04" },
];

export default function AnalyticsPage() {
  const [traces, setTraces] = useState<TraceInfo[]>([]);
  const [auditLogs, setAuditLogs] = useState<AuditEntry[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    Promise.all([
      api.listTraces().then((response) => response.traces).catch(() => []),
      api.getAuditLogs({ limit: 10 }).then((response) => response.entries).catch(() => []),
    ]).then(([recentTraces, entries]) => {
      setTraces(recentTraces as TraceInfo[]);
      setAuditLogs(entries as AuditEntry[]);
      setLoaded(true);
    });
  }, []);

  return (
    <div className="route-page">
      <header className="page-intro">
        <div>
          <span className="eyebrow">Observability</span>
          <h1 className="page-title">Analytics</h1>
        </div>
        <span className="status-pill">
          <span className="dot busy" />
          Signal Monitor
        </span>
      </header>

      <section className="metric-strip pearl-pane" aria-label="Metric references">
        {METRICS.map((metric) => (
          <article className="metric-module" key={metric.metric}>
            <span className="metric-number">{metric.number}</span>
            <h2 className="metric-title">{metric.label}</h2>
            <code className="metric-code">{metric.metric}</code>
          </article>
        ))}
      </section>

      <section className="analytics-split">
        <article className="feed-panel midnight-pane">
          <div className="feed-head">
            <h2 className="feed-title">Recent Traces</h2>
            <span className="feed-count">{String(traces.length).padStart(2, "0")}</span>
          </div>

          {!loaded ? (
            [1, 2, 3].map((item) => <div className="dark-skeleton" key={item} style={{ height: 62, marginBottom: 8 }} />)
          ) : traces.length === 0 ? (
            <div className="empty-state dark-empty">No traces recorded yet.</div>
          ) : (
            traces.slice(-5).reverse().map((trace) => (
              <div className="trace-item" key={trace.id}>
                <h3 className="trace-name">
                  {trace.name}
                  <span className="inline-tag">{trace.agent_type}</span>
                </h3>
                <div className="trace-spans">
                  {trace.spans.map((span, index) => (
                    <span className="trace-span" key={`${span.name}-${index}`}>
                      {span.name} / {span.duration_ms.toFixed(0)}ms
                    </span>
                  ))}
                </div>
              </div>
            ))
          )}
        </article>

        <article className="feed-panel pearl-pane">
          <div className="feed-head">
            <h2 className="feed-title">Audit Log</h2>
            <span className="feed-count">{String(auditLogs.length).padStart(2, "0")}</span>
          </div>

          {!loaded ? (
            [1, 2, 3].map((item) => <div className="skeleton" key={item} style={{ height: 48, marginBottom: 8 }} />)
          ) : auditLogs.length === 0 ? (
            <div className="empty-state">No audit entries found.</div>
          ) : (
            auditLogs.map((entry, index) => (
              <div className="audit-item" key={`${entry.timestamp}-${index}`}>
                <span className="audit-time">{entry.timestamp?.slice(11, 19)}</span>
                <span className="audit-action">{entry.action}</span>
                <span className="audit-details">{entry.agent_type} / {JSON.stringify(entry.details)}</span>
              </div>
            ))
          )}
        </article>
      </section>

      <div className="endpoint-bar pearl-pane">
        <div>
          <span className="eyebrow">Prometheus Surface</span>
          <p className="action-copy" style={{ marginTop: 8 }}>Scrape endpoint for external monitoring ingestion.</p>
        </div>
        <code className="endpoint-code">/metrics</code>
      </div>
    </div>
  );
}
