import { Badge, Button, Card, Dialog, Input, Notice, Pagination, Select, Spinner, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import { Link } from "react-router";
import {
  useCreateRedactionRule,
  useDeleteRedactionRule,
  useRedactionRules,
  useRedactionStatus,
  useUpdateRedactionRule,
} from "../lib/admin";
import { summarisePolicy } from "../lib/entities";
import { usePaginated } from "../lib/paging";
import type { RedactionPolicy, RedactionRule, RedactionScope, RedactionStatus } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { PolicyFields } from "../components/PolicyFields";
import { SCOPES, SubjectPicker, scopeNoun } from "../components/SubjectPicker";
import styles from "./Admin.module.css";

/**
 * Redaction rules, one policy per subject (ADR 0038).
 *
 * Reached from the Redaction screen rather than from the navigation, which is
 * deliberately short: this is the second question about redaction, and it is
 * asked by somebody who is already looking at the first.
 *
 * The precedence rule is the quota engine's inverted — quotas are *all rules
 * must pass*, redaction is *the strictest applicable answer wins* — and the
 * consequence is worth stating on the page: adding a rule here cannot make
 * anything less protected, whatever it says.
 */
const EMPTY_POLICY: RedactionPolicy = {
  default_mode: "anonymise_restore",
  entities: {},
  patterns: [],
  allow_list: [],
};

export function AdminRedactionRules() {
  const paged = usePaginated(25);
  const [scope, setScope] = useState("");
  const [state, setState] = useState("");
  const rules = useRedactionRules(paged.page, { scope, is_active: state });
  const status = useRedactionStatus();
  const update = useUpdateRedactionRule();
  const remove = useDeleteRedactionRule();

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<RedactionRule | null>(null);

  const columns: Column<RedactionRule>[] = [
    {
      key: "rule",
      header: "Rule",
      render: (rule) => (
        <>
          <div>{rule.name || <em className={styles.muted}>unnamed</em>}</div>
          <div className={styles.muted}>
            {scopeNoun(rule.scope)} ·{" "}
            {/* Null is not a missing name: the subject was deleted, so the rule
                matches nothing and looks identical to one that works. */}
            {rule.subject_label ?? <Badge tone="danger">Subject deleted</Badge>}
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
        <div className={styles.rowActions}>
          <Button onClick={() => setEditing(rule)}>Edit</Button>
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
    <div className={styles.page}>
      <PageHeader
        title="Redaction rules"
        subtitle="The strictest applicable rule wins, so adding one can only protect more."
        actions={
          <>
            <Link to="/admin/redaction">Redaction</Link>
            <Button variant="primary" onClick={() => setCreating(true)}>
              New rule
            </Button>
          </>
        }
      />

      {remove.error ? (
        <Notice tone="danger" title="Could not delete the rule">
          {remove.error instanceof Error ? remove.error.message : "Unknown error."}
        </Notice>
      ) : null}
      {update.error ? (
        <Notice tone="danger" title="Could not change the rule">
          {update.error instanceof Error ? update.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Card>
        <div className={styles.filters}>
          <Select label="Scope" value={scope} onChange={(event) => setScope(event.target.value)}>
            <option value="">Every scope</option>
            {SCOPES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
          <Select label="State" value={state} onChange={(event) => setState(event.target.value)}>
            <option value="">Active and inactive</option>
            <option value="true">Active</option>
            <option value="false">Inactive</option>
          </Select>
        </div>
      </Card>

      <Card flush>
        {rules.isPending ? (
          <Spinner label="Loading redaction rules" />
        ) : rules.error ? (
          <Notice tone="danger" title="Could not load redaction rules">
            {rules.error instanceof Error ? rules.error.message : "Unknown error."}
          </Notice>
        ) : (
          <>
            <Table
              columns={columns}
              rows={rules.data?.items ?? []}
              rowKey={(rule) => rule.id}
              empty="No rules. Every request runs under the deployment policy."
              caption="Scoped redaction rules and the subject each one attaches to."
            />
            <Pagination
              total={rules.data?.total ?? 0}
              limit={paged.limit}
              offset={paged.offset}
              onOffsetChange={paged.setOffset}
              noun="rules"
              busy={rules.isFetching}
            />
          </>
        )}
      </Card>

      {/* Mounted only while it is open. A closed `<dialog>` still renders its
          children, so leaving it mounted puts a second Scope control in the
          document next to the filter above — and seeds the next rule's form
          with the last one's answers. */}
      {creating && (
        <CreateRuleDialog status={status.data ?? null} onClose={() => setCreating(false)} />
      )}
      <EditRuleDialog
        rule={editing}
        status={status.data ?? null}
        onClose={() => setEditing(null)}
      />
    </div>
  );
}

/**
 * The subject cannot be changed afterwards, so it is chosen here and nowhere
 * else: a rule *is* a decision about one subject, and re-pointing it at another
 * would silently make two subjects' histories read as one.
 */
function CreateRuleDialog({
  status,
  onClose,
}: {
  status: RedactionStatus | null;
  onClose: () => void;
}) {
  const create = useCreateRedactionRule();
  const [name, setName] = useState("");
  const [scope, setScope] = useState<RedactionScope>("group");
  const [scopeId, setScopeId] = useState("");
  const [policy, setPolicy] = useState<RedactionPolicy>(EMPTY_POLICY);
  const [reason, setReason] = useState("");

  // Unmounted on close, so there is no state left to reset.
  const close = onClose;

  const submit = () =>
    create.mutate(
      { name: name.trim(), scope, scope_id: scopeId, policy, reason: reason.trim() },
      { onSuccess: close },
    );

  return (
    <Dialog
      open
      title="New redaction rule"
      onClose={close}
      footer={
        <>
          <Button onClick={close}>Cancel</Button>
          <Button
            variant="primary"
            busy={create.isPending}
            disabled={scopeId.trim().length === 0}
            onClick={submit}
          >
            Create
          </Button>
        </>
      }
    >
      {/* Both refusals the API makes — a subject that does not exist, and a
          second rule for one that already has one — arrive as a sentence worth
          reading. */}
      {create.error ? (
        <Notice tone="danger">
          {create.error instanceof Error ? create.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Input
        label="Name"
        value={name}
        onChange={(event) => setName(event.target.value)}
        placeholder="clinical group, stricter"
      />

      <div className={styles.formRow}>
        <Select
          label="Scope"
          value={scope}
          onChange={(event) => {
            setScope(event.target.value as RedactionScope);
            setScopeId("");
          }}
        >
          {SCOPES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </Select>
        <SubjectPicker scope={scope} value={scopeId} onChange={setScopeId} />
      </div>

      <PolicyFields
        policy={policy}
        onChange={setPolicy}
        entityTypes={status?.service?.entities ?? []}
        scoreThreshold={status?.score_threshold ?? null}
      />

      <Input
        label="Reason"
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        placeholder="ethics approval 2026-14"
        hint="Optional. Kept with the rule."
      />
    </Dialog>
  );
}

function EditRuleDialog({
  rule,
  status,
  onClose,
}: {
  rule: RedactionRule | null;
  status: RedactionStatus | null;
  onClose: () => void;
}) {
  // Mounted only while a rule is open, and keyed on its id: the form seeds its
  // state from the rule once, so re-using one instance across two rules would
  // show the first one's policy under the second one's name.
  if (rule === null) return null;
  return <EditRuleForm key={rule.id} rule={rule} status={status} onClose={onClose} />;
}

function EditRuleForm({
  rule,
  status,
  onClose,
}: {
  rule: RedactionRule;
  status: RedactionStatus | null;
  onClose: () => void;
}) {
  const update = useUpdateRedactionRule();
  const [name, setName] = useState(rule.name);
  const [policy, setPolicy] = useState<RedactionPolicy>(() => structuredClone(rule.policy));
  const [reason, setReason] = useState(rule.reason);

  const submit = () =>
    update.mutate(
      { id: rule.id, name: name.trim(), policy, reason: reason.trim() },
      { onSuccess: onClose },
    );

  return (
    <Dialog
      open
      title={`Edit ${rule.name || scopeNoun(rule.scope).toLowerCase()} rule`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={update.isPending} onClick={submit}>
            Save rule
          </Button>
        </>
      }
    >
      {update.error ? (
        <Notice tone="danger">
          {update.error instanceof Error ? update.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <p className={styles.muted}>
        {scopeNoun(rule.scope)} · {rule.subject_label ?? "subject deleted"}
      </p>

      <Input label="Name" value={name} onChange={(event) => setName(event.target.value)} />

      <PolicyFields
        policy={policy}
        onChange={setPolicy}
        entityTypes={status?.service?.entities ?? []}
        scoreThreshold={status?.score_threshold ?? null}
      />

      <Input
        label="Reason"
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        hint="Optional. Kept with the rule."
      />
    </Dialog>
  );
}
