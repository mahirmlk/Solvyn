"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, AgentInfo, HealthStatus } from "@/lib/api";
import { AgentCard } from "@/components/AgentCard";

const quickActions = [
  {
    href: "/workflows",
    number: "01",
    title: "Run Workflow",
    description: "Launch a feature, bugfix, or release pipeline.",
  },
  {
    href: "/sessions",
    number: "02",
    title: "Review Sessions",
    description: "Inspect recent autonomous execution histories.",
  },
  {
    href: "/analytics",
    number: "03",
    title: "Open Telemetry",
    description: "Read traces, audit activity, and metrics.",
  },
];

export default function Dashboard() {
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    Promise.all([
      api.health().catch(() => ({ status: "error", service: "solvyn" } as HealthStatus)),
      api.listAgents().then((response) => response.agents).catch(() => [] as AgentInfo[]),
    ]).then(([status, registeredAgents]) => {
      setHealth(status as HealthStatus);
      setAgents(registeredAgents as AgentInfo[]);
      setLoaded(true);
    });
  }, []);

  const healthy = health?.status === "ok";

  return (
    <div className="route-page">
      <section className="dashboard-hero" aria-label="Solvyn system overview">
        <div className="hero-pearl">
          <div>
            <p className="eyebrow">AI Engineering Command Center</p>
            <p className="hero-tagline">
              Autonomous agents coordinate planning, implementation, validation, and delivery from one operational surface.
            </p>
          </div>

          <div className="hero-actions">
            <Link className="action-button" href="/workflows">
              Execute
            </Link>
            <Link className="action-button secondary" href="/sessions">
              History
            </Link>
          </div>
        </div>

        <div className="hero-midnight">
          <div className={`status-pill light ${health && !healthy ? "error" : ""}`}>
            <span className={`dot ${healthy ? "busy" : ""}`} />
            {health ? (healthy ? "System Online" : "System Offline") : "Checking System"}
          </div>

          <div className="readout-grid" aria-label="Platform readouts">
            <div className="readout">
              <span className="readout-label">Registered Agents</span>
              <span className="readout-value">{loaded ? String(agents.length).padStart(2, "0") : "--"}</span>
            </div>
            <div className="readout">
              <span className="readout-label">Pipelines Ready</span>
              <span className="readout-value">03</span>
            </div>
            <div className="readout">
              <span className="readout-label">Control State</span>
              <span className="readout-value">{healthy ? "Live" : "Standby"}</span>
            </div>
          </div>
        </div>

        <h1 className="hero-wordmark">SOLVYN</h1>
      </section>

      <div className="dashboard-lower">
        <section aria-labelledby="agents-heading">
          <div className="section-head">
            <h2 className="section-title" id="agents-heading">Agents</h2>
            <span className="eyebrow">{loaded ? `${agents.length} Registered` : "Loading Registry"}</span>
          </div>

          {!loaded ? (
            <div className="agents-grid">
              {[1, 2, 3].map((item) => <div className="skeleton" key={item} style={{ height: 190 }} />)}
            </div>
          ) : agents.length === 0 ? (
            <div className="panel empty-state">No agents registered yet.</div>
          ) : (
            <div className="agents-grid">
              {agents.map((agent) => (
                <AgentCard
                  key={agent.type}
                  name={agent.name || agent.type}
                  description={agent.description}
                  status={(agent.status || "idle") as "idle" | "running" | "error" | "success"}
                  capabilities={agent.capabilities || []}
                />
              ))}
            </div>
          )}
        </section>

        <section className="action-grid" aria-label="Primary actions">
          {quickActions.map((action) => (
            <Link className="action-tile" href={action.href} key={action.href}>
              <span className="eyebrow">{action.number}</span>
              <h3 className="action-title">{action.title}</h3>
              <p className="action-copy">{action.description}</p>
            </Link>
          ))}
        </section>
      </div>
    </div>
  );
}
