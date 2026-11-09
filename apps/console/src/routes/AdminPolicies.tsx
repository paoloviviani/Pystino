import { Badge, Button, Card, Dialog, EmptyState, Notice, Spinner, buttonClasses } from "@llmp/ui";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { PolicyTemplatePicker } from "../components/PolicyTemplatePicker";
import { PrivacyHeader } from "../components/PrivacyHeader";
import { Tip } from "../components/Tip";
import { summarisePolicy } from "../lib/entities";
import { buildPolicyFromTemplate, skippedNote } from "../lib/policyTemplates";
import { MUTED, PAGE } from "../lib/layout";
import { useDeletePolicy, usePolicies, usePrivacyEngines, useSavePolicy } from "../lib/privacy";
import type { RedactionPolicyRecord } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * Redaction policies (ADR 0113): what to hide, and nothing else.
 *
 * A policy has no scope. Which requests it applies to is a pipeline's business,
 * so the same policy can serve a chat pipeline and an embedding one, and a
 * pipeline's scope is edited without touching what is hidden.
 */
export function AdminPolicies() {
  const policies = usePolicies();
  const save = useSavePolicy();
  const remove = useDeletePolicy();
  const navigate = useNavigate();
  const engines = usePrivacyEngines();
  const [picking, setPicking] = useState(false);
  const [deleting, setDeleting] = useState<RedactionPolicyRecord | null>(null);
  const items = policies.data?.items ?? [];

  return (
    <div className={PAGE}>
      <PrivacyHeader
        tab="policies"
        actions={
          <>
            <Button variant="primary" onClick={() => navigate("/admin/privacy/policies/new")}>
              {m["privacy.policies.new"]()}
            </Button>
            <Button onClick={() => setPicking(true)}>{m["policy_template.new"]()}</Button>
          </>
        }
      />
      <PolicyTemplatePicker
        open={picking}
        busy={engines.isPending}
        onClose={() => setPicking(false)}
        skippedFor={(template) => buildPolicyFromTemplate(template, engines.data).skipped}
        onPick={(template) => {
          const built = buildPolicyFromTemplate(template, engines.data);
          navigate("/admin/privacy/policies/new", {
            state: {
              fromTemplate: {
                name: template.name(),
                policy: built.policy,
                note: skippedNote(built.skipped),
              },
            },
          });
        }}
      />
      <p className={`m-0 flex items-center gap-2 text-small ${MUTED}`}>
        {m["privacy.policies.lead"]()}
        <Tip text={m["privacy.policies.lead_tip"]()} />
      </p>
      {policies.isPending && <Spinner label={m["privacy.loading"]()} />}
      {Boolean(policies.error) && (
        <Notice tone="danger" title={m["privacy.load_error"]()}>
          {/* i18n: server text, the API's own message. */}
          {policies.error instanceof Error ? policies.error.message : m["privacy.unknown_error"]()}
        </Notice>
      )}
      {Boolean(save.error || remove.error) && (
        <Notice tone="danger" title={m["privacy.save_error"]()}>
          {/* i18n: server text, the API's own message. */}
          {((save.error ?? remove.error) as Error).message}
        </Notice>
      )}
      {policies.data && items.length === 0 && (
        <EmptyState title={m["privacy.policies.empty"]()} detail={m["privacy.policies.empty_hint"]()} />
      )}
      {items.map((p) => (
        <Card key={p.id}>
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <Link to={`/admin/privacy/policies/${p.id}`}>
                  {/* i18n: server text, the name the administrator gave the policy. */}
                  <strong>{p.name}</strong>
                </Link>
                {p.shared_with_users && (
                  <Badge tone="accent">{m["privacy.policies.shared_badge"]()}</Badge>
                )}
                {p.engine_available === false && (
                  <Badge tone="warn">{m["privacy.policies.engine_down"]()}</Badge>
                )}
                {!p.is_active && <Badge tone="neutral">{m["privacy.badge.off"]()}</Badge>}
                {p.used_by.length > 0 && (
                  <Badge tone="neutral">
                    {m["privacy.policies.used_by"]({ count: String(p.used_by.length) })}
                  </Badge>
                )}
              </div>
              <div className="flex flex-wrap gap-1">
                <Link
                  to={`/admin/privacy/policies/${p.id}`}
                  className={buttonClasses("secondary", "sm")}
                >
                  {m["privacy.row.edit"]()}
                </Link>
                <Button
                  size="sm"
                  onClick={() => navigate("/admin/privacy/policies/new", { state: { cloneFrom: p } })}
                >
                  {m["privacy.row.duplicate"]()}
                </Button>
                <Button size="sm" className="text-danger" onClick={() => setDeleting(p)}>
                  {m["privacy.row.delete"]()}
                </Button>
              </div>
            </div>
            <p className="m-0 text-body">{summarisePolicy(p.policy)}</p>
            {p.used_by.length > 0 && (
              <p className={`m-0 text-small ${MUTED}`}>
                {m["privacy.policies.used_by_names"]({ names: p.used_by.join(", ") })}
              </p>
            )}
          </div>
        </Card>
      ))}
      <Dialog
        open={deleting !== null}
        title={m["privacy.policies.delete_title"]({ name: deleting?.name ?? "" })}
        onClose={() => setDeleting(null)}
        footer={
          <>
            <Button onClick={() => setDeleting(null)}>{m["privacy.delete.cancel"]()}</Button>
            <Button
              variant="danger"
              busy={remove.isPending}
              disabled={(deleting?.used_by.length ?? 0) > 0}
              onClick={() => {
                if (deleting) remove.mutate(deleting.id, { onSuccess: () => setDeleting(null) });
              }}
            >
              {m["privacy.delete.confirm"]()}
            </Button>
          </>
        }
      >
        <p className="m-0">
          {(deleting?.used_by.length ?? 0) > 0
            ? m["privacy.policies.in_use"]({ names: deleting?.used_by.join(", ") ?? "" })
            : m["privacy.policies.delete_body"]()}
        </p>
      </Dialog>
    </div>
  );
}
