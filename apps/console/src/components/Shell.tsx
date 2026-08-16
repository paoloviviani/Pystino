import { Badge, Button } from "@llmp/ui";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { NavLink } from "react-router";
import { request } from "../lib/api";
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

          <UserMenu me={me} name={name} />
        </div>
      </header>

      <main className={styles.main}>{children}</main>
    </div>
  );
}

/**
 * Who you are, and how to stop being them.
 *
 * A disclosure button rather than a bare "Sign out" link, because the header
 * already carries the name and the admin badge and a fourth item in that row
 * crowds it on a laptop.
 *
 * Deliberately *not* `role="menu"`. That role is a promise of the full ARIA
 * menu pattern — arrow-key navigation, roving tabindex, typeahead — and a
 * popover that claims it without implementing it is worse for a screen-reader
 * user than one that claims nothing. This is a disclosure containing ordinary
 * buttons: `aria-expanded` on the trigger, and Tab works.
 */
function UserMenu({ me, name }: { me: Me; name: string }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      // Escape closes it, which a `<dialog>` would give for free and a plain
      // popover does not.
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const signOut = async () => {
    setBusy(true);
    // Where to send the browser once our own session is gone. The gateway
    // answers with the identity provider's end-session URL, and going there is
    // what actually signs the person out: dropping our cookie alone leaves
    // Keycloak's SSO session standing, so /auth/login is answered without a
    // password prompt and they arrive back as themselves. That is what "logout
    // does nothing" looked like.
    let target = "/auth/login";
    try {
      const result = await request<{ redirect_to: string | null }>("/auth/logout", {
        method: "POST",
      });
      if (result.redirect_to) target = result.redirect_to;
    } catch {
      // The cookie may already be gone, or the network may be down. Either way
      // the useful next step is the same: go to the login page and find out.
    }
    // A full navigation, not a client-side route change: every cached query in
    // this tab was fetched as the signed-out user's predecessor, and throwing
    // the whole page away is the only way to be sure none of it is still on
    // screen behind a spinner.
    window.location.assign(target);
  };

  return (
    <div className={styles.identity} ref={container}>
      <button
        type="button"
        className={styles.identityButton}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className={styles.identityName}>{name}</span>
        {me.is_admin && <Badge tone="accent">Administrator</Badge>}
        <span className={styles.chevron} aria-hidden="true" />
      </button>

      {open && (
        <div className={styles.menu}>
          <div className={styles.menuMeta}>
            {me.email && <div className={styles.menuEmail}>{me.email}</div>}
            <div className={styles.menuGroups}>
              {me.default_billing_group
                ? `Billing to ${me.default_billing_group.name}`
                : "No default billing group"}
            </div>
          </div>
          <Button variant="ghost" busy={busy} onClick={signOut}>
            Sign out
          </Button>
        </div>
      )}
    </div>
  );
}
