/**
 * The identity policy (ADR 0048): who may come to exist, which claim names
 * their groups, what an IdP group means here.
 *
 * This is the console-editable half of OIDC. The connection — issuer, client
 * secret, redirect — stays in the environment on purpose: it is read once at
 * startup, and making the IdP connection hot would put its reachability on the
 * request path. The policy is what an operator actually changes, so it lives
 * in the append-only `oidc_config` table and lands on every worker within the
 * poll interval the API reports.
 *
 * Roles are not part of it (ADR 0069): authorisation is a gateway fact, so an
 * administrator is made on the Users screen — never by a group claim, and no
 * longer by naming an admin group here either. That input existed while the
 * flag was derived from membership; the derivation is gone and the input went
 * with it.
 */

import { Card, Notice, Select, Spinner } from "@llmp/ui";
import { useEffect, useState } from "react";
import { useOidcPolicy, useUpdateOidcPolicy } from "../lib/admin";
import type { OidcPolicyInput } from "../lib/types";
import { FORM } from "../lib/layout";
import { useOptionalToast } from "../lib/toast";

export function ProvisioningPolicySection() {
  const policy = useOidcPolicy();
  const save = useUpdateOidcPolicy();
  const toast = useOptionalToast();

  // Local edit state per field: each saves on its own, as its own decision —
  // one knob, one row in the policy history — rather than a form that must be
  // submitted as a whole.
  const [autoProvision, setAutoProvision] = useState(true);
  const [unknownPolicy, setUnknownPolicy] = useState<"refuse" | "create_inactive">("refuse");

  useEffect(() => {
    if (!policy.data) return;
    setAutoProvision(policy.data.auto_provision);
    setUnknownPolicy(policy.data.unknown_user_policy);
  }, [policy.data]);

  const put = (body: OidcPolicyInput, ok: string) =>
    save.mutate(body, {
      onSuccess: () => toast?.add({ title: ok, type: "success" }),
      onError: (caught: unknown) =>
        toast?.add({
          title: caught instanceof Error ? caught.message : "Could not save",
          type: "error",
        }),
    });

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

  return (
    <div className={FORM}>
      {save.error ? (
        <Notice tone="danger" title="Could not save">
          {save.error instanceof Error ? save.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <label className="flex cursor-pointer items-center gap-2">
        <input
          type="checkbox"
          checked={autoProvision}
          onChange={(event) => {
            setAutoProvision(event.target.checked);
            put({ auto_provision: event.target.checked }, "Provisioning updated");
          }}
        />
        <span>
          Create an account on first sign-in
          <span className="mt-0.5 block text-xs text-ink-faint">
            {autoProvision
              ? "On: anyone who signs in through the identity provider gets an active account at once. Turn it off to refuse first-time sign-ins or hold them for your approval."
              : "Off: a first-time sign-in is not a user yet. It follows the rule below."}
          </span>
        </span>
      </label>

      {autoProvision ? null : (
        <Select
          label="First-time sign-in while provisioning is off"
          value={unknownPolicy}
          onChange={(event) => {
            const value = event.target.value as "refuse" | "create_inactive";
            setUnknownPolicy(value);
            put({ unknown_user_policy: value }, "First-time sign-in rule updated");
          }}
          hint={
            unknownPolicy === "refuse"
              ? "The stranger is told to ask an administrator. Nothing is created."
              : "The account is created disabled, waiting for you to enable it on the Users screen."
          }
        >
          <option value="refuse">Refuse — ask an administrator</option>
          <option value="create_inactive">Create disabled, awaiting approval</option>
        </Select>
      )}
    </div>
  );
}
