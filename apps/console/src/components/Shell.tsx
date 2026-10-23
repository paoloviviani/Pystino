import {
  Badge,
  Button,
  Dialog,
  MenuCheckboxItem,
  MenuContent,
  MenuItem,
  MenuRoot,
  MenuSection,
  MenuSeparator,
  MenuTrigger,
  Notice,
  Select,
  Tooltip,
} from "@llmp/ui";
import { useState } from "react";
import type { ComponentType, CSSProperties, ReactNode, SVGProps } from "react";
import { NavLink, useLocation } from "react-router";
import { ADMIN_GROUPS } from "../lib/adminNav";
import { request } from "../lib/api";
import {
  WIDE_LAYOUT_MAX_WIDTH,
  isWideRoute,
  SIDEBAR_GROUP,
  SIDEBAR_LINK,
  SIDEBAR_LINK_ACTIVE,
  SIDEBAR_LINK_COLLAPSED,
} from "../lib/layout";
import { useSetDefaultBillingGroup } from "../lib/queries";
import { useOptionalToast } from "../lib/toast";
import { applyTheme, rememberTheme, storedTheme } from "../lib/theme";
import type { Theme } from "../lib/theme";
import type { Me } from "../lib/types";
import { m } from "../paraglide/messages.js";
import {
  BoxIcon,
  ChartColumnIcon,
  EyeOffIcon,
  GaugeIcon,
  GlobeIcon,
  HouseIcon,
  LayoutDashboardIcon,
  ListIcon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
  ServerIcon,
  SettingsIcon,
  ShieldIcon,
  ShuffleIcon,
  UserIcon,
  UsersRoundIcon,
  XIcon,
} from "./icons";

export interface ShellProps {
  me: Me;
  children: ReactNode;
}

interface NavItem {
  to: string;
  /** A message function: the list is built once, at import. */
  label: () => string;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  /** Exact rather than prefix matching: "/" must not be active everywhere. */
  end?: boolean;
}

/**
 * The reader's own navigation: three screens, no categories to name.
 */
const NAV_YOU: NavItem[] = [
  { to: "/", label: m["shell.nav.overview"], icon: HouseIcon, end: true },
  { to: "/reports", label: m["shell.nav.your_usage"], icon: ChartColumnIcon },
  { to: "/requests", label: m["shell.nav.your_requests"], icon: ListIcon },
];

/**
 * Which icon stands for an administration screen.
 *
 * Keyed by route rather than living in `ADMIN_GROUPS` (lib/adminNav.ts): the
 * landing page does not draw icons, and the vocabulary of that module is the
 * screens and their order, not how the sidebar decorates them.
 */
const ADMIN_ICONS: Record<string, ComponentType<SVGProps<SVGSVGElement>>> = {
  "/admin": LayoutDashboardIcon,
  "/admin/reports": ChartColumnIcon,
  "/admin/requests": ListIcon,
  "/admin/quotas": GaugeIcon,
  "/admin/providers": ServerIcon,
  "/admin/models": BoxIcon,
  "/admin/routing": ShuffleIcon,
  "/admin/search": GlobeIcon,
  "/admin/redaction": EyeOffIcon,
  "/admin/users": UserIcon,
  "/admin/groups": UsersRoundIcon,
  "/admin/audit": ShieldIcon,
  "/admin/settings": SettingsIcon,
};

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

/**
 * Whether the sidebar is collapsed to its icon rail, remembered the same way
 * and for the same reason as the exact-figures preference: a property of one
 * browser, not of the account, and worth less than a migration.
 */
const SIDEBAR_COLLAPSED_KEY = "llmp.console.sidebarCollapsed";

function readSidebarCollapsed(): boolean {
  try {
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "true";
  } catch {
    return false;
  }
}

/**
 * The console's frame: a bar that carries the title and the account, and a
 * sidebar that carries the navigation.
 *
 * The navigation used to be pills in the bar, and twelve of them in
 * administration is a row that scrolls off a laptop's edge; a sidebar holds
 * them at any count and gives each a line of its own. It is collapsible —
 * down to an icon rail, the labels on tooltips — because a person who has
 * learned the screens does not need to keep paying the width, and the choice
 * is remembered per browser like the other display preferences. On a phone
 * the sidebar does not collapse but slides in as a drawer, which is the same
 * element with the labels kept: a rail of icons has no room to earn there.
 *
 * The bar keeps exactly two things: the title, which is the way home (what
 * the wordmark was for), and the account. Everything else lives in the
 * sidebar, so "where can I go" and "who am I" stop sharing a row.
 */
