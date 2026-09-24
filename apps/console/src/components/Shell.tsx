import {
  Badge,
  Button,
  Dialog,
  Input,
  MenuCheckboxItem,
  MenuContent,
  MenuItem,
  MenuRoot,
  MenuSection,
  MenuSeparator,
  MenuTrigger,
  Notice,
  Select,
} from "@llmp/ui";
import { useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { NavLink, useLocation } from "react-router";
import logoUrl from "../assets/logo.png";
import { request } from "../lib/api";
import { useSetDefaultBillingGroup } from "../lib/queries";
import { useOptionalToast } from "../lib/toast";
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
  { to: "/admin/redaction", label: "Redaction" },
  { to: "/admin/providers", label: "Providers" },
  { to: "/admin/search", label: "Web search" },
  // No Pricing entry: a model's prices live on the model's own page, because
  // "what is this model" and "what does it cost" are one question asked in one
  // place. See routes/AdminModelDetail.tsx.
  { to: "/admin/models", label: "Models" },
  { to: "/admin/users", label: "Users" },
  { to: "/admin/groups", label: "Groups" },
  { to: "/admin/settings", label: "Settings" },
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

  // The phone's navigation panel. Desktop never hides the pills, so this is
  // only read through the mobile-only classes; a route change closes it.
  const [navOpen, setNavOpen] = useState(false);

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
        <div className="relative mx-auto flex max-w-[var(--layout-max-width)] items-center gap-6 px-5 py-3 max-[40rem]:gap-3 max-[40rem]:px-4">
          {/* The wordmark is the way home, which is what a wordmark is for
              everywhere else on the web. `end`, so it is only "current" on the
              overview itself rather than on every route beneath it. */}
          <NavLink
            to="/"
            end
            className="flex shrink-0 items-center gap-2 rounded-sm text-ink no-underline focus-visible:outline-none focus-visible:shadow-focus"
          >
            {/* The Pystino mark, from the simplified_logo.png the operator
                pushed to GitHub (564px original), resized to a 72px 2x asset
                (8-bit, 2.4 KB) and displayed at 36px — a step up from the
                28px the detailed original sat at. Decorative in the strict
                sense — `alt=""`, with the wordmark beside it carrying the
                name — so it costs a screen reader nothing. */}
            <img src={logoUrl} alt="" className="h-9 w-auto" />
            <span className="font-bold">Pystino</span>
          </NavLink>

          {/* One navigation, two shapes: inline pills on a wide screen, and on
              a phone the same element re-dressed as a panel the hamburger
              drops under the bar. Rendered once and always in the DOM — a
              CSS-hidden element stays in the accessibility tree, and there is
              exactly one "Providers" link for a test to find, whichever shape
              the reader is looking at. */}
          <nav
            className={[
              "flex flex-1 items-center gap-1",
              "max-[40rem]:absolute max-[40rem]:inset-x-0 max-[40rem]:top-full max-[40rem]:z-20",
              "max-[40rem]:flex-col max-[40rem]:items-stretch max-[40rem]:border-b max-[40rem]:border-line",
              "max-[40rem]:bg-surface max-[40rem]:p-3 max-[40rem]:shadow-md",
              !navOpen && "max-[40rem]:hidden",
            ].join(" ")}
            aria-label={inAdmin ? "Administration" : "Sections"}
          >
            {items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/" || item.to === "/admin"}
                onClick={() => setNavOpen(false)}
                className={({ isActive }) =>
                  [
                    isActive ? `${NAV_LINK} ${NAV_LINK_ACTIVE}` : NAV_LINK,
                    "max-[40rem]:w-full",
                  ].join(" ")
                }
              >
                {item.label}
              </NavLink>
            ))}
            {me.is_admin && (
              // Administration is a place, not a section — but it rides in the
              // one navigation rather than getting a second element for each
              // screen size. On a desktop it is dressed as a control (it
              // changes where you are; reading like the sections beside it
              // would make administration look like one of them); on a phone
              // it is the last row of the hamburger panel, still one click.
              <NavLink
                to={inAdmin ? "/" : "/admin"}
                end={inAdmin}
                onClick={() => setNavOpen(false)}
                className={({ isActive }) =>
                  [
                    isActive ? `${NAV_LINK} ${NAV_LINK_ACTIVE}` : NAV_LINK,
                    "max-[40rem]:w-full",
                    "min-[40rem]:ml-2 min-[40rem]:rounded-full min-[40rem]:border",
                    "min-[40rem]:border-line-strong min-[40rem]:px-3 min-[40rem]:py-1.5",
                    "min-[40rem]:font-medium min-[40rem]:whitespace-nowrap min-[40rem]:text-ink",
                    "min-[40rem]:no-underline min-[40rem]:hover:bg-sunken",
                  ].join(" ")
                }
              >
                {inAdmin ? "User view" : "Admin view"}
              </NavLink>
            )}
          </nav>

          {/* The right cluster: on a phone, the hamburger that opens the
              navigation above. */}
          <div className="ml-auto flex shrink-0 items-center gap-2">
            <button
              type="button"
              aria-label="Menu"
              aria-expanded={navOpen}
              onClick={() => setNavOpen((open) => !open)}
              className="hidden size-10 cursor-pointer items-center justify-center rounded-full text-ink-muted transition-colors hover:bg-sunken hover:text-ink focus-visible:outline-none focus-visible:shadow-focus max-[40rem]:inline-flex"
            >
              <svg
                aria-hidden="true"
                viewBox="0 0 16 16"
                className="size-5"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
              >
                <path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" />
              </svg>
            </button>

            <UserMenu
              me={me}
              name={name}
              exactMoney={exactMoney}
              onSetExactMoney={setExactMoneyPersisted}
              theme={theme}
              onSetTheme={setTheme_}
            />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[var(--layout-max-width)] px-5 pt-6 pb-7 max-[40rem]:px-4 max-[40rem]:pt-5 max-[40rem]:pb-6">
        {children}
      </main>
    </div>
  );
}

