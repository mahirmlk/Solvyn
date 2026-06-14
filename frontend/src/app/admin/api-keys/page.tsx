"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api, AdminApiKey, AdminApiKeyCreated, AdminTenant } from "@/lib/api";

function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function ApiKeysView() {
  const searchParams = useSearchParams();
  const initialTenant = searchParams.get("tenant") ?? "";

  const [tenants, setTenants] = useState<AdminTenant[]>([]);
  const [tenantId, setTenantId] = useState<string>(initialTenant);
  const [keys, setKeys] = useState<AdminApiKey[]>([]);
  const [tenantsLoaded, setTenantsLoaded] = useState(false);
  const [keysLoaded, setKeysLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newKeyName, setNewKeyName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [createdKey, setCreatedKey] = useState<AdminApiKeyCreated | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">("idle");

  const selectedTenant = useMemo(
    () => tenants.find((tenant) => tenant.id === tenantId) ?? null,
    [tenants, tenantId],
  );

  const refreshTenants = useCallback(async () => {
    try {
      const response = await api.listAdminTenants();
      setTenants(response.tenants);
      setTenantsLoaded(true);
      if (!tenantId && response.tenants.length > 0) {
        setTenantId(response.tenants[0].id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load tenants");
      setTenantsLoaded(true);
    }
  }, [tenantId]);

  const refreshKeys = useCallback(async (id: string) => {
    if (!id) {
      setKeys([]);
      setKeysLoaded(true);
      return;
    }
    setKeysLoaded(false);
    setError(null);
    try {
      const response = await api.listAdminApiKeys(id);
      setKeys(response.api_keys);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load API keys");
      setKeys([]);
    } finally {
      setKeysLoaded(true);
    }
  }, []);

  useEffect(() => {
    refreshTenants();
  }, [refreshTenants]);

  useEffect(() => {
    refreshKeys(tenantId);
  }, [tenantId, refreshKeys]);

  async function handleCreate(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!tenantId) {
      setError("Select a tenant first");
      return;
    }
    setSubmitting(true);
    setError(null);
    setCreatedKey(null);
    try {
      const result = await api.createAdminApiKey(tenantId, newKeyName.trim() || "default");
      setCreatedKey(result);
      setNewKeyName("");
      await refreshKeys(tenantId);
      await refreshTenants();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create key");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRevoke(key: AdminApiKey) {
    if (!window.confirm(`Revoke "${key.name}"? This cannot be undone.`)) return;
    setError(null);
    try {
      await api.revokeAdminApiKey(key.id);
      await refreshKeys(tenantId);
      await refreshTenants();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Revoke failed");
    }
  }

  async function copyKey() {
    if (!createdKey) return;
    try {
      if (navigator.clipboard) {
        await navigator.clipboard.writeText(createdKey.plaintext_key);
      } else {
        const textarea = document.createElement("textarea");
        textarea.value = createdKey.plaintext_key;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
      }
      setCopyState("copied");
      window.setTimeout(() => setCopyState("idle"), 1800);
    } catch {
      setCopyState("error");
      window.setTimeout(() => setCopyState("idle"), 1800);
    }
  }

  return (
    <div className="route-page">
      <header className="page-intro">
        <div>
          <span className="eyebrow">Administration</span>
          <h1 className="page-title">API Keys</h1>
        </div>
        <span className="status-pill">
          <span className="dot busy" />
          {keysLoaded ? `${keys.length} Keys` : "Loading Vault"}
        </span>
      </header>

      <section className="page-split admin-tenants-workbench">
        <aside className="pearl-pane workflow-index">
          <span className="eyebrow">Target Tenant</span>
          <h2 className="section-title" style={{ marginTop: 14 }}>
            {selectedTenant ? selectedTenant.name : "Select a tenant"}
          </h2>

          <div className="admin-tenant-picker">
            {!tenantsLoaded ? (
              [1, 2, 3].map((item) => <div className="skeleton" key={item} style={{ height: 48, marginBottom: 8 }} />)
            ) : tenants.length === 0 ? (
              <div className="empty-state" style={{ padding: 16 }}>
                No tenants available.
              </div>
            ) : (
              tenants.map((tenant) => (
                <button
                  className={`workflow-choice${tenantId === tenant.id ? " active" : ""}`}
                  key={tenant.id}
                  onClick={() => setTenantId(tenant.id)}
                  type="button"
                >
                  <span className="workflow-choice-name">{tenant.name}</span>
                  <span className="workflow-choice-copy">{tenant.slug}</span>
                  <span className="workflow-choice-meta">{tenant.api_keys_count} Keys</span>
                </button>
              ))
            )}
          </div>

          <form className="admin-form" onSubmit={handleCreate} style={{ marginTop: 28 }}>
            <span className="eyebrow">Mint a new key</span>
            <label className="field" style={{ marginTop: 14 }}>
              <span className="field-label">Key Label</span>
              <input
                className="editorial-input"
                onChange={(event) => setNewKeyName(event.target.value)}
                placeholder="e.g. staging-integration"
                value={newKeyName}
              />
            </label>
            <button
              className="action-button"
              disabled={submitting || !tenantId}
              type="submit"
            >
              {submitting ? "Generating..." : "Generate API Key"}
            </button>
          </form>

          {createdKey ? (
            <div className="admin-key-banner" role="status">
              <span className="eyebrow">Key Issued — copy once</span>
              <code className="admin-key-mono">{createdKey.plaintext_key}</code>
              <div className="admin-key-actions">
                <button className="action-button secondary" onClick={copyKey} type="button">
                  {copyState === "copied" ? "Copied" : copyState === "error" ? "Copy Failed" : "Copy"}
                </button>
              </div>
              <p className="action-copy" style={{ marginTop: 8 }}>
                Prefix <strong>{createdKey.key_prefix}</strong> will be visible in the table.
              </p>
            </div>
          ) : null}
        </aside>

        <section className="midnight-pane workflow-detail">
          <div className="detail-header">
            <div>
              <span className="eyebrow copy-light-muted">Key Vault</span>
              <h2 className="detail-title">{selectedTenant ? selectedTenant.slug : "—"}</h2>
            </div>
            <span className="status-pill light">
              {keys.filter((key) => key.is_active).length} Active
            </span>
          </div>

          {error ? <div className="admin-error">{error}</div> : null}

          {!tenantId ? (
            <div className="workflow-placeholder">
              <span className="eyebrow copy-light-muted">Awaiting Selection</span>
              <h2 className="workflow-placeholder-title">Choose a tenant to manage API keys.</h2>
              <p className="workflow-placeholder-copy">
                Keys are issued per-tenant. Select a tenant from the roster to view, mint, and revoke credentials.
              </p>
            </div>
          ) : !keysLoaded ? (
            <div style={{ padding: 20 }}>
              {[1, 2, 3].map((item) => (
                <div className="dark-skeleton" key={item} style={{ height: 56, marginBottom: 8 }} />
              ))}
            </div>
          ) : keys.length === 0 ? (
            <div className="empty-state dark-empty">No API keys issued for this tenant yet.</div>
          ) : (
            <div className="admin-key-table">
              <div className="admin-key-head">
                <span>Label</span>
                <span>Prefix</span>
                <span>Created</span>
                <span>Last Used</span>
                <span>Status</span>
                <span>Actions</span>
              </div>
              {keys.map((key) => (
                <div className="admin-key-row" key={key.id}>
                  <span className="ledger-title">{key.name}</span>
                  <code className="admin-key-prefix mono">{key.masked}</code>
                  <span className="ledger-date">{formatDate(key.created_at)}</span>
                  <span className="ledger-date">{formatDate(key.last_used_at)}</span>
                  <span className={`status-pill light ${key.is_active ? "success" : "error"}`}>
                    <span className="dot" />
                    {key.is_active ? "Active" : "Revoked"}
                  </span>
                  <div className="admin-tenant-actions">
                    <button
                      className="action-button secondary"
                      disabled={!key.is_active}
                      onClick={() => handleRevoke(key)}
                      type="button"
                    >
                      Revoke
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </section>
    </div>
  );
}

export default function AdminApiKeysPage() {
  return (
    <Suspense
      fallback={
        <div className="route-page">
          <header className="page-intro">
            <div>
              <span className="eyebrow">Administration</span>
              <h1 className="page-title">API Keys</h1>
            </div>
            <span className="status-pill">
              <span className="dot busy" />
              Loading
            </span>
          </header>
        </div>
      }
    >
      <ApiKeysView />
    </Suspense>
  );
}
