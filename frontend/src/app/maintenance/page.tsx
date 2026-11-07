"use client";

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  api,
  MaintenanceScheduleInfo,
  MaintenanceTaskTarget,
  OrganizationInfo,
  ProjectInfo,
} from "@/lib/api";
import styles from "./page.module.css";

type OrganizationMaintenance = {
  organization: OrganizationInfo;
  projects: ProjectInfo[];
  schedules: MaintenanceScheduleInfo[];
  canManage: boolean;
};

export default function MaintenancePage() {
  const [groups, setGroups] = useState<OrganizationMaintenance[]>([]);
  const selectedOrganizationId = useRef("");
  const [loading, setLoading] = useState(true);
  const [loadingTasks, setLoadingTasks] = useState(false);
  const [saving, setSaving] = useState(false);
  const [updatingId, setUpdatingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [organizationId, setOrganizationId] = useState("");
  const [projectId, setProjectId] = useState("");
  const [taskId, setTaskId] = useState("");
  const [tasks, setTasks] = useState<MaintenanceTaskTarget[]>([]);
  const [title, setTitle] = useState("");
  const [intervalDays, setIntervalDays] = useState(7);

  const loadSchedules = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const { organizations } = await api.listOrganizations();
      const nextGroups = await Promise.all(organizations.map(async (organization) => {
        const [projectResult, scheduleResult] = await Promise.all([
          api.listProjects(organization.id),
          api.listMaintenanceSchedules(organization.id),
        ]);
        return {
          organization,
          projects: projectResult.projects.filter((project) => project.is_active),
          schedules: scheduleResult.schedules,
          canManage: scheduleResult.can_manage,
        };
      }));
      setGroups(nextGroups);
      const nextOrganizationId = selectedOrganizationId.current && nextGroups.some((group) =>
        group.organization.id === selectedOrganizationId.current && group.canManage && group.projects.length > 0,
      )
        ? selectedOrganizationId.current
        : nextGroups.find((group) => group.canManage && group.projects.length > 0)?.organization.id
          || nextGroups.find((group) => group.canManage)?.organization.id
          || nextGroups[0]?.organization.id
          || "";
      selectedOrganizationId.current = nextOrganizationId;
      setOrganizationId(nextOrganizationId);
      setProjectId((current) => {
        const organization = nextGroups.find((group) => group.organization.id === nextOrganizationId);
        if (current && organization?.projects.some((project) => project.id === current)) return current;
        return organization?.projects[0]?.id || "";
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load maintenance schedules.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void loadSchedules(); }, [loadSchedules]);

  const selectedGroup = groups.find((group) => group.organization.id === organizationId) || null;
  const selectedProjects = selectedGroup?.projects || [];

  useEffect(() => {
    if (!projectId || !organizationId) {
      setTasks([]);
      setTaskId("");
      return;
    }
    let active = true;
    setTasks([]);
    setTaskId("");
    setLoadingTasks(true);
    api.listMaintenanceTaskTargets(organizationId, projectId)
      .then((result) => {
        if (!active) return;
        setTasks(result.tasks);
        setTaskId((current) => result.tasks.some((task) => task.id === current) ? current : result.tasks[0]?.id || "");
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setTasks([]);
        setTaskId("");
        setError(reason instanceof Error ? reason.message : "Could not load eligible tasks.");
      })
      .finally(() => { if (active) setLoadingTasks(false); });
    return () => { active = false; };
  }, [organizationId, projectId]);

  const visibleSchedules = useMemo(
    () => groups.flatMap(({ organization, projects, schedules, canManage }) => schedules
      .filter((schedule) => schedule.maintenance_type === "code_quality")
      .map((schedule) => ({
        organization,
        project: projects.find((project) => project.id === schedule.project_id),
        schedule,
        canManage,
      }))),
    [groups],
  );

  async function createSchedule(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedGroup || !projectId || !taskId) return;
    setSaving(true);
    setError(null);
    try {
      await api.createMaintenanceSchedule(selectedGroup.organization.id, {
        project_id: projectId,
        task_id: taskId,
        maintenance_type: "code_quality",
        title: title.trim(),
        interval_days: intervalDays,
      });
      setTitle("");
      await loadSchedules();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create this schedule.");
    } finally {
      setSaving(false);
    }
  }

  async function toggleSchedule(organization: OrganizationInfo, schedule: MaintenanceScheduleInfo) {
    setUpdatingId(schedule.id);
    setError(null);
    try {
      await api.updateMaintenanceSchedule(organization.id, schedule.id, { enabled: !schedule.enabled });
      await loadSchedules();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not update this schedule.");
    } finally {
      setUpdatingId(null);
    }
  }

  return (
    <div className="route-page">
      <header className="page-intro">
        <div>
          <span className="eyebrow">Proactive engineering</span>
          <h1 className="page-title">Maintenance</h1>
        </div>
        <span className="status-pill">{loading ? "Loading" : `${visibleSchedules.length} Code quality checks`}</span>
      </header>

      {error && (
        <section className={styles.notice} role="alert">
          <p>{error}</p>
          {(error.toLowerCase().includes("auth") || error.toLowerCase().includes("token") || error.includes("401")) && (
            <Link className="action-button secondary" href="/login">Sign in</Link>
          )}
        </section>
      )}

      <section className={`${styles.explainer} pearl-pane`} aria-label="How scheduled checks work">
        <span className="eyebrow">Approval stays in control</span>
        <p>A due schedule requests a fixed <code>ruff check .</code> verification for its selected task and current workspace. It appears in approvals and waits for an owner or admin to approve before the worker runs it.</p>
        <Link className="action-button secondary" href="/approvals">Review approvals</Link>
      </section>

      {!loading && selectedGroup?.canManage && selectedProjects.length > 0 && (
        <section className={`${styles.createPanel} pearl-pane`} aria-labelledby="create-maintenance-title">
          <div className="section-head">
            <div>
              <span className="eyebrow">Recurring check</span>
              <h2 className={styles.sectionTitle} id="create-maintenance-title">Schedule a code quality check</h2>
            </div>
          </div>
          <form className={styles.form} onSubmit={createSchedule}>
            <div className={styles.formGrid}>
              <label className="field">
                <span className="field-label">Organization</span>
                <select className="editorial-input" onChange={(event) => {
                  const nextOrganizationId = event.target.value;
                  selectedOrganizationId.current = nextOrganizationId;
                  setOrganizationId(nextOrganizationId);
                  setProjectId(groups.find((group) => group.organization.id === nextOrganizationId)?.projects[0]?.id || "");
                }} value={organizationId}>
                  {groups.filter((group) => group.canManage && group.projects.length > 0).map(({ organization }) => <option key={organization.id} value={organization.id}>{organization.name}</option>)}
                </select>
              </label>
              <label className="field">
                <span className="field-label">Project</span>
                <select className="editorial-input" onChange={(event) => setProjectId(event.target.value)} required value={projectId}>
                  {selectedProjects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
                </select>
              </label>
            </div>
            <div className={styles.formGrid}>
              <label className="field">
                <span className="field-label">Workspace task</span>
                <select className="editorial-input" disabled={loadingTasks || tasks.length === 0} onChange={(event) => setTaskId(event.target.value)} required value={taskId}>
                  {tasks.map((task) => <option key={task.id} value={task.id}>{task.title} · {task.status}</option>)}
                </select>
                <span className="copy-muted">Only assigned tasks with a ready workspace are listed.</span>
              </label>
              <label className="field">
                <span className="field-label">Frequency</span>
                <select className="editorial-input" onChange={(event) => setIntervalDays(Number(event.target.value))} value={intervalDays}>
                  <option value={1}>Every day</option>
                  <option value={7}>Every week</option>
                  <option value={14}>Every two weeks</option>
                  <option value={30}>Every 30 days</option>
                </select>
              </label>
            </div>
            <label className="field">
              <span className="field-label">Schedule name</span>
              <input className="editorial-input" maxLength={255} onChange={(event) => setTitle(event.target.value)} required value={title} />
            </label>
            <div className={styles.formFooter}>
              <span className="copy-muted">
                {loadingTasks ? "Loading eligible tasks…" : tasks.length === 0 ? "No eligible tasks in this project yet." : "Owner or admin access is required to create schedules."}
              </span>
              <button className="action-button" disabled={saving || loadingTasks || !taskId || !title.trim()} type="submit">
                {saving ? "Scheduling…" : "Create schedule"}
              </button>
            </div>
          </form>
        </section>
      )}

      {!loading && groups.length > 0 && !selectedGroup?.canManage && (
        <p className={styles.readOnly}>Schedule changes are available to organization owners and admins.</p>
      )}
      {!loading && selectedGroup?.canManage && selectedProjects.length === 0 && (
        <p className={styles.readOnly}>Create a project before scheduling a code quality check.</p>
      )}

      <section className={styles.scheduleList} aria-label="Code quality schedules">
        {loading ? <div className="skeleton" style={{ minHeight: 160 }} /> : visibleSchedules.length === 0 ? (
          <div className={styles.empty}>No code quality schedules yet.</div>
        ) : visibleSchedules.map(({ organization, project, schedule, canManage }) => (
          <article className={`${styles.scheduleCard} pearl-pane`} key={schedule.id}>
            <div className={styles.cardHeading}>
              <div>
                <span className="eyebrow">{organization.name}{project ? ` / ${project.name}` : ""}</span>
                <h2>{schedule.title}</h2>
              </div>
              <span className={`status-pill${schedule.enabled ? " success" : " light"}`}>{schedule.enabled ? "Enabled" : "Paused"}</span>
            </div>
            <dl className={styles.metadata}>
              <div><dt>Frequency</dt><dd>Every {schedule.interval_days} day{schedule.interval_days === 1 ? "" : "s"}</dd></div>
              <div><dt>Next request</dt><dd>{new Date(schedule.next_run_at).toLocaleString()}</dd></div>
              <div><dt>Task</dt><dd>{schedule.task_title || (schedule.task_id ? "Assigned workspace task" : "Legacy reminder")}</dd></div>
            </dl>
            <div className={styles.cardFooter}>
              <p>Each due run creates an approval request; it does not execute until approved.</p>
              {canManage && (
                <button className="action-button secondary" disabled={updatingId === schedule.id} onClick={() => void toggleSchedule(organization, schedule)} type="button">
                  {updatingId === schedule.id ? "Updating…" : schedule.enabled ? "Pause schedule" : "Enable schedule"}
                </button>
              )}
            </div>
          </article>
        ))}
      </section>
    </div>
  );
}
