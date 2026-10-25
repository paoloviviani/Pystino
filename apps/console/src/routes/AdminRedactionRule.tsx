import { Button, Card, Input, Notice, Select, Spinner } from "@llmp/ui";
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
import { RulePanel } from "../components/RulePanel";
import { Message } from "../components/Message";
import { SubjectPicker, scopeNoun, scopeNounLower } from "../components/SubjectPicker";
import { FORM, PAGE } from "../lib/layout";
import { m } from "../paraglide/messages.js";
import { useOptionalToast } from "../lib/toast";

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

/** A new rule's form before anyone has touched it, in the shape `dirty` compares. */
const INITIAL = JSON.stringify({
  name: "",
  scope: "all",
  scopeId: "",
  policy: EMPTY_POLICY,
  reason: "",
});

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
            ? m["redaction_rule.toast.saved"]()
            : cloneFrom
              ? m["redaction_rule.toast.cloned"]()
              : m["redaction_rule.toast.created"](),
          type: "success",
        });
        navigate("/admin/redaction");
      },
      onError: (error: Error) =>
        toast?.add({
          title: m["redaction_rule.error.save_title"](),
          // i18n: server text — the gateway's own error message.
          description: error instanceof Error ? error.message : m["redaction_rule.error.unknown"](),
          type: "error",
        }),
    };
    if (existing) {
      // The subject travels only when it changed: the gateway validates what
      // it is given, and revalidating a subject that has since been deleted
      // would turn an edit of anything else into a 404 through no fault of
      // the admin.
      const subjectChanged = existing.scope !== scope || (existing.scope_id ?? "") !== scopeId;
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
        <Spinner label={m["redaction_rule.loading"]()} />
      </div>
    );
  }
  if (ruleId && !existing) {
    return (
      <div className={PAGE}>
        <Notice tone="danger" title={m["redaction_rule.missing.title"]()}>
          <Message
            message={m["redaction_rule.missing.body"]}
            markup={{
              link: (children) => <a href="/admin/redaction">{children}</a>,
            }}
          />
        </Notice>
      </div>
    );
  }

  // What the form held when it was last saved, or loaded. A rule that has been
  // typed into and put back is not "unsaved", which a flag set on the first
  // keystroke would say. A clone has no saved state at all, so it is always
  // unsaved until it is created.
  const current = JSON.stringify({ name, scope, scopeId, policy, reason });
  const baseline = existing
    ? JSON.stringify({
        name: existing.name,
        scope: existing.scope,
        scopeId: existing.scope_id ?? "",
        policy: existing.policy,
        reason: existing.reason,
      })
    : cloneFrom
      ? null
      : INITIAL;
  const dirty = baseline === null || current !== baseline;
  // A scoped rule without a subject would match nothing, and a clone has none
  // until one is chosen.
  const needsSubject = scope === "" || (scope !== "all" && !scopeId);

  const title = name.trim()
    ? name.trim()
    : existing
      ? m["redaction_rule.title.unnamed"]()
      : cloneFrom
        ? m["redaction_rule.title.clone"]()
        : m["redaction_rule.title.new"]();
  // i18n: server text — `subject_label` is the API's name for the subject; the
  // scope noun is the console's own when the API has none.
  const subtitle = existing
    ? existing.scope === "all"
      ? m["redaction_rule.subtitle.every"]()
      : existing.subject_label === null
        ? m["redaction_rule.subtitle.subject_missing"]({
            scope: scopeNounLower(existing.scope),
          })
        : m["redaction_rule.subtitle.subject"]({
            scope: scopeNounLower(existing.scope),
            subject: existing.subject_label,
          })
    : m["redaction_rule.subtitle"]();

  return (
    <div className={PAGE}>
      {/* The rule's name is the title: a page called "Rule · research" said
          which subject it was about and not which rule, and two rules for two
          groups looked the same in a tab. */}
      <PageHeader title={title} subtitle={subtitle} />

      {cloneFrom && !existing && (
        <Notice
          tone="info"
          title={m["redaction_rule.clone_notice.title"]({
            name: cloneFrom.name || m["redaction_rule.clone_notice.unnamed"](),
          })}
        >
          {m["redaction_rule.clone_notice.body"]()}
        </Notice>
      )}

      {error ? (
        <Notice tone="danger" title={m["redaction_rule.error.save_title"]()}>
          {/* i18n: server text — the gateway's own error message. */}
          {error instanceof Error ? error.message : m["redaction_rule.error.unknown"]()}
        </Notice>
      ) : null}

      {/* Two columns where there is room: the form, and beside it what the form
          adds up to. Below 80rem the summary follows the form in one column. */}
      <div className="grid items-start gap-5 min-[80rem]:grid-cols-[minmax(0,1fr)_minmax(20rem,25rem)]">
        <div className="flex min-w-0 flex-col gap-5">
          <Card
            title={m["redaction_rule.name_card"]()}
            description={m["redaction_rule.name_card_hint"]()}
          >
            <div className={FORM}>
              <Input
                label={m["redaction_rule.name.label"]()}
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
              <Input
                label={m["redaction_rule.reason.label"]()}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </div>
          </Card>

          <Card
            title={m["redaction_rule.subject_card"]()}
            description={m["redaction_rule.subject_card_hint"]()}
          >
            <div className={FORM}>
              {existing && existing.scope !== "all" && existing.subject_label === null && (
                // The one state a rule cannot stay in: scope_id is not a foreign
                // key, so a deleted subject leaves the rule matching nothing.
                // Saying so is the point of the subject_label contract; offering
                // the repair is what an editable subject is for.
                <Notice tone="warn" title={m["redaction_rule.subject_missing.title"]()}>
                  {m["redaction_rule.subject_missing.body"]({
                    scope: scopeNounLower(existing.scope),
                  })}
                </Notice>
              )}
              <Select
                label={m["redaction_rule.scope.label"]()}
                value={scope}
                onChange={(event) => {
                  setScope(event.target.value as RedactionScope | "");
                  setScopeId("");
                }}
              >
                {/* The clone's subject is deliberately unset — it cannot keep
                    the source's, and defaulting to the catch-all would make
                    "Every request" a choice nobody made. */}
                {cloneFrom && !existing && (
                  <option value="">{m["redaction_rule.scope.choose"]()}</option>
                )}
                {SCOPE_OPTIONS.map((option) => (
                  <option key={option} value={option}>
                    {scopeNoun(option)}
                  </option>
                ))}
              </Select>
              {scope !== "" && (
                <SubjectPicker scope={scope} value={scopeId} onChange={setScopeId} />
              )}
            </div>
          </Card>

          <PolicyFields
            policy={policy}
            onChange={setPolicy}
            entityTypes={entityTypes}
            scoreThreshold={status.data?.score_threshold ?? null}
          />
        </div>

        {/* Sticky below the page header and the console's own bar (3.75rem), so
            the summary stays in view while a long list of types scrolls past. */}
        <aside className="min-w-0 min-[80rem]:sticky min-[80rem]:top-[5.5rem]">
          <RulePanel policy={policy} scope={scope} scopeId={scopeId} />
        </aside>
      </div>

      {/* Stuck to the bottom of the screen rather than sitting in the last card:
          the page is a long form, and the button that saves it used to be
          below everything, out of sight for most of the time it was needed. */}
      <div
        role="region"
        aria-label={m["redaction_rule.savebar.label"]()}
        className="sticky bottom-0 z-10 -mx-1 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line bg-surface px-4 py-3 shadow-md max-[40rem]:px-3"
      >
        <p className="m-0 text-body text-ink-muted" role="status">
          {needsSubject
            ? m["redaction_rule.savebar.need_subject"]()
            : dirty
              ? baseline === null
                ? m["redaction_rule.savebar.clone"]()
                : m["redaction_rule.savebar.unsaved"]()
              : existing
                ? m["redaction_rule.savebar.saved"]()
                : ""}
        </p>
        <div className="flex items-center gap-2">
          <Button onClick={() => navigate("/admin/redaction")}>{m["redaction_rule.back"]()}</Button>
          <Button variant="primary" busy={saving} disabled={needsSubject} onClick={submit}>
            {existing ? m["redaction_rule.submit.save"]() : m["redaction_rule.submit.create"]()}
          </Button>
        </div>
      </div>
    </div>
  );
}
