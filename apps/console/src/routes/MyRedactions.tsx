import { Badge, Button, Card, Input, Notice, Pagination, Select, Spinner, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { entityLabel, modeLabel } from "../lib/entities";
import { formatDateTime } from "../lib/format";
import { FIELD, FIELD_LABEL, FORM, FORM_ROW, MUTED, NOWRAP, PAGE, TEXTAREA } from "../lib/layout";
import {
  useDeleteRedactionRule,
  useMyRedactionRules,
  useRedactionEvents,
  useRulesInForce,
  useSaveRedactionRule,
  useTestRedaction,
} from "../lib/myRedaction";
import type {
  AdminRuleView,
  RedactionEvent,
  UserRedactionRule,
  UserRedactionRuleInput,
} from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * Redactions: what the gateway hides from providers for a user, in one place.
 *
 * Four things, top to bottom: what the administrators' rules already do (read
 * only), the user's own rules (which can only add protection), a box to try
 * text against both, and the log of requests where something was hidden. The
 * log holds no values, only what kind of thing, how many and which rule.
 */

const PAGE_SIZE = 25;

/** The four modes a user may choose; there is no "off" to pick. */
const USER_MODES = ["anonymise_restore", "anonymise", "redact", "block"] as const;

const SCOPE_LABEL: Record<AdminRuleView["scope"], () => string> = {
  all: m["my_redaction.scope.all"],
  group: m["my_redaction.scope.group"],
  user: m["my_redaction.scope.user"],
  model: m["my_redaction.scope.model"],
  provider: m["my_redaction.scope.provider"],
  api_key: m["my_redaction.scope.api_key"],
};

/** What a mode means for a reader, in a sentence. */
function modeSentence(mode: string): string {
  switch (mode) {
    case "anonymise_restore":
      return m["my_redaction.mode.anonymise_restore"]();
    case "anonymise":
      return m["my_redaction.mode.anonymise"]();
    case "redact":
      return m["my_redaction.mode.redact"]();
    case "block":
      return m["my_redaction.mode.block"]();
    default:
      return m["my_redaction.mode.off"]();
  }
}

function AdminRule({ rule }: { rule: AdminRuleView }) {
  const where = rule.subject_label
    ? m["my_redaction.applies_to_subject"]({
        scope: SCOPE_LABEL[rule.scope](),
        subject: rule.subject_label,
      })
    : SCOPE_LABEL[rule.scope]();
  return (
    <li className="rounded-lg border border-line p-4">
      <p className="m-0 font-medium">
        {/* i18n: server text, the name an administrator gave the rule. */}
        {rule.name || SCOPE_LABEL[rule.scope]()}
      </p>
      <p className={`m-0 text-dense ${MUTED}`}>{where}</p>
      <ul className="mt-2 mb-0 flex list-disc flex-col gap-1 pl-5 text-body">
        {rule.default_mode !== "off" && (
          <li>
            {m["my_redaction.default_mode"]({ what: modeSentence(rule.default_mode) })}
          </li>
        )}
        {rule.entities.map((entity) => (
          <li key={entity.entity_type}>
            {m["my_redaction.type_line"]({
              type: entityLabel(entity.entity_type),
              what: modeSentence(entity.mode),
            })}
          </li>
        ))}
        {rule.patterns.map((pattern) => (
          <li key={pattern.name}>
            {m["my_redaction.pattern_line"]({
              name: pattern.name,
              what: modeSentence(pattern.mode),
            })}
          </li>
        ))}
        {rule.exceptions > 0 && (
          <li>{m["my_redaction.exceptions"]({ count: rule.exceptions })}</li>
        )}
      </ul>
    </li>
  );
}

function RulesInForce() {
  const inForce = useRulesInForce();
  return (
    <Card title={m["my_redaction.in_force.title"]()}>
      <p className={`mt-0 ${MUTED}`}>{m["my_redaction.in_force.intro"]()}</p>
      {inForce.isPending || !inForce.data ? (
        inForce.isError ? (
          <Notice tone="danger" title={m["my_redaction.error"]()}>
            {/* i18n: server text, the API's own error message. */}
            {inForce.error instanceof Error
              ? inForce.error.message
              : m["my_redaction.unknown_error"]()}
          </Notice>
        ) : (
          <Spinner label={m["my_redaction.loading"]()} />
        )
      ) : (
        <>
          {!inForce.data.engine_active && (
            <Notice tone="warn" title={m["my_redaction.engine_off.title"]()}>
              {m["my_redaction.engine_off.body"]()}
            </Notice>
          )}
          {inForce.data.rules.length === 0 ? (
            <p className={`mb-0 ${MUTED}`}>{m["my_redaction.in_force.none"]()}</p>
          ) : (
            <ul className="m-0 flex list-none flex-col gap-3 p-0">
              {inForce.data.rules.map((rule) => (
                <AdminRule key={rule.id} rule={rule} />
              ))}
            </ul>
          )}
        </>
      )}
    </Card>
  );
}