export function Shell({ me, children }: ShellProps) {
  const location = useLocation();
  // The route decides, not a toggle. A person who follows a link into an admin
  // screen arrives with the right navigation around it, which a stateful switch
  // would not give them.
  const inAdmin = location.pathname.startsWith("/admin");
  // Listings opt in to a wider column; see lib/layout.ts.
  const wide = isWideRoute(location.pathname);
  const name = me.display_name || me.email || m["shell.signed_in"]();

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

  const [collapsed, setCollapsed] = useState(() => readSidebarCollapsed());
  const setCollapsedPersisted = (next: boolean) => {
    setCollapsed(next);
    try {
      window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(next));
    } catch {
      // The same small loss as above.
    }
  };

  // The phone's drawer. Desktop never hides the sidebar behind state, so this
  // is only read through the mobile-only classes; a route change closes it.
  const [drawerOpen, setDrawerOpen] = useState(false);
  // The rail is the desktop's collapsed shape. The drawer keeps its labels
  // whatever the desktop preference says — icon-only rows on a phone are a
  // list of guesses — so "collapsed" drives the classes while this decides
  // whether the labels render at all.
  const rail = collapsed && !drawerOpen;

  // Light and dark: the OS's answer until the reader picks, then theirs. See
  // lib/theme.ts for why this is a class and a localStorage key and nothing else.
  const [theme, setTheme] = useState<Theme>(() => storedTheme());
  const setTheme_ = (next: Theme) => {
    setTheme(next);
    applyTheme(next);
    rememberTheme(next);
  };

  return (
    <div
      className="flex min-h-dvh flex-col"
      style={wide ? ({ "--layout-max-width": WIDE_LAYOUT_MAX_WIDTH } as CSSProperties) : undefined}
    >
      <header className="sticky top-0 z-20 border-b border-line bg-surface">
        <div className="mx-auto flex h-15 w-full max-w-[var(--layout-max-width)] items-center gap-1 px-5 max-[40rem]:px-3">
          {/* On a phone, the hamburger that brings the drawer in. */}
          <button
            type="button"
            aria-label={m["shell.menu"]()}
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen(true)}
            className="inline-flex size-9 shrink-0 cursor-pointer items-center justify-center rounded-full text-ink-muted transition-colors hover:bg-sunken hover:text-ink focus-visible:outline-none focus-visible:shadow-focus min-[40rem]:hidden"
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

          {/* On a desktop, the collapse toggle: the sidebar down to its icon
              rail and back. `aria-pressed` rather than `aria-expanded` — the
              button names a state of the frame, not a disclosure of content. */}
          <button
            type="button"
            aria-label={collapsed ? m["shell.sidebar.expand"]() : m["shell.sidebar.collapse"]()}
            aria-pressed={collapsed}
            onClick={() => setCollapsedPersisted(!collapsed)}
            className="hidden size-9 shrink-0 cursor-pointer items-center justify-center rounded-full text-ink-muted transition-colors hover:bg-sunken hover:text-ink focus-visible:outline-none focus-visible:shadow-focus min-[40rem]:inline-flex"
          >
            {collapsed ? (
              <PanelLeftOpenIcon className="size-4.5" />
            ) : (
              <PanelLeftCloseIcon className="size-4.5" />
            )}
          </button>

          {/* The title is the way home, which is what the wordmark was for:
              the shortest route back to your own overview from six levels
              into administration. */}
          <NavLink
            to="/"
            className="ml-1 rounded-sm font-semibold text-ink no-underline transition-colors hover:text-ink-muted focus-visible:outline-none focus-visible:shadow-focus max-[40rem]:ml-0"
          >
            {/* i18n: literal — the product's own name */}Pystino LLM gateway
          </NavLink>

          <div className="ml-auto flex shrink-0 items-center gap-2">
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

      <div className="mx-auto flex w-full max-w-[var(--layout-max-width)] flex-1 items-stretch gap-5 px-5 max-[40rem]:gap-0 max-[40rem]:px-4">
        {drawerOpen && (
          // The drawer's backdrop, and the click that closes it. A button
          // rather than a div: the dismiss is an action, and a keyboard
          // reader gets it named.
          <button
            type="button"
            aria-label={m["shell.sidebar.close"]()}
            onClick={() => setDrawerOpen(false)}
            className="fixed inset-0 z-30 cursor-pointer bg-ink/40 min-[40rem]:hidden"
          />
        )}

        <aside
          className={[
            // The phone's drawer: fixed to the left edge, over the content
            // and the bar (the close button below stands in for the
            // hamburger it covers).
            "fixed inset-y-0 left-0 z-40 flex w-60 shrink-0 flex-col border-r border-line bg-surface shadow-lg",
            // The desktop's column: sticky under the bar, the height of the
            // viewport less the bar, and never wider than its rail allows.
            "min-[40rem]:sticky min-[40rem]:inset-y-auto min-[40rem]:left-auto min-[40rem]:top-15 min-[40rem]:z-auto",
            "min-[40rem]:max-h-[calc(100dvh-3.75rem)] min-[40rem]:self-start min-[40rem]:border-line-quiet min-[40rem]:shadow-none",
            collapsed && "min-[40rem]:w-14",
            !drawerOpen && "max-[40rem]:hidden",
          ]
            .filter(Boolean)
            .join(" ")}
        >
          {/* Mobile only: the drawer covers the bar, so it carries its own
              way out. */}
          <div className="flex shrink-0 items-center justify-end px-3 pt-3 min-[40rem]:hidden">
            <button
              type="button"
              aria-label={m["shell.sidebar.close"]()}
              onClick={() => setDrawerOpen(false)}
              className="inline-flex size-9 cursor-pointer items-center justify-center rounded-full text-ink-muted transition-colors hover:bg-sunken hover:text-ink focus-visible:outline-none focus-visible:shadow-focus"
            >
              <XIcon className="size-4.5" />
            </button>
          </div>

          <SidebarNav
            me={me}
            inAdmin={inAdmin}
            rail={rail}
            onClose={() => setDrawerOpen(false)}
          />
        </aside>

        <main className="min-w-0 flex-1 pt-6 pb-7 max-[40rem]:pt-5 max-[40rem]:pb-6">
          {children}
        </main>
      </div>
    </div>
  );
}

