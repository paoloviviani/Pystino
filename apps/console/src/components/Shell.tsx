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

// The full route list from the Phase 2 plan. Items not yet built are absent
// rather than present-and-dead: a navigation link that goes nowhere is worse
// than a missing one, because it reads as a bug rather than as unfinished work.
const NAV: NavItem[] = [{ to: "/", label: "Overview" }];

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
