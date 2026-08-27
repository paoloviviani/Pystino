import { Badge, Button, Card, Dialog, Input, Notice, Select, Spinner, Stat } from "@llmp/ui";
import { useState } from "react";
import {
  useRedactionStatus,
  useSetRedactionEngine,
  useSetRedactionPolicy,
} from "../lib/admin";
import type {
  EntityMode,
  EntityPolicy,
  RedactionEngineOption,
  RedactionPolicy,
  RedactionStatus,
} from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import styles from "./Admin.module.css";

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
export function AdminRedaction() {
  const status = useRedactionStatus();

  return (
    <div className={styles.page}>
      <PageHeader
        title="Redaction"
        subtitle="What the gateway strips from prompts before they reach a provider, and
          whether the detection service is answering."
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

      <EngineList status={status} />
      <PolicyEditor status={status} />

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

      <Card
        title="Configuration"
        description={
          status.source === "console"
            ? "From the environment this gateway started with, except the engine, which was set here."
            : "From the environment this gateway started with."
        }
      >
        <dl className={styles.details}>
          <Row label="Engine">
            <code className={styles.code}>{status.engine}</code>{" "}
            {/* Worth showing even with one entry: it is how an operator sees
                that installing a plugin worked (ADR 0026). */}
            <span className={styles.muted}>
              · installed: {status.installed_engines.join(", ") || "none"}
            </span>
          </Row>
          {/* Which of the two decided. Invisible otherwise, and it is exactly
              the confusion a database override introduces: an environment
              variable that no longer takes effect looks like a broken one. */}
          <Row label="Set by">
            {status.source === "console" && status.configured ? (
              <>
                This console
                {status.configured.changed_by ? ` · ${status.configured.changed_by}` : ""}
                {" · "}
                {new Date(status.configured.changed_at).toLocaleString()}
                {status.configured.reason && (
                  <div className={styles.muted}>{status.configured.reason}</div>
                )}
              </>
            ) : (
              <>
                The deployment&apos;s environment
                <span className={styles.muted}> · GATEWAY_REDACTION__ENGINE</span>
              </>
            )}
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

/**
 * What each kind of detected entity is worth doing something about (ADR 0037).
 *
 * Two questions per row, not one, and collapsing them is what made this screen
 * hard to write: *what does the model see*, and *what does the reader get back*.
 * A single "redact / do not redact" switch cannot express "the model must not
 * see this name, and the person reading the answer should" — which is the mode
 * almost every deployment wants for almost every entity.
 *
 * The rows come from the detector rather than from a list here. Entity labels
 * belong to whatever engine is installed (ADR 0026), so hard-coding them would
 * mean a console that silently omits a recogniser the engine gained last week —
 * the same failure this feature exists to fix, one level up.
 */
const MODES: { value: EntityMode; label: string; hint: string }[] = [
  { value: "off", label: "Not redacted", hint: "left exactly as the caller wrote it" },
  {
    value: "anonymise_restore",
    label: "Anonymise, restore in the answer",
    hint: "placeholder upstream, real value back to the reader",
  },
  {
    value: "anonymise",
    label: "Anonymise",
    hint: "placeholder upstream and in the answer",
  },
  { value: "redact", label: "Redact", hint: "<PERSON>, so two people look the same" },
];

function PolicyEditor({ status }: { status: RedactionStatus }) {
  const save = useSetRedactionPolicy();
  // Seeded once and then owned by the form. Deriving it from `status` on every
  // render would discard an edit the moment the status query refetched.
  const [draft, setDraft] = useState<RedactionPolicy>(() => structuredClone(status.policy));
  const [allowList, setAllowList] = useState(() => status.policy.allow_list.join(", "));
  const [reason, setReason] = useState("");

  // Every type the detector actually holds, plus anything the policy already
  // names — a rule about a type this engine does not report is still a rule, and
  // dropping it from the screen would silently delete it on the next save.
  const known = [
    ...new Set([...(status.service?.entities ?? []), ...Object.keys(draft.entities)]),
  ].sort();

  const modeOf = (entity: string): EntityMode => draft.entities[entity]?.mode ?? draft.default_mode;

  const setEntity = (entity: string, patch: Partial<EntityPolicy>) =>
    setDraft((current) => ({
      ...current,
      entities: {
        ...current.entities,
        [entity]: {
          mode: patch.mode ?? modeOf(entity),
          threshold: patch.threshold !== undefined ? patch.threshold : (current.entities[entity]?.threshold ?? null),
        },
      },
    }));

  const submit = () =>
    save.mutate(
      {
        policy: {
          ...draft,
          allow_list: allowList
            .split(",")
            .map((item) => item.trim())
            .filter(Boolean),
        },
        reason: reason.trim(),
      },
      { onSuccess: () => setReason("") },
    );

  return (
    <Card
      title="What is redacted"
      description="Per entity type. The detector finds all of them; this decides which ones
        are acted on, and how."
    >
      <div className={styles.form}>
        {save.error ? (
          <Notice tone="danger">
            {save.error instanceof Error ? save.error.message : "Unknown error."}
          </Notice>
        ) : null}
        {save.isSuccess && !save.isPending && (
          <Notice tone="info">
            Saved. Other workers apply it within {Math.round(status.propagation_seconds)}s.
          </Notice>
        )}
        {status.policy_source === "environment" && (
          <Notice tone="info">
            This is the deployment&rsquo;s default policy. Saving here records a decision that
            overrides it, and keeps who changed it and why.
          </Notice>
        )}

        <Select
          label="A type nobody has ruled on"
          value={draft.default_mode}
          onChange={(event) =>
            setDraft((current) => ({
              ...current,
              default_mode: event.target.value as EntityMode,
            }))
          }
          hint="Applies to anything not listed below — including a recogniser the engine
            gains in a later release. Protecting it by default is the safer direction."
        >
          {MODES.map((mode) => (
            <option key={mode.value} value={mode.value}>
              {mode.label}
            </option>
          ))}
        </Select>

        {known.length === 0 ? (
          <p className={styles.muted}>
            The detection service has not reported which entity types it holds, so there is
            nothing to list. The default above still applies to everything it finds.
          </p>
        ) : (
          <div className={styles.checkList}>
            {known.map((entity) => (
              <div key={entity} className={styles.formRow}>
                <Select
                  label={entity}
                  value={modeOf(entity)}
                  onChange={(event) =>
                    setEntity(entity, { mode: event.target.value as EntityMode })
                  }
                >
                  {MODES.map((mode) => (
                    <option key={mode.value} value={mode.value}>
                      {mode.label}
                    </option>
                  ))}
                </Select>
                <Input
                  label={`Confidence for ${entity}`}
                  hideLabel
                  type="number"
                  min="0"
                  max="1"
                  step="0.05"
                  placeholder={`${status.score_threshold} (default)`}
                  value={draft.entities[entity]?.threshold ?? ""}
                  onChange={(event) =>
                    setEntity(entity, {
                      threshold: event.target.value === "" ? null : Number(event.target.value),
                    })
                  }
                />
              </div>
            ))}
          </div>
        )}

        <Input
          label="Never redact these values"
          value={allowList}
          onChange={(event) => setAllowList(event.target.value)}
          placeholder="ilpost.it, example.org"
          hint="Comma separated, matched exactly and case-insensitively. For values a
            detector is right about the shape of and wrong about the meaning of — a
            corporate domain is a URL, and it identifies nobody."
        />

        <Input
          label="Reason for this change"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="why this changed"
          hint="Required when the change protects less: a type switched off, a mode
            downgraded, or a value exempted. Kept permanently, like an engine change."
        />

        <div>
          <Button variant="primary" busy={save.isPending} onClick={submit}>
            Save policy
          </Button>
        </div>
      </div>
    </Card>
  );
}


/**
 * The installed engines, and which one is in force.
 *
 * Switching is a two-step for one case only: an engine that redacts nothing
 * needs a written reason, because that is the change which makes the system
 * quietly stop protecting anything and the reason is kept permanently. Every
 * other switch is one click — asking for a justification to *turn protection on*
 * would be friction with no reader.
 */
function EngineList({ status }: { status: RedactionStatus }) {
  const setEngine = useSetRedactionEngine();
  const [confirming, setConfirming] = useState<RedactionEngineOption | null>(null);

  const choose = (engine: RedactionEngineOption) => {
    if (!engine.redacts) {
      setConfirming(engine);
      return;
    }
    setEngine.mutate({ engine: engine.name, reason: "" });
  };

  return (
    <Card
      title="Engine"
      description="One is in force at a time. Installing an engine through the llmp.redactors
        entry point adds it here."
    >
      {setEngine.error ? (
        <Notice tone="danger" title="The engine was not changed">
          {setEngine.error instanceof Error ? setEngine.error.message : "Unknown error."}
        </Notice>
      ) : null}

      {/* Stated rather than implied. A change that looks instant and is not is
          worse than one that says how long it takes. */}
      {setEngine.isSuccess && status.propagation_seconds > 0 && (
        <Notice tone="info">
          Saved. This worker switched immediately; any other worker picks it up within{" "}
          {status.propagation_seconds} seconds.
        </Notice>
      )}

      <div className={styles.engineList}>
        {status.engines.map((engine) => (
          <div
            key={engine.name}
            className={`${styles.engine} ${engine.is_active ? styles.engineActive : ""}`}
          >
            <div className={styles.engineBody}>
              <div className={styles.engineName}>
                {engine.label}
                <code className={styles.code}>{engine.name}</code>
                {engine.is_active && <Badge tone="accent">In force</Badge>}
                {/* The one property that decides whether this screen means
                    anything, said on every row rather than only on the
                    active one. */}
                {!engine.redacts && <Badge tone="danger">Redacts nothing</Badge>}
              </div>
              <div className={styles.muted}>{engine.description}</div>
              {/* Server-computed, rendered verbatim: the wording lives with the
                  rule, the same convention as the warnings above. */}
              {engine.blocked_reason && (
                <div className={styles.muted}>
                  <strong>Cannot be enabled.</strong> {engine.blocked_reason}
                </div>
              )}
            </div>
            <div className={styles.engineAction}>
              {engine.is_active ? (
                <Button variant="secondary" disabled>
                  Enabled
                </Button>
              ) : (
                <Button
                  variant={engine.redacts ? "primary" : "secondary"}
                  disabled={engine.blocked_reason !== null || setEngine.isPending}
                  onClick={() => choose(engine)}
                >
                  {engine.redacts ? "Enable" : "Turn redaction off"}
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
        onConfirm={(reason) =>
          confirming &&
          setEngine.mutate(
            { engine: confirming.name, reason },
            { onSuccess: () => setConfirming(null) },
          )
        }
      />
    </Card>
  );
}

/**
 * The one switch that needs a sentence typed out.
 *
 * The reason is a required field rather than a checkbox saying "I understand",
 * because a checkbox produces no record. This one is stored on an append-only
 * row and is what a data-protection review reads six months later.
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
  onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");

  return (
    <Dialog
      open={engine !== null}
      title="Turn redaction off"
      onClose={onCancel}
      footer={
        <>
          <Button variant="secondary" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            variant="danger"
            disabled={reason.trim().length === 0 || pending}
            onClick={() => onConfirm(reason.trim())}
          >
            {pending ? "Saving…" : "Turn it off"}
          </Button>
        </>
      }
    >
      <Notice tone="danger">
        Prompts will reach providers exactly as callers sent them. Nothing is stripped, and
        nothing about a request already sent is changed.
      </Notice>
      <Input
        label="Reason"
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        placeholder="e.g. detection service migration, 24h window agreed with the DPO"
        hint="Kept permanently, with who made the change and when. This is the record a
          later review reads."
      />
    </Dialog>
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
