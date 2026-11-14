"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { api, GitHubIntegrationInfo, ProjectInfo, RepositoryCandidateInfo, RepositoryInfo, RepositoryInspection } from "@/lib/api";
import styles from "./page.module.css";

export default function RepositoriesPage() {
  const [organizations, setOrganizations] = useState<{ id: string; name: string }[]>([]);
  const [organizationId, setOrganizationId] = useState("");
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [projectId, setProjectId] = useState("");
  const [integration, setIntegration] = useState<GitHubIntegrationInfo | null>(null);
  const [repositories, setRepositories] = useState<RepositoryInfo[]>([]);
  const [repositoryCandidates, setRepositoryCandidates] = useState<RepositoryCandidateInfo[]>([]);
  const [selectedCandidateName, setSelectedCandidateName] = useState("");
  const [candidatePage, setCandidatePage] = useState(1);
  const [candidateHasMore, setCandidateHasMore] = useState(false);
  const [loadingCandidates, setLoadingCandidates] = useState(false);
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

  function changeOrganization(nextOrganizationId: string) {
    setOrganizationId(nextOrganizationId);
    setLoadingOrganization(true);
    setLoadingRepositories(false);
    setLoadingCandidates(false);
    setProjects([]);
    setProjectId("");
    setIntegration(null);
    setRepositories([]);
    setSelectedRepositoryId("");
    setInspection(null);
    setRepositoryCandidates([]);
    setSelectedCandidateName("");
    setCandidatePage(1);
    setCandidateHasMore(false);
    setToken("");
    setRepositoryName("");
    setError(null);
    setNotice(null);
  }

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
      return;
    }
    let active = true;
    setLoadingOrganization(true);
    setError(null);
    setProjects([]);
    setProjectId("");
    setIntegration(null);
    setRepositories([]);
    setSelectedRepositoryId("");
    setInspection(null);
    Promise.all([api.listProjects(organizationId), api.getGitHubIntegration(organizationId)])
      .then(([projectResult, status]) => {
        if (!active) return;
        setProjects(projectResult.projects);
        setProjectId((current) => projectResult.projects.some((project) => project.id === current)
          ? current
          : projectResult.projects[0]?.id || "");
        setIntegration(status);
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

  useEffect(() => {
    if (!projectId || !integration?.connected || !integration.can_manage) {
      setRepositoryCandidates([]);
      setSelectedCandidateName("");
      setCandidateHasMore(false);
      return;
    }
    let active = true;
    setLoadingCandidates(true);
    setError(null);
    api.listRepositoryCandidates(projectId, candidatePage)
      .then((result) => {
        if (!active) return;
        setRepositoryCandidates(result.repositories);
        setCandidateHasMore(result.has_more);
        setSelectedCandidateName((current) => result.repositories.some((repository) => repository.full_name === current)
          ? current
          : result.repositories.find((repository) => repository.connectable && !repository.is_connected)?.full_name || "");
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "Could not discover repositories for this GitHub connection.");
      })
      .finally(() => { if (active) setLoadingCandidates(false); });
    return () => { active = false; };
  }, [projectId, candidatePage, integration?.connected, integration?.can_manage]);

  async function connectGitHub(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!organizationId || !token.trim()) return;
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
    if (!projectId || !repositoryName.trim()) return;
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

  async function connectCandidate() {
    if (!projectId || !selectedCandidateName) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const repository = await api.connectProjectRepository(projectId, selectedCandidateName);
      setRepositories((current) => current.some((item) => item.id === repository.id) ? current : [...current, repository]);
      setRepositoryCandidates((current) => current.map((item) => item.full_name === repository.full_name
        ? { ...item, is_connected: true }
        : item));
      setSelectedRepositoryId(repository.id);
      setInspection(null);
      setNotice("Connected " + repository.full_name + ".");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not connect the selected repository.");
      const [candidateRefresh, repositoryRefresh] = await Promise.allSettled([
        api.listRepositoryCandidates(projectId, candidatePage),
        api.listProjectRepositories(projectId),
      ]);
      if (candidateRefresh.status === "fulfilled") {
        const result = candidateRefresh.value;
        setRepositoryCandidates(result.repositories);
        setCandidateHasMore(result.has_more);
        setSelectedCandidateName((current) => result.repositories.some((candidate) => candidate.full_name === current)
          ? current
          : result.repositories.find((candidate) => candidate.connectable && !candidate.is_connected)?.full_name || "");
      }
      if (repositoryRefresh.status === "fulfilled") {
        const rows = repositoryRefresh.value.repositories;
        setRepositories(rows);
        setSelectedRepositoryId((current) => rows.some((repository) => repository.id === current) ? current : "");
        setInspection(null);
      }
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
              <select className="editorial-input" value={organizationId} onChange={(event) => changeOrganization(event.target.value)} disabled={loadingOrganization || saving || inspecting}>
                {organizations.map((organization) => (
                  <option key={organization.id} value={organization.id}>{organization.name}</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="field-label">Project</span>
              <select className="editorial-input" value={projectId} onChange={(event) => { setCandidatePage(1); setProjectId(event.target.value); }} disabled={!projects.length || loadingOrganization || saving || inspecting}>
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
              {integration?.can_manage ? (
                <>
                  <form className={styles.tokenForm} onSubmit={connectGitHub}>
                    <label className="field">
                      <span className="field-label">{integration.connected ? "Replace GitHub token" : "GitHub token"}</span>
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
                      {saving ? "Verifying…" : integration.connected ? "Replace token" : "Connect GitHub"}
                    </button>
                  </form>
                  <p className={styles.helper}>Only organization owners and admins can connect or replace credentials.</p>
                </>
              ) : integration ? (
                <p className={styles.helper}>GitHub credential management is read-only for members and viewers. Ask an organization owner or admin to connect or replace the token.</p>
              ) : null}
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
              {integration?.connected && integration.can_manage && (
                <div className={styles.repositoryForm}>
                  <label className="field">
                    <span className="field-label">Discover from connected GitHub account</span>
                    <select className="editorial-input mono" disabled={loadingCandidates || !repositoryCandidates.length} onChange={(event) => setSelectedCandidateName(event.target.value)} value={selectedCandidateName}>
                      <option value="">{loadingCandidates ? "Loading repositories…" : "Choose a repository"}</option>
                      {repositoryCandidates.map((candidate) => (
                        <option disabled={!candidate.connectable || candidate.is_connected} key={candidate.id} value={candidate.full_name}>
                          {candidate.full_name} · {candidate.private ? "Private" : "Public"}{candidate.is_connected ? " · Connected" : !candidate.connectable ? " · Empty repository" : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                  <div className={styles.discoveryActions}>
                    <button className="action-button" disabled={saving || !selectedCandidateName || !repositoryCandidates.find((item) => item.full_name === selectedCandidateName)?.connectable || repositoryCandidates.find((item) => item.full_name === selectedCandidateName)?.is_connected} onClick={() => void connectCandidate()} type="button">
                      {saving ? "Connecting…" : "Connect selected"}
                    </button>
                    <button className="action-button secondary" disabled={saving || loadingCandidates || candidatePage <= 1} onClick={() => setCandidatePage((page) => page - 1)} type="button">Previous</button>
                    <button className="action-button secondary" disabled={saving || loadingCandidates || !candidateHasMore} onClick={() => setCandidatePage((page) => page + 1)} type="button">Next page</button>
                  </div>
                  <p className={styles.helper}>Discovery shows repositories visible to the organization credential. Private status is shown; the credential is never returned.</p>
                </div>
              )}
              {integration?.can_manage ? (
                <>
                  <details className={styles.manualConnect}>
                    <summary>Connect by repository name</summary>
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
                  </details>
                  {!integration.connected && (
                    <p className={styles.helper}>Public repositories can be connected without a token. Private repositories require the organization GitHub token above.</p>
                  )}
                </>
              ) : integration ? (
                <p className={styles.helper}>Repository connections are read-only for members and viewers. Ask an organization owner or admin to connect repositories.</p>
              ) : null}

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
