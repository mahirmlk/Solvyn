"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, OrganizationInfo, ProjectInfo } from "@/lib/api";
import styles from "@/components/WorkspacePages.module.css";

type OrganizationProjects = { organization: OrganizationInfo; projects: ProjectInfo[] };

export default function ProjectsPage() {
  const [groups, setGroups] = useState<OrganizationProjects[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [description, setDescription] = useState("");
  const [organizationId, setOrganizationId] = useState("");
  const [saving, setSaving] = useState(false);

  const loadProjects = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const { organizations } = await api.listOrganizations();
      const projects = await Promise.all(organizations.map(async (organization) => ({
        organization,
        projects: (await api.listProjects(organization.id)).projects,
      })));
      setGroups(projects);
      setOrganizationId((current) => current || organizations[0]?.id || "");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load projects.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void loadProjects(); }, [loadProjects]);

  function updateName(value: string) {
    setName(value);
    setSlug(value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""));
  }

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSaving(true);
    try {
      await api.createProject({ organization_id: organizationId, name: name.trim(), slug: slug.trim(), description: description.trim() });
      setName("");
      setSlug("");
      setDescription("");
      await loadProjects();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create the project.");
    } finally {
      setSaving(false);
    }
  }

  const projectCount = groups.reduce((count, group) => count + group.projects.length, 0);

  return (
    <div className="route-page">
      <header className="page-intro">
        <div>
          <span className="eyebrow">Workspace</span>
          <h1 className="page-title">Projects</h1>
        </div>
        <span className="status-pill">{loading ? "Loading" : `${projectCount} Projects`}</span>
      </header>

      {error && (
        <section className={styles.notice} role="alert">
          <p>{error}</p>
          {(error.toLowerCase().includes("auth") || error.toLowerCase().includes("token") || error.includes("401")) && (
            <Link className="action-button secondary" href="/login">Sign in</Link>
          )}
        </section>
      )}

      {!loading && groups.length > 0 && (
        <section className={`${styles.createPanel} pearl-pane`} aria-labelledby="create-project-title">
          <div className="section-head">
            <div>
              <span className="eyebrow">Start a workspace</span>
              <h2 className={styles.sectionTitle} id="create-project-title">Create a project</h2>
            </div>
          </div>
          <form className={styles.form} onSubmit={createProject}>
            <label className="field">
              <span className="field-label">Organization</span>
              <select className="editorial-input" onChange={(event) => setOrganizationId(event.target.value)} required value={organizationId}>
                {groups.map(({ organization }) => <option key={organization.id} value={organization.id}>{organization.name}</option>)}
              </select>
            </label>
            <div className={styles.formGrid}>
              <label className="field">
                <span className="field-label">Project name</span>
                <input className="editorial-input" onChange={(event) => updateName(event.target.value)} required value={name} />
              </label>
              <label className="field">
                <span className="field-label">Slug</span>
                <input className="editorial-input mono" maxLength={100} onChange={(event) => setSlug(event.target.value)} pattern="[a-z0-9][a-z0-9-]*" required value={slug} />
              </label>
            </div>
            <label className="field">
              <span className="field-label">Description</span>
              <textarea className="editorial-input" onChange={(event) => setDescription(event.target.value)} rows={2} value={description} />
            </label>
            <div className={styles.formFooter}>
              <span className="copy-muted">Creating a project requires owner or admin access.</span>
              <button className="action-button" disabled={saving || !organizationId} type="submit">{saving ? "Creating…" : "Create project"}</button>
            </div>
          </form>
        </section>
      )}

      <section className={styles.groups} aria-label="Projects by organization">
        {loading ? (
          <div className="skeleton" style={{ minHeight: 180 }} />
        ) : groups.length === 0 && !error ? (
          <div className="empty-state">No organizations are available for this account.</div>
        ) : groups.map(({ organization, projects }) => (
          <section className={styles.group} key={organization.id}>
            <div className={styles.groupHead}>
              <div>
                <span className="eyebrow">Organization</span>
                <h2 className={styles.sectionTitle}>{organization.name}</h2>
              </div>
              <span className="status-pill">{projects.length} Projects</span>
            </div>
            {projects.length === 0 ? (
              <div className={`empty-state ${styles.empty}`}>No projects in this organization yet.</div>
            ) : (
              <div className={styles.projectGrid}>
                {projects.map((project) => (
                  <article className={`${styles.projectCard} pearl-pane`} key={project.id}>
                    <div className={styles.cardTop}>
                      <span className={`status-pill ${project.is_active ? "success" : ""}`}>{project.is_active ? "Active" : "Inactive"}</span>
                      <span className={`mono ${styles.slug}`}>{project.slug}</span>
                    </div>
                    <h3>{project.name}</h3>
                    <p>{project.description || "No project description has been added."}</p>
                    <div className={styles.cardFoot}>
                      <span>{project.workspace_id ? "Workspace linked" : "No workspace linked"}</span>
                      <span className="mono">{project.id.slice(0, 8)}</span>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>
        ))}
      </section>
    </div>
  );
}
