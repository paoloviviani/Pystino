/**
 * The way into administration.
 *
 * A landing page rather than a redirect to the first section, because the ten
 * screens under here are not a sequence and the first of them is not more
 * important than the rest. Sending somebody straight to Usage would make the
 * other nine feel like sub-pages of a report.
 *
 * It also gives the section a shape: each card says what the screen decides,
 * not what it is called. "Models" is a noun; "what exists, and what it costs"
 * is the question it answers. The cards sit in five categories, and the list
 * is the one in lib/adminNav.ts — the same list the sidebar reads, so the two
 * cannot drift into two orderings of one set of screens.
 */

import { Card } from "@llmp/ui";
import { Link } from "react-router";

import { PageHeader } from "../components/PageHeader";
import { ADMIN_GROUPS } from "../lib/adminNav";
import {
  PAGE,
  SECTION_GROUP,
  SECTION_GROUP_HEADING,
  SECTION_LINK,
  SECTIONS,
} from "../lib/layout";
import { m } from "../paraglide/messages.js";

export function Admin() {
  return (
    <div className={PAGE}>
      <PageHeader title={m["admin.heading"]()} subtitle={m["admin.subtitle"]()} />

      {ADMIN_GROUPS.map((group) => (
        <section key={group.id} aria-labelledby={`admin-group-${group.id}`} className={SECTION_GROUP}>
          <h2 id={`admin-group-${group.id}`} className={SECTION_GROUP_HEADING}>
            {group.heading()}
          </h2>
          <div className={SECTIONS}>
            {group.sections.map((section) => (
              // The whole card is the target. A title-only link in a card of text
              // gives a mouse a strip to hit and everything else to miss.
              <Link key={section.to} to={section.to} className={SECTION_LINK}>
                <Card title={section.title()} description={section.description()} />
              </Link>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
