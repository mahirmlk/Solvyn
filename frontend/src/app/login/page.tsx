"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import styles from "@/components/WorkspacePages.module.css";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await api.login(email, password);
      router.replace("/projects");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Sign in failed.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="route-page">
      <header className="page-intro">
        <div>
          <span className="eyebrow">Solvyn Workspace</span>
          <h1 className="page-title">Sign in</h1>
        </div>
      </header>
      <section className={`pearl-pane ${styles.loginPanel}`}>
        <p className="copy-muted">Use your Solvyn account to open your organizations and projects.</p>
        <form className={styles.form} onSubmit={submit}>
          <label className="field">
            <span className="field-label">Email</span>
            <input autoComplete="email" className="editorial-input" onChange={(event) => setEmail(event.target.value)} required type="email" value={email} />
          </label>
          <label className="field">
            <span className="field-label">Password</span>
            <input autoComplete="current-password" className="editorial-input" onChange={(event) => setPassword(event.target.value)} required type="password" value={password} />
          </label>
          {error && <p className={styles.error} role="alert">{error}</p>}
          <button className="action-button" disabled={submitting} type="submit">
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </section>
    </div>
  );
}
