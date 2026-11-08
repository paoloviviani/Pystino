import { Badge, Button, Dialog, Input, Notice, Spinner } from "@llmp/ui";
import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";

import { PageHeader } from "../components/PageHeader";
import { PrivacyRuleBuilder } from "../components/PrivacyRuleBuilder";
import { useGroups } from "../lib/admin";
import { MUTED, PAGE } from "../lib/layout";
import {
  KINDS,
  kindLabel,
  ruleSentence,
  useCheckPrivacyRule,
  useCreatePrivacyRule,
  useDeletePrivacyRule,
  usePrivacyProfiles,
  usePrivacyRules,
  useUpdatePrivacyRule,
} from "../lib/privacy";
import { PRIVACY_TEMPLATES, type PrivacyTemplate } from "../lib/privacyTemplates";
import { useOptionalToast } from "../lib/toast";
import type { PrivacyAction, PrivacyCondition, PrivacyIssue, PrivacyKind } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * One rule: its kind (fixed once saved), a name, and the sentence builder.
 *
 * Only real problems are shown, one line each, and they are asked of the
 * gateway as the rule is written (`/rules/check`), so what the editor warns
 * about is what the list will badge after saving.
 */

const DEFAULT_ACTION: Record<PrivacyKind, PrivacyAction> = {
  read: { type: "reader", reader: "ocr" },
  embed: { type: "refuse" },
};