/**
 * The sidebar's contents: the navigation of the place you are in, and at the
 * bottom the switch to the other place.
 *
 * Rendered once and always in the DOM, whatever the width — the phone hides it
 * with `hidden`, the desktop re-dresses it as a rail — so there is exactly one
 * "Providers" link for a test to find, whichever shape the reader is looking
 * at.
 */
function SidebarNav({
  me,
  inAdmin,
  rail,
  onClose,
}: {
  me: Me;
  inAdmin: boolean;
  rail: boolean;
  onClose: () => void;
}) {
  const admin = inAdmin && me.is_admin;

  return (
    <>
      <nav
        aria-label={admin ? m["shell.nav.administration"]() : m["shell.nav.sections"]()}
        className="min-h-0 flex-1 overflow-y-auto px-2.5 py-4 max-[40rem]:py-5"
      >
        {admin ? (
          <>
            {/* The landing page itself leads the list: an administrator who
                wants the overview of sections gets it from the sidebar too,
                and a group heading over it would say less than the dashboard
                icon does. */}
            <NavList
              items={[
                {
                  to: "/admin",
                  label: m["shell.nav.summary"],
                  icon: ADMIN_ICONS["/admin"] ?? SettingsIcon,
                  end: true,
                },
              ]}
              rail={rail}
              onClose={onClose}
            />
            {/*
                The five categories the landing page groups its cards into
                (lib/adminNav.ts), so the sidebar and the page cannot drift
                into two orderings of one set of screens.
            */}
            {ADMIN_GROUPS.map((group) => (
              <div key={group.id} className="mt-4">
                {rail ? (
                  // The rail cannot hold a label; a rule between the icon
                  // clusters keeps the categories readable as categories.
                  <div className="mx-2 my-2 border-t border-line-quiet" />
                ) : (
                  <p className={`${SIDEBAR_GROUP} px-2.5 pb-1.5`}>{group.heading()}</p>
                )}
                <NavList
                  items={group.sections.map((section) => ({
                    to: section.to,
                    label: section.title,
                    icon: ADMIN_ICONS[section.to] ?? SettingsIcon,
                  }))}
                  rail={rail}
                  onClose={onClose}
                />
              </div>
            ))}
          </>
        ) : (
          <NavList items={NAV_YOU} rail={rail} onClose={onClose} />
        )}
      </nav>

      {/* Administration is a place, not a section — so the way between the two
          places is not a link among the links but the sidebar's one control,
          pinned at the bottom where a switcher belongs. Absent for a reader
          with no second place to switch to: a non-administrator offered "Admin
          view" would be offered a screen that refuses them. */}
      {me.is_admin && (
        <div className="shrink-0 border-t border-line-quiet p-2.5">
          <SideLink
            to={inAdmin ? "/" : "/admin"}
            end={inAdmin}
            label={inAdmin ? m["shell.nav.user_view"]() : m["shell.nav.admin_view"]()}
            icon={ShieldIcon}
            rail={rail}
            onClose={onClose}
            neverActive
            className="text-ink"
          />
        </div>
      )}
    </>
  );
}

