"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, CurrentUser } from "@/lib/api";

const NAV_ITEMS = [
  { label: "Overview", href: "/" },
  { label: "Projects", href: "/projects" },
  { label: "Teammates", href: "/teammates" },
  { label: "Workflows", href: "/workflows" },
  { label: "Sessions", href: "/sessions" },
  { label: "Analytics", href: "/analytics" },
  { label: "Admin", href: "/admin/tenants" },
];

export function Sidebar() {
  const pathname = usePathname();
  const router = useRouter();
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [identityLoaded, setIdentityLoaded] = useState(false);

  useEffect(() => {
    api.me().then(setUser).catch(() => setUser(null)).finally(() => setIdentityLoaded(true));
  }, [pathname]);

  async function signOut() {
    await api.logout().catch(() => undefined);
    setUser(null);
    router.push("/login");
  }

  return (
    <header className="topbar">
      <Link className="brand" href="/" aria-label="Solvyn dashboard">
        <span className="brand-name">Solvyn</span>
        <span className="brand-caption">AI Engineering</span>
      </Link>

      <nav className="nav-links" aria-label="Primary navigation">
        {NAV_ITEMS.map((item) => {
          const active = pathname === item.href || (item.href !== "/" && pathname.startsWith(`${item.href}/`));
          return (
            <Link
              key={item.href}
              className={`nav-link${active ? " active" : ""}`}
              href={item.href}
              aria-current={active ? "page" : undefined}
            >
              {item.label}
            </Link>
          );
        })}
      </nav>

      <div className="topbar-meta">
        <span className="system-signal">
          <span className="dot busy" />
          Live
        </span>
        <span className="topbar-divider" />
        {user ? (
          <>
            <span className="profile-label">{user.display_name || user.email}</span>
            <button className="nav-link" onClick={signOut} type="button">Sign out</button>
          </>
        ) : identityLoaded ? (
          <Link className="nav-link" href="/login">Sign in</Link>
        ) : (
          <span className="profile-label">Loading identity</span>
        )}
      </div>
    </header>
  );
}