/**
 * Up to two leading letters of the display name, for the avatar badge. An
 * email yields one letter; that is correct, not a bug to be cleverer about.
 */
function initialsOf(name: string): string {
  return (
    name
      .split(/\s+|@|\./)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0]?.toUpperCase() ?? "")
      .join("") || "?"
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
  const [changingBillingGroup, setChangingBillingGroup] = useState(false);
  // Lifted here rather than into the dialog: the dialog stays mounted behind
  // `open` (Base UI animates its close), so a value seeded in its own state
  // would show the *previous* opening's pick the next time it opens. Seeding
  // it at the moment the menu item is clicked is what makes it track the
  // current default each time.
  const [billingGroupId, setBillingGroupId] = useState("");
  const openBillingGroupDialog = () => {
    setBillingGroupId(me.default_billing_group?.id ?? me.groups[0]?.id ?? "");
    setChangingBillingGroup(true);
  };

  const signOut = async () => {
    setBusy(true);
    // Where to send the browser once our own session is gone. The gateway
    // answers with the identity provider's end-session URL, and going there is
    // what actually signs the person out: dropping our cookie alone leaves the
    // provider's SSO session standing, so /auth/login is answered without a
    // password prompt and they arrive back as themselves. That is what "logout
    // does nothing" looked like (found with Keycloak; any SSO provider does it).
    //
    // The fallback is the console's own login page — the SPA route that asks
    // /auth/methods and renders whichever doors exist. /auth/login, the API
    // route, is only a door *to a provider*: on a deployment with local
    // password auth and no identity provider it is a 503 error envelope, and
    // landing a person who just signed out on raw JSON is how "console signout
    // returns No identity provider is enabled" was reported.
    let target = "/console/login";
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
    <MenuRoot>
      {/* A round badge, not a name-and-chevron row: the email address in a
          header was the single widest thing on a phone, and it said nothing
          the menu itself does not say. The accessible name keeps the person's
          name so a screen reader hears who is signed in, and the initials are
          decoration (aria-hidden) — an initial pair like "DA" is a guess about
          a name, not a statement of one. The dot is the admin marker the
          header row used to carry as a full badge. */}
      <MenuTrigger
        aria-label={`Account — ${name}`}
        className="relative flex size-9 cursor-pointer items-center justify-center rounded-full bg-accent-subtle text-sm font-medium text-accent transition-colors hover:bg-accent hover:text-ink-inverse focus-visible:outline-none focus-visible:shadow-focus data-popup-open:bg-accent data-popup-open:text-ink-inverse"
      >
        <span aria-hidden="true">{initialsOf(name)}</span>
        {me.is_admin && (
          <span
            aria-hidden="true"
            className="absolute right-0 bottom-0 size-2.5 rounded-full border-2 border-surface bg-accent"
          />
        )}
      </MenuTrigger>

      <MenuContent align="end">
        <div className="mb-1.5 flex flex-col gap-1 border-b border-line-quiet px-2 pb-2 text-xs">
          {me.email && <div className="break-all text-ink">{me.email}</div>}
          <div className="text-ink-faint">
            {me.default_billing_group
              ? `Billing to ${me.default_billing_group.name}`
              : "No default billing group"}
          </div>
          {me.is_admin && <Badge tone="accent">Administrator</Badge>}
        </div>

        {/* Reader preferences live with who you are, not as controls on every
            screen that shows a figure. Both are checkboxes for the same reason:
            a *state*, not an action, so the menu item says what is on. */}
        {/* Self-service, from the caller's own groups only — the endpoint
            already refuses one the caller does not belong to (ADR 0061), so
            this is the picker for it rather than a second copy of that rule.
            Absent when there is nothing to pick from: a user in no group has
            no default to choose, and a picker with zero options is worse than
            the plain sentence above. */}
        {me.groups.length > 0 && (
          <MenuItem onClick={openBillingGroupDialog}>
            {me.default_billing_group ? "Change default billing group…" : "Set default billing group…"}
          </MenuItem>
        )}
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

      <BillingGroupDialog
        me={me}
        open={changingBillingGroup}
        groupId={billingGroupId}
        onChangeGroupId={setBillingGroupId}
        onClose={() => setChangingBillingGroup(false)}
      />
    </MenuRoot>
  );
}


