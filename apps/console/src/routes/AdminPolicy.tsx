import { Button, Card, Input, Notice, Spinner } from "@llmp/ui";
import { useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router";

import { PageHeader } from "../components/PageHeader";
import { PolicyFields, seededPolicy } from "../components/PolicyFields";
import { RulePanel } from "../components/RulePanel";
import { useRedactionStatus } from "../lib/admin";
import { Tip } from "../components/Tip";
import { FORM, PAGE } from "../lib/layout";
import { usePolicies, usePrivacyEngines, useSavePolicy } from "../lib/privacy";
import { useOptionalToast } from "../lib/toast";
import { withModes, withoutModes } from "../lib/privacy";
import type { RedactionPolicy, RedactionPolicyRecord } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * One redaction policy, on its own page (ADR 0113): a name and the policy
 * document in two groups (patterns, and entities read by an engine the policy
 * picks), a mode for each. No scope: pipelines choose where it applies.
 *
 * A page rather than a dialog because a policy is thirty entity types, a set of
 * patterns and an allow-list. It starts with the credential patterns present
 * and everything else off, as a new rule always did: a deployment filters nothing
 * until somebody says otherwise.
 */
export function AdminPolicy() {
  const { policyId: param } = useParams();
  const policyId = param === "new" ? undefined : param;
  const navigate = useNavigate();
  const locationState = useLocation().state as {
    cloneFrom?: RedactionPolicyRecord;
    fromTemplate?: { name: string; policy: RedactionPolicy; note: string | null };
  } | null;
  const cloneFrom = locationState?.cloneFrom ?? null;
  const fromTemplate = locationState?.fromTemplate ?? null;
  const status = useRedactionStatus();
  const policies = usePolicies();
  const engines = usePrivacyEngines();
  const save = useSavePolicy();
  const toast = useOptionalToast();
  const existing = policies.data?.items.find((p) => p.id === policyId) ?? null;

  const [name, setName] = useState(
    cloneFrom ? `${cloneFrom.name} (copy)` : (fromTemplate?.name ?? ""),
  );
  const [policy, setPolicy] = useState<RedactionPolicy>(
    cloneFrom
      ? withModes(structuredClone(cloneFrom.policy))
      : fromTemplate
        ? structuredClone(fromTemplate.policy)
        : seededPolicy(),
  );
  const [shared, setShared] = useState(cloneFrom?.shared_with_users ?? false);
  const [reason, setReason] = useState(cloneFrom?.reason ?? "");
  const [active, setActive] = useState(cloneFrom ? false : true);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);

  if (existing && loadedFor !== existing.id) {
    setLoadedFor(existing.id);
    setName(existing.name);
    setPolicy(withModes(structuredClone(existing.policy)));
    setShared(existing.shared_with_users);
    setReason(existing.reason);
    setActive(existing.is_active);
  }

  if (policyId && policies.isPending) {
    return (
      <div className={PAGE}>
        <Spinner label={m["privacy.loading"]()} />
      </div>
    );
  }
  if (policyId && !existing) {
    return (
      <div className={PAGE}>
        <Notice tone="danger" title={m["privacy.policy.missing"]()}>
          <a href="/admin/privacy/policies">{m["privacy.policy.back"]()}</a>
        </Notice>
      </div>
    );
  }

  const submit = () =>
    save.mutate(
      {
        id: policyId,
        body: {
          name,
          policy: withoutModes(policy) as never,
          reason,
          is_active: active,
          shared_with_users: shared,
        },
      },
      {
        onSuccess: () => {
          toast?.add({ title: m["privacy.policy.saved"](), type: "success" });
          navigate("/admin/privacy/policies");
        },
        onError: (error: Error) =>
          toast?.add({
            title: m["privacy.save_error"](),
            // i18n: server text, the gateway's own error message.
            description: error.message,
            type: "error",
          }),
      },
    );

  return (
    <div className={PAGE}>
      <PageHeader
        title={name.trim() || m["privacy.policy.title_new"]()}
        subtitle={m["privacy.policy.subtitle"]()}
      />
      {save.error && (
        <Notice tone="danger" title={m["privacy.save_error"]()}>
          {/* i18n: server text, the gateway's own error message. */}
          {save.error.message}
        </Notice>
      )}
      {fromTemplate && !policyId && (
        <Notice title={m["policy_template.note_added"]()}>
          {fromTemplate.note ?? m["policy_template.lead"]()}
        </Notice>
      )}
      <div className="grid items-start gap-5 min-[80rem]:grid-cols-[minmax(0,1fr)_minmax(20rem,25rem)]">
        <div className="flex min-w-0 flex-col gap-5">
          <Card title={m["privacy.policy.name_card"]()} description={m["privacy.policy.name_hint"]()}>
            <div className={FORM}>
              <Input
                label={m["privacy.policy.name"]()}
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
              <Input
                label={m["privacy.policy.reason"]()}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
              <label className="flex items-center gap-2 text-body">
                <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
                {m["privacy.policy.active"]()}
              </label>
              <label className="flex items-center gap-2 text-body">
                <input
                  type="checkbox"
                  checked={shared}
                  onChange={(e) => setShared(e.target.checked)}
                />
                {m["privacy.policy.shared"]()}
                <Tip text={m["privacy.policy.shared_tip"]()} />
              </label>
            </div>
          </Card>
          <PolicyFields
            policy={policy}
            onChange={setPolicy}
            engines={engines.data ?? null}
            scoreThreshold={status.data?.score_threshold ?? null}
            detectionOnly
          />
        </div>
        <aside className="min-w-0 min-[80rem]:sticky min-[80rem]:top-[5.5rem]">
          <RulePanel policy={policy} />
        </aside>
      </div>
      <div
        role="region"
        aria-label={m["privacy.edit.savebar"]()}
        className="sticky bottom-0 z-10 flex flex-wrap items-center justify-end gap-2 rounded-lg border border-line bg-surface px-4 py-3 shadow-md"
      >
        <Button onClick={() => navigate("/admin/privacy/policies")}>{m["privacy.policy.back"]()}</Button>
        <Button variant="primary" busy={save.isPending} disabled={!name.trim()} onClick={submit}>
          {existing ? m["privacy.edit.save"]() : m["privacy.edit.create"]()}
        </Button>
      </div>
    </div>
  );
}
