import { Badge, Button, Card, Notice } from "@llmp/ui";
import { useState } from "react";

import { entityLabel, modeLabel } from "../lib/entities";
import { CODE, FIELD_LABEL, MUTED, TEXTAREA } from "../lib/layout";
import { useTryPolicy } from "../lib/privacy";
import type { PolicyTryItem, PolicyTryResult, RedactionPolicy } from "../lib/types";
import { m } from "../paraglide/messages.js";
import { Tip } from "./Tip";

/** The longest sample the endpoint takes. */
export const TRY_MAX_CHARS = 50_000;

/**
 * Try the policy as it is being edited, saved or not, on a sample.
 *
 * Real detection with the policy's own engine and nothing stored: the sample and
 * the result live in this component and in the mutation, which the query cache
 * does not index. A pasted text is real data. It runs on a button and not as
 * the form changes, because an engine read is the slow part of the whole path.
 */
export function PolicyTry({ policy }: { policy: RedactionPolicy }) {
  const run = useTryPolicy();
  const [text, setText] = useState("");
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          {m["policy_try.title"]()}
          <Tip text={m["policy_try.tip"]()} />
        </span>
      }
    >
      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor="policy-try-text" className={FIELD_LABEL}>
            {m["policy_try.label"]()}
          </label>
          <textarea
            id="policy-try-text"
            rows={4}
            value={text}
            maxLength={TRY_MAX_CHARS}
            placeholder={m["policy_try.placeholder"]()}
            onChange={(event) => setText(event.target.value)}
            className={TEXTAREA}
          />
        </div>
        <div>
          <Button
            variant="primary"
            busy={run.isPending}
            disabled={text.trim() === ""}
            onClick={() => run.mutate({ policy, text })}
          >
            {m["policy_try.run"]()}
          </Button>
        </div>
        {run.isError && (
          <Notice tone="danger" title={m["policy_try.error"]()}>
            {/* i18n: server text, the gateway's own error message. */}
            {run.error instanceof Error ? run.error.message : m["privacy.unknown_error"]()}
          </Notice>
        )}
        {run.data && <TryResult result={run.data} />}
      </div>
    </Card>
  );
}

function ItemList({ heading, items }: { heading: string; items: PolicyTryItem[] }) {
  return (
    <section className="flex flex-col gap-1" aria-label={heading}>
      <h3 className="m-0 text-dense font-medium text-ink-muted">{heading}</h3>
      <ul className="m-0 flex list-none flex-col gap-1 p-0 text-body">
        {items.map((item, index) => (
          <li key={index} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <strong>{entityLabel(item.type)}</strong>
            <Badge tone="neutral">{modeLabel(item.mode)}</Badge>
            <code className={CODE}>{item.text}</code>
            {item.group === "entity" && (
              <span className={`text-small ${MUTED}`}>
                {item.engine}
                {item.score !== null && ` · ${item.score.toFixed(2)}`}
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

export function TryResult({ result }: { result: PolicyTryResult }) {
  const patterns = result.items.filter((item) => item.group === "pattern");
  const entities = result.items.filter((item) => item.group === "entity");
  return (
    <div className="flex flex-col gap-3" aria-live="polite" data-testid="policy-try-result">
      {/* Not "nothing found": the engine could not run, so the entity items are
          missing, and an empty list below would read as an all-clear. */}
      {result.unavailable && (
        <Notice tone="warn" title={m["policy_try.unavailable"]()}>
          {m["policy_try.unavailable_text"]()}{" "}
          {/* i18n: server text, the service's own reason. */}
          {result.unavailable}
        </Notice>
      )}
      {result.blocked && (
        <Notice tone="warn" title={m["policy_try.blocked"]()}>
          {m["policy_try.blocked_text"]()}
        </Notice>
      )}
      {patterns.length > 0 && <ItemList heading={m["policy_try.group_pattern"]()} items={patterns} />}
      {entities.length > 0 && <ItemList heading={m["policy_try.group_entity"]()} items={entities} />}
      {result.items.length === 0 && !result.unavailable && (
        <p className={`m-0 text-body ${MUTED}`}>{m["policy_try.none"]()}</p>
      )}
      {result.redacted_text !== null && !result.blocked && (
        <div className="flex flex-col gap-1">
          <h3 className="m-0 text-dense font-medium text-ink-muted">
            {m["policy_try.redacted"]()}
          </h3>
          <pre
            className={`m-0 max-h-64 overflow-auto rounded-md bg-sunken p-3 text-body whitespace-pre-wrap [overflow-wrap:anywhere] ${CODE}`}
            data-testid="policy-try-output"
          >
            {result.redacted_text}
          </pre>
        </div>
      )}
    </div>
  );
}
