import { Card } from "@llmp/ui";
import { summariseRule } from "../lib/ruleSummary";
import type { RedactionPolicy } from "../lib/types";
import { m } from "../paraglide/messages.js";
import { PolicyTry } from "./PolicyTry";

export interface RulePanelProps {
  /** The form as it stands, saved or not. */
  policy: RedactionPolicy;
  /** The gateway will not use this rule's allowlist; see `summariseRule`. */
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
export function RulePanel({ policy, allowListIgnored = false }: RulePanelProps) {
  return (
    <div className="flex flex-col gap-5">
      <Card title={m["redaction_rule.summary.title"]()}>
        <p className="m-0 text-body" data-testid="rule-summary">
          {summariseRule(policy, allowListIgnored)}
        </p>
      </Card>

      <PolicyTry policy={policy} />
    </div>
  );
}
