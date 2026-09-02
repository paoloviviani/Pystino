import {
  Badge,
  Button,
  Card,
  Dialog,
  Input,
  Notice,
  Select,
  Spinner,
  Stat,
  Table,
  
} from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useId, useState } from "react";
import { Link, useNavigate } from "react-router";
import {
  useDeleteRedactionRule,
  usePreviewRedaction,
  useRedactionStatus,
  useRedactionRules,
  useSetRedactionEngine,
  useUpdateRedactionRule,
} from "../lib/admin";
import { entityLabel, modeLabel, summarisePolicy } from "../lib/entities";
import { useOptionalToast } from "../lib/toast";
import type {
  RedactionEngineOption,
  RedactionPreviewSpan,
  RedactionScope,
  RedactionStatus,
  RedactionRule,
} from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { SCOPES, SubjectPicker, scopeNoun } from "../components/SubjectPicker";
import {
  CHIPS,
  CODE,
  DETAILS,
  DETAIL_LABEL,
  DETAIL_VALUE,
  ENGINE,
  ENGINE_ACTION,
  ENGINE_ACTIVE,
  ENGINE_BODY,
  ENGINE_NAME,
  FIELD,
  FIELD_LABEL,
  FORM,
  FORM_ROW,
  MUTED,
  NOWRAP,
  PAGE,
  ROW_ACTIONS,
  SAMPLE,
  STATS,
  TEXTAREA,
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
export function AdminRedaction() {
  const status = useRedactionStatus();

  return (
    <div className={PAGE}>
      <PageHeader
        title="Redaction"
        subtitle="What is stripped from prompts before they reach a provider."
      />

      {status.isPending && <Spinner label="Loading redaction" />}
      {status.error ? (
        <Notice tone="danger" title="Could not load redaction settings">
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
      <RulesList />
      <PreviewBox />

      <Card>
        <div className={STATS}>
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
                ? "no service to call"
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

      {/* No "Response" row and no score threshold. Both were process-wide
          settings that stopped being the answer when policy became per entity
          (ADR 0037): restoring is `anonymise_restore` against `anonymise`, per
          type, and a threshold is a column on each type in each rule. Reporting
          one number here read as *the* threshold, which it is not. */}
      <Card
        title="Configuration"
        description={
          status.source === "console"
            ? "From the gateway's environment, except the engine, which was set here."
            : "From the gateway's environment."
        }
      >
        <dl className={DETAILS}>
          <Row label="Engine">
            <code className={CODE}>{status.engine}</code>{" "}
            {/* Worth showing even with one entry: it is how an operator sees
                that installing a plugin worked (ADR 0026). */}
            <span className={MUTED}>
              · installed: {status.installed_engines.join(", ") || "none"}
            </span>
          </Row>
          {/* Which of the two decided. Invisible otherwise, and it is exactly
              the confusion a database override introduces: an environment
              variable that no longer takes effect looks like a broken one. */}
          <Row label="Source">
            {status.source === "console" && status.configured ? (
              <>
                This console
                {status.configured.changed_by ? ` · ${status.configured.changed_by}` : ""}
                {" · "}
                {new Date(status.configured.changed_at).toLocaleString()}
                {status.configured.reason && (
                  <div className={MUTED}>{status.configured.reason}</div>
                )}
              </>
            ) : (
              <>
                The deployment&apos;s environment
                <span className={MUTED}> · GATEWAY_REDACTION__ENGINE</span>
              </>
            )}
          </Row>
          <Row label="Detection endpoint">
            {status.endpoint ? (
              <code className={CODE}>{status.endpoint}</code>
            ) : (
              <span className={MUTED}>none — in-process engine</span>
            )}
          </Row>
          <Row label="Failure mode">
            {status.fail_open ? (
              <Badge tone="danger">Forward unredacted</Badge>
            ) : (
              <Badge tone="ok">Refuse the request</Badge>
            )}
          </Row>
          <Row label="Language">
            {status.language}
            {service?.models?.[status.language] && (
              <span className={MUTED}> · {service.models[status.language]}</span>
            )}
          </Row>
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
            <span className={MUTED}>
              placeholders derive from it, so it must stay stable
            </span>
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
          title="Entity types"
          description="GATEWAY_REDACTION__ENTITY_TYPES limits the search to these. It sets
            the policy below; the console overrides it."
        >
          <div className={CHIPS}>
            {status.entity_types.map((entity) => (
              <Badge key={entity}>{entity}</Badge>
            ))}
          </div>
        </Card>
      )}

      {service?.reachable && service.degraded_languages.length > 0 && (
        <Card title="Degraded languages" description="Served without a named-entity model.">
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
/**
 * Every rule, in force order, with the catch-all first.
 *
 * This is the whole answer to "what does this deployment redact", which is why
 * it is the first thing on the screen after the engine. The previous shape had a
 * deployment policy edited here and rules edited elsewhere, so the screen showed
 * half the answer and gave no sign that the other half existed — reported from
 * the live console, twice.
 *
 * Configuration is a page per rule, not a dialog: a policy is thirty entity
 * types, a set of patterns and an allow-list.
 */
function RulesList() {
  const rules = useRedactionRules({ limit: 200 });
  const update = useUpdateRedactionRule();
  const remove = useDeleteRedactionRule();
  const navigate = useNavigate();

  // The catch-all first, then the narrower scopes in the order they are folded.
  // Reading order is fold order, so the screen matches how a request is decided.
  const order: RedactionScope[] = ["all", "provider", "model", "group", "user", "api_key"];
  const items = [...(rules.data?.items ?? [])].sort(
    (a, b) => order.indexOf(a.scope) - order.indexOf(b.scope),
  );
  const hasCatchAll = items.some((rule) => rule.scope === "all" && rule.is_active);

  const columns: Column<RedactionRule>[] = [
    {
      key: "subject",
      header: "Applies to",
      render: (rule) => (
        <>
          <div>
            <Link to={`/admin/redaction/rules/${rule.id}`}>
              {rule.scope === "all" ? "Every request" : (rule.subject_label ?? "deleted subject")}
            </Link>
          </div>
          <div className={MUTED}>
            {scopeNoun(rule.scope)}
            {rule.name ? ` · ${rule.name}` : ""}
          </div>
        </>
      ),
    },
    { key: "policy", header: "Policy", render: (rule) => summarisePolicy(rule.policy) },
    {
      key: "state",
      header: "State",
      render: (rule) =>
        rule.is_active ? <Badge tone="ok">Active</Badge> : <Badge>Inactive</Badge>,
    },
    {
      key: "actions",
      header: "",
      render: (rule) => (
        <div className={ROW_ACTIONS}>
          <Button onClick={() => navigate(`/admin/redaction/rules/${rule.id}`)}>Edit</Button>
          <Button
            busy={update.isPending && update.variables?.id === rule.id}
            onClick={() => update.mutate({ id: rule.id, is_active: !rule.is_active })}
          >
            {rule.is_active ? "Deactivate" : "Activate"}
          </Button>
          <Button
            variant="ghost"
            busy={remove.isPending && remove.variables === rule.id}
            onClick={() => remove.mutate(rule.id)}
          >
            Delete
          </Button>
        </div>
      ),
    },
  ];

  return (
    <>
      {/* Not a warning: a deployment that redacts nothing may be the intended
          one. It is stated because the alternative is a screen that looks
          configured and is not. */}
      {!rules.isPending && !hasCatchAll && (
        <Notice tone="info" title="Nothing is redacted by default">
          Prompts reach providers as callers wrote them unless a rule below says
          otherwise. Add a rule for every request to change that.
        </Notice>
      )}
      <Card
        title="Rules"
        description="The strictest applicable rule wins, so adding one can only protect more."
        flush
        actions={
          <Button variant="primary" onClick={() => navigate("/admin/redaction/rules/new")}>
            New rule
          </Button>
        }
      >
        {rules.isPending ? (
          <Spinner label="Loading rules" />
        ) : rules.error ? (
          <Notice tone="danger" title="Could not load rules">
            {rules.error instanceof Error ? rules.error.message : "Unknown error."}
          </Notice>
        ) : (
          <Table
            columns={columns}
            rows={items}
            rowKey={(rule) => rule.id}
            empty="No rules, so nothing is redacted."
            caption="Redaction rules and the subject each one applies to."
          />
        )}
      </Card>
    </>
  );
}


/**
 * What the provider would actually receive.
 *
 * The one place an operator can see what the detector does, which until now was
 * findable only by reading an upstream request body — that is how the Italian
 * bug was found, and it took a packet capture to find a verb read as a person.
 *
 * Three things are shown rather than one, because the rewritten text alone
 * explains nothing: the spans say *why* each substitution happened, the mode
 * says which rule decided, and a block is a state of its own — nothing is
 * rewritten in that case, so there is no text to show and echoing the sample
 * back would read as "this is what would be sent".
 */
function PreviewBox() {
  const preview = usePreviewRedaction();
  const toast = useOptionalToast();
  const sampleId = useId();
  const [text, setText] = useState("");
  const [scope, setScope] = useState<RedactionScope | "">("");
  const [scopeId, setScopeId] = useState("");
  // What was sent, kept apart from what is typed: the spans are offsets into
  // the submitted sample, and slicing the live textarea with them would
  // mislabel every match the moment a character is typed after a run.
  const [sample, setSample] = useState("");

  // A scope with no subject is not a narrower preview, it is the deployment
  // policy wearing a label — so the run waits for the subject rather than
  // quietly answering a different question.
  const ready = text.trim().length > 0 && (scope === "" || scopeId.trim().length > 0);

  const run = () => {
    setSample(text);
    preview.mutate(
      scope === "" ? { text } : { text, scope, scope_id: scopeId },
      {
        onSuccess: () => toast?.add({ title: "Preview ran.", type: "success" }),
        onError: (error) =>
          toast?.add({
            title: "The preview did not run",
            description: error instanceof Error ? error.message : "Unknown error.",
            type: "error",
          }),
      },
    );
  };

  const result = preview.data;

  const columns: Column<RedactionPreviewSpan>[] = [
    {
      key: "match",
      header: "Match",
      render: (span) => <code className={CODE}>{sample.slice(span.start, span.end)}</code>,
    },
    {
      key: "entity",
      header: "Detected as",
      render: (span) => (
        <>
          <div>{entityLabel(span.entity_type)}</div>
          <div className={`${MUTED} ${CODE}`}>{span.entity_type}</div>
        </>
      ),
    },
    {
      key: "score",
      header: "Score",
      numeric: true,
      render: (span) => (
        <>
          <div>{span.score.toFixed(2)}</div>
          <div className={`${MUTED} ${NOWRAP}`}>needs {span.threshold}</div>
        </>
      ),
    },
    {
      key: "mode",
      header: "Applied",
      render: (span) =>
        span.allow_listed ? <Badge tone="warn">Allow-listed</Badge> : modeLabel(span.mode),
    },
  ];

  return (
    <Card title="Preview" description="Run a sample through the policy in force.">
      <div className={FORM}>
        {preview.error ? (
          <Notice tone="danger">
            {preview.error instanceof Error ? preview.error.message : "Unknown error."}
          </Notice>
        ) : null}

        <div className={FIELD}>
          <label className={FIELD_LABEL} htmlFor={sampleId}>
            Sample
          </label>
          <textarea
            id={sampleId}
            className={TEXTAREA}
            rows={4}
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="Riassumi le notizie del giorno da ilpost.it"
          />
          <p className={MUTED}>Not logged anywhere.</p>
        </div>

        <div className={FORM_ROW}>
          <Select
            label="Preview as"
            value={scope}
            onChange={(event) => {
              setScope(event.target.value as RedactionScope | "");
              setScopeId("");
            }}
          >
            <option value="">Nobody in particular</option>
            {SCOPES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
          {scope !== "" && (
            <SubjectPicker scope={scope} value={scopeId} onChange={setScopeId} />
          )}
        </div>

        <div>
          <Button
            variant="primary"
            busy={preview.isPending}
            disabled={!ready}
            onClick={run}
          >
            Run preview
          </Button>
        </div>

        {result && (
          <>
            {/* Server-computed, rendered verbatim: an engine that detects
                nothing must say so, or an empty result reads as a clean
                prompt. */}
            {result.note && <Notice tone="warn">{result.note}</Notice>}

            {result.blocked ? (
              <Notice tone="danger" title="This request would be blocked">
                {result.blocked_reason ?? "A blocked entity type was found."}
              </Notice>
            ) : (
              <div className={FIELD}>
                <span className={FIELD_LABEL}>What the provider receives</span>
                <p className={SAMPLE}>{result.redacted_text}</p>
              </div>
            )}

            <p className={MUTED}>
              {result.entity_count} replaced · {result.engine} engine ·{" "}
              {result.scope === null ? "the deployment policy" : `${scopeNoun(result.scope)} rule`}
            </p>

            <Table
              columns={columns}
              rows={result.spans}
              rowKey={(span, index) => `${span.entity_type}-${span.start}-${index}`}
              empty="Nothing was detected in this sample."
              caption="What the detector found, and what the policy did with it."
            />
          </>
        )}
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
        onSuccess: () => toast?.add({ title: `${engine.label} is in force.`, type: "success" }),
        onError: (error) =>
          toast?.add({
            title: "The engine was not changed",
            description: error instanceof Error ? error.message : "Unknown error.",
            type: "error",
          }),
      },
    );
  };

  return (
    <Card
      title="Engine"
      description="One is in force at a time; llmp.redactors plugins appear here."
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
          Saved. Other workers apply it within {status.propagation_seconds}s.
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
                {engine.is_active && <Badge tone="accent">In force</Badge>}
                {/* The one property that decides whether this screen means
                    anything, said on every row rather than only on the
                    active one. */}
                {!engine.redacts && <Badge tone="danger">Redacts nothing</Badge>}
              </div>
              <div className={MUTED}>{engine.description}</div>
              {/* Server-computed, rendered verbatim: the wording lives with the
                  rule, the same convention as the warnings above. */}
              {engine.blocked_reason && (
                <div className={MUTED}>
                  <strong>Cannot be enabled.</strong> {engine.blocked_reason}
                </div>
              )}
            </div>
            <div className={ENGINE_ACTION}>
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
            {
              onSuccess: () => {
                setConfirming(null);
                toast?.add({ title: "Redaction is off.", type: "success" });
              },
              onError: (error) =>
                toast?.add({
                  title: "The engine was not changed",
                  description: error instanceof Error ? error.message : "Unknown error.",
                  type: "error",
                }),
            },
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
        Prompts will reach providers exactly as callers sent them.
      </Notice>
      <Input
        label="Reason"
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        placeholder="detection service migration"
        hint="Kept permanently, with who changed it and when."
      />
    </Dialog>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className={DETAIL_LABEL}>{label}</dt>
      <dd className={DETAIL_VALUE}>{children}</dd>
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
