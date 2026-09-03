/**
 * The identity policy (ADR 0048): who may come to exist, which claim names
 * their groups, what an IdP group means here, which local group confers admin.
 *
 * This is the console-editable half of OIDC. The connection — issuer, client
 * secret, redirect — stays in the environment on purpose: it is read once at
 * startup, and making the IdP connection hot would put its reachability on the
 * request path. The policy is what an operator actually changes, so it lives
 * in the append-only `oidc_config` table and lands on every worker within the
 * poll interval the API reports.
 */

import {
  Badge,
  Button,
  Card,
  Input,
  Notice,
  Select,
  Spinner,
} from "@llmp/ui";
import { useEffect, useState } from "react";
import { useOidcPolicy, useUpdateOidcPolicy } from "../lib/admin";
import { CODE, DETAILS, DETAIL_LABEL, DETAIL_VALUE, FORM, FORM_ROW, PAGE } from "../lib/layout";
import { useOptionalToast } from "../lib/toast";
import { PageHeader } from "../components/PageHeader";

interface MappingRow {
  idp: string;
  local: string;
}

export function ProvisioningPolicySection() {
  const policy = useOidcPolicy();
  const save = useUpdateOidcPolicy();
  const toast = useOptionalToast();

  // The form is seeded from the policy in force, never from an empty document:
  // saving with a field left blank would otherwise record "the environment
  // decides" for a value the operator merely did not look at.
  const [autoProvision, setAutoProvision] = useState(true);
  const [unknownPolicy, setUnknownPolicy] = useState<"refuse" | "create_inactive">("refuse");
  const [groupsClaim, setGroupsClaim] = useState("groups");
  const [adminGroups, setAdminGroups] = useState("");
  const [mappings, setMappings] = useState<MappingRow[]>([]);
  const [reason, setReason] = useState("");

  useEffect(() => {
    if (!policy.data) return;
    setAutoProvision(policy.data.auto_provision);
    setUnknownPolicy(policy.data.unknown_user_policy);
    setGroupsClaim(policy.data.groups_claim);
    setAdminGroups(policy.data.admin_groups.join(", "));
    setMappings(policy.data.group_mappings.map((rule) => ({ ...rule })));
  }, [policy.data]);

  const close = () => {
    save.reset();
    setReason("");
  };

  const submit = () => {
    const cleaned = adminGroups
      .split(",")
      .map((name) => name.trim())
      .filter((name) => name !== "");
    save.mutate(
      {
        auto_provision: autoProvision,
        unknown_user_policy: autoProvision ? undefined : unknownPolicy,
        groups_claim: groupsClaim.trim(),
        admin_groups: cleaned,
        group_mappings: mappings.filter((rule) => rule.idp.trim() && rule.local.trim()),
        reason: reason.trim() || undefined,
      },
      {
        onSuccess: () => {
          toast?.add({ title: "Identity policy saved", type: "success" });
          close();
        },
        onError: () => toast?.add({ title: "Could not save the policy", type: "error" }),
      },
    );
  };

  if (policy.isPending || policy.error) {
    return (
      <Card>
        {policy.isPending ? (
          <Spinner label="Loading the identity policy" />
        ) : (
          <Notice tone="danger" title="Could not load the identity policy">
            {policy.error instanceof Error ? policy.error.message : "Unknown error."}
          </Notice>
        )}
      </Card>
    );
  }

  const data = policy.data;
  // The early returns above leave the query loaded; this is for the type
  // system, which cannot see through the query object's discriminated states.
  if (!data) return null;
  const changedLocally =
    autoProvision !== data.auto_provision ||
    (!autoProvision && unknownPolicy !== data.unknown_user_policy) ||
    groupsClaim.trim() !== data.groups_claim ||
    adminGroups !== data.admin_groups.join(", ") ||
    JSON.stringify(mappings) !== JSON.stringify(data.group_mappings);

  return (
    <div className={PAGE}>
      <PageHeader
        title="Identity"
        subtitle="Who may become a user, and what identity-provider groups mean here.
          Saved decisions take effect on every worker within a few seconds."
        actions={
          <Badge tone={data.source === "console" ? "accent" : "neutral"}>
            {data.source === "console" ? "set in the console" : "from the environment"}
          </Badge>
        }
      />

      {save.error ? (
        <Notice tone="danger" title="Could not save the policy">
          {save.error instanceof Error ? save.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Card title="Automatic provisioning">
        <div className={FORM}>
          <label className="flex cursor-pointer items-center gap-2">
            <input
              type="checkbox"
              checked={autoProvision}
              onChange={(event) => setAutoProvision(event.target.checked)}
            />
            <span>
              Create an account on first sign-in
              <span className="mt-0.5 block text-xs text-ink-faint">
                Off, a first-time sign-in is not a user yet: it follows the rule below.
              </span>
            </span>
          </label>

          {autoProvision ? null : (
            <Select
              label="First-time sign-in while provisioning is off"
              value={unknownPolicy}
              onChange={(event) =>
                setUnknownPolicy(event.target.value as "refuse" | "create_inactive")
              }
              hint={
                unknownPolicy === "refuse"
                  ? "The stranger is told to ask an administrator. Nothing is created."
                  : "The account is created disabled, waiting for you to enable it here."
              }
            >
              <option value="refuse">Refuse — ask an administrator</option>
              <option value="create_inactive">Create disabled, awaiting approval</option>
            </Select>
          )}
        </div>
      </Card>

      <Card title="Groups">
        <div className={FORM}>
          <Input
            label="Group claim"
            value={groupsClaim}
            onChange={(event) => setGroupsClaim(event.target.value)}
            hint="Where the identity provider puts the person's groups. Dotted paths reach
              into nested claims (e.g. realm_access.roles)."
          />
          <Input
            label="Administrator groups"
            value={adminGroups}
            onChange={(event) => setAdminGroups(event.target.value)}
            hint="Local group names, comma-separated. Membership of any of them grants
              admin, in both directions: leaving the group removes it."
          />

          <div>
            <div className="text-xs font-medium tracking-[0.01em] text-ink-muted">
              Mapping rules
            </div>
            <p className="mt-0.5 text-xs text-ink-faint">
              What an identity-provider group is called here. Unmapped groups keep
              their own name; several may share one local name.
            </p>
            <div className="mt-2 flex flex-col gap-2">
              {mappings.map((rule, index) => (
                <div key={index} className="flex items-end gap-2">
                  <Input
                    label={index === 0 ? "In the IdP" : undefined}
                    hideLabel={index !== 0}
                    value={rule.idp}
                    onChange={(event) =>
                      setMappings((current) =>
                        current.map((r, i) =>
                          i === index ? { ...r, idp: event.target.value } : r,
                        ),
                      )
                    }
                  />
                  <Input
                    label={index === 0 ? "Here" : undefined}
                    hideLabel={index !== 0}
                    value={rule.local}
                    onChange={(event) =>
                      setMappings((current) =>
                        current.map((r, i) =>
                          i === index ? { ...r, local: event.target.value } : r,
                        ),
                      )
                    }
                  />
                  <Button
                    variant="ghost"
                    onClick={() =>
                      setMappings((current) => current.filter((_, i) => i !== index))
                    }
                  >
                    Remove
                  </Button>
                </div>
              ))}
            </div>
            <Button
              variant="ghost"
              className="mt-2"
              onClick={() => setMappings((current) => [...current, { idp: "", local: "" }])}
            >
              Add mapping
            </Button>
          </div>
        </div>
      </Card>

      <Card title="Save">
        <div className={FORM}>
          <Input
            label="Reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            hint="Optional, and recorded with the change: the history of who opened or
              closed this door, and why, is kept permanently."
          />
          <div className={FORM_ROW}>
            <div className={DETAILS}>
              <dt className={DETAIL_LABEL}>In force</dt>
              <dd className={DETAIL_VALUE}>
                {data.source === "console" ? (
                  <>
                    Console decision
                    {data.configured?.changed_by ? (
                      <>
                        {" "}
                        by <span className={CODE}>{data.configured.changed_by}</span>
                      </>
                    ) : null}
                    {data.configured
                      ? `, ${new Date(data.configured.changed_at).toLocaleString()}`
                      : null}
                  </>
                ) : (
                  "The environment's settings"
                )}
              </dd>
              {data.configured?.reason ? (
                <>
                  <dt className={DETAIL_LABEL}>Reason</dt>
                  <dd className={DETAIL_VALUE}>{data.configured.reason}</dd>
                </>
              ) : null}
              <dt className={DETAIL_LABEL}>Propagation</dt>
              <dd className={DETAIL_VALUE}>
                every worker within {Math.round(data.propagation_seconds)}s
              </dd>
            </div>
            <div className={FORM} />
          </div>
          <div className="flex justify-end gap-2">
            <Button onClick={close} disabled={!changedLocally && reason === ""}>
              Reset
            </Button>
            <Button variant="primary" busy={save.isPending} disabled={!changedLocally} onClick={submit}>
              Save policy
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
