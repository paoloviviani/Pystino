import { Badge, Button, MoneyPrecisionProvider } from "@llmp/ui";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { NavLink, useLocation } from "react-router";
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
}

/**
 * Two navigations, and which one you see is where you are.
 *
 * They were one bar with two headed groups, and that could not work: two
 * heading rows above two link rows in a horizontal header have no shared
 * baseline, so nothing lined up with anything. The layout was arguing with the
 * information.
 *
 * The fix is not typographic. Administration is a *place*, not a section of the
 * page a person is on — an administrator looking at their own spend is not
 * administering anything — so it gets its own space, reached by the Admin
 * button beside their name, and the header shows one flat row either way.
 */
const NAV_YOU: NavItem[] = [
  { to: "/", label: "Overview" },
  { to: "/reports", label: "Your usage" },
];

const NAV_ADMIN: NavItem[] = [
  { to: "/admin", label: "Summary" },
  { to: "/admin/reports", label: "Usage" },
  { to: "/admin/quotas", label: "Quotas" },
  { to: "/admin/redaction", label: "Redaction" },
  { to: "/admin/providers", label: "Providers" },
  // No Pricing entry: a model's prices live on the model's own page, because
  // "what is this model" and "what does it cost" are one question asked in one
  // place. See routes/AdminModelDetail.tsx.
  { to: "/admin/models", label: "Models" },
  { to: "/admin/users", label: "Users" },
];

/**
 * Whether this reader has asked for exact figures, remembered across reloads.
 *
 * `localStorage` rather than a server-side preference: it is a property of how
 * one person is reading one browser, not of who they are, and a round trip to
 * store it would be a migration and an endpoint for a checkbox. Wrapped because
 * a browser with site data blocked throws on access rather than returning null,
 * and a console that will not load because it could not read a display
 * preference is a worse outcome than a preference that does not stick.
 */
const EXACT_MONEY_KEY = "llmp.console.exactMoney";

function readExactMoney(): boolean {
  try {
    return window.localStorage.getItem(EXACT_MONEY_KEY) === "true";
  } catch {
    return false;
  }
}

export function Shell({ me, children }: ShellProps) {
  const location = useLocation();
  // The route decides, not a toggle. A person who follows a link into an admin
  // screen arrives with the right navigation around it, which a stateful switch
  // would not give them.
  const inAdmin = location.pathname.startsWith("/admin");
  const items = inAdmin && me.is_admin ? NAV_ADMIN : NAV_YOU;
  const name = me.display_name || me.email || "Signed in";

  // Administrators only. A reader looking at their own spend has no use for
  // twelve decimal places, and the figures that need reconciling against a
  // provider's invoice are all on screens they cannot open.
  const [exactMoney, setExactMoney] = useState(() => me.is_admin && readExactMoney());
  const toggleExactMoney = () => {
    const next = !exactMoney;
    setExactMoney(next);
    try {
      window.localStorage.setItem(EXACT_MONEY_KEY, String(next));
    } catch {
      // Not sticking across reloads is a small loss; failing the click is not.
    }
  };

  return (
    <MoneyPrecisionProvider exact={exactMoney}>
    <div className={styles.shell}>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          {/* The wordmark is the way home, which is what a wordmark is for
              everywhere else on the web. `end`, so it is only "current" on the
              overview itself rather than on every route beneath it. */}
          <NavLink to="/" end className={styles.brand}>
            {/* Circle, triangle, square — a placeholder rather than a logo,
                which is the foundation's to supply. It outlived the Bauhaus
                palette that chose it, and is kept because three plain shapes in
                the accent's own hues say "this is a tool" without pretending to
                be branding. The circle and the square are pseudo-elements; the
                triangle needs a real element because it is drawn with
                borders. */}
            <span className={styles.brandMark} aria-hidden="true">
              <span className={styles.brandShape} />
            </span>
            <span className={styles.brandName}>LLM platform</span>
          </NavLink>

          <nav className={styles.nav} aria-label={inAdmin ? "Administration" : "Sections"}>
            {items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/" || item.to === "/admin"}
                className={({ isActive }) =>
                  isActive ? `${styles.navLink} ${styles.navLinkActive}` : styles.navLink
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>

          {me.is_admin && (
            // A link, not a menu item: it is a place to go, and burying the way
            // into administration one click deeper than the way out of it would
            // be the wrong way round. Labelled by where it leads, so the reader
            // in admin sees the way back rather than a button that does nothing.
            <NavLink
              to={inAdmin ? "/" : "/admin"}
              className={styles.adminLink}
              end={inAdmin}
            >
              {inAdmin ? "Leave admin" : "Admin"}
            </NavLink>
          )}

          <UserMenu
            me={me}
            name={name}
            exactMoney={exactMoney}
            onToggleExactMoney={toggleExactMoney}
          />
        </div>
      </header>

      <main className={styles.main}>{children}</main>
    </div>
    </MoneyPrecisionProvider>
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
function UserMenu({
  me,
  name,
  exactMoney,
  onToggleExactMoney,
}: {
  me: Me;
  name: string;
  exactMoney: boolean;
  onToggleExactMoney: () => void;
}) {
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
    // what actually signs the person out: dropping our cookie alone leaves the
    // provider's SSO session standing, so /auth/login is answered without a
    // password prompt and they arrive back as themselves. That is what "logout
    // does nothing" looked like (found with Keycloak; any SSO provider does it).
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
          {/* A reader preference, so it lives with the other one — who you are
              — rather than as a control on every screen that shows a figure.
              Administrators only: see the Shell. */}
          {me.is_admin && (
            <label className={styles.menuToggle}>
              <input type="checkbox" checked={exactMoney} onChange={onToggleExactMoney} />
              <span>
                Exact figures
                <span className={styles.menuHint}>
                  Every decimal the ledger holds. Otherwise rounded to milli-units.
                </span>
              </span>
            </label>
          )}
          <Button variant="ghost" busy={busy} onClick={signOut}>
            Sign out
          </Button>
        </div>
      )}
    </div>
  );
}
