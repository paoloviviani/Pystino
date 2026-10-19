/**
 * The way into administration.
 *
 * A landing page rather than a redirect to the first section, because the eight
 * screens under here are not a sequence and the first of them is not more
 * important than the rest. Sending somebody straight to Usage would make the
 * other seven feel like sub-pages of a report.
 *
 * It also gives the section a shape: each card says what the screen decides,
 * not what it is called. "Models" is a noun; "what exists, and what it costs"
 * is the question it answers.
 */

import { Card } from "@llmp/ui";
import { Link } from "react-router";

import { PageHeader } from "../components/PageHeader";
import { PAGE, SECTION_LINK, SECTIONS } from "../lib/layout";
import { m } from "../paraglide/messages.js";

/** A message function rather than a string: a string here is resolved once, at
 *  import, and frozen against whichever language the page happened to load in. */
type Message = () => string;

interface Section {
  to: string;
  title: Message;
  description: Message;
}

// Same order as the header's admin nav (components/Shell.tsx), and Settings is
// last in both. Two orderings of one set of screens is a small thing that makes
// a section feel arbitrary — and Settings led here while coming last there,
// which is the one position that reads as "start with this".
const ADMIN_SECTIONS: Section[] = [
  {
    to: "/admin/reports",
    title: m["admin.sections.usage.title"],
    description: m["admin.sections.usage.description"],
  },
  {
    to: "/admin/quotas",
    title: m["admin.sections.quotas.title"],
    description: m["admin.sections.quotas.description"],
  },
  {
    to: "/admin/redaction",
    title: m["admin.sections.redaction.title"],
    description: m["admin.sections.redaction.description"],
  },
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
    // Beside Models, the same adjacency the header's nav holds: an alias is a
    // name for a model, and a section the landing page omits is one
    // administrators do not learn exists (ADR 0101).
    to: "/admin/routing",
    title: m["admin.sections.routing.title"],
    description: m["admin.sections.routing.description"],
  },
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
  {
    to: "/admin/search",
    title: m["admin.sections.web_search.title"],
    description: m["admin.sections.web_search.description"],
  },
  {
    to: "/admin/settings",
    title: m["admin.sections.settings.title"],
    description: m["admin.sections.settings.description"],
  },
];

export function Admin() {
  return (
    <div className={PAGE}>
      <PageHeader title={m["admin.heading"]()} subtitle={m["admin.subtitle"]()} />

      <div className={SECTIONS}>
        {ADMIN_SECTIONS.map((section) => (
          // The whole card is the target. A title-only link in a card of text
          // gives a mouse a strip to hit and everything else to miss.
          <Link key={section.to} to={section.to} className={SECTION_LINK}>
            <Card title={section.title()} description={section.description()} />
          </Link>
        ))}
      </div>
    </div>
  );
}
