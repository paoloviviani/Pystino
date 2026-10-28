import { m } from "../paraglide/messages.js";

/** A message function rather than a string: a string here is resolved once,
 *  at import, and frozen against whichever language the page happened to load
 *  in. */
type Message = () => string;

export interface AdminSection {
  to: string;
  title: Message;
  description: Message;
}

export interface AdminGroup {
  /** Stable, language-independent: heading anchors and test hooks. */
  id: string;
  heading: Message;
  sections: AdminSection[];
}

/**
 * The administration screens, grouped by the question each answers.
 *
 * This one list is read twice — by the sidebar (components/Shell.tsx) and by
 * the landing page (routes/Admin.tsx) — so the two can no longer disagree:
 * two orderings of one set of screens is a small thing that makes a section
 * feel arbitrary, and it was kept honest until now only by a test that knew
 * about both files.
 *
 * The grouping is the five-way split the owner chose for the landing page's
 * cards, and the sidebar carries it unchanged ("same in the sidebar"): what
 * was spent and what may be; where requests go and what they cost; what
 * leaves for a provider; who holds access; how the deployment is wired.
 */
export const ADMIN_GROUPS: AdminGroup[] = [
  {
    id: "usage-limits",
    heading: m["shell.nav.group.usage_limits"],
    sections: [
      {
        to: "/admin/reports",
        title: m["admin.sections.usage.title"],
        description: m["admin.sections.usage.description"],
      },
      {
        to: "/admin/requests",
        title: m["admin.sections.requests.title"],
        description: m["admin.sections.requests.description"],
      },
      {
        to: "/admin/quotas",
        title: m["admin.sections.quotas.title"],
        description: m["admin.sections.quotas.description"],
      },
      {
        // Beside Quotas: the rules there, each user's consumption of them here.
        to: "/admin/quota-usage",
        title: m["admin.sections.quota_usage.title"],
        description: m["admin.sections.quota_usage.description"],
      },
    ],
  },
  {
    id: "serving",
    heading: m["shell.nav.group.serving"],
    sections: [
      {
        to: "/admin/providers",
        title: m["admin.sections.providers.title"],
        description: m["admin.sections.providers.description"],
      },
      {
        to: "/admin/models",
        title: m["admin.sections.models.title"],
        description: m["admin.sections.models.description"],
      },
      {
        // Beside Models, in both places this list is read: an alias is a name
        // for a model, and a section nobody learns exists is one they cannot
        // use (ADR 0101).
        to: "/admin/routing",
        title: m["admin.sections.routing.title"],
        description: m["admin.sections.routing.description"],
      },
      {
        // A search backend is a provider, and a search "model" is a tier —
        // Serving, not Insights, is where the backends belong.
        to: "/admin/search",
        title: m["admin.sections.web_search.title"],
        description: m["admin.sections.web_search.description"],
      },
    ],
  },
  {
    id: "privacy",
    heading: m["shell.nav.group.privacy"],
    sections: [
      {
        to: "/admin/redaction",
        title: m["admin.sections.redaction.title"],
        description: m["admin.sections.redaction.description"],
      },
    ],
  },
  {
    id: "people",
    heading: m["shell.nav.group.people"],
    sections: [
      {
        to: "/admin/users",
        title: m["admin.sections.users.title"],
        description: m["admin.sections.users.description"],
      },
      {
        to: "/admin/groups",
        title: m["admin.sections.groups.title"],
        description: m["admin.sections.groups.description"],
      },
    ],
  },
  {
    id: "deployment",
    heading: m["shell.nav.group.deployment"],
    sections: [
      {
        to: "/admin/audit",
        title: m["admin.sections.audit.title"],
        description: m["admin.sections.audit.description"],
      },
      // Settings is last in both places this list is read: it is the one
      // position that reads as "start with this" if it led, and it is the
      // wiring of the deployment rather than its traffic.
      {
        to: "/admin/settings",
        title: m["admin.sections.settings.title"],
        description: m["admin.sections.settings.description"],
      },
    ],
  },
];
