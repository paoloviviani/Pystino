import { Badge, Card, Notice, Stat, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { entityLabel, modeLabel } from "../lib/entities";
import type {
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
export function Warnings({ status }: { status: RedactionStatus }) {
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