export function AdminPrivacyRule() {
  const { ruleId: param } = useParams();
  const ruleId = param === "new" ? undefined : param;
  const [search] = useSearchParams();
  const navigate = useNavigate();
  const toast = useOptionalToast();
  const rules = usePrivacyRules();
  const profiles = usePrivacyProfiles();
  const groups = useGroups();
  const existing = rules.data?.items.find((r) => r.id === ruleId) ?? null;

  const create = useCreatePrivacyRule();
  const update = useUpdatePrivacyRule();
  const remove = useDeletePrivacyRule();
  const check = useCheckPrivacyRule();

  const startKind: PrivacyKind = search.get("kind") === "embed" ? "embed" : "read";
  const [kind, setKind] = useState<PrivacyKind>(startKind);
  const [name, setName] = useState("");
  const [conditions, setConditions] = useState<PrivacyCondition[]>([]);
  const [action, setAction] = useState<PrivacyAction>(DEFAULT_ACTION[startKind]);
  const [observeOnly, setObserveOnly] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [templates, setTemplates] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [issues, setIssues] = useState<PrivacyIssue[]>([]);
  const [problem, setProblem] = useState<string | null>(null);

  // Keyed seeding, as the redaction rule form does: the fields take the rule's
  // values once the listing lands, and not again on every refetch.
  if (existing && loadedFor !== existing.id) {
    setLoadedFor(existing.id);
    setKind(existing.kind as PrivacyKind);
    setName(existing.name);
    setConditions(existing.conditions.map((c) => ({ ...c })));
    setAction({ ...existing.action } as PrivacyAction);
    setObserveOnly(existing.observe_only);
    setEnabled(existing.enabled);
  }

  const profileItems = profiles.data?.items ?? [];
  const names = new Map(profileItems.map((p) => [p.id, p.name]));
  const groupNames = (groups.data?.items ?? []).map((g) => g.name);

  // Ask the gateway what a rule like this would be warned about. A short pause
  // after the last change, so typing does not send a request per letter.
  const { mutate: runCheck } = check;
  useEffect(() => {
    const timer = setTimeout(() => {
      runCheck(
        {
          kind,
          name: name || "Draft",
          enabled,
          observe_only: observeOnly,
          conditions,
          action,
          ...(ruleId ? { rule_id: ruleId } : {}),
        },
        {
          onSuccess: (result) => {
            setIssues(result.issues);
            setProblem(result.error ?? null);
          },
        },
      );
    }, 300);
    return () => clearTimeout(timer);
  }, [kind, name, enabled, observeOnly, conditions, action, ruleId, runCheck]);

  const apply = (template: PrivacyTemplate) => {
    setKind(template.kind);
    setName(template.name());
    setConditions(template.conditions.map((c) => ({ ...c })));
    setAction({ ...template.action });
    setObserveOnly(template.observeOnly ?? false);
    setTemplates(false);
  };

  const changeKind = (next: PrivacyKind) => {
    if (next === kind) return;
    // Conditions and actions are specific to a kind: carrying them over would
    // keep ones the other kind does not have.
    setKind(next);
    setConditions([]);
    setAction(DEFAULT_ACTION[next]);
  };

  const saving = create.isPending || update.isPending;
  const error = create.error ?? update.error ?? remove.error;
  const done = (message: string) => ({
    onSuccess: () => {
      toast?.add({ title: message });
      navigate("/admin/privacy");
    },
  });

  const save = () => {
    if (existing) {
      update.mutate(
        { id: existing.id, body: { name, enabled, observe_only: observeOnly, conditions, action } },
        done(m["privacy.toast.saved"]()),
      );
    } else {
      create.mutate(
        { kind, name, enabled, observe_only: observeOnly, conditions, action },
        done(m["privacy.toast.created"]()),
      );
    }
  };

  if (ruleId && rules.isPending) return <Spinner label={m["privacy.loading"]()} />;
  if (ruleId && !existing && rules.data) {
    return (
      <Notice tone="warn" title={m["privacy.missing.title"]()}>
        {m["privacy.missing.body"]()}
      </Notice>
    );
  }

  return (
    <div className={PAGE}>
      <PageHeader
        title={existing ? m["privacy.rule.title_edit"]() : m["privacy.rule.title_new"]()}
        subtitle={ruleSentence({ kind, conditions, action }, names)}
        actions={
          <>
            <Button type="button" onClick={() => setTemplates(true)}>
              {m["privacy.rule.templates"]()}
            </Button>
            <Button type="button" onClick={() => navigate("/admin/privacy")}>
              {m["privacy.rule.back"]()}
            </Button>
          </>
        }
      />

      <Dialog
        open={templates}
        title={m["privacy.templates.title"]()}
        onClose={() => setTemplates(false)}
      >
        <ul className="m-0 flex list-none flex-col gap-3 p-0">
          {PRIVACY_TEMPLATES.map((template) => (
            <li key={template.id} className="flex items-start justify-between gap-3">
              <div>
                <p className="m-0 font-medium">{template.title()}</p>
                <p className={`m-0 text-small ${MUTED}`}>{template.explain()}</p>
              </div>
              <Button type="button" onClick={() => apply(template)}>
                {m["privacy.templates.use"]()}
              </Button>
            </li>
          ))}
        </ul>
      </Dialog>

      <div className="flex flex-col gap-4 rounded-lg border border-line bg-surface p-4">
        {!existing && (
          <div role="radiogroup" aria-label={m["privacy.rule.kind"]()} className="flex flex-wrap gap-2">
            {KINDS.map((k) => (
              <Button
                key={k}
                type="button"
                role="radio"
                aria-checked={kind === k}
                variant={kind === k ? "primary" : "secondary"}
                onClick={() => changeKind(k)}
              >
                {kindLabel(k)}
              </Button>
            ))}
          </div>
        )}
        {existing && (
          <p className="m-0">
            <Badge>{kindLabel(kind)}</Badge>
          </p>
        )}
        <Input
          label={m["privacy.rule.name"]()}
          value={name}
          maxLength={120}
          onChange={(event) => setName(event.target.value)}
        />
        <PrivacyRuleBuilder
          kind={kind}
          conditions={conditions}
          action={action}
          onConditions={setConditions}
          onAction={setAction}
          profiles={profileItems}
          groups={groupNames}
        />
        <div className="flex flex-wrap gap-x-6 gap-y-2">
          <label className="flex items-center gap-2 text-body">
            <input
              type="checkbox"
              checked={observeOnly}
              onChange={(event) => setObserveOnly(event.target.checked)}
            />
            {m["privacy.rule.observe_only"]()}
          </label>
          <label className="flex items-center gap-2 text-body">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(event) => setEnabled(event.target.checked)}
            />
            {m["privacy.rule.enabled"]()}
          </label>
        </div>
        {observeOnly && <p className={`m-0 text-small ${MUTED}`}>{m["privacy.rule.observe_hint"]()}</p>}

        {problem && <Notice tone="danger">{problem}</Notice>}
        {issues.map((issue) => (
          <Notice key={`${issue.code}-${issue.message}`} tone="warn">
            {issue.message}
          </Notice>
        ))}
        {error && (
          <Notice tone="danger" title={m["privacy.error.save"]()}>
            {error instanceof Error ? error.message : m["privacy.error.unknown"]()}
          </Notice>
        )}

        <div className="flex flex-wrap justify-between gap-2">
          <div>
            {existing && (
              <Button type="button" onClick={() => setConfirmDelete(true)}>
                {m["privacy.rule.delete"]()}
              </Button>
            )}
          </div>
          <div className="flex gap-2">
            <Button type="button" onClick={() => navigate("/admin/privacy")}>
              {m["privacy.rule.cancel"]()}
            </Button>
            <Button
              type="button"
              variant="primary"
              disabled={saving || !name.trim() || problem !== null}
              onClick={save}
            >
              {m["privacy.rule.save"]()}
            </Button>
          </div>
        </div>
      </div>

      <Dialog
        open={confirmDelete}
        title={m["privacy.delete.title"]()}
        onClose={() => setConfirmDelete(false)}
        footer={
          <>
            <Button type="button" onClick={() => setConfirmDelete(false)}>
              {m["privacy.rule.cancel"]()}
            </Button>
            <Button
              type="button"
              variant="primary"
              onClick={() =>
                existing && remove.mutate(existing.id, done(m["privacy.toast.deleted"]()))
              }
            >
              {m["privacy.rule.delete"]()}
            </Button>
          </>
        }
      >
        <p className="m-0">{m["privacy.delete.body"]({ name })}</p>
      </Dialog>
    </div>
  );
}