function RuleStatus({ rule }: { rule: UserRedactionRule }) {
  if (rule.status === "adds_protection") {
    return <Badge tone="ok">{m["my_redaction.rule.adds"]()}</Badge>;
  }
  return (
    <span>
      <Badge tone="warn">{m["my_redaction.rule.covered"]()}</Badge>
      <span className={`block text-small ${MUTED}`}>
        {rule.admin_mode
          ? m["my_redaction.rule.covered_by_mode"]({ mode: modeLabel(rule.admin_mode) })
          : m["my_redaction.rule.covered_by"]()}
      </span>
    </span>
  );
}

const EMPTY_FORM = { kind: "mode", name: "", regex: "", mode: "redact" } as const;

type FormState = {
  kind: "pattern" | "mode";
  name: string;
  regex: string;
  mode: (typeof USER_MODES)[number];
};

function MyRules() {
  const rules = useMyRedactionRules();
  const save = useSaveRedactionRule();
  const remove = useDeleteRedactionRule();
  const [editing, setEditing] = useState<string | undefined>(undefined);
  const [form, setForm] = useState<FormState>({ ...EMPTY_FORM });
  const items = rules.data?.items ?? [];

  const reset = () => {
    setEditing(undefined);
    setForm({ ...EMPTY_FORM });
    save.reset();
  };

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const body: UserRedactionRuleInput = {
      kind: form.kind,
      name: form.name.trim(),
      mode: form.mode,
      regex: form.kind === "pattern" ? form.regex : null,
    };
    save.mutate({ id: editing, body }, { onSuccess: reset });
  };

  const columns: Column<UserRedactionRule>[] = [
    {
      key: "what",
      header: m["my_redaction.rules.column.rule"](),
      render: (rule) =>
        rule.kind === "pattern" ? (
          <span>
            <span className="font-medium">{rule.name}</span>
            <code className="block text-small [overflow-wrap:anywhere]">{rule.regex}</code>
          </span>
        ) : (
          <span>
            <span className="font-medium">{entityLabel(rule.name)}</span>{" "}
            <span className={`text-small ${MUTED}`}>{rule.name}</span>
          </span>
        ),
    },
    {
      key: "mode",
      header: m["my_redaction.rules.column.mode"](),
      render: (rule) => modeLabel(rule.mode),
    },
    {
      key: "status",
      header: m["my_redaction.rules.column.status"](),
      render: (rule) => <RuleStatus rule={rule} />,
    },
    {
      key: "actions",
      header: "",
      label: m["my_redaction.rules.column.actions"](),
      render: (rule) => (
        <span className="flex gap-2">
          <Button
            onClick={() => {
              setEditing(rule.id);
              setForm({
                kind: rule.kind,
                name: rule.name,
                regex: rule.regex ?? "",
                mode: rule.mode as FormState["mode"],
              });
            }}
          >
            {m["my_redaction.rules.edit"]()}
          </Button>
          <Button
            onClick={() => remove.mutate(rule.id)}
            disabled={remove.isPending}
            aria-label={m["my_redaction.rules.remove_aria"]({ name: rule.name })}
          >
            {m["my_redaction.rules.remove"]()}
          </Button>
        </span>
      ),
    },
  ];

  return (
    <Card title={m["my_redaction.rules.title"]()} flush>
      <div className="p-5">
        <p className={`mt-0 ${MUTED}`}>{m["my_redaction.rules.intro"]()}</p>
        {rules.isError && (
          <Notice tone="danger" title={m["my_redaction.error"]()}>
            {/* i18n: server text, the API's own error message. */}
            {rules.error instanceof Error ? rules.error.message : m["my_redaction.unknown_error"]()}
          </Notice>
        )}
      </div>
      <Table
        stack
        columns={columns}
        rows={items}
        rowKey={(rule) => rule.id}
        empty={m["my_redaction.rules.empty"]()}
        caption={m["my_redaction.rules.caption"]()}
      />
      <form className={`${FORM} border-t border-line p-5`} onSubmit={submit}>
        <h3 className="m-0 text-body font-semibold">
          {editing ? m["my_redaction.form.edit"]() : m["my_redaction.form.add"]()}
        </h3>
        {save.isError && (
          <Notice tone="danger" title={m["my_redaction.form.error"]()}>
            {/* i18n: server text, the API's own error message (it names what RE2 refused). */}
            {save.error instanceof Error ? save.error.message : m["my_redaction.unknown_error"]()}
          </Notice>
        )}
        <div className={FORM_ROW}>
          <Select
            label={m["my_redaction.form.kind"]()}
            value={form.kind}
            onChange={(e) => setForm({ ...form, kind: e.target.value as FormState["kind"] })}
          >
            <option value="mode">{m["my_redaction.form.kind_mode"]()}</option>
            <option value="pattern">{m["my_redaction.form.kind_pattern"]()}</option>
          </Select>
          <Input
            label={
              form.kind === "pattern"
                ? m["my_redaction.form.pattern_name"]()
                : m["my_redaction.form.type_name"]()
            }
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            maxLength={64}
            required
            hint={
              form.kind === "pattern"
                ? m["my_redaction.form.pattern_name_hint"]()
                : m["my_redaction.form.type_name_hint"]()
            }
          />
          <Select
            label={m["my_redaction.form.mode"]()}
            value={form.mode}
            onChange={(e) => setForm({ ...form, mode: e.target.value as FormState["mode"] })}
          >
            {USER_MODES.map((mode) => (
              <option key={mode} value={mode}>
                {modeLabel(mode)}
              </option>
            ))}
          </Select>
        </div>
        {form.kind === "pattern" && (
          <Input
            label={m["my_redaction.form.regex"]()}
            value={form.regex}
            onChange={(e) => setForm({ ...form, regex: e.target.value })}
            maxLength={512}
            required
            hint={m["my_redaction.form.regex_hint"]()}
            className="font-mono"
          />
        )}
        <p className={`m-0 text-small ${MUTED}`}>{m["my_redaction.form.only_stricter"]()}</p>
        <div className="flex gap-2">
          <Button type="submit" variant="primary" disabled={save.isPending || !form.name.trim()}>
            {editing ? m["my_redaction.form.save"]() : m["my_redaction.form.add_button"]()}
          </Button>
          {editing && <Button onClick={reset}>{m["my_redaction.form.cancel"]()}</Button>}
        </div>
      </form>
    </Card>
  );
}

