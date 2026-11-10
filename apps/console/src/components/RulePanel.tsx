import { Card } from "@llmp/ui";
import { summariseDefinition, withoutModes } from "../lib/privacy";
import type { RedactionPolicy } from "../lib/types";
import { m } from "../paraglide/messages.js";
import { PolicyTry } from "./PolicyTry";

export interface RulePanelProps {
  /** The form as it stands, saved or not. */
  policy: RedactionPolicy;
  /** Kept for callers that pass it; a summary of what is looked for needs no caveat. */
  allowListIgnored?: boolean;
}

/**
 * What the rule does, and a place to try it.
 *
 * Both cards read the *unsaved* form. The summary is computed from it in the
 * browser; the try-it box sends it to the gateway as `policy`, because the
 * stored policy is exactly what is being changed. Nothing here is written
 * anywhere.
 */
export function RulePanel({ policy }: RulePanelProps) {
  return (
    <div className="flex flex-col gap-5">
      <Card title={m["redaction_rule.summary.title"]()}>
        <p className="m-0 text-body" data-testid="rule-summary">
          {summariseDefinition(withoutModes(policy)) || m["rule_summary.empty"]()}
        </p>
      </Card>

      <PolicyTry policy={policy} />
    </div>
  );
}
