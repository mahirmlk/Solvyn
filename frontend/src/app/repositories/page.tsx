"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { api, GitHubIntegrationInfo, ProjectInfo, RepositoryInfo, RepositoryInspection } from "@/lib/api";
import styles from "./page.module.css";

export default function RepositoriesPage() {
  const [organizations, setOrganizations] = useState<{ id: string; name: string }[]>([]);
  const [organizationId, setOrganizationId] = useState("");
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [projectId, setProjectId] = useState("");
  const [integration, setIntegration] = useState<GitHubIntegrationInfo | null>(null);
  const [canManage, setCanManage] = useState(false);
  const [repositories, setRepositories] = useState<RepositoryInfo[]>([]);
  const [selectedRepositoryId, setSelectedRepositoryId] = useState("");
  const [inspection, setInspection] = useState<RepositoryInspection | null>(null);
  const [token, setToken] = useState("");
  const [repositoryName, setRepositoryName] = useState("");
  const [loadingOrganizations, setLoadingOrganizations] = useState(true);
  const [loadingOrganization, setLoadingOrganization] = useState(false);
  const [loadingRepositories, setLoadingRepositories] = useState(false);
  const [saving, setSaving] = useState(false);
  const [inspecting, setInspecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api.listOrganizations()
      .then(({ organizations: rows }) => {
        if (!active) return;
        setOrganizations(rows);
        setOrganizationId(rows[0]?.id || "");
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "Could not load organizations.");
      })
      .finally(() => {
        if (active) setLoadingOrganizations(false);
      });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!organizationId) {
      setProjects([]);
      setProjectId("");
      setIntegration(null);
      setCanManage(false);
      setToken("");
      setRepositoryName("");
      setNotice(null);
      return;
    }
    let active = true;
    setLoadingOrganization(true);
    setError(null);
    setProjects([]);
    setProjectId("");
    setIntegration(null);
    setCanManage(false);
    setToken("");
    setRepositoryName("");
    setNotice(null);
    setRepositories([]);
    setSelectedRepositoryId("");
    setInspection(null);
    Promise.all([api.listProjects(organizationId), api.getGitHubIntegration(organizationId)])
      .then(async ([projectResult, status]) => {
        if (!active) return;
        setProjects(projectResult.projects);
        setProjectId((current) => projectResult.projects.some((project) => project.id === current)
          ? current
          : projectResult.projects[0]?.id || "");
        setIntegration(status);
        try {
          // This existing read endpoint returns the server's OWNER_ADMIN capability.
          const permission = await api.listMaintenanceSchedules(organizationId);
          if (active) setCanManage(permission.can_manage);
        } catch {
          if (active) {
            setCanManage(false);
            setError("Could not confirm organization management access. Repository changes are disabled.");
          }
        }
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "Could not load organization repositories.");
      })
      .finally(() => {
        if (active) setLoadingOrganization(false);
      });
    return () => { active = false; };
  }, [organizationId]);

  useEffect(() => {
    if (!projectId) {
      setRepositories([]);
      setSelectedRepositoryId("");
      setInspection(null);
      return;
    }
    let active = true;
    setLoadingRepositories(true);
    setError(null);
    api.listProjectRepositories(projectId)
      .then(({ repositories: rows }) => {
        if (!active) return;
        setRepositories(rows);
        setSelectedRepositoryId((current) => rows.some((repository) => repository.id === current) ? current : "");
        setInspection(null);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "Could not load connected repositories.");
      })
      .finally(() => {
        if (active) setLoadingRepositories(false);
      });
    return () => { active = false; };
  }, [projectId]);

  async function connectGitHub(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!organizationId || !canManage || !token.trim()) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.connectGitHubIntegration(organizationId, token.trim());
      setIntegration(result);
      setToken("");
      setNotice("Connected GitHub as " + (result.account_login || "the configured account") + ".");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not connect the GitHub account.");
    } finally {
      setSaving(false);
    }
  }

  async function connectRepository(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectId || !canManage || !repositoryName.trim()) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const repository = await api.connectProjectRepository(projectId, repositoryName.trim());
      const { repositories: rows } = await api.listProjectRepositories(projectId);
      setRepositories(rows);
      setSelectedRepositoryId(repository.id);
      setRepositoryName("");
      setInspection(null);
      setNotice("Connected " + repository.full_name + ".");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not connect the repository.");
    } finally {
      setSaving(false);
    }
  }

  async function inspectRepository(repositoryId: string) {
    setSelectedRepositoryId(repositoryId);
    setInspection(null);
    setInspecting(true);
    setError(null);
    try {
      setInspection(await api.inspectRepository(repositoryId));
    } catch (reason) {
      setInspection(null);
      setError(reason instanceof Error ? reason.message : "Could not inspect the repository.");
    } finally {
      setInspecting(false);
    }
  }

  const currentProject = projects.find((project) => project.id === projectId);

  return (
    <div className="route-page">
      <header className="page-intro">
        <div>
          <span className="eyebrow">Engineering</span>
          <h1 className="page-title">Repositories</h1>
        </div>
        <span className={"status-pill" + (integration?.connected ? " success" : "")}>
          {integration?.connected ? "GitHub connected" : "GitHub not connected"}
        </span>
      </header>

      {error && <div className={styles.error} role="alert">{error}</div>}
      {notice && <div className={styles.notice} role="status">{notice}</div>}

      {loadingOrganizations ? (
        <div className="skeleton" style={{ minHeight: 160 }} />
      ) : organizations.length === 0 ? (
        <section className={styles.panel}>
          <h2>No organization is available</h2>
          <p>Sign in to an organization before connecting a repository.</p>
          <Link className="action-button secondary" href="/login">Sign in</Link>
        </section>
      ) : (
        <>
          <div className={styles.scopeBar}>
            <label className="field">
              <span className="field-label">Organization</span>
              <select className="editorial-input" value={organizationId} onChange={(event) => setOrganizationId(event.target.value)} disabled={loadingOrganization || saving || inspecting}>
                {organizations.map((organization) => (
                  <option key={organization.id} value={organization.id}>{organization.name}</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="field-label">Project</span>
              <select className="editorial-input" value={projectId} onChange={(event) => setProjectId(event.target.value)} disabled={!projects.length || loadingOrganization || saving || inspecting}>
                {projects.length
                  ? projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)
                  : <option value="">No projects yet</option>}
              </select>
            </label>
          </div>

          {loadingOrganization ? (
            <div className="skeleton" style={{ minHeight: 140 }} />
          ) : (
            <section className={styles.panel} aria-labelledby="github-title">
              <div className={styles.panelHead}>
                <div>
                  <span className="eyebrow">Organization integration</span>
                  <h2 id="github-title">GitHub access</h2>
                </div>
                {integration?.connected && (
                  <span className="status-pill success">Connected as {integration.account_login || "GitHub user"}</span>
                )}
              </div>
              <p className={styles.muted}>
                Use an organization-owned GitHub token with access to the repositories you want Solvyn to inspect.
                The token is sent directly to Solvyn and stored encrypted; it is never shown again.
              </p>
              {canManage ? (
                <form className={styles.tokenForm} onSubmit={connectGitHub}>
                  <label className="field">
                    <span className="field-label">{integration?.connected ? "Replace GitHub token" : "GitHub token"}</span>
                    <input
                      autoComplete="new-password"
                      className="editorial-input mono"
                      onChange={(event) => setToken(event.target.value)}
                      placeholder="Paste a GitHub token"
                      required
                      type="password"
                      value={token}
                    />
                  </label>
                  <button className="action-button" disabled={saving || !token.trim()} type="submit">
                    {saving ? "Verifying…" : integration?.connected ? "Replace token" : "Connect GitHub"}
                  </button>
                </form>
              ) : (
                <p className={styles.helper}>Only organization owners and admins can connect or replace GitHub credentials.</p>
              )}
            </section>
          )}

          {!currentProject ? (
            <section className={styles.panel}>
              <h2>Create a project first</h2>
              <p>Repositories are connected to a project so teammate access can follow the project boundary.</p>
              <Link className="action-button secondary" href="/projects">Open Projects</Link>
            </section>
          ) : (
            <section className={styles.panel} aria-labelledby="project-repositories-title">
              <div className={styles.panelHead}>
                <div>
                  <span className="eyebrow">{currentProject.name}</span>
                  <h2 id="project-repositories-title">Connected repositories</h2>
                </div>
                <span className="status-pill">{repositories.length} Repositories</span>
              </div>
              {canManage ? (
                <form className={styles.repositoryForm} onSubmit={connectRepository}>
                  <label className="field">
                    <span className="field-label">GitHub repository</span>
                    <input
                      className="editorial-input mono"
                      onChange={(event) => setRepositoryName(event.target.value)}
                      placeholder="owner/repository"
                      required
                      value={repositoryName}
                    />
                  </label>
                  <button className="action-button" disabled={saving || !repositoryName.trim()} type="submit">
                    {saving ? "Connecting…" : "Connect repository"}
                  </button>
                </form>
              ) : (
                <p className={styles.helper}>Only organization owners and admins can connect repositories to projects.</p>
              )}
              {!integration?.connected && (
                <p className={styles.helper}>Public repositories can be connected without a token. Private repositories require the organization GitHub token above.</p>
              )}

              {loadingRepositories ? (
                <div className="skeleton" style={{ minHeight: 90 }} />
              ) : repositories.length === 0 ? (
                <div className={styles.empty}>No repositories are connected to this project yet.</div>
              ) : (
                <ul className={styles.repositoryList}>
                  {repositories.map((repository) => (
                    <li className={styles.repositoryItem} key={repository.id}>
                      <div>
                        <a href={repository.url} rel="noreferrer" target="_blank">{repository.full_name}</a>
                        <span className={styles.muted}>Default branch: <code>{repository.default_branch}</code></span>
                      </div>
                      <button
                        className="action-button secondary"
                        disabled={inspecting}
                        onClick={() => void inspectRepository(repository.id)}
                        type="button"
                      >
                        {inspecting && selectedRepositoryId === repository.id ? "Inspecting…" : "Inspect"}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          )}

          {inspection && (
            <section className={styles.panel} aria-labelledby="inspection-title">
              <div className={styles.panelHead}>
                <div>
                  <span className="eyebrow">Repository inspection snapshot</span>
                  <h2 id="inspection-title">{repositories.find((repository) => repository.id === selectedRepositoryId)?.full_name}</h2>
                </div>
                <span className="status-pill">{inspection.file_count.toLocaleString()} files</span>
              </div>
              <div className={styles.inspectionGrid}>
                <div><span className="field-label">Revision</span><code>{inspection.revision}</code></div>
                <div><span className="field-label">Default branch</span><code>{inspection.project_context.default_branch}</code></div>
                <div><span className="field-label">Languages</span><span>{Object.keys(inspection.languages).join(", ") || "Not detected"}</span></div>
                <div><span className="field-label">Frameworks</span><span>{inspection.project_context.frameworks.join(", ") || "Not detected"}</span></div>
              </div>
              <div className={styles.detailBlock}>
                <span className="field-label">Top-level paths</span>
                <p className={styles.muted}>{inspection.top_level_entries.join(" · ") || "No paths returned"}</p>
              </div>
              {Object.keys(inspection.dependencies).length > 0 && (
                <div className={styles.detailBlock}>
                  <span className="field-label">Detected dependencies</span>
                  <ul className={styles.dependencyList}>
                    {Object.entries(inspection.dependencies).map(([manager, packages]) => (
                      <li key={manager}><strong>{manager}</strong><span>{packages.join(", ")}</span></li>
                    ))}
                  </ul>
                </div>
              )}
              {inspection.project_context.readme_excerpt && (
                <div className={styles.detailBlock}>
                  <span className="field-label">README excerpt · untrusted repository text</span>
                  <pre className={styles.readme}>{inspection.project_context.readme_excerpt}</pre>
                </div>
              )}
            </section>
          )}
        </>
      )}
    </div>
  );
}
