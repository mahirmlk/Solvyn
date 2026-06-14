"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV_ITEMS = [
  { label: "Command", href: "/" },
  { label: "Workflows", href: "/workflows" },
  { label: "Sessions", href: "/sessions" },
  { label: "Analytics", href: "/analytics" },
  { label: "Admin", href: "/admin/tenants" },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <header className="topbar">
      <Link className="brand" href="/" aria-label="Solvyn dashboard">
        <span className="brand-name">Solvyn</span>
        <span className="brand-caption">AI Engineering</span>
      </Link>

      <nav className="nav-links" aria-label="Primary navigation">
        {NAV_ITEMS.map((item) => {
          const active = pathname === item.href;
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
        <span className="profile-label">Operator</span>
      </div>
    </header>
  );
}