/**
 * Choosing your own default billing group, from your own groups.
 *
 * `PUT /api/me/default-billing-group` (ADR 0061) already refuses a group the
 * caller does not belong to, so `me.groups` — the only groups the endpoint
 * would ever accept — is offered without a second membership check here. This
 * is the console half of the gap a bootstrap admin fell into live: the
 * gateway's own 403 named the fix ("set a default billing group in the
 * management API") and the console had no way to do it, only to report that
 * it was missing.
 */
function BillingGroupDialog({
  me,
  open,
  groupId,
  onChangeGroupId,
  onClose,
}: {
  me: Me;
  open: boolean;
  groupId: string;
  onChangeGroupId: (id: string) => void;
  onClose: () => void;
}) {
  const setDefault = useSetDefaultBillingGroup();
  const toast = useOptionalToast();
  const [error, setError] = useState<string | null>(null);

  const close = () => {
    setError(null);
    setDefault.reset();
    onClose();
  };

  const submit = () => {
    setError(null);
    setDefault.mutate(
      { group_id: groupId },
      {
        onSuccess: () => {
          toast?.add({ title: "Default billing group set", type: "success" });
          close();
        },
        onError: (caught: unknown) =>
          setError(caught instanceof Error ? caught.message : "Unknown error."),
      },
    );
  };

  return (
    <Dialog
      open={open}
      title="Default billing group"
      onClose={close}
      footer={
        <>
          <Button onClick={close}>Cancel</Button>
          <Button variant="primary" busy={setDefault.isPending} disabled={!groupId} onClick={submit}>
            Save
          </Button>
        </>
      }
    >
      {error ? (
        <Notice tone="danger" title="Could not set the default billing group">
          {error}
        </Notice>
      ) : null}
      <Select
        label="Group"
        value={groupId}
        onChange={(event) => onChangeGroupId(event.target.value)}
      >
        {me.groups.map((group) => (
          <option key={group.id} value={group.id}>
            {group.name}
          </option>
        ))}
      </Select>
    </Dialog>
  );
}
