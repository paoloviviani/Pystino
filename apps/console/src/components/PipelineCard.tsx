import { Badge, Button, Card, Switch, buttonClasses } from "@llmp/ui";
import { Link } from "react-router";

import { MUTED } from "../lib/layout";
import { actionPhrase, issueBadges, ruleSentence, scopeWords } from "../lib/privacy";
import type { PrivacyKind, PrivacyPipeline } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * One pipeline as a card: its name and scope, then its rules as sentences, the
 * fixed last rule last. The list is the model, so it has to read aloud.
 */
export function PipelineCard({
  pipeline,
  own = false,
  editTo,
  onToggle,
  onDelete,
  onMove,
  first = false,
  last = false,
}: {
  pipeline: PrivacyPipeline;
  /** A user's own: the who is "only me". */
  own?: boolean;
  /** Absent for a read-only card (the administrator's, shown to a user). */
  editTo?: string;
  onToggle?: (enabled: boolean) => void;
  onDelete?: () => void;
  onMove?: (delta: -1 | 1) => void;
  first?: boolean;
  last?: boolean;
}) {
  const kind = pipeline.kind as PrivacyKind;
  const [who, target] = scopeWords(pipeline, own);
  const flags = [...pipeline.rules, pipeline.otherwise].map(issueBadges);
  const warn = flags.some((f) => f.never || f.partly || f.check);
  return (
    <Card>
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            {/* i18n: server text, the name the author gave the pipeline. */}
            <strong>{pipeline.name}</strong>
            <Badge tone="neutral">{who}</Badge>
            <Badge tone="neutral">{target}</Badge>
            {!pipeline.enabled && !onToggle && <Badge tone="neutral">{m["privacy.badge.off"]()}</Badge>}
            {warn && <Badge tone="warn">{m["privacy.badge.check"]()}</Badge>}
          </div>
          {editTo && (
            <div className="flex flex-wrap items-center gap-1">
              {onMove && (
                <>
                  <Button
                    size="sm"
                    aria-label={m["privacy.row.up"]({ name: pipeline.name })}
                    disabled={first}
                    onClick={() => onMove(-1)}
                  >
                    ↑
                  </Button>
                  <Button
                    size="sm"
                    aria-label={m["privacy.row.down"]({ name: pipeline.name })}
                    disabled={last}
                    onClick={() => onMove(1)}
                  >
                    ↓
                  </Button>
                </>
              )}
              {onToggle && (
                <label className="mr-1 flex items-center gap-2 text-dense">
                  <Switch
                    size="sm"
                    label={m["privacy.row.switch"]({ name: pipeline.name })}
                    checked={pipeline.enabled}
                    onChange={onToggle}
                  />
                  <span aria-hidden className={pipeline.enabled ? "" : MUTED}>
                    {pipeline.enabled ? m["privacy.row.on"]() : m["privacy.row.off"]()}
                  </span>
                </label>
              )}
              <Link to={editTo} className={buttonClasses("secondary", "sm")}>
                {m["privacy.row.edit"]()}
              </Link>
              {onDelete && (
                <Button size="sm" variant="danger" onClick={onDelete}>
                  {m["privacy.row.delete"]()}
                </Button>
              )}
            </div>
          )}
        </div>
        <ol className="m-0 flex list-none flex-col gap-1 p-0 text-body">
          {pipeline.rules.map((rule, index) => (
            <li key={rule.id} className="flex gap-2">
              <span className={`w-5 shrink-0 text-right text-small ${MUTED}`} aria-hidden="true">
                {index + 1}
              </span>
              <span className={rule.enabled ? "" : MUTED}>
                {ruleSentence(kind, rule, pipeline.labels)}
                {!rule.enabled && ` (${m["privacy.badge.off"]()})`}
              </span>
            </li>
          ))}
          <li className="flex gap-2">
            <span className={`w-5 shrink-0 text-right text-small ${MUTED}`} aria-hidden="true">
              ∗
            </span>
            <span>
              {m["privacy.phrase.else"]({
                action: actionPhrase(kind, pipeline.otherwise.action, pipeline.labels),
              })}
            </span>
          </li>
        </ol>
      </div>
    </Card>
  );
}
