import { Badge, Card, Notice } from "@llmp/ui";
import { Fragment, useEffect, useState } from "react";
import { usePreviewRedaction } from "../lib/admin";
import { entityLabel } from "../lib/entities";
import { CODE, MUTED } from "../lib/layout";
import { summariseRule } from "../lib/ruleSummary";
import type { RedactionPolicy, RedactionPreview, RedactionScope } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * How long the sample waits for the last keystroke before it is sent. Long
 * enough that typing a sentence is one request rather than forty, short enough
 * that stopping to read does not feel like waiting for a button.
 */
export const PREVIEW_DEBOUNCE_MS = 500;

export interface RulePanelProps {
  /** The form as it stands, saved or not. */
  policy: RedactionPolicy;
  scope: RedactionScope | "";
  scopeId: string;
  /** The gateway will not use this rule's allowlist; see `summariseRule`. */
  allowListIgnored?: boolean;
}

/**
 * What the rule does, and a place to try it.
 *
 * Both cards read the *unsaved* form. The summary is computed from it in the
 * browser; the test sends it to the gateway as `policy`, because the stored
 * rule is exactly what the person is in the middle of changing and previewing
 * that would answer a question nobody asked. Nothing here is written anywhere.
 *
 * Neither the sample nor the result is kept: they live in this component's
 * state and in the mutation, which the query cache does not index. A pasted
 * prompt is real personal data, and the gateway makes the same promise on its
 * side.
 */
export function RulePanel({ policy, scope, scopeId, allowListIgnored = false }: RulePanelProps) {
  const preview = usePreviewRedaction();
  const [text, setText] = useState("");

  // Which subject to test as. The catch-all stands alone; a narrower scope is
  // sent only once its subject is chosen, and until then the draft is tested on
  // top of the catch-all, which is what any narrower rule would sit on.
  const subject =
    scope === "all"
      ? { scope: "all" as const }
      : scope !== "" && scopeId !== ""
        ? { scope, scope_id: scopeId }
        : {};

  const signature = JSON.stringify({ text, policy, subject });
  const { mutate, reset } = preview;
  useEffect(() => {
    if (text.trim() === "") {
      reset();
      return;
    }
    const timer = setTimeout(() => mutate({ text, policy, ...subject }), PREVIEW_DEBOUNCE_MS);
    return () => clearTimeout(timer);
    // `signature` stands for text, policy and subject together: depending on the
    // objects themselves would restart the timer on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  return (
    <div className="flex flex-col gap-5">
      <Card title={m["redaction_rule.summary.title"]()}>
        <p className="m-0 text-body" data-testid="rule-summary">
          {summariseRule(policy, allowListIgnored)}
        </p>
      </Card>

      <Card title={m["redaction_rule.test.title"]()} description={m["redaction_rule.test.hint"]()}>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <label
              htmlFor="rule-test-text"
              className="text-dense font-medium tracking-[0.01em] text-ink-muted"
            >
              {m["redaction_rule.test.label"]()}
            </label>
            <textarea
              id="rule-test-text"
              rows={4}
              value={text}
              maxLength={20_000}
              placeholder={m["redaction_rule.test.placeholder"]()}
              onChange={(event) => setText(event.target.value)}
              className="w-full resize-y rounded-md border border-line bg-surface px-3 py-2 text-body text-ink transition-shadow placeholder:text-ink-faint hover:border-line-strong focus-visible:shadow-focus focus-visible:outline-none max-[40rem]:text-[16px]"
            />
          </div>

          {preview.isError && (
            <Notice tone="danger" title={m["redaction_rule.test.error"]()}>
              {/* i18n: server text — the gateway's own error message. */}
              {preview.error instanceof Error
                ? preview.error.message
                : m["redaction_rule.error.unknown"]()}
            </Notice>
          )}

          {text.trim() !== "" && preview.isPending && !preview.data && (
            <p className={`m-0 text-body ${MUTED}`} role="status">
              {m["redaction_rule.test.running"]()}
            </p>
          )}

          {preview.data && text.trim() !== "" && (
            <PreviewResult result={preview.data} stale={preview.isPending} />
          )}
        </div>
      </Card>
    </div>
  );
}

/** A span the policy actually rewrote, as opposed to one it only found. */
function replaced(span: RedactionPreview["spans"][number]): boolean {
  return span.mode !== "off" && !span.allow_listed && span.score >= span.threshold;
}

/**
 * The gateway's answer. The output is shown whole, with the placeholders the
 * gateway wrote in it picked out: the spans in the response are positions in
 * the sample, not in the rewrite, so the rewrite itself is the one honest place
 * to point at what changed.
 */
function PreviewResult({ result, stale }: { result: RedactionPreview; stale: boolean }) {
  const left = [
    ...new Set(result.spans.filter((span) => !replaced(span)).map((span) => span.entity_type)),
  ];
  return (
    <div className={`flex flex-col gap-3 ${stale ? "opacity-60" : ""}`} aria-live="polite">
      {result.note && <Notice tone="info">{result.note}</Notice>}

      {result.blocked ? (
        <Notice tone="warn" title={m["redaction_rule.test.blocked"]()}>
          {/* i18n: server text — names the type that tripped the block. */}
          {result.blocked_reason}
        </Notice>
      ) : (
        <>
          <div className="flex flex-col gap-1">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="m-0 text-dense font-medium text-ink-muted">
                {m["redaction_rule.test.output"]()}
              </h3>
              <Badge tone={result.entity_count > 0 ? "accent" : "neutral"}>
                {m["redaction_rule.test.replaced"]({
                  count: result.entity_count,
                })}
              </Badge>
            </div>
            <pre
              className={`m-0 max-h-64 overflow-auto rounded-md bg-sunken p-3 text-body whitespace-pre-wrap [overflow-wrap:anywhere] ${CODE}`}
              data-testid="rule-test-output"
            >
              {highlight(result.redacted_text ?? "")}
            </pre>
          </div>
          {result.entity_count === 0 && (
            <p className={`m-0 text-body ${MUTED}`}>{m["redaction_rule.test.none"]()}</p>
          )}
        </>
      )}

      {left.length > 0 && (
        <p className={`m-0 text-small ${MUTED}`}>
          {m["redaction_rule.test.left"]({
            types: left.map((type) => entityLabel(type)).join(", "),
          })}
        </p>
      )}
    </div>
  );
}

/** The gateway's placeholders: `<PERSON>` for Redact, `<PERSON_1a2b…>` for Anonymise. */
const PLACEHOLDER = /(<[A-Za-z][A-Za-z0-9_]*>)/g;

function highlight(text: string) {
  return text.split(PLACEHOLDER).map((piece, index) =>
    // `split` with a capture group puts the matches at the odd positions.
    index % 2 === 1 ? (
      <mark key={index} className="rounded-sm bg-accent-subtle px-0.5 font-medium text-accent">
        {piece}
      </mark>
    ) : (
      <Fragment key={index}>{piece}</Fragment>
    ),
  );
}
