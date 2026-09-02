import {
  Badge,
  MenuCheckboxItem,
  MenuContent,
  MenuItem,
  MenuRoot,
  MenuSection,
  MenuSeparator,
  MenuTrigger,
} from "@llmp/ui";
import { useState } from "react";
import type { ReactNode } from "react";
import { NavLink, useLocation } from "react-router";
import logoUrl from "../assets/logo.png";
import { request } from "../lib/api";
import { applyTheme, rememberTheme, storedTheme } from "../lib/theme";
import type { Theme } from "../lib/theme";
import type { Me } from "../lib/types";

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
  { to: "/admin/identity", label: "Identity" },
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

/*
 * Header chrome, in one place: the sticky bar, the nav pills, and the identity
 * button all share it. Breakpoints are `max-[40rem]`, matching the width the
 * CSS Modules this replaces used to break at.
 */
const NAV_LINK =
  "rounded-full px-3 py-2 text-sm font-medium tracking-[0.01em] whitespace-nowrap no-underline " +
  "text-ink-muted transition-colors hover:bg-sunken hover:text-ink " +
  "focus-visible:outline-none focus-visible:shadow-focus";
const NAV_LINK_ACTIVE = "bg-accent-subtle text-accent hover:bg-accent-subtle hover:text-accent";

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
  const setExactMoneyPersisted = (next: boolean) => {
    setExactMoney(next);
    try {
      window.localStorage.setItem(EXACT_MONEY_KEY, String(next));
    } catch {
      // Not sticking across reloads is a small loss; failing the click is not.
    }
  };

  // Light and dark: the OS's answer until the reader picks, then theirs. See
  // lib/theme.ts for why this is a class and a localStorage key and nothing else.
  const [theme, setTheme] = useState<Theme>(() => storedTheme());
  const setTheme_ = (next: Theme) => {
    setTheme(next);
    applyTheme(next);
    rememberTheme(next);
  };

  return (
    <div className="min-h-dvh">
      <header className="sticky top-0 z-10 border-b border-line bg-surface">
        <div className="mx-auto flex max-w-[var(--layout-max-width)] items-center gap-6 px-5 py-3 max-[40rem]:flex-wrap max-[40rem]:gap-3 max-[40rem]:px-4">
          {/* The wordmark is the way home, which is what a wordmark is for
              everywhere else on the web. `end`, so it is only "current" on the
              overview itself rather than on every route beneath it. */}
          <NavLink
            to="/"
            end
            className="flex shrink-0 items-center gap-2 rounded-sm text-ink no-underline focus-visible:outline-none focus-visible:shadow-focus"
          >
            {/* The Pistin mark, from the simplified_logo.png the operator
                pushed to GitHub (564px original), resized to a 72px 2x asset
                (8-bit, 2.4 KB) and displayed at 36px — a step up from the
                28px the detailed original sat at. Decorative in the strict
                sense — `alt=""`, with the wordmark beside it carrying the
                name — so it costs a screen reader nothing. */}
            <img src={logoUrl} alt="" className="h-9 w-auto" />
            <span className="font-bold">Pistin Gateway</span>
          </NavLink>

          <nav
            className="flex flex-1 items-center gap-1 max-[40rem]:order-3 max-[40rem]:w-full max-[40rem]:flex-nowrap max-[40rem]:overflow-x-auto max-[40rem]:[scrollbar-width:none] max-[40rem]:[-ms-overflow-style:none] max-[40rem]:[&::-webkit-scrollbar]:hidden"
            aria-label={inAdmin ? "Administration" : "Sections"}
          >
            {items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/" || item.to === "/admin"}
                className={({ isActive }) =>
                  isActive ? `${NAV_LINK} ${NAV_LINK_ACTIVE}` : NAV_LINK
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
            // Styled as a control rather than a nav link: it changes *where you
            // are*, and reading like the sections beside it would make
            // administration look like one of them.
            <NavLink
              to={inAdmin ? "/" : "/admin"}
              end={inAdmin}
              className="rounded-full border border-line-strong px-3 py-1.5 text-sm font-medium whitespace-nowrap text-ink no-underline transition-colors hover:bg-sunken focus-visible:outline-none focus-visible:shadow-focus"
            >
              {inAdmin ? "Leave admin" : "Admin"}
            </NavLink>
          )}

          <UserMenu
            me={me}
            name={name}
            exactMoney={exactMoney}
            onSetExactMoney={setExactMoneyPersisted}
            theme={theme}
            onSetTheme={setTheme_}
          />
        </div>
      </header>

      <main className="mx-auto max-w-[var(--layout-max-width)] px-5 pt-6 pb-7 max-[40rem]:px-4 max-[40rem]:pt-5 max-[40rem]:pb-6">
        {children}
      </main>
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
 * Deliberately *not* `role="menu"` by hand — that role is a promise of the full
 * ARIA menu pattern, and the Base UI menu beneath these items is what delivers
 * it now (arrow keys, roving focus, typeahead, outside-press dismissal). What
 * the CSS Modules version implemented itself with document-level listeners is
 * the library's job here (ADR 0047).
 */
function UserMenu({
  me,
  name,
  exactMoney,
  onSetExactMoney,
  theme,
  onSetTheme,
}: {
  me: Me;
  name: string;
  exactMoney: boolean;
  onSetExactMoney: (next: boolean) => void;
  theme: Theme;
  onSetTheme: (theme: Theme) => void;
}) {
  const [busy, setBusy] = useState(false);

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
    // `ml-auto` pushes the identity to the right edge on desktop; once the nav
    // drops to its own row on a phone, it is the only thing left to do that job.
    <MenuRoot>
      <MenuTrigger
        className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-ink-muted transition-colors hover:bg-sunken focus-visible:outline-none focus-visible:shadow-focus data-popup-open:bg-sunken"
      >
        {/* A long email should not push the badge off the header on a narrow
            window; a phone has less room for it than a laptop. */}
        <span className="max-w-64 truncate max-[40rem]:max-w-32">{name}</span>
        {me.is_admin && <Badge tone="accent">Administrator</Badge>}
        <svg
          aria-hidden="true"
          viewBox="0 0 16 16"
          className="size-2.5 shrink-0 transition-transform duration-150 data-popup-open:rotate-180"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="m4 6 4 4 4-4" />
        </svg>
      </MenuTrigger>

      <MenuContent align="end">
        <div className="mb-1.5 flex flex-col gap-1 border-b border-line-quiet px-2 pb-2 text-xs">
          {me.email && <div className="break-all text-ink">{me.email}</div>}
          <div className="text-ink-faint">
            {me.default_billing_group
              ? `Billing to ${me.default_billing_group.name}`
              : "No default billing group"}
          </div>
        </div>

        {/* Reader preferences live with who you are, not as controls on every
            screen that shows a figure. Both are checkboxes for the same reason:
            a *state*, not an action, so the menu item says what is on. */}
        <MenuSection label="Preferences">
          {me.is_admin && (
            <MenuCheckboxItem
              checked={exactMoney}
              onCheckedChange={(checked) => onSetExactMoney(checked === true)}
            >
              <span>
                Exact figures
                <span className="mt-0.5 block text-xs text-ink-faint">
                  Every decimal the ledger holds. Otherwise rounded to milli-units.
                </span>
              </span>
            </MenuCheckboxItem>
          )}
          <MenuCheckboxItem
            checked={theme === "dark"}
            onCheckedChange={(checked) => onSetTheme(checked ? "dark" : "light")}
          >
            Dark theme
          </MenuCheckboxItem>
        </MenuSection>

        <MenuSeparator />
        <MenuItem onClick={signOut} className={busy ? "cursor-progress opacity-60" : undefined}>
          Sign out
        </MenuItem>
      </MenuContent>
    </MenuRoot>
  );
}
