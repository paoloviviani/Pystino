import { Link } from "react-router";

import { PageHeader } from "./PageHeader";
import { HowItWorks } from "./HowItWorks";
import { m } from "../paraglide/messages.js";

/**
 * The head of the administrator's Privacy area: the title and the four tabs
 * (ADR 0113). Pipelines say when and how; redaction policies say what to hide;
 * detectors say how it is found; Test runs the real path.
 */

export type PrivacyTab = "pipelines" | "policies" | "detectors" | "test";

const TABS: { tab: PrivacyTab; to: string; label: () => string }[] = [
  { tab: "pipelines", to: "/admin/privacy", label: m["privacy.tab.pipelines"] },
  { tab: "policies", to: "/admin/privacy/policies", label: m["privacy.tab.policies"] },
  { tab: "detectors", to: "/admin/privacy/detectors", label: m["privacy.tab.detectors"] },
  { tab: "test", to: "/admin/privacy/test", label: m["privacy.tab.test"] },
];

export function PrivacyHeader({
  tab,
  actions,
}: {
  tab: PrivacyTab;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title={m["privacy.area.title"]()}
        actions={
          <>
            <HowItWorks />
            {actions}
          </>
        }
      />
      <nav aria-label={m["privacy.tab.label"]()} className="border-b border-line">
        <ul className="m-0 flex list-none flex-wrap gap-1 p-0">
          {TABS.map((item) => (
            <li key={item.tab}>
              <Link
                to={item.to}
                aria-current={item.tab === tab ? "page" : undefined}
                className={`-mb-px inline-block border-b-2 px-3 py-2 text-body no-underline ${
                  item.tab === tab
                    ? "border-accent font-semibold text-ink"
                    : "border-transparent text-ink-muted hover:text-ink"
                }`}
              >
                {item.label()}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
    </div>
  );
}
