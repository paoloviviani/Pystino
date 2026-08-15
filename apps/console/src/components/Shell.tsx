import { Badge } from "@llmp/ui";
import type { ReactNode } from "react";
import { NavLink } from "react-router";
import type { Me } from "../lib/types";
import styles from "./Shell.module.css";

export interface ShellProps {
  me: Me;
  children: ReactNode;
}

interface NavItem {
  to: string;
  label: string;
  adminOnly?: boolean;
}

// Admin items are filtered out for a non-administrator. The API enforces the
// same rule independently — this only keeps the navigation honest about what
// the reader can actually open.
const NAV: NavItem[] = [
  { to: "/", label: "Overview" },
  { to: "/admin/reports", label: "Reports", adminOnly: true },
  { to: "/admin/quotas", label: "Quotas", adminOnly: true },
  { to: "/admin/providers", label: "Providers", adminOnly: true },
  { to: "/admin/models", label: "Models", adminOnly: true },
  { to: "/admin/pricing", label: "Pricing", adminOnly: true },
  { to: "/admin/users", label: "Users", adminOnly: true },
];

export function Shell({ me, children }: ShellProps) {
  const items = NAV.filter((item) => !item.adminOnly || me.is_admin);
  const name = me.display_name || me.email || "Signed in";

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <div className={styles.brand}>
            <span className={styles.brandMark} aria-hidden="true" />
            <span className={styles.brandName}>LLM platform</span>
          </div>

          <nav className={styles.nav} aria-label="Sections">
            {items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                className={({ isActive }) =>
                  isActive ? `${styles.navLink} ${styles.navLinkActive}` : styles.navLink
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>

          <div className={styles.identity}>
            <span className={styles.identityName}>{name}</span>
            {me.is_admin && <Badge tone="accent">Administrator</Badge>}
          </div>
        </div>
      </header>

      <main className={styles.main}>{children}</main>
    </div>
  );
}
