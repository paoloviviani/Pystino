import { Button, Card, Notice, Select, Spinner } from "@llmp/ui";
import { useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router";
import {
  useCreateRedactionRule,
  useRedactionRules,
  useRedactionStatus,
  useUpdateRedactionRule,
} from "../lib/admin";
import type { RedactionPolicy, RedactionRule, RedactionScope } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { PolicyFields, seededPolicy } from "../components/PolicyFields";
import { SubjectPicker, scopeNoun } from "../components/SubjectPicker";
import { FORM, PAGE } from "../lib/layout";
import { useOptionalToast } from "../lib/toast";

//: Widest first, matching the order rules are folded in and listed.
const SCOPE_OPTIONS: RedactionScope[] = ["all", "provider", "model", "group", "user", "api_key"];


/**
 * The name and reason fields are native inputs labelled with `aria-label`
 * rather than a visible label, so they sit outside `@llmp/ui`'s `Input` (which
 * is built around a visible label) — but they wear the same control chrome,
 * written out from the package's `controlClass`. `Admin.module.css` never
 * defined the `.input` they used to cite, so until this conversion they were
 * rendering with no styling at all; writing the chrome here turns that
 * accident into a decision.
 */
const INPUT =
  "w-full rounded-md border border-line bg-surface px-3 py-2 text-base text-ink " +
  "placeholder:text-ink-faint transition-shadow hover:border-line-strong " +
  "focus-visible:outline-none focus-visible:shadow-focus";

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
 * The subject is editable, both to re-point a rule at a neighbour and because a
 * rule whose subject was deleted (``scope_id`` is not a foreign key) used to be
 * unrepairable dead weight. A move is validated server-side like a creation and
 * a taken subject is a 409, so two rules still cannot disagree about one
 * subject — what made the freeze safe to lift.
 *
 * Cloning arrives here through router state with the source rule: the policy
 * and reason come across, the name gains "(copy)", and the subject does not —
 * one rule per subject means the clone cannot keep the source's, and the clone
 * is created inactive, because an active clone starts redacting for a subject
 * nobody has reviewed.
 */
export function AdminRedactionRule() {
  const { ruleId: param } = useParams();
  // One route serves both, because "new" cannot collide with a uuid and a second
  // route would duplicate every line of this form.
  const ruleId = param === "new" ? undefined : param;
  const navigate = useNavigate();
  const cloneFrom =
    (useLocation().state as { cloneFrom?: RedactionRule } | null)?.cloneFrom ?? null;
  const status = useRedactionStatus();
  // The rule comes from the same listing the previous screen renders, so
  // arriving by link costs no request that the list did not already make.
  const rules = useRedactionRules({ limit: 200 });
  const existing = rules.data?.items.find((rule) => rule.id === ruleId) ?? null;

  const create = useCreateRedactionRule();
  const update = useUpdateRedactionRule();
  const toast = useOptionalToast();

  // Cloning seeds synchronously from router state, so plain initialisers are
  // enough; editing seeds in the keyed block below, once the listing lands.
  const [name, setName] = useState(cloneFrom ? `${cloneFrom.name} (copy)` : "");
  const [scope, setScope] = useState<RedactionScope | "">(cloneFrom ? "" : "all");
  const [scopeId, setScopeId] = useState("");
  const [policy, setPolicy] = useState<RedactionPolicy>(
    cloneFrom ? structuredClone(cloneFrom.policy) : EMPTY_POLICY,
  );
  const [reason, setReason] = useState(cloneFrom ? cloneFrom.reason : "");
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
    if (scope === "") return; // Unreachable: the button is disabled until a subject is chosen.
    const done = {
      // The toast, not the redirect, is what says the write landed: the rule
      // screen is gone the moment the navigation below runs, so a Notice there
      // could never be read.
      onSuccess: () => {
        toast?.add({
          title: existing
            ? "Rule saved."
            : cloneFrom
              ? "Clone created — it starts inactive."
              : "Rule created.",
          type: "success",
        });
        navigate("/admin/redaction");
      },
      onError: (error: Error) =>
        toast?.add({
          title: "Could not save the rule",
          description: error instanceof Error ? error.message : "Unknown error.",
          type: "error",
        }),
    };
    if (existing) {
      // The subject travels only when it changed: the gateway validates what
      // it is given, and revalidating a subject that has since been deleted
      // would turn an edit of anything else into a 404 through no fault of
      // the admin.
      const subjectChanged =
        existing.scope !== scope || (existing.scope_id ?? "") !== scopeId;
      update.mutate(
        {
          id: existing.id,
          name,
          policy,
          reason,
          ...(subjectChanged && {
            scope,
            scope_id: scope === "all" ? null : scopeId,
          }),
        },
        done,
      );
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
          // A clone is a draft for another subject: inactive until reviewed,
          // because redacting is the kind of thing that must not start as a
          // side effect of copying.
          ...(cloneFrom && { is_active: false }),
        },
        done,
      );
    }
  };

  if (ruleId && rules.isPending) {
    return (
      <div className={PAGE}>
        <Spinner label="Loading the rule" />
      </div>
    );
  }
  if (ruleId && !existing) {
    return (
      <div className={PAGE}>
        <Notice tone="danger" title="No such rule">
          It may have been deleted. <a href="/admin/redaction">Back to redaction</a>
        </Notice>
      </div>
    );
  }

  return (
    <div className={PAGE}>
      <PageHeader
        title={
          existing
            ? `Rule · ${existing.subject_label ?? scopeNoun(existing.scope)}`
            : cloneFrom
              ? "Clone rule"
              : "New rule"
        }
        subtitle="The strictest applicable rule wins, so adding one can only protect more."
        actions={<Button onClick={() => navigate("/admin/redaction")}>Back</Button>}
      />

      {cloneFrom && !existing && (
        <Notice tone="info" title={`Cloning “${cloneFrom.name || "unnamed rule"}”`}>
          The policy and reason are copied. A clone starts inactive — choose its
          subject, review it, and activate it from the rules list.
        </Notice>
      )}

      {error ? (
        <Notice tone="danger" title="Could not save the rule">
          {error instanceof Error ? error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Card title="Subject">
        {existing && existing.scope !== "all" && existing.subject_label === null && (
          // The one state a rule cannot stay in: scope_id is not a foreign key,
          // so a deleted subject leaves the rule matching nothing. Saying so is
          // the point of the subject_label contract; offering the repair is
          // what an editable subject is for.
          <Notice tone="warn" title="This rule's subject no longer exists">
            It matches no request. Choose another {scopeNoun(existing.scope).toLowerCase()}{" "}
            below, or scope it to every request.
          </Notice>
        )}
        <Select
          label="Scope"
          value={scope}
          onChange={(event) => {
            setScope(event.target.value as RedactionScope | "");
            setScopeId("");
          }}
        >
          {/* The clone's subject is deliberately unset — it cannot keep the
              source's, and defaulting to the catch-all would make "Every
              request" a choice nobody made. */}
          {cloneFrom && !existing && <option value="">Choose a subject…</option>}
          {SCOPE_OPTIONS.map((option) => (
            <option key={option} value={option}>
              {scopeNoun(option)}
            </option>
          ))}
        </Select>
        {scope !== "" && <SubjectPicker scope={scope} value={scopeId} onChange={setScopeId} />}
      </Card>

      <PolicyFields
        policy={policy}
        onChange={setPolicy}
        entityTypes={entityTypes}
        scoreThreshold={status.data?.score_threshold ?? null}
      />

      <Card title="Name and reason">
        <div className={FORM}>
          <input
            aria-label="Rule name"
            className={INPUT}
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Rule name"
          />
          <input
            aria-label="Reason (optional)"
            className={INPUT}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Reason (optional)"
          />
          <div>
            <Button
              variant="primary"
              busy={saving}
              // A scoped rule without a subject would match nothing, and a
              // clone has none until one is chosen.
              disabled={scope === "" || (scope !== "all" && !scopeId)}
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
