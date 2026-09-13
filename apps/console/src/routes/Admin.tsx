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

interface Section {
  to: string;
  title: string;
  description: string;
}

// Same order as the header's admin nav (components/Shell.tsx), and Settings is
// last in both. Two orderings of one set of screens is a small thing that makes
// a section feel arbitrary — and Settings led here while coming last there,
// which is the one position that reads as "start with this".
const ADMIN_SECTIONS: Section[] = [
  {
    to: "/admin/reports",
    title: "Usage",
    description: "What the whole deployment spent, by group, person, model or day.",
  },
  {
    to: "/admin/quotas",
    title: "Quotas",
    description: "The ceilings, what they have consumed, and resetting one on the record.",
  },
  {
    to: "/admin/redaction",
    title: "Redaction",
    description: "What is stripped from prompts before a provider sees them, and for whom.",
  },
  {
    to: "/admin/providers",
    title: "Providers",
    description: "Where requests go, the credentials to get there, and what each reports.",
  },
  {
    to: "/admin/models",
    title: "Models",
    description: "What is on offer, who may use it, and what it costs per token.",
  },
  {
    to: "/admin/users",
    title: "Users",
    description: "People, their accounts, and the keys they hold.",
  },
  {
    to: "/admin/groups",
    title: "Groups",
    description: "Who belongs together, and what each group may use.",
  },
  {
    to: "/admin/search",
    title: "Web search",
    description:
      "The search backends, who may run them, and how many searches each group has spent.",
  },
  {
    to: "/admin/settings",
    title: "Settings",
    description: "The mail server, the identity providers, and who may become a user.",
  },
];

export function Admin() {
  return (
    <div className={PAGE}>
      <PageHeader
        title="Administration"
        subtitle="Everything that applies to other people. Your own account is under Overview."
      />

      <div className={SECTIONS}>
        {ADMIN_SECTIONS.map((section) => (
          // The whole card is the target. A title-only link in a card of text
          // gives a mouse a strip to hit and everything else to miss.
          <Link key={section.to} to={section.to} className={SECTION_LINK}>
            <Card title={section.title} description={section.description} />
          </Link>
        ))}
      </div>
    </div>
  );
}
