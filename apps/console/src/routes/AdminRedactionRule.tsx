import { Button, Card, Notice, Select, Spinner } from "@llmp/ui";
import { useState } from "react";
import { useNavigate, useParams } from "react-router";
import {
  useCreateRedactionRule,
  useRedactionRules,
  useRedactionStatus,
  useUpdateRedactionRule,
} from "../lib/admin";
import type { RedactionPolicy, RedactionScope } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { PolicyFields, seededPolicy } from "../components/PolicyFields";
import { SubjectPicker, scopeNoun } from "../components/SubjectPicker";
import styles from "./Admin.module.css";

//: Widest first, matching the order rules are folded in and listed.
const SCOPE_OPTIONS: RedactionScope[] = ["all", "provider", "model", "group", "user", "api_key"];

/**
 * What a new rule starts as: off for every entity type, and the credential
 * patterns already present.
 *
 * Off, because since ADR 0039 a deployment filters nothing until somebody says
 * otherwise. With the patterns, because the detector finds no credentials at
 * all — an empty list is not a neutral starting point there, it is a policy
 * that silently does not cover them. They can be deleted, which is a decision;
 * their absence would not have been.
 */
const EMPTY_POLICY: RedactionPolicy = seededPolicy();

/**
 * One rule, on its own page (ADR 0039).
 *
 * A page rather than a dialog because a policy is thirty entity types, a set of
 * patterns and an allow-list — a dialog that scrolls is a dialog that should
 * have been a page. It also makes a rule addressable, which is what lets the
 * list on the Redaction screen be a list of links rather than a list of buttons
 * that open something.
 *
 * The subject is chosen here and nowhere else: a rule *is* a decision about one
 * subject, and re-pointing it at another would silently make two subjects'
 * histories read as one.
 */
export function AdminRedactionRule() {
  const { ruleId: param } = useParams();
  // One route serves both, because "new" cannot collide with a uuid and a second
  // route would duplicate every line of this form.
  const ruleId = param === "new" ? undefined : param;
  const navigate = useNavigate();
  const status = useRedactionStatus();
  // The rule comes from the same listing the previous screen renders, so
  // arriving by link costs no request that the list did not already make.
  const rules = useRedactionRules({ limit: 200 });
  const existing = rules.data?.items.find((rule) => rule.id === ruleId) ?? null;

  const create = useCreateRedactionRule();
  const update = useUpdateRedactionRule();

  const [name, setName] = useState("");
  const [scope, setScope] = useState<RedactionScope>("all");
  const [scopeId, setScopeId] = useState("");
  const [policy, setPolicy] = useState<RedactionPolicy>(EMPTY_POLICY);
  const [reason, setReason] = useState("");
  const [loadedFor, setLoadedFor] = useState<string | null>(null);

  // Keyed seeding, the same shape the model editor uses: without it the fields
  // keep the previous rule's values when the list resolves.
  if (existing && loadedFor !== existing.id) {
    setLoadedFor(existing.id);
    setName(existing.name);
    setScope(existing.scope);
    setScopeId(existing.scope_id ?? "");
    setPolicy(structuredClone(existing.policy));
    setReason(existing.reason);
  }

  const saving = create.isPending || update.isPending;
  const error = create.error ?? update.error;
  const entityTypes = status.data?.service?.entities ?? [];

  const submit = () => {
    const done = { onSuccess: () => navigate("/admin/redaction") };
    if (existing) {
      update.mutate({ id: existing.id, name, policy, reason }, done);
    } else {
      create.mutate(
        {
          name,
          scope,
          // Null for the catch-all, which applies to every request and so names
          // no subject.
          scope_id: scope === "all" ? null : scopeId,
          policy,
          reason,
        },
        done,
      );
    }
  };

  if (ruleId && rules.isPending) {
    return (
      <div className={styles.page}>
        <Spinner label="Loading the rule" />
      </div>
    );
  }
  if (ruleId && !existing) {
    return (
      <div className={styles.page}>
        <Notice tone="danger" title="No such rule">
          It may have been deleted. <a href="/admin/redaction">Back to redaction</a>
        </Notice>
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <PageHeader
        title={existing ? `Rule · ${existing.subject_label ?? scopeNoun(existing.scope)}` : "New rule"}
        subtitle="The strictest applicable rule wins, so adding one can only protect more."
        actions={<Button onClick={() => navigate("/admin/redaction")}>Back</Button>}
      />

      {error ? (
        <Notice tone="danger" title="Could not save the rule">
          {error instanceof Error ? error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Card title="Subject">
        {existing ? (
          <p className={styles.muted}>
            {scopeNoun(existing.scope)} · {existing.subject_label ?? "deleted"}
          </p>
        ) : (
          <>
            <Select
              label="Scope"
              value={scope}
              onChange={(event) => {
                setScope(event.target.value as RedactionScope);
                setScopeId("");
              }}
            >
              {SCOPE_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {scopeNoun(option)}
                </option>
              ))}
            </Select>
            <SubjectPicker scope={scope} value={scopeId} onChange={setScopeId} />
          </>
        )}
      </Card>

      <PolicyFields
        policy={policy}
        onChange={setPolicy}
        entityTypes={entityTypes}
        scoreThreshold={status.data?.score_threshold ?? null}
      />

      <Card title="Name and reason">
        <div className={styles.form}>
          <input
            aria-label="Rule name"
            className={styles.input}
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="what this rule is for"
          />
          <input
            aria-label="Reason"
            className={styles.input}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="why it exists"
          />
          <div>
            <Button
              variant="primary"
              busy={saving}
              disabled={!existing && scope !== "all" && !scopeId}
              onClick={submit}
            >
              {existing ? "Save rule" : "Create rule"}
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