function NavList({
  items,
  rail,
  onClose,
}: {
  items: NavItem[];
  rail: boolean;
  onClose: () => void;
}) {
  return (
    <ul className="flex flex-col gap-0.5">
      {items.map((item) => (
        <li key={item.to}>
          <SideLink
            to={item.to}
            end={item.end}
            label={item.label()}
            icon={item.icon}
            rail={rail}
            onClose={onClose}
          />
        </li>
      ))}
    </ul>
  );
}

/**
 * One sidebar row, in both of its shapes.
 *
 * Expanded, icon and label; in the rail, the icon alone with the label on a
 * tooltip — and as the link's `aria-label`, so the accessible name survives
 * the text's removal. The active state follows the route, as the header pills
 * used to; the view switcher declines it, because the control that moves you
 * somewhere is not the marker of where you are.
 */
function SideLink({
  to,
  end = false,
  label,
  icon: Icon,
  rail,
  onClose,
  neverActive = false,
  className,
}: {
  to: string;
  end?: boolean;
  label: string;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  rail: boolean;
  onClose: () => void;
  neverActive?: boolean;
  className?: string;
}) {
  const link = (
    <NavLink
      to={to}
      end={end}
      onClick={onClose}
      aria-label={rail ? label : undefined}
      className={({ isActive }) =>
        [
          SIDEBAR_LINK,
          isActive && !neverActive ? SIDEBAR_LINK_ACTIVE : "",
          rail ? SIDEBAR_LINK_COLLAPSED : "",
          className,
        ]
          .filter(Boolean)
          .join(" ")
      }
    >
      <Icon className="size-4 shrink-0" />
      {!rail && label}
    </NavLink>
  );

  return rail ? <Tooltip label={label}>{link}</Tooltip> : link;
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
    // does nothing" looked like (any SSO provider does it).
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
        aria-label={m["shell.account_aria"]({ name })}
        className="relative flex size-9 cursor-pointer items-center justify-center rounded-full bg-accent-subtle text-body font-medium text-accent transition-colors hover:bg-accent hover:text-ink-inverse focus-visible:outline-none focus-visible:shadow-focus data-popup-open:bg-accent data-popup-open:text-ink-inverse"
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
        <div className="mb-1.5 flex flex-col gap-1 border-b border-line-quiet px-2 pb-2 text-dense">
          {me.email && <div className="break-all text-ink">{me.email}</div>}
          <div className="text-ink-faint">
            {me.default_billing_group
              ? m["shell.billing_to"]({ group: me.default_billing_group.name })
              : m["shell.no_billing_group"]()}
          </div>
          {me.is_admin && <Badge tone="accent">{m["shell.administrator"]()}</Badge>}
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
            {me.default_billing_group
              ? m["shell.change_billing_group"]()
              : m["shell.set_billing_group"]()}
          </MenuItem>
        )}
        <MenuSection label={m["shell.preferences"]()}>
          {me.is_admin && (
            <MenuCheckboxItem
              checked={exactMoney}
              onCheckedChange={(checked) => onSetExactMoney(checked === true)}
            >
              <span>
                {m["shell.exact_figures"]()}
                <span className="mt-0.5 block text-dense text-ink-faint">
                  {m["shell.exact_figures_hint"]()}
                </span>
              </span>
            </MenuCheckboxItem>
          )}
          <MenuCheckboxItem
            checked={theme === "dark"}
            onCheckedChange={(checked) => onSetTheme(checked ? "dark" : "light")}
          >
            {m["shell.dark_theme"]()}
          </MenuCheckboxItem>
        </MenuSection>

        <MenuSeparator />
        <MenuItem onClick={signOut} className={busy ? "cursor-progress opacity-60" : undefined}>
          {m["shell.sign_out"]()}
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
          toast?.add({ title: m["shell.billing_group.set"](), type: "success" });
          close();
        },
        onError: (caught: unknown) =>
          // i18n: server text — the API's own error message.
          setError(
            caught instanceof Error ? caught.message : m["shell.billing_group.unknown_error"](),
          ),
      },
    );
  };

  return (
    <Dialog
      open={open}
      title={m["shell.billing_group.title"]()}
      onClose={close}
      footer={
        <>
          <Button onClick={close}>{m["shell.billing_group.cancel"]()}</Button>
          <Button variant="primary" busy={setDefault.isPending} disabled={!groupId} onClick={submit}>
            {m["shell.billing_group.save"]()}
          </Button>
        </>
      }
    >
      {error ? (
        <Notice tone="danger" title={m["shell.billing_group.error_title"]()}>
          {error}
        </Notice>
      ) : null}
      <Select
        label={m["shell.billing_group.group_label"]()}
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