function TestBox() {
  const [text, setText] = useState("");
  // What was tested, kept so the offsets in the answer still point at it after the box is edited.
  const [tested, setTested] = useState("");
  const test = useTestRedaction();

  return (
    <Card title={m["my_redaction.test.title"]()}>
      <div className={FORM}>
        <div className={FIELD}>
          <label className={FIELD_LABEL} htmlFor="my-redaction-test">
            {m["my_redaction.test.label"]()}
          </label>
          <textarea
            id="my-redaction-test"
            className={TEXTAREA}
            rows={4}
            maxLength={20_000}
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder={m["my_redaction.test.placeholder"]()}
          />
          <p className={`m-0 text-small ${MUTED}`}>{m["my_redaction.test.not_logged"]()}</p>
        </div>
        <div>
          <Button
            variant="primary"
            disabled={test.isPending || !text.trim()}
            onClick={() => {
              setTested(text);
              test.mutate(text);
            }}
          >
            {m["my_redaction.test.run"]()}
          </Button>
        </div>
        {test.isError && (
          <Notice tone="danger" title={m["my_redaction.error"]()}>
            {/* i18n: server text, the API's own error message. */}
            {test.error instanceof Error ? test.error.message : m["my_redaction.unknown_error"]()}
          </Notice>
        )}
        {test.data && (
          <div role="status" className="flex flex-col gap-2">
            {test.data.note && <Notice tone="warn">{test.data.note}</Notice>}
            {test.data.blocked && (
              <Notice tone="danger" title={m["my_redaction.test.blocked"]()}>
                {m["my_redaction.test.blocked_body"]()}
              </Notice>
            )}
            {test.data.engine_active && test.data.spans.length === 0 && (
              <p className="m-0">{m["my_redaction.test.nothing"]()}</p>
            )}
            {test.data.spans.length > 0 && (
              <ul className="m-0 flex list-none flex-col gap-2 p-0">
                {test.data.spans.map((span) => (
                  <li
                    key={`${span.start}-${span.end}-${span.entity_type}`}
                    className="rounded-lg border border-line p-3"
                  >
                    <code className="[overflow-wrap:anywhere]">{tested.slice(span.start, span.end)}</code>
                    <p className="m-0 text-dense">
                      {m["my_redaction.test.span"]({
                        type: entityLabel(span.entity_type),
                        what: modeSentence(span.mode),
                      })}
                    </p>
                    <p className={`m-0 text-small ${MUTED}`}>
                      {span.rule
                        ? span.rule.owner === "user"
                          ? m["my_redaction.test.by_you"]({ name: span.rule.name })
                          : m["my_redaction.test.by_admin"]({ name: span.rule.name })
                        : m["my_redaction.test.by_unknown"]()}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

function entitySummary(event: RedactionEvent) {
  return event.entities.map((entity) => (
    <span key={entity.type} className="block">
      {m["my_redaction.log.entity"]({
        type: entityLabel(entity.type),
        count: entity.count,
        mode: modeLabel(entity.mode),
      })}
    </span>
  ));
}

const SOURCE_LABEL: Record<string, () => string> = {
  message: m["my_redaction.source.message"],
  tool_result: m["my_redaction.source.tool_result"],
  document: m["my_redaction.source.document"],
};

function Log() {
  const [requestId, setRequestId] = useState("");
  const [offset, setOffset] = useState(0);
  const log = useRedactionEvents({ requestId, limit: PAGE_SIZE, offset });
  const page = log.data;

  const columns: Column<RedactionEvent>[] = [
    {
      key: "time",
      header: m["my_redaction.log.column.time"](),
      nowrap: true,
      render: (event) => formatDateTime(event.created_at),
    },
    {
      key: "model",
      header: m["my_redaction.log.column.model"](),
      render: (event) => (
        <span>
          {event.model}
          {event.provider && <span className={`block text-small ${MUTED}`}>{event.provider}</span>}
          {event.byok && (
            <>
              {" "}
              <Badge>{m["my_redaction.log.own_key"]()}</Badge>
            </>
          )}
        </span>
      ),
    },
    {
      key: "what",
      header: m["my_redaction.log.column.hidden"](),
      render: (event) => entitySummary(event),
    },
    {
      key: "rule",
      header: m["my_redaction.log.column.rule"](),
      hideBelow: "md",
      render: (event) =>
        event.entities.map((entity) => (
          <span key={entity.type} className="block text-small">
            {entity.rule
              ? entity.rule.owner === "user"
                ? m["my_redaction.log.rule_you"]({ name: entity.rule.name })
                : m["my_redaction.log.rule_admin"]({ name: entity.rule.name })
              : m["my_redaction.test.by_unknown"]()}
          </span>
        )),
    },
    {
      key: "sources",
      header: m["my_redaction.log.column.where"](),
      hideBelow: "lg",
      render: (event) =>
        [...new Set(event.entities.flatMap((entity) => entity.sources))]
          .map((source) => (SOURCE_LABEL[source] ?? (() => source))())
          .join(", "),
    },
    {
      key: "outcome",
      header: m["my_redaction.log.column.outcome"](),
      render: (event) =>
        event.blocked ? (
          <Badge tone="danger">{m["my_redaction.log.blocked"]()}</Badge>
        ) : (
          <Badge tone="ok">{m["my_redaction.log.hidden"]()}</Badge>
        ),
    },
    {
      key: "id",
      header: m["my_redaction.log.column.request"](),
      hideBelow: "lg",
      render: (event) => <code className={`${NOWRAP} text-small`}>{event.request_id}</code>,
    },
  ];

  return (
    <Card title={m["my_redaction.log.title"]()} flush>
      <div className="flex flex-col gap-4 p-5">
        <p className={`m-0 ${MUTED}`}>{m["my_redaction.log.intro"]()}</p>
        <Input
          label={m["my_redaction.log.filter"]()}
          value={requestId}
          onChange={(e) => {
            setRequestId(e.target.value);
            setOffset(0);
          }}
          placeholder={m["my_redaction.log.filter_placeholder"]()}
        />
        {log.isError && (
          <Notice tone="danger" title={m["my_redaction.error"]()}>
            {/* i18n: server text, the API's own error message. */}
            {log.error instanceof Error ? log.error.message : m["my_redaction.unknown_error"]()}
          </Notice>
        )}
      </div>
      {log.isPending ? (
        <Spinner label={m["my_redaction.loading"]()} />
      ) : (
        <>
          <Table
            stack
            columns={columns}
            rows={page?.items ?? []}
            rowKey={(event, index) => `${event.request_id}-${index}`}
            empty={m["my_redaction.log.empty"]()}
            caption={m["my_redaction.log.caption"]()}
          />
          <Pagination
            total={page?.total ?? 0}
            limit={PAGE_SIZE}
            offset={offset}
            onOffsetChange={setOffset}
            noun={m["my_redaction.log.noun"]()}
            busy={log.isFetching}
          />
        </>
      )}
    </Card>
  );
}

export function MyRedactions() {
  return (
    <div className={PAGE}>
      <PageHeader
        title={m["my_redaction.title"]()}
        subtitle={m["my_redaction.subtitle"]()}
      />
      <RulesInForce />
      <MyRules />
      <TestBox />
      <Log />
    </div>
  );
}
