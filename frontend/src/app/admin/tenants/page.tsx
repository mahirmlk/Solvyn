"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { api, AdminTenant } from "@/lib/api";

type FormMode = "create" | "edit";

const EMPTY_FORM = {
  name: "",
  slug: "",
  role: "user",
  is_active: true,
  monthly_token_quota: "",
  monthly_cost_quota: "",
};

function formatNumber(value: number): string {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value);
}

function formatCurrency(value: number): string {
  return `$${new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 }).format(value)}`;
}

export default function AdminTenantsPage() {
  const router = useRouter();
  const [tenants, setTenants] = useState<AdminTenant[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<FormMode>("create");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [submitting, setSubmitting] = useState(false);
  const [createdKey, setCreatedKey] = useState<{ tenant: string; key: string } | null>(null);

  async function refresh() {
    setError(null);
    try {
      const response = await api.listAdminTenants();
      setTenants(response.tenants);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load tenants");
    } finally {
      setLoaded(true);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  const totals = useMemo(() => {
    return tenants.reduce(
      (acc, tenant) => {
        acc.tokens += tenant.total_tokens;
        acc.cost += tenant.total_cost;
        acc.sessions += tenant.sessions_count;
        acc.keys += tenant.api_keys_count;
        if (tenant.is_active) acc.active += 1;
        return acc;
      },
      { tokens: 0, cost: 0, sessions: 0, keys: 0, active: 0 },
    );
  }, [tenants]);

  function resetForm() {
    setForm({ ...EMPTY_FORM });
    setMode("create");
    setEditingId(null);
  }

  function startEdit(tenant: AdminTenant) {
    setMode("edit");
    setEditingId(tenant.id);
    setForm({
      name: tenant.name,
      slug: tenant.slug,
      role: tenant.role,
      is_active: tenant.is_active,
      monthly_token_quota: tenant.monthly_token_quota?.toString() ?? "",
      monthly_cost_quota: tenant.monthly_cost_quota?.toString() ?? "",
    });
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    setCreatedKey(null);

    const payload: Record<string, unknown> = {
      name: form.name.trim(),
      slug: form.slug.trim().toLowerCase(),
      role: form.role,
      is_active: form.is_active,
    };
    if (form.monthly_token_quota.trim() !== "") payload.monthly_token_quota = Number(form.monthly_token_quota);
    if (form.monthly_cost_quota.trim() !== "") payload.monthly_cost_quota = Number(form.monthly_cost_quota);

    try {
      if (mode === "create") {
        const tenant = await api.createAdminTenant(payload);
        if (tenant.initial_api_key) {
          setCreatedKey({ tenant: tenant.name, key: tenant.initial_api_key.plaintext_key });
        }
      } else if (editingId) {
        await api.updateAdminTenant(editingId, payload);
      }
      resetForm();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Submit failed");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete(tenant: AdminTenant) {
    if (!window.confirm(`Soft-delete tenant "${tenant.name}"? This deactivates it.`)) return;
    setError(null);
    try {
      await api.deleteAdminTenant(tenant.id);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    }
  }

  return (
    <div className="route-page">
      <header className="page-intro">
        <div>
          <span className="eyebrow">Administration</span>
          <h1 className="page-title">Tenants</h1>
        </div>
        <span className="status-pill">
          <span className="dot busy" />
          {loaded ? `${tenants.length} Tenants` : "Loading Ledger"}
        </span>
      </header>

      <section className="metric-strip pearl-pane" aria-label="Tenant rollup">
        <article className="metric-module">
          <span className="metric-number">01</span>
          <h2 className="metric-title">Active Tenants</h2>
          <span className="readout-value">{String(totals.active).padStart(2, "0")}</span>
        </article>
        <article className="metric-module">
          <span className="metric-number">02</span>
          <h2 className="metric-title">Sessions Logged</h2>
          <span className="readout-value">{formatNumber(totals.sessions)}</span>
        </article>
        <article className="metric-module">
          <span className="metric-number">03</span>
          <h2 className="metric-title">Tokens Billed</h2>
          <span className="readout-value">{formatNumber(totals.tokens)}</span>
        </article>
        <article className="metric-module">
          <span className="metric-number">04</span>
          <h2 className="metric-title">Active API Keys</h2>
          <span className="readout-value">{formatNumber(totals.keys)}</span>
        </article>
      </section>

      <div className="page-split admin-tenants-workbench">
        <aside className="pearl-pane workflow-index">
          <span className="eyebrow">Provisioning Console</span>
          <h2 className="section-title" style={{ marginTop: 14 }}>
            {mode === "create" ? "New Tenant" : `Edit: ${form.name || form.slug}`}
          </h2>
          <form className="admin-form" onSubmit={handleSubmit} style={{ marginTop: 20 }}>
            <label className="field">
              <span className="field-label">Display Name</span>
              <input
                className="editorial-input"
                onChange={(event) => setForm({ ...form, name: event.target.value })}
                required
                value={form.name}
              />
            </label>
            <label className="field">
              <span className="field-label">Slug</span>
              <input
                className="editorial-input"
                onChange={(event) => setForm({ ...form, slug: event.target.value })}
                pattern="^[a-z0-9][a-z0-9-]*$"
                required
                value={form.slug}
              />
            </label>
            <label className="field">
              <span className="field-label">Role</span>
              <select
                className="editorial-input"
                onChange={(event) => setForm({ ...form, role: event.target.value })}
                value={form.role}
              >
                <option value="admin">admin</option>
                <option value="user">user</option>
                <option value="viewer">viewer</option>
              </select>
            </label>
            <label className="field admin-toggle">
              <input
                checked={form.is_active}
                onChange={(event) => setForm({ ...form, is_active: event.target.checked })}
                type="checkbox"
              />
              <span className="field-label" style={{ marginBottom: 0 }}>
                Active
              </span>
            </label>
            <label className="field">
              <span className="field-label">Monthly Token Quota</span>
              <input
                className="editorial-input"
                inputMode="numeric"
                min={0}
                onChange={(event) => setForm({ ...form, monthly_token_quota: event.target.value })}
                type="number"
                value={form.monthly_token_quota}
              />
            </label>
            <label className="field">
              <span className="field-label">Monthly Cost Quota (USD)</span>
              <input
                className="editorial-input"
                inputMode="decimal"
                min={0}
                onChange={(event) => setForm({ ...form, monthly_cost_quota: event.target.value })}
                step="0.01"
                type="number"
                value={form.monthly_cost_quota}
              />
            </label>

            <div className="admin-form-actions">
              <button className="action-button" disabled={submitting} type="submit">
                {submitting ? "Saving..." : mode === "create" ? "Provision Tenant" : "Save Changes"}
              </button>
              {mode === "edit" ? (
                <button className="action-button secondary" onClick={resetForm} type="button">
                  Cancel
                </button>
              ) : null}
            </div>
          </form>

          {createdKey ? (
            <div className="admin-key-banner" role="status">
              <span className="eyebrow">API Key for {createdKey.tenant}</span>
              <code className="admin-key-mono">{createdKey.key}</code>
              <p className="action-copy" style={{ marginTop: 8 }}>
                Copy this key now — it will not be shown again.
              </p>
            </div>
          ) : null}
        </aside>

        <section className="midnight-pane workflow-detail">
          <div className="detail-header">
            <div>
              <span className="eyebrow copy-light-muted">Tenant Roster</span>
              <h2 className="detail-title">{tenants.length} Records</h2>
            </div>
            <span className="status-pill light">{formatCurrency(totals.cost)} Billed</span>
          </div>

          {error ? <div className="admin-error">{error}</div> : null}

          {!loaded ? (
            <div style={{ padding: 20 }}>
              {[1, 2, 3].map((item) => <div className="dark-skeleton" key={item} style={{ height: 56, marginBottom: 8 }} />)}
            </div>
          ) : tenants.length === 0 ? (
            <div className="empty-state dark-empty">No tenants provisioned yet.</div>
          ) : (
            <div className="admin-tenant-table">
              <div className="admin-tenant-head">
                <span>Tenant</span>
                <span>Status</span>
                <span>Usage</span>
                <span>Keys</span>
                <span>Actions</span>
              </div>
              {tenants.map((tenant) => (
                <div className="admin-tenant-row" key={tenant.id}>
                  <div className="admin-tenant-meta">
                    <span className="ledger-id mono">{tenant.slug}</span>
                    <span className="ledger-title">{tenant.name}</span>
                    <span className="eyebrow copy-light-muted">{tenant.role}</span>
                  </div>
                  <span className={`status-pill light ${tenant.is_active ? "success" : "error"}`}>
                    <span className="dot" />
                    {tenant.is_active ? "Active" : "Disabled"}
                  </span>
                  <div className="admin-tenant-usage">
                    <span className="readout-value">{formatNumber(tenant.total_tokens)} tk</span>
                    <span className="action-copy">{formatCurrency(tenant.total_cost)}</span>
                    <span className="eyebrow copy-light-muted">{tenant.sessions_count} sessions</span>
                  </div>
                  <span className="readout-value">{tenant.api_keys_count}</span>
                  <div className="admin-tenant-actions">
                    <button
                      className="action-button secondary"
                      onClick={() => startEdit(tenant)}
                      type="button"
                    >
                      Edit
                    </button>
                    <button
                      className="action-button secondary"
                      onClick={() => router.push(`/admin/api-keys?tenant=${tenant.id}`)}
                      type="button"
                    >
                      Keys
                    </button>
                    <button
                      className="action-button secondary"
                      disabled={!tenant.is_active}
                      onClick={() => handleDelete(tenant)}
                      type="button"
                    >
                      Disable
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
