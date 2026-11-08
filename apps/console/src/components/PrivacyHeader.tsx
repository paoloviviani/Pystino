import { Link } from "react-router";

import { m } from "../paraglide/messages.js";
import { MUTED } from "../lib/layout";
import { PageHeader } from "./PageHeader";

/**
 * The head of the Privacy area: the title, the four steps a request goes
 * through, and the tabs. Routing and redaction are one pipeline (ADR 0113), so
 * they are one area; the strip says the order and, in a few words, how each
 * step combines its rules, which is the part that was not clear.
 */

export type PrivacyTab = "rules" | "hidden" | "detectors" | "test";
export type PrivacyStep = 1 | 2 | 3 | 4;

interface StepDef {
  step: PrivacyStep;
  to: string | null;
  name: () => string;
  note: () => string;
}

const STEPS: StepDef[] = [
  { step: 1, to: "/admin/privacy", name: m["privacy.steps.route"], note: m["privacy.steps.route_note"] },
  { step: 2, to: "/admin/privacy/detectors", name: m["privacy.steps.detect"], note: m["privacy.steps.detect_note"] },
  { step: 3, to: "/admin/privacy/hidden", name: m["privacy.steps.hide"], note: m["privacy.steps.hide_note"] },
  { step: 4, to: null, name: m["privacy.steps.send"], note: () => "" },
];

/** The chip styling, shared with the Test result so a lit step reads the same. */
export const STEP_BASE =
  "inline-flex min-w-0 items-baseline gap-1.5 rounded-lg border px-2.5 py-1 text-small no-underline";
export const STEP_IDLE = "border-line bg-surface text-ink";
export const STEP_LIT = "border-accent bg-sunken text-ink";

function StepChip({ def, lit }: { def: StepDef; lit: boolean }) {
  const body = (
    <>
      <span className="font-semibold">
        {def.step} {def.name()}
      </span>
      {def.note() && <span className={`text-dense ${MUTED}`}>{def.note()}</span>}
    </>
  );
  const cls = `${STEP_BASE} ${lit ? STEP_LIT : STEP_IDLE}`;
  return def.to ? (
    <Link to={def.to} className={cls} aria-current={lit ? "step" : undefined}>
      {body}
    </Link>
  ) : (
    <span className={cls} aria-current={lit ? "step" : undefined}>
      {body}
    </span>
  );
}

export function PrivacySteps({ lit = [] }: { lit?: PrivacyStep[] }) {
  return (
    <div className="flex flex-col gap-1">
      <nav aria-label={m["privacy.steps.label"]()}>
        <ol className="m-0 flex list-none flex-wrap items-center gap-x-2 gap-y-1.5 p-0">
          {STEPS.map((def, index) => (
            <li key={def.step} className="flex items-center gap-2">
              {index > 0 && (
                <span aria-hidden="true" className={MUTED}>
                  →
                </span>
              )}
              <StepChip def={def} lit={lit.includes(def.step)} />
            </li>
          ))}
        </ol>
      </nav>
      <p className={`m-0 text-small ${MUTED}`}>{m["privacy.steps.summary"]()}</p>
    </div>
  );
}

const TABS: { tab: PrivacyTab; to: string; label: () => string }[] = [
  { tab: "rules", to: "/admin/privacy", label: m["privacy.tab.rules"] },
  { tab: "hidden", to: "/admin/privacy/hidden", label: m["privacy.tab.hidden"] },
  { tab: "detectors", to: "/admin/privacy/detectors", label: m["privacy.tab.detectors"] },
  { tab: "test", to: "/admin/privacy/test", label: m["privacy.tab.test"] },
];

const STEP_OF_TAB: Record<PrivacyTab, PrivacyStep[]> = {
  rules: [1],
  detectors: [2],
  hidden: [3],
  test: [],
};

export function PrivacyHeader({
  tab,
  lit,
  actions,
}: {
  tab: PrivacyTab;
  /** Steps to light: the tab's own step by default, the ones that ran on Test. */
  lit?: PrivacyStep[];
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title={m["privacy.area.title"]()}
        actions={actions}
      />
      <PrivacySteps lit={lit ?? STEP_OF_TAB[tab]} />
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
