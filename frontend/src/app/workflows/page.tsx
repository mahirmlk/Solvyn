"use client";

import { useEffect, useState } from "react";
import { api, WorkflowDetail, WorkflowInfo, WorkflowResult } from "@/lib/api";
import { ExecutionGraph } from "@/components/ExecutionGraph";

export default function WorkflowsPage() {
  const [workflows, setWorkflows] = useState<WorkflowInfo[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<WorkflowDetail | null>(null);
  const [result, setResult] = useState<WorkflowResult | null>(null);
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [running, setRunning] = useState(false);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api.listWorkflows()
      .then((response) => {
        setWorkflows(response.workflows);
        setLoaded(true);
      })
      .catch(() => setLoaded(true));
  }, []);

  async function selectWorkflow(name: string) {
    setSelected(name);
    setResult(null);
    const workflow = await api.getWorkflow(name);
    setDetail(workflow);
    setInputs(workflow.default_inputs);
  }

  async function runWorkflow() {
    if (!selected) return;
    setRunning(true);
    try {
      const execution = await api.runWorkflow(selected, inputs);
      setResult(execution);
    } catch (error) {
      console.error(error);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="route-page">
      <header className="page-intro">
        <div>
          <span className="eyebrow">Pipeline Control</span>
          <h1 className="page-title">Workflows</h1>
        </div>
        <span className="status-pill">
          <span className="dot busy" />
          Ready to Execute
        </span>
      </header>

      <div className="page-split workflow-workbench">
        <aside className="pearl-pane workflow-index">
          <span className="eyebrow">Available Programs</span>
          <div className="workflow-list">
            {!loaded ? (
              [1, 2, 3].map((item) => <div className="skeleton" key={item} style={{ height: 92 }} />)
            ) : workflows.length === 0 ? (
              <div className="empty-state">No workflows found.</div>
            ) : (
              workflows.map((workflow) => (
                <button
                  className={`workflow-choice${selected === workflow.name ? " active" : ""}`}
                  key={workflow.name}
                  onClick={() => selectWorkflow(workflow.name)}
                  type="button"
                >
                  <span className="workflow-choice-name">{workflow.name}</span>
                  <span className="workflow-choice-copy">{workflow.description}</span>
                  <span className="workflow-choice-meta">{workflow.steps} Steps</span>
                </button>
              ))
            )}
          </div>
        </aside>

        <section className="midnight-pane workflow-detail">
          {!detail ? (
            <div className="workflow-placeholder">
              <span className="eyebrow copy-light-muted">Select Program</span>
              <h2 className="workflow-placeholder-title">Coordinate an autonomous delivery run.</h2>
              <p className="workflow-placeholder-copy">
                Choose a workflow from the collection to inspect its participating agents, edit inputs, and execute the sequence.
              </p>
            </div>
          ) : (
            <>
              <div className="detail-header">
                <div>
                  <span className="eyebrow copy-light-muted">Selected Program</span>
                  <h2 className="detail-title">{selected}</h2>
                </div>
                <span className="status-pill light">{detail.steps.length} Steps</span>
              </div>

              <p className="workflow-description">{detail.description}</p>

              <div className="workflow-steps" aria-label="Workflow steps">
                {detail.steps.map((step, index) => (
                  <span className="workflow-step-tag" key={`${step.agent_type}-${index}`}>
                    {String(index + 1).padStart(2, "0")} {step.agent_type}
                  </span>
                ))}
              </div>

              {Object.entries(detail.default_inputs).map(([key, value]) => (
                <label className="field" key={key}>
                  <span className="field-label">{key}</span>
                  <textarea
                    className="editorial-input"
                    onChange={(event) => setInputs({ ...inputs, [key]: event.target.value })}
                    placeholder={value}
                    rows={2}
                    value={inputs[key] || ""}
                  />
                </label>
              ))}

              <button className="action-button run-button" disabled={running} onClick={runWorkflow} type="button">
                {running ? (
                  <>
                    <span className="spinner" />
                    Executing
                  </>
                ) : (
                  "Run Workflow"
                )}
              </button>

              {result && (
                <section className="result-panel">
                  <div className="result-head">
                    <h3 className="feed-title">Execution Result</h3>
                    <span className={`status-pill light ${result.status === "completed" ? "success" : "error"}`}>
                      {result.status}
                    </span>
                  </div>
                  <ExecutionGraph
                    nodes={result.nodes.map((node, index) => ({
                      id: `node-${index}`,
                      label: node.agent_type,
                      agent: node.agent_type,
                      status: (node.status === "completed" ? "success" : node.status) as "pending" | "running" | "success" | "error",
                    }))}
                    edges={[]}
                  />
                </section>
              )}
            </>
          )}
        </section>
      </div>
    </div>
  );
}
