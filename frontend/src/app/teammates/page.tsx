"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, OrganizationInfo, ProjectInfo, TeammateInfo } from "@/lib/api";
import styles from "@/components/WorkspacePages.module.css";

type OrganizationTeam = {
  organization: OrganizationInfo;
  projects: ProjectInfo[];
  teammates: TeammateInfo[];
};

const ROLES = ["Tech Lead", "Software Engineer", "QA Engineer", "Security Engineer"];

export default function TeammatesPage() {
  const [groups, setGroups] = useState<OrganizationTeam[]>([]);
  const [organizationId, setOrganizationId] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState(ROLES[1]);
  const [description, setDescription] = useState("");
  const [responsibilities, setResponsibilities] = useState("");
  const [projectIds, setProjectIds] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [busyTeammateId, setBusyTeammateId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadTeams = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const { organizations } = await api.listOrganizations();
      const teams = await Promise.all(organizations.map(async (organization) => {
        const [projectResult, teammateResult] = await Promise.all([
          api.listProjects(organization.id),
          api.listTeammates(organization.id),
        ]);
        return { organization, projects: projectResult.projects, teammates: teammateResult.teammates };
      }));
      setGroups(teams);
      setOrganizationId((current) => current || organizations[0]?.id || "");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load teammates.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void loadTeams(); }, [loadTeams]);

  const selectedOrganization = groups.find(({ organization }) => organization.id === organizationId);
  const teammateCount = groups.reduce((count, group) => count + group.teammates.length, 0);

  function toggleProject(projectId: string) {
    setProjectIds((current) => current.includes(projectId)
      ? current.filter((id) => id !== projectId)
      : [...current, projectId]);
  }

  async function createTeammate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSaving(true);
    try {
      await api.createTeammate({
        organization_id: organizationId,
        name: name.trim(),
        role,
        description: description.trim(),
        responsibilities: responsibilities.trim(),
        project_ids: projectIds,
      });
      setName("");
      setDescription("");
      setResponsibilities("");
      setProjectIds([]);
      await loadTeams();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create the teammate.");
    } finally {
      setSaving(false);
    }
  }

  async function toggleEnabled(teammate: TeammateInfo) {
    setError(null);
    setBusyTeammateId(teammate.id);
    try {
      await api.updateTeammate(teammate.id, { is_enabled: !teammate.is_enabled });
      await loadTeams();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not update the teammate.");
    } finally {
      setBusyTeammateId(null);
    }
  }

  return (
    <div className="route-page">
      <header className="page-intro">
        <div>
          <span className="eyebrow">Your engineering team</span>
          <h1 className="page-title">Teammates</h1>
        </div>
        <span className="status-pill">{loading ? "Loading" : `${teammateCount} Teammates`}</span>
      </header>

      {error && (
        <section className={styles.notice} role="alert">
          <p>{error}</p>
          {(error.includes("401") || error.toLowerCase().includes("unauthorized")) && (
            <Link className="action-button secondary" href="/login">Sign in</Link>
          )}
        </section>
      )}

      {loading ? (
        <div className={styles.groups}><div className="skeleton" style={{ minHeight: 180 }} /></div>
      ) : groups.length === 0 ? (
        <div className="empty-state">No organizations are available for this account.</div>
      ) : (
        <>
          <section className={`${styles.createPanel} pearl-pane`} aria-labelledby="create-teammate-title">
            <div className="section-head">
              <div>
                <span className="eyebrow">Persistent engineering roles</span>
                <h2 className={styles.sectionTitle} id="create-teammate-title">Add a teammate</h2>
              </div>
            </div>
            <form className={styles.form} onSubmit={createTeammate}>
              <div className={styles.formGrid}>
                <label className="field">
                  <span className="field-label">Organization</span>
                  <select className="editorial-input" onChange={(event) => { setOrganizationId(event.target.value); setProjectIds([]); }} required value={organizationId}>
                    {groups.map(({ organization }) => <option key={organization.id} value={organization.id}>{organization.name}</option>)}
                  </select>
                </label>
                <label className="field">
                  <span className="field-label">Engineering role</span>
                  <select className="editorial-input" onChange={(event) => setRole(event.target.value)} value={role}>
                    {ROLES.map((option) => <option key={option} value={option}>{option}</option>)}
                  </select>
                </label>
              </div>
              <label className="field">
                <span className="field-label">Name</span>
                <input className="editorial-input" maxLength={255} onChange={(event) => setName(event.target.value)} required value={name} />
              </label>
              <label className="field">
                <span className="field-label">Description</span>
                <textarea className="editorial-input" onChange={(event) => setDescription(event.target.value)} rows={2} value={description} />
              </label>
              <label className="field">
                <span className="field-label">Responsibilities</span>
                <textarea className="editorial-input" onChange={(event) => setResponsibilities(event.target.value)} rows={3} value={responsibilities} />
              </label>
              {selectedOrganization && selectedOrganization.projects.length > 0 && (
                <fieldset className={styles.projectChoices}>
                  <legend className="field-label">Assign to projects</legend>
                  {selectedOrganization.projects.map((project) => (
                    <label className={styles.projectChoice} key={project.id}>
                      <input checked={projectIds.includes(project.id)} onChange={() => toggleProject(project.id)} type="checkbox" />
                      <span>{project.name}</span>
                    </label>
                  ))}
                </fieldset>
              )}
              <div className={styles.formFooter}>
                <span className="copy-muted">Creating and managing teammates requires organization owner or admin access.</span>
                <button className="action-button" disabled={saving || !organizationId || !name.trim()} type="submit">{saving ? "Creating…" : "Create teammate"}</button>
              </div>
            </form>
          </section>

          <section className={styles.groups} aria-label="Teammates by organization">
            {groups.map(({ organization, teammates }) => (
              <section className={styles.group} key={organization.id}>
                <div className={styles.groupHead}>
                  <div>
                    <span className="eyebrow">Organization</span>
                    <h2 className={styles.sectionTitle}>{organization.name}</h2>
                  </div>
                  <span className="status-pill">{teammates.length} Teammates</span>
                </div>
                {teammates.length === 0 ? (
                  <div className={`empty-state ${styles.empty}`}>No teammates in this organization yet.</div>
                ) : (
                  <div className={styles.projectGrid}>
                    {teammates.map((teammate) => (
                      <article className={`${styles.projectCard} pearl-pane`} key={teammate.id}>
                        <div className={styles.cardTop}>
                          <span className={`status-pill ${teammate.is_enabled ? "success" : ""}`}>{teammate.is_enabled ? teammate.status : "Disabled"}</span>
                          <span className={`mono ${styles.slug}`}>{teammate.role}</span>
                        </div>
                        <h3>{teammate.name}</h3>
                        <p>{teammate.description || teammate.responsibilities || "No responsibilities have been added."}</p>
                        <div className={styles.teammateFoot}>
                          <span className="mono">{teammate.agent_type}</span>
                          <button className="action-button secondary" disabled={busyTeammateId === teammate.id} onClick={() => void toggleEnabled(teammate)} type="button">
                            {busyTeammateId === teammate.id ? "Saving…" : teammate.is_enabled ? "Disable" : "Enable"}
                          </button>
                        </div>
                      </article>
                    ))}
                  </div>
                )}
              </section>
            ))}
          </section>
        </>
      )}
    </div>
  );
}
