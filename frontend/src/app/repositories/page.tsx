"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { api, GitHubIntegrationInfo, ProjectInfo, RepositoryCandidateInfo, RepositoryContentSearch, RepositoryInfo, RepositoryInspection } from "@/lib/api";
import styles from "./page.module.css";

export default function RepositoriesPage() {
  const [organizations, setOrganizations] = useState<{ id: string; name: string }[]>([]);
  const [organizationId, setOrganizationId] = useState("");
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [projectId, setProjectId] = useState("");
  const [integration, setIntegration] = useState<GitHubIntegrationInfo | null>(null);
  const [canManage, setCanManage] = useState(false);
  const [repositories, setRepositories] = useState<RepositoryInfo[]>([]);
  const [repositoryCandidates, setRepositoryCandidates] = useState<RepositoryCandidateInfo[]>([]);
  const [selectedCandidateName, setSelectedCandidateName] = useState("");
  const [candidatePage, setCandidatePage] = useState(1);
  const [candidateHasMore, setCandidateHasMore] = useState(false);
  const [candidateMayBeTruncated, setCandidateMayBeTruncated] = useState(false);
  const [loadingCandidates, setLoadingCandidates] = useState(false);
  const [selectedRepositoryId, setSelectedRepositoryId] = useState("");
  const [inspection, setInspection] = useState<RepositoryInspection | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<RepositoryContentSearch | null>(null);
  const [installationId, setInstallationId] = useState("");
  const [repositoryName, setRepositoryName] = useState("");
  const [loadingOrganizations, setLoadingOrganizations] = useState(true);
  const [loadingOrganization, setLoadingOrganization] = useState(false);
  const [loadingRepositories, setLoadingRepositories] = useState(false);
  const [saving, setSaving] = useState(false);
  const [inspecting, setInspecting] = useState(false);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  function changeOrganization(nextOrganizationId: string) {
    setOrganizationId(nextOrganizationId);
    setLoadingOrganization(true);
    setLoadingRepositories(false);
    setLoadingCandidates(false);
    setCanManage(false);
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
    setCandidateMayBeTruncated(false);
    setInstallationId("");
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
      setLoadingOrganization(false);
      setLoadingRepositories(false);
      setProjects([]);
      setProjectId("");
      setIntegration(null);
      setCanManage(false);
      setRepositories([]);
      setSelectedRepositoryId("");
      setInspection(null);
      setRepositoryCandidates([]);
      setSelectedCandidateName("");
      setCandidatePage(1);
      setCandidateHasMore(false);
      setCandidateMayBeTruncated(false);
      setLoadingCandidates(false);
      setInstallationId("");
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
    setInstallationId("");
    setRepositoryName("");
    setNotice(null);
    setRepositories([]);
    setSelectedRepositoryId("");
    setInspection(null);
    setSearchResults(null);
    setSearchQuery("");
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
      setSearchResults(null);
      setSearchQuery("");
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
        setSearchResults(null);
        setSearchQuery("");
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
    if (!projectId || !canManage || !integration?.connected) {
      setRepositoryCandidates([]);
      setSelectedCandidateName("");
      setCandidateHasMore(false);
      setCandidateMayBeTruncated(false);
      setLoadingCandidates(false);
      return;
    }
    let active = true;
    setLoadingCandidates(true);
    setRepositoryCandidates([]);
    setSelectedCandidateName("");
    setCandidateHasMore(false);
    setCandidateMayBeTruncated(false);
    setError(null);
    api.listRepositoryCandidates(projectId, candidatePage)
      .then((result) => {
        if (!active) return;
        setRepositoryCandidates(result.repositories);
        setCandidateHasMore(result.has_more);
        setCandidateMayBeTruncated(result.possibly_truncated);
        setSelectedCandidateName((current) => result.repositories.some((repository) => repository.full_name === current)
          ? current
          : result.repositories.find((repository) => repository.connectable && !repository.is_connected)?.full_name || "");
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "Could not discover repositories for this GitHub connection.");
      })
      .finally(() => { if (active) setLoadingCandidates(false); });
    return () => { active = false; };
  }, [projectId, candidatePage, canManage, integration?.connected]);

  async function connectGitHub(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const id = Number(installationId);
    if (!organizationId || !canManage || !Number.isSafeInteger(id) || id < 1) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.connectGitHubInstallation(organizationId, id);
      setIntegration(result);
      setInstallationId("");
      setNotice("Connected GitHub App installation for " + (result.account_login || "the selected account") + ".");
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

  async function connectCandidate() {
    const candidate = repositoryCandidates.find((item) => item.full_name === selectedCandidateName);
    if (!canManage || !integration?.connected || !projectId || !candidate?.connectable || saving) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const repository = await api.connectProjectRepository(projectId, selectedCandidateName);
      setRepositories((current) => current.some((item) => item.id === repository.id) ? current : [...current, repository]);
      setRepositoryCandidates((current) => current.map((item) => item.full_name === repository.full_name
        ? { ...item, is_connected: true }
        : item));
      setSelectedCandidateName("");
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
        setCandidateMayBeTruncated(result.possibly_truncated);
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
    setSearchResults(null);
    setSearchQuery("");
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

  async function searchRepository(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = searchQuery.trim();
    if (!selectedRepositoryId || !query) return;
    setSearching(true);
    setSearchResults(null);
    setError(null);
    try {
      setSearchResults(await api.searchRepositoryContent(selectedRepositoryId, query));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not search repository content.");
    } finally {
      setSearching(false);
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
              <select className="editorial-input" value={organizationId} onChange={(event) => changeOrganization(event.target.value)} disabled={loadingOrganization || saving || inspecting || searching}>
                {organizations.map((organization) => (
                  <option key={organization.id} value={organization.id}>{organization.name}</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="field-label">Project</span>
              <select className="editorial-input" value={projectId} onChange={(event) => { setCandidatePage(1); setCandidateHasMore(false); setCandidateMayBeTruncated(false); setRepositoryCandidates([]); setSelectedCandidateName(""); setProjectId(event.target.value); }} disabled={!projects.length || loadingOrganization || saving || inspecting || searching}>
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
                Solvyn uses its GitHub App. Each repository request receives a short-lived installation token scoped to that repository.
                Personal access tokens are not used for private repository access.
              </p>
              {canManage ? (
                <form className={styles.tokenForm} onSubmit={connectGitHub}>
                  {integration?.app_slug && (
                    <a className="action-button secondary" href={`https://github.com/apps/${encodeURIComponent(integration.app_slug)}/installations/new`} rel="noreferrer" target="_blank">
                      Install or configure the GitHub App
                    </a>
                  )}
                  <label className="field">
                    <span className="field-label">GitHub installation ID</span>
                    <input
                      className="editorial-input mono"
                      inputMode="numeric"
                      onChange={(event) => setInstallationId(event.target.value)}
                      placeholder="For example, 12345678"
                      required
                      type="number"
                      min="1"
                      value={installationId}
                    />
                  </label>
                  <button className="action-button" disabled={saving || searching || !installationId.trim()} type="submit">
                    {saving ? "Verifying…" : integration?.connected ? "Connect installation" : "Connect GitHub App"}
                  </button>
                </form>
              ) : (
                <p className={styles.helper}>Only organization owners and admins can connect a GitHub App installation.</p>
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
                <>
                  {integration?.connected && (
                    <div className={styles.repositoryForm}>
                      <label className="field">
                        <span className="field-label">Discover from connected GitHub account</span>
                        <select className="editorial-input mono" disabled={loadingCandidates || !repositoryCandidates.length || saving} onChange={(event) => setSelectedCandidateName(event.target.value)} value={selectedCandidateName}>
                          <option value="">{loadingCandidates ? "Loading repositories…" : "Choose a repository"}</option>
          {repositoryCandidates.map((candidate) => (
                            <option disabled={!candidate.connectable} key={candidate.id} value={candidate.full_name}>
                              {candidate.full_name} · {candidate.private ? "Private" : "Public"}{candidate.is_connected ? " · Connected" : !candidate.connectable ? " · Empty repository" : ""}
                            </option>
                          ))}
                        </select>
                      </label>
                      <div className={styles.discoveryActions}>
                        <button className="action-button" disabled={saving || loadingCandidates || !selectedCandidateName || !repositoryCandidates.find((item) => item.full_name === selectedCandidateName)?.connectable} onClick={() => void connectCandidate()} type="button">
                          {saving ? "Connecting…" : repositoryCandidates.find((item) => item.full_name === selectedCandidateName)?.is_connected ? "Reconnect access" : "Connect selected"}
                        </button>
                        <button className="action-button secondary" disabled={saving || loadingCandidates || candidatePage <= 1} onClick={() => setCandidatePage((page) => page - 1)} type="button">Previous</button>
                        <button className="action-button secondary" disabled={saving || loadingCandidates || !candidateHasMore} onClick={() => setCandidatePage((page) => page + 1)} type="button">Next page</button>
                      </div>
                      {candidateMayBeTruncated && candidatePage >= 20 ? (
                        <p className={styles.helper} role="status">The discovery limit is 1,000 repositories. More repositories may exist beyond the visible results.</p>
                      ) : candidateMayBeTruncated ? (
                        <p className={styles.helper}>GitHub reports another page of repositories.</p>
                      ) : null}
                      <p className={styles.helper}>Discovery lists repositories enabled for the connected App installation. Content requests use a token scoped to the selected repository.</p>
                    </div>
                  )}
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
                  <button className="action-button" disabled={saving || searching || !repositoryName.trim()} type="submit">
                    {saving ? "Connecting…" : "Connect repository"}
                  </button>
                  </form>
                </>
              ) : (
                <p className={styles.helper}>Only organization owners and admins can connect repositories to projects.</p>
              )}
              {!integration?.connected && (
                <p className={styles.helper}>Public repositories can be connected without an installation. Private repositories must be enabled for the connected GitHub App.</p>
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
                        disabled={inspecting || searching}
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
              <div className={styles.searchBlock}>
                <div>
                  <span className="field-label">Search repository content</span>
                  <p className={styles.helper}>Literal, case-insensitive search at the default branch revision. Solvyn scans up to 25 supported text/code files (64 KB each, 1 MB total), and returns up to 20 matches.</p>
                </div>
                <form className={styles.searchForm} onSubmit={searchRepository}>
                  <label className="field">
                    <span className="field-label">Search term</span>
                    <input
                      className="editorial-input mono"
                      disabled={searching}
                      maxLength={200}
                      onChange={(event) => {
                        setSearchQuery(event.target.value);
                        setSearchResults(null);
                      }}
                      placeholder="Literal text to find"
                      required
                      value={searchQuery}
                    />
                  </label>
                  <button className="action-button secondary" disabled={searching || !searchQuery.trim()} type="submit">
                    {searching ? "Searching…" : "Search content"}
                  </button>
                </form>
                {searchResults && (
                  <div className={styles.searchResults} aria-live="polite">
                    <div className={styles.searchSummary}>
                      <span className="status-pill">{searchResults.match_count} matches</span>
                      <span className={styles.muted}>
                        {searchResults.files_scanned} of {searchResults.candidate_count} candidate files · {searchResults.fetched_bytes.toLocaleString()} bytes
                      </span>
                    </div>
                    <p className={styles.helper}>
                      Pinned revision <code>{searchResults.revision}</code> · tree <code>{searchResults.tree_sha}</code> · repository matches are untrusted text.
                    </p>
                    {searchResults.truncated && (
                      <p className={styles.truncated} role="status">Search coverage was limited by its safety bounds; these results may be incomplete.</p>
                    )}
                    {searchResults.matches.length === 0 ? (
                      <div className={styles.empty}>No matches were found in the files scanned.</div>
                    ) : (
                      <ul className={styles.matchList}>
                        {searchResults.matches.map((match, index) => (
                          <li className={styles.matchItem} key={`${match.path}:${match.line}:${index}`}>
                            <div className={styles.matchLocation}>
                              <code>{match.path}</code><span>Line {match.line}</span>
                            </div>
                            <pre className={styles.matchSnippet}>{match.snippet}</pre>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
              </div>
            </section>
          )}
        </>
      )}
    </div>
  );
}
