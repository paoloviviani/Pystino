import { Notice } from "@llmp/ui";
import type { ReactNode } from "react";
import { NavLink } from "react-router";

import { PageHeader } from "../components/PageHeader";
import { PAGE, SIDEBAR_LINK_ACTIVE } from "../lib/layout";
import { ScopeContext } from "../lib/scope";
import type { Me } from "../lib/types";
import { m } from "../paraglide/messages.js";
import { AdminModelDetail } from "./AdminModelDetail";
import { AdminModels } from "./AdminModels";
import { AdminProviders } from "./AdminProviders";

/**
 * A user's own providers and models (bring your own key).
 *
 * Not new screens: the administrators' Providers and Models, rendered with the
 * scope set to "me" so they talk to `/api/me/...` and drop what a private row
 * cannot have (grants, the public flag, aliases). One implementation, so the
 * form a user fills in cannot drift from the one an administrator does.
 * Cerea links here (`<origin>/console/me/providers`), so the path is fixed.
 */

type Part = "providers" | "models" | "model";

const TAB_BASE =
  "rounded-md px-3 py-1.5 text-body font-medium text-ink-muted hover:text-ink";

function Frame({ me, children }: { me: Me; children: ReactNode }) {
  // The route is reachable by a typed address, so the page checks the
  // capability itself and says why, rather than rendering a form whose every
  // call would be answered 403. The gateway refuses regardless.
  if (me.capabilities?.byok !== true) {
    return (
      <div className={PAGE}>
        <PageHeader title={m["byok.title"]()} />
        <Notice tone="warn" title={m["byok.unavailable.title"]()}>
          {m["byok.unavailable.body"]()}
        </Notice>
      </div>
    );
  }

  return (
    <ScopeContext.Provider value="me">
      <div className={PAGE}>
        <PageHeader title={m["byok.title"]()} subtitle={m["byok.note"]()} />
        <nav aria-label={m["byok.tabs_aria"]()} className="flex gap-1 border-b border-line pb-2">
          {[
            { to: "/me/providers", label: m["byok.tab.providers"]() },
            { to: "/me/models", label: m["byok.tab.models"]() },
          ].map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              className={({ isActive }) => `${TAB_BASE} ${isActive ? SIDEBAR_LINK_ACTIVE : ""}`}
            >
              {tab.label}
            </NavLink>
          ))}
        </nav>
        {children}
      </div>
    </ScopeContext.Provider>
  );
}

const PARTS: Record<Part, () => ReactNode> = {
  providers: () => <AdminProviders />,
  models: () => <AdminModels />,
  model: () => <AdminModelDetail />,
};

export function MyProviders({ me, part }: { me: Me; part: Part }) {
  return <Frame me={me}>{PARTS[part]()}</Frame>;
}
