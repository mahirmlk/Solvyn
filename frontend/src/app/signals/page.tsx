"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  OrganizationInfo,
  ProactiveEventInfo,
} from "@/lib/api";
import styles from "./page.module.css";

function formatDate(value: string | null): string {
  if (!value) return "Date unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function signalLabel(event: ProactiveEventInfo): string {
  if (event.event_type === "workflow_run") return "CI failure";
  if (event.event_type === "dependabot_alert") return "Dependency alert";
  if (event.event_type === "code_scanning_alert") return "Code scanning";
  if (event.event_type === "secret_scanning_alert") return "Secret scanning";
  if (event.event_type === "scheduled_maintenance") return "Scheduled maintenance";
  if (event.event_type === "issues") return "GitHub issue";
  if (event.event_type === "pull_request") return "Pull request";
  if (event.event_type === "task_assignment") return "Teammate assignment";
  return event.event_type.replaceAll("_", " ");
}

export default function SignalsPage() {
  const [organizations, setOrganizations] = useState<OrganizationInfo[]>([]);
  const [organizationId, setOrganizationId] = useState("");
  const [events, setEvents] = useState<ProactiveEventInfo[]>([]);
  const [canManage, setCanManage] = useState(false);
  const [loadingOrganizations, setLoadingOrganizations] = useState(true);
  const [loadingEvents, setLoadingEvents] = useState(false);
  const [acknowledgingId, setAcknowledgingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const latestRequest = useRef(0);

  useEffect(() => {
    let active = true;
    api.listOrganizations()
      .then(({ organizations: nextOrganizations }) => {
        if (!active) return;
        const activeOrganizations = nextOrganizations.filter((organization) => organization.is_active);
        setOrganizations(activeOrganizations);
        setOrganizationId((current) => current || activeOrganizations[0]?.id || "");
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "Could not load organizations.");
      })
      .finally(() => {
        if (active) setLoadingOrganizations(false);
      });
    return () => { active = false; };
  }, []);

  const refresh = useCallback(async () => {
    if (!organizationId) {
      setEvents([]);
      setCanManage(false);
      return;
    }
    const requestId = ++latestRequest.current;
    setLoadingEvents(true);
    setError(null);
    setEvents([]);
    try {
      const result = await api.listProactiveEvents(organizationId);
      if (requestId === latestRequest.current) {
        setEvents(result.events);
        setCanManage(result.can_manage);
      }
    } catch (reason) {
      if (requestId === latestRequest.current) {
        setError(reason instanceof Error ? reason.message : "Could not load project signals.");
        setEvents([]);
      }
    } finally {
      if (requestId === latestRequest.current) setLoadingEvents(false);
    }
  }, [organizationId]);

  useEffect(() => { void refresh(); }, [refresh]);

  async function acknowledge(event: ProactiveEventInfo) {
    setAcknowledgingId(event.id);
    setNotice(null);
    try {
      const updated = await api.acknowledgeProactiveEvent(organizationId, event.id);
      setEvents((current) => current.map((item) => item.id === updated.id ? updated : item));
      setNotice("Signal acknowledged.");
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "Could not acknowledge this signal.");
    } finally {
      setAcknowledgingId(null);
    }
  }

  const selectedOrganization = organizations.find((item) => item.id === organizationId);

  return (
    <div className="route-page">
      <header className="page-intro">
        <div>
          <span className="eyebrow">Proactive engineering</span>
          <h1 className="page-title">Signals</h1>
        </div>
        <button className="action-button secondary" disabled={loadingEvents || !organizationId} onClick={() => void refresh()} type="button">
          {loadingEvents ? "Refreshing…" : "Refresh signals"}
        </button>
      </header>

      <section className={styles.intro} aria-label="Signal inbox controls">
        <label className={styles.organizationPicker}>
          <span className="field-label">Organization</span>
          <select
            className="editorial-input"
            disabled={loadingOrganizations || organizations.length === 0}
            onChange={(event) => { setOrganizationId(event.target.value); setNotice(null); }}
            value={organizationId}
          >
            {organizations.map((organization) => (
              <option key={organization.id} value={organization.id}>{organization.name}</option>
            ))}
          </select>
        </label>
        <p>CI failures, security alerts, GitHub issues and pull requests, teammate assignments, and scheduled maintenance for this organization. Signals are for human review; assignments do not start work automatically.</p>
      </section>

      {error && <p className={styles.message} role="alert">{error}</p>}
      {notice && <p className={styles.message} role="status">{notice}</p>}

      <section className={styles.feed} aria-label="Proactive signals">
        <div className={styles.feedHeading}>
          <div>
            <span className="eyebrow">Triage inbox</span>
            <h2>{selectedOrganization?.name || "Organization signals"}</h2>
          </div>
          <span className={styles.count}>{loadingEvents ? "Loading…" : `${events.length} recent`}</span>
        </div>

        {!loadingOrganizations && !error && organizations.length === 0 && (
          <div className={styles.empty}>No organizations are connected to this account yet.</div>
        )}
        {!loadingOrganizations && organizationId && !loadingEvents && events.length === 0 && !error && (
          <div className={styles.empty}>No proactive signals recorded for this organization.</div>
        )}

        <div className={styles.eventList}>
          {events.map((event) => (
            <article className={`${styles.event} pearl-pane`} key={event.id}>
              <div className={styles.eventTopline}>
                <span className={styles.kind}>{signalLabel(event)}</span>
                <span className={`${styles.status} ${event.status === "acknowledged" ? styles.acknowledged : ""}`}>
                  {event.status.replaceAll("_", " ")}
                </span>
              </div>
              <h3>{event.summary || event.workflow_name}</h3>
              <p className={styles.meta}>
                <span>{event.repository_name || "Scheduled task"}</span>
                {event.branch && <span>Branch: {event.branch}</span>}
                {event.conclusion && <span>Result: {event.conclusion}</span>}
                <time dateTime={event.created_at || undefined}>{formatDate(event.created_at)}</time>
              </p>
              <div className={styles.eventActions}>
                {event.details_url && (
                  <a href={event.details_url} rel="noreferrer" target="_blank">Open source details</a>
                )}
                {canManage && event.status !== "acknowledged" && (
                  <button
                    className="action-button secondary"
                    disabled={acknowledgingId !== null}
                    onClick={() => void acknowledge(event)}
                    type="button"
                  >
                    {acknowledgingId === event.id ? "Saving…" : "Acknowledge"}
                  </button>
                )}
              </div>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
