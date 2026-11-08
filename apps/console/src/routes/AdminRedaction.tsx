import { Badge, Button, Card, Dialog, Notice, Stat, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import { useSetRedactionEngine } from "../lib/admin";
import { entityLabel, modeLabel } from "../lib/entities";
import { useOptionalToast } from "../lib/toast";
import type {
  RedactionEngineOption,
  RedactionPreview,
  RedactionPreviewSpan,
  RedactionStatus,
} from "../lib/types";
import { formatNumber, formatDateTime } from "../lib/format";
import { m } from "../paraglide/messages.js";
import {
  CHIPS,
  CODE,
  DETAILS,
  DETAIL_LABEL,
  DETAIL_VALUE,
  ENGINE,
  ENGINE_ACTION,
  ENGINE_TEXT,
  ENGINE_ACTIVE,
  ENGINE_BODY,
  ENGINE_NAME,
  FIELD,
  FIELD_LABEL,
  FORM,
  MUTED,
  NOWRAP,
  SAMPLE,
  STATS,
} from "../lib/layout";


/**
 * What the redaction layer is doing, and which engine it is doing it with.
 *
 * Mostly read-only still. The one thing an admin can change here is **which
 * installed engine is in force** (ADR 0033); the endpoint, the placeholder key
 * and the detection parameters remain deployment configuration, and per-scope
 * rules are still specified in docs/redaction-scoping-plan.md.
 *
 * Until this screen existed the console could not answer the first question
 * anyone asks — is redaction on at all — and the answer is not derivable from
 * anywhere else in the UI. That is why the *evidence* is given as much room as
 * the configuration: a layer that is switched on and detecting nothing looks
 * exactly like one that has nothing to find, and only the entity count tells
 * them apart.
 *
 * The engine list is deliberately a list of described choices and not a
 * dropdown. What an operator is choosing between is not two names — it is
 * "strips personal data before it leaves" versus "does not", and only the
 * description says which is which. `blocked_reason` comes from the API for the
 * same reason the warnings do: the rule that decides whether an engine can run
 * is the engine's own, and re-deriving it here would mean the browser knowing
 * which settings each engine needs.
 */
function Warnings({ status }: { status: RedactionStatus }) {
  return (
    <>
      {/* i18n: server text — the API's own wording, rendered verbatim.
          Restating a caveat in the UI is how the two end up disagreeing about
          what is wrong. */}
      {status.warnings.map((note) => (
        <Notice key={note} tone={status.enabled ? "warn" : "danger"}>
          {note}
        </Notice>
      ))}
    </>
  );
}

/**
 * A titled block inside a profile card: the engine choice and the detection
 * families used to be cards of their own; they are settings of the profile now.
 */
function Section({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 border-t border-line-quiet pt-3">
      <div>
        <h3 className="m-0 text-body font-semibold">{title}</h3>
        <p className={`m-0 text-small ${MUTED}`}>{description}</p>
      </div>
      {children}
    </div>
  );
}

/**
 * The engine choice and the Presidio detection families, for the profile card
 * that is the Presidio engine. The same calls and the same append-only row as
 * before; only where they are shown changed.
 */
export function EngineSettings({ status }: { status: RedactionStatus }) {
  return (
    <>
      <Warnings status={status} />
      <EngineList status={status} />
      {status.installed_engines.includes("http") && (
        <DetectorFamilies
          inForce={status.engine === "http"}
          key={`${status.engine}-${status.presidio_pattern_matching ?? "default"}-${status.presidio_ner ?? "default"}`}
          status={status}
        />
      )}
    </>
  );
}

/** The activity and configuration read-out, behind a disclosure on the Detectors tab. */
export function EngineDetails({ status }: { status: RedactionStatus }) {
  return (
    <details>
      <summary className="cursor-pointer text-small font-medium">
        {m["privacy.detectors.engine_details"]()}
      </summary>
      <div className="mt-3 flex flex-col gap-4">
        <Detail status={status} />
      </div>
    </details>
  );
}

function Detail({ status }: { status: RedactionStatus }) {
  const { service, activity } = status;

  return (
    <>
      <Card>
        <div className={STATS}>
          <Stat
            label={m["admin_redaction.stat.redaction"]()}
            value={
              status.enabled
                ? m["admin_redaction.stat.on"]()
                : m["admin_redaction.stat.off"]()
            }
            detail={
              status.enabled
                ? m["admin_redaction.stat.engine_detail"]({ engine: status.engine })
                : m["admin_redaction.stat.off_detail"]()
            }
          />
          <Stat
            label={m["admin_redaction.stat.service"]()}
            value={
              service === null
                ? m["admin_redaction.stat.service_unknown"]()
                : service.reachable
                  ? m["admin_redaction.stat.service_ok"]()
                  : m["admin_redaction.stat.service_down"]()
            }
            detail={
              service === null
                ? m["admin_redaction.stat.service_none"]()
                : service.reachable
                  ? // i18n: server text — the engine's own name, version and latency.
                    `${service.engine ?? "unknown"} ${service.engine_version ?? ""} · ${service.latency_ms}ms`
                  : service.detail
            }
          />
          <Stat
            label={m["admin_redaction.stat.entities"]()}
            value={formatNumber(activity.entities_redacted)}
            detail={m["admin_redaction.stat.entities_detail"]({
              redacted: activity.requests_redacted,
              requests: activity.requests,
              window:
                activity.window_seconds % 86_400 === 0
                  ? m["admin_redaction.activity.window_days"]({
                      count: activity.window_seconds / 86_400,
                    })
                  : m["admin_redaction.activity.window_hours"]({
                      count: Math.round(activity.window_seconds / 3600),
                    })}
            )}
          />
        </div>
      </Card>

      {/* No "Response" row and no score threshold. Both were process-wide
          settings that stopped being the answer when policy became per entity
          (ADR 0037): restoring is `anonymise_restore` against `anonymise`, per
          type, and a threshold is a column on each type in each rule. Reporting
          one number here read as *the* threshold, which it is not. */}
      <Card
        title={m["admin_redaction.config.card"]()}
        description={
          status.source === "console"
            ? m["admin_redaction.config.description_console"]()
            : m["admin_redaction.config.description_env"]()
        }
      >
        <dl className={DETAILS}>
          <Row label={m["admin_redaction.config.engine"]()}>
            <code className={CODE}>{status.engine}</code>{" "}
            {/* Worth showing even with one entry: it is how an operator sees
                that installing a plugin worked (ADR 0026). */}
            <span className={MUTED}>
              {m["admin_redaction.config.installed"]({
                engines: status.installed_engines.join(", ") || m["admin_redaction.config.none_engines"](),
              })}
            </span>
          </Row>
          {/* Which of the two decided. Invisible otherwise, and it is exactly
              the confusion a database override introduces: an environment
              variable that no longer takes effect looks like a broken one. */}
          <Row label={m["admin_redaction.config.source"]()}>
            {status.source === "console" && status.configured ? (
              <>
                {m["admin_redaction.config.source_console"]()}
                {/* i18n: server text — who changed it, and the reason they gave. */}
                {status.configured.changed_by ? ` · ${status.configured.changed_by}` : ""}
                {" · "}
                {formatDateTime(status.configured.changed_at)}
                {status.configured.reason && (
                  <div className={MUTED}>{status.configured.reason}</div>
                )}
              </>
            ) : (
              <>
                {m["admin_redaction.config.source_env"]()}
                <span className={MUTED}>
                  {/* i18n: literal — the setting's own name, which an operator
                      greps for. */} · GATEWAY_REDACTION__ENGINE
                </span>
              </>
            )}
          </Row>
          <Row label={m["admin_redaction.config.endpoint"]()} presidioOnly={status.engine !== "http"}>
            {status.endpoint ? (
              <code className={CODE}>{status.endpoint}</code>
            ) : (
              <span className={MUTED}>{m["admin_redaction.config.endpoint_none"]()}</span>
            )}
          </Row>
          <Row label={m["admin_redaction.config.failure_mode"]()}>
            {status.fail_open ? (
              <Badge tone="danger">{m["admin_redaction.config.fail_open"]()}</Badge>
            ) : (
              <Badge tone="ok">{m["admin_redaction.config.fail_closed"]()}</Badge>
            )}
          </Row>
          <Row label={m["admin_redaction.config.language"]()} presidioOnly={status.engine !== "http"}>
            {status.language}
            {service?.models?.[status.language] && (
              <span className={MUTED}> · {service.models[status.language]}</span>
            )}
          </Row>
          <Row label={m["admin_redaction.config.timeout"]()} presidioOnly={status.engine !== "http"}>
            {m["admin_redaction.config.timeout_value"]({ seconds: status.timeout_seconds })}
          </Row>
          <Row label={m["admin_redaction.config.cache"]()} presidioOnly={status.engine !== "http"}>
            {status.cache_size === 0
              ? m["admin_redaction.config.cache_disabled"]()
              : m["admin_redaction.config.cache_texts"]({ count: status.cache_size })}
          </Row>
          <Row label={m["admin_redaction.config.placeholder_key"]()}>
            {status.placeholder_key_set ? (
              <Badge tone="ok">{m["admin_redaction.config.key_set"]()}</Badge>
            ) : (
              <Badge tone="warn">{m["admin_redaction.config.key_not_set"]()}</Badge>
            )}{" "}
            <span className={MUTED}>{m["admin_redaction.config.key_note"]()}</span>
          </Row>
        </dl>
      </Card>

      {/* Only when the deprecated environment variable is set, and then only to
          explain where the policy below came from. Two cards answering "what is
          looked for" in different vocabularies is the confusion this screen was
          rebuilt to remove: the policy editor lists every type the detector
          holds, with what happens to each. Null and a list are still different
          facts — null means every type — but the policy is where that shows now. */}
      {status.entity_types && (
        <Card
          title={m["admin_redaction.entity_types.card"]()}
          description={m["admin_redaction.entity_types.description"]()}
        >
          <div className={CHIPS}>
            {status.entity_types.map((entity) => (
              <Badge key={entity}>{entity}</Badge>
            ))}
          </div>
        </Card>
      )}

      {service?.reachable && service.degraded_languages.length > 0 && (
        <Card
          title={m["admin_redaction.degraded.card"]()}
          description={m["admin_redaction.degraded.description"]()}
        >
          <div className={CHIPS}>
            {service.degraded_languages.map((language) => (
              <Badge key={language} tone="warn">
                {language}
              </Badge>
            ))}
          </div>
        </Card>
      )}
    </>
  );
}

/**
 * The deployment's policy: what each kind of detected entity is worth doing
 * something about (ADR 0037).
 *
 * The form itself is `PolicyFields`, shared with a scoped rule and with a
 * person's own policy. What lives here is the part that is only true of the
 * deployment policy: it is the floor everything else folds onto, so a change to
 * it is the one that can protect *less*, and that is what the reason field is
 * for.
 */
export function RedactionPreviewResult({
  result,
  sample,
}: {
  result: RedactionPreview;
  sample: string;
}) {
  const columns: Column<RedactionPreviewSpan>[] = [
    {
      key: "match",
      header: m["admin_redaction.preview.column.match"](),
      render: (span) => <code className={CODE}>{sample.slice(span.start, span.end)}</code>,
    },
    {
      key: "entity",
      header: m["admin_redaction.preview.column.detected"](),
      render: (span) => (
        <>
          <div>{entityLabel(span.entity_type)}</div>
          <div className={`${MUTED} ${CODE}`}>{span.entity_type}</div>
        </>
      ),
    },
    {
      key: "score",
      header: m["admin_redaction.preview.column.score"](),
      numeric: true,
      render: (span) => (
        <>
          {/* Two decimal places is what a detector score means here; a score
              is not a count, so it is not `formatNumber`. */}
          <div>{span.score.toFixed(2)}</div>
          <div className={`${MUTED} ${NOWRAP}`}>
            {m["admin_redaction.preview.needs"]({ threshold: span.threshold })}
          </div>
        </>
      ),
    },
    {
      key: "mode",
      header: m["admin_redaction.preview.column.applied"](),
      render: (span) =>
        span.allow_listed ? (
          <Badge tone="warn">{m["admin_redaction.preview.allow_listed"]()}</Badge>
        ) : (
          modeLabel(span.mode)
        ),
    },
  ];

  return (
    <div className={FORM}>
            {/* i18n: server text — rendered verbatim: an engine that detects
                nothing must say so, or an empty result reads as a clean prompt. */}
            {result.note && <Notice tone="warn">{result.note}</Notice>}

            {result.blocked ? (
              <Notice tone="danger" title={m["admin_redaction.preview.blocked_title"]()}>
                {/* i18n: server text — the block reason is the API's. */}
                {result.blocked_reason ?? m["admin_redaction.preview.blocked_fallback"]()}
              </Notice>
            ) : (
              <div className={FIELD}>
                <span className={FIELD_LABEL}>{m["admin_redaction.preview.receives"]()}</span>
                {/* i18n: server text — the rewritten sample is the answer. */}
                <p className={SAMPLE}>{result.redacted_text}</p>
              </div>
            )}

            <p className={MUTED}>
              {m["admin_redaction.preview.summary_replaced"]({ count: result.entity_count })} ·{" "}
              {m["admin_redaction.preview.summary_engine"]({ engine: result.engine })} ·{" "}
              {m["admin_redaction.preview.summary_policy"]()}
            </p>

            <div className="overflow-x-auto">
            <Table
              columns={columns}
              rows={result.spans}
              rowKey={(span, index) => `${span.entity_type}-${span.start}-${index}`}
              empty={m["admin_redaction.preview.empty"]()}
              caption={m["admin_redaction.preview.caption"]()}
            />
            </div>
    </div>
  );
}

/**
 * The two Presidio recognizer families as big on/off switches.
 *
 * Pattern matching finds structured identifiers; named-entity recognition
 * reads names and places. Each switch starts from the deployment default —
 * the explicit console choice when one was saved, otherwise whether the
 * detector build actually offers that family — and flipping one saves
 * immediately to the same append-only engine row as the engine itself, so the
 * report of who changed redaction stays in one place. There is no third
 * "default" state to pick: the default is only the starting position.
 *
 * Custom gateway patterns are policy rather than detector families, so a
 * rule's own patterns still run whatever these say. Rule building reads the
 * detector's effective entity list, which is why a family switched off here
 * removes its entity types from a new rule — and why a rule that still names
 * one carries a warning instead of failing.
 */
function DetectorFamilies({ status, inForce }: { status: RedactionStatus; inForce: boolean }) {
  const setEngine = useSetRedactionEngine();
  const toast = useOptionalToast();
  const service = status.service;
  // Whether the detector build offers each family at all. Only consulted when
  // no explicit choice was saved: a slim no-NER build then starts its model
  // switch off rather than pretending names are detected.
  const offersPatterns =
    service === null || !service.reachable || !service.family_partition
      ? null
      : service.pattern_entities.length > 0;
  const offersNer =
    service === null || !service.reachable || !service.family_partition
      ? null
      : service.model_entities.length > 0;
  const [patterns, setPatterns] = useState<boolean>(
    status.presidio_pattern_matching ?? offersPatterns ?? true,
  );
  const [ner, setNer] = useState<boolean>(
    status.presidio_ner ?? offersNer ?? true,
  );

  const toggle = (which: "patterns" | "ner", next: boolean) => {
    if (!inForce) return;
    const prevPatterns = patterns;
    const prevNer = ner;
    const nextPatterns = which === "patterns" ? next : patterns;
    const nextNer = which === "ner" ? next : ner;
    setPatterns(nextPatterns);
    setNer(nextNer);
    setEngine.mutate(
      {
        engine: status.engine,
        reason: status.configured?.reason ?? "",
        presidio_pattern_matching: nextPatterns,
        presidio_ner: nextNer,
      },
      {
        onSuccess: () =>
          toast?.add({ title: m["admin_redaction.families.toast_saved"](), type: "success" }),
        onError: (error) => {
          // Back to what the server still holds: an optimistic switch that
          // failed must not sit showing a state that is not in force.
          setPatterns(prevPatterns);
          setNer(prevNer);
          toast?.add({
            title: m["admin_redaction.families.toast_failed"](),
            // i18n: server text — the API's own error message.
            description: error instanceof Error ? error.message : m["admin_redaction.unknown_error"](),
            type: "error",
          });
        },
      },
    );
  };

  return (
    <Section
      title={m["admin_redaction.families.card"]()}
      description={m["admin_redaction.families.description"]()}
    >
      {/* Stored values stay shown so they apply once Presidio is enabled; the
          section is dimmed because nothing here is in force until then. */}
      {!inForce && <p className={MUTED}>{m["admin_redaction.families.inactive_note"]()}</p>}
      {setEngine.error ? (
        <Notice tone="danger" title={m["admin_redaction.families.toast_failed"]()}>
          {/* i18n: server text — the API's own error message. */}
          {setEngine.error instanceof Error
            ? setEngine.error.message
            : m["admin_redaction.unknown_error"]()}
        </Notice>
      ) : null}

      <div className={`flex flex-col gap-4${inForce ? "" : " opacity-60"}`}>
        <FamilySwitch
          label={m["admin_redaction.families.patterns"]()}
          description={m["admin_redaction.families.patterns_description"]()}
          checked={patterns}
          pending={setEngine.isPending}
          inactive={!inForce}
          onToggle={(next) => toggle("patterns", next)}
        />
        <FamilySwitch
          label={m["admin_redaction.families.ner"]()}
          description={m["admin_redaction.families.ner_description"]()}
          checked={ner}
          pending={setEngine.isPending}
          inactive={!inForce}
          onToggle={(next) => toggle("ner", next)}
        />
      </div>

      {setEngine.isPending && <p className={MUTED}>{m["admin_redaction.families.saving"]()}</p>}
    </Section>
  );
}

/**
 * One big on/off switch.
 *
 * A `role="switch"` button rather than a checkbox: the control reads as the
 * thing it is — a state flip that saves immediately — and the visible On/Off
 * text is hidden from assistive tech because `aria-checked` already says it.
 */
function FamilySwitch({
  label,
  description,
  checked,
  pending,
  inactive,
  onToggle,
}: {
  label: string;
  description: string;
  checked: boolean;
  pending: boolean;
  inactive: boolean;
  onToggle: (next: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="min-w-0">
        <div className="font-semibold">{label}</div>
        <div className={MUTED}>{description}</div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <span aria-hidden className={`text-body ${MUTED}`}>
          {checked ? m["admin_redaction.families.on"]() : m["admin_redaction.families.off"]()}
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={checked}
          aria-label={label}
          disabled={pending || inactive}
          aria-disabled={inactive || undefined}
          onClick={() => onToggle(!checked)}
          className="relative h-9 w-16 shrink-0 cursor-pointer rounded-full border border-line-strong bg-sunken transition-colors data-[checked=true]:border-accent data-[checked=true]:bg-accent disabled:cursor-not-allowed disabled:opacity-60 focus-visible:outline-none focus-visible:shadow-focus"
          data-checked={checked}
        >
          <span
            aria-hidden
            className={`absolute top-1 left-1 size-7 rounded-full border border-line-strong bg-surface transition-transform ${checked ? "translate-x-7" : "translate-x-0"}`}
          />
        </button>
      </div>
    </div>
  );
}

/**
 * The installed engines, and which one is in force.
 *
 * Switching is a two-step for one case only: an engine that redacts nothing
 * gets a confirmation, because that is the change which makes the system
 * quietly stop protecting anything. It used to demand a written reason as
 * well; that was dropped on the grounds that a sentence typed to get past a
 * dialog is not an audit trail, and the row still records the engine, who
 * changed it and when. Every other switch is one click — asking anyone to
 * justify *turning protection on* would be friction with no reader.
 */
function EngineList({ status }: { status: RedactionStatus }) {
  const setEngine = useSetRedactionEngine();
  const toast = useOptionalToast();
  const [confirming, setConfirming] = useState<RedactionEngineOption | null>(null);

  const choose = (engine: RedactionEngineOption) => {
    if (!engine.redacts) {
      setConfirming(engine);
      return;
    }
    setEngine.mutate(
      { engine: engine.name, reason: "" },
      {
        onSuccess: () =>
          toast?.add({
            // i18n: server text — the engine's own label, from its plugin.
            title: m["admin_redaction.engine.toast_in_force"]({ label: engine.label }),
            type: "success",
          }),
        onError: (error) =>
          toast?.add({
            title: m["admin_redaction.engine.toast_failed"](),
            // i18n: server text — the API's own error message.
            description: error instanceof Error ? error.message : m["admin_redaction.unknown_error"](),
            type: "error",
          }),
      },
    );
  };

  return (
    <Section
      title={m["admin_redaction.engine.card"]()}
      description={m["admin_redaction.engine.description"]()}
    >
      {setEngine.error ? (
        <Notice tone="danger" title={m["admin_redaction.engine.toast_failed"]()}>
          {/* i18n: server text — the API's own error message. */}
          {setEngine.error instanceof Error
            ? setEngine.error.message
            : m["admin_redaction.unknown_error"]()}
        </Notice>
      ) : null}

      {/* Stated rather than implied. A change that looks instant and is not is
          worse than one that says how long it takes. */}
      {setEngine.isSuccess && status.propagation_seconds > 0 && (
        <Notice tone="info">
          {m["admin_redaction.engine.propagated"]({ seconds: status.propagation_seconds })}
        </Notice>
      )}

      <div className="flex flex-col gap-3">
        {status.engines.map((engine) => (
          <div
            key={engine.name}
            className={engine.is_active ? `${ENGINE} ${ENGINE_ACTIVE}` : ENGINE}
          >
            <div className={ENGINE_BODY}>
              <div className={ENGINE_NAME}>
                {engine.label}
                <code className={CODE}>{engine.name}</code>
                {engine.is_active && <Badge tone="accent">{m["admin_redaction.badge.in_force"]()}</Badge>}
                {/* The one property that decides whether this screen means
                    anything, said on every row rather than only on the
                    active one. */}
                {!engine.redacts && (
                  <Badge tone="danger">{m["admin_redaction.badge.redacts_nothing"]()}</Badge>
                )}
              </div>
              {/* i18n: server text — the engine's own description, from its plugin. */}
              <div className={ENGINE_TEXT}>{engine.description}</div>
              {/* i18n: server text — rendered verbatim: the wording lives with
                  the rule, the same convention as the warnings above. */}
              {engine.blocked_reason && (
                <div className={ENGINE_TEXT}>
                  <strong>{m["admin_redaction.engine.cannot_enable"]()}</strong>{" "}
                  {engine.blocked_reason}
                </div>
              )}
            </div>
            <div className={ENGINE_ACTION}>
              {engine.is_active ? (
                <Button variant="secondary" disabled>
                  {m["admin_redaction.engine.enabled"]()}
                </Button>
              ) : (
                <Button
                  variant={engine.redacts ? "primary" : "secondary"}
                  disabled={engine.blocked_reason !== null || setEngine.isPending}
                  onClick={() => choose(engine)}
                >
                  {engine.redacts
                    ? m["admin_redaction.engine.enable"]()
                    : m["admin_redaction.engine.turn_off"]()}
                </Button>
              )}
            </div>
          </div>
        ))}
      </div>

      <ConfirmOff
        engine={confirming}
        pending={setEngine.isPending}
        onCancel={() => setConfirming(null)}
        onConfirm={() =>
          confirming &&
          setEngine.mutate(
            { engine: confirming.name, reason: "" },
            {
              onSuccess: () => {
                setConfirming(null);
                toast?.add({ title: m["admin_redaction.engine.off_toast"](), type: "success" });
              },
              onError: (error) =>
                toast?.add({
                  title: m["admin_redaction.engine.toast_failed"](),
                  // i18n: server text — the API's own error message.
                  description: error instanceof Error
                    ? error.message
                    : m["admin_redaction.unknown_error"](),
                  type: "error",
                }),
            },
          )
        }
      />
    </Section>
  );
}

/**
 * The one switch that stops and asks.
 *
 * Confirmation without a form: what this change does is worth a sentence *to*
 * the operator, not one *from* them. The append-only row still records which
 * engine, who chose it and when, which is the part a later review can rely on.
 */
function ConfirmOff({
  engine,
  pending,
  onCancel,
  onConfirm,
}: {
  engine: RedactionEngineOption | null;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog
      open={engine !== null}
      title={m["admin_redaction.confirm.title"]()}
      onClose={onCancel}
      footer={
        <>
          <Button variant="secondary" onClick={onCancel}>
            {m["admin_redaction.confirm.cancel"]()}
          </Button>
          <Button variant="danger" disabled={pending} onClick={onConfirm}>
            {pending
              ? m["admin_redaction.confirm.saving"]()
              : m["admin_redaction.confirm.turn_off"]()}
          </Button>
        </>
      }
    >
      <Notice tone="danger">{m["admin_redaction.confirm.warning"]()}</Notice>
      <p className="m-0 text-body text-ink-muted">{m["admin_redaction.confirm.recorded"]()}</p>
    </Dialog>
  );
}

function Row({
  label,
  children,
  presidioOnly = false,
}: {
  label: string;
  children: React.ReactNode;
  /** A Presidio-engine setting while another engine is in force: dimmed and tagged. */
  presidioOnly?: boolean;
}) {
  return (
    <>
      <dt className={presidioOnly ? `${DETAIL_LABEL} opacity-60` : DETAIL_LABEL}>
        {label}
        {presidioOnly && <span className={MUTED}> {m["admin_redaction.config.presidio_only"]()}</span>}
      </dt>
      <dd className={presidioOnly ? `${DETAIL_VALUE} opacity-60` : DETAIL_VALUE}>{children}</dd>
    </>
  );
}

