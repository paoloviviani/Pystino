import { Badge, Card, Notice, Spinner, Stat } from "@llmp/ui";
import { useRedactionStatus } from "../lib/admin";
import type { RedactionStatus } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import styles from "./Admin.module.css";

/**
 * What the redaction layer is doing, right now.
 *
 * Read-only, because redaction is process configuration read at startup
 * (ADR 0012) — there is nothing on this page an operator could change even if
 * the form existed. Making it configurable, and scopeable per model, provider,
 * user or group, is specified in docs/redaction-scoping-plan.md and needs a
 * database row rather than more environment variables.
 *
 * Until this screen existed the console could not answer the first question
 * anyone asks — is redaction on at all — and the answer is not derivable from
 * anywhere else in the UI. That is why the *evidence* is given as much room as
 * the configuration: a layer that is switched on and detecting nothing looks
 * exactly like one that has nothing to find, and only the entity count tells
 * them apart.
 */
export function AdminRedaction() {
  const status = useRedactionStatus();

  return (
    <div className={styles.page}>
      <PageHeader
        title="Redaction"
        subtitle="What the gateway strips from prompts before they reach a provider, and
          whether the detection service is answering. Configured per deployment, not here."
      />

      {status.isPending && <Spinner label="Reading the redaction configuration" />}
      {status.error ? (
        <Notice tone="danger" title="Could not read the redaction configuration">
          {status.error instanceof Error ? status.error.message : "Unknown error."}
        </Notice>
      ) : null}

      {status.data && <Detail status={status.data} />}
    </div>
  );
}

function Detail({ status }: { status: RedactionStatus }) {
  const { service, activity } = status;

  return (
    <>
      {/* The API's own wording, rendered verbatim. Restating a caveat in the UI
          is how the two end up disagreeing about what is wrong. */}
      {status.warnings.map((note) => (
        <Notice key={note} tone={status.enabled ? "warn" : "danger"}>
          {note}
        </Notice>
      ))}

      <Card>
        <div className={styles.stats}>
          <Stat
            label="Redaction"
            value={status.enabled ? "On" : "Off"}
            detail={
              status.enabled
                ? `${status.engine} engine`
                : "prompts reach the provider unchanged"
            }
          />
          <Stat
            label="Detection service"
            value={
              service === null ? "—" : service.reachable ? "Answering" : "Not answering"
            }
            detail={
              service === null
                ? "this engine has no service to call"
                : service.reachable
                  ? `${service.engine ?? "unknown"} ${service.engine_version ?? ""} · ${service.latency_ms}ms`
                  : service.detail
            }
          />
          <Stat
            label="Entities removed"
            value={activity.entities_redacted.toLocaleString()}
            detail={`from ${activity.requests_redacted.toLocaleString()} of ${activity.requests.toLocaleString()} requests, last ${hours(activity.window_seconds)}`}
          />
        </div>
      </Card>

      <Card title="Configuration" description="From the environment this gateway started with.">
        <dl className={styles.details}>
          <Row label="Engine">
            <code className={styles.code}>{status.engine}</code>{" "}
            {/* Worth showing even with one entry: it is how an operator sees
                that installing a plugin worked (ADR 0026). */}
            <span className={styles.muted}>
              · installed: {status.installed_engines.join(", ") || "none"}
            </span>
          </Row>
          <Row label="Detection endpoint">
            {status.endpoint ? (
              <code className={styles.code}>{status.endpoint}</code>
            ) : (
              <span className={styles.muted}>none — in-process engine</span>
            )}
          </Row>
          <Row label="On detection failure">
            {status.fail_open ? (
              <Badge tone="danger">Forward unredacted</Badge>
            ) : (
              <Badge tone="ok">Refuse the request</Badge>
            )}
          </Row>
          <Row label="Response">
            {status.restore_in_response
              ? "Placeholders swapped back to the original values"
              : "Placeholders left in place"}
          </Row>
          <Row label="Language">
            {status.language}
            {service?.models?.[status.language] && (
              <span className={styles.muted}> · {service.models[status.language]}</span>
            )}
          </Row>
          <Row label="Score threshold">{status.score_threshold}</Row>
          <Row label="Timeout">{status.timeout_seconds}s</Row>
          <Row label="Detection cache">
            {status.cache_size === 0 ? "disabled" : `${status.cache_size.toLocaleString()} texts`}
          </Row>
          <Row label="Placeholder key">
            {status.placeholder_key_set ? (
              <Badge tone="ok">Set</Badge>
            ) : (
              <Badge tone="warn">Not set</Badge>
            )}{" "}
            <span className={styles.muted}>
              placeholders derive from it, so it must stay stable
            </span>
          </Row>
        </dl>
      </Card>

      <Card
        title="Entity types"
        description={
          status.entity_types
            ? "Only these are looked for. Anything else is left in the prompt."
            : "Every type the engine offers is looked for; nothing is filtered out."
        }
      >
        {/* Null and empty are different facts, and conflating them would be the
            difference between "everything" and "nothing". */}
        <div className={styles.chips}>
          {(status.entity_types ?? service?.entities ?? []).map((entity) => (
            <Badge key={entity}>{entity}</Badge>
          ))}
        </div>
        {status.entity_types === null && service === null && (
          <p className={styles.muted}>
            The engine offers no list, so what it detects cannot be shown here.
          </p>
        )}
      </Card>

      {service?.reachable && service.degraded_languages.length > 0 && (
        <Card title="Degraded languages" description="Served without a named-entity model.">
          <div className={styles.chips}>
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

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className={styles.detailLabel}>{label}</dt>
      <dd className={styles.detailValue}>{children}</dd>
    </>
  );
}

/** The activity window, in the unit a person would say it in. */
function hours(seconds: number): string {
  if (seconds % 86_400 === 0) {
    const days = seconds / 86_400;
    return days === 1 ? "24 hours" : `${days} days`;
  }
  const value = Math.round(seconds / 3600);
  return value === 1 ? "hour" : `${value} hours`;
}
