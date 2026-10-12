/**
 * The identity policy (ADR 0048; join modes ADR 0095): who may join, which
 * claim names their groups, what an IdP group means here.
 *
 * This is the console-editable half of OIDC. The connection — issuer, client
 * secret, redirect — stays in the environment on purpose: it is read once at
 * startup, and making the IdP connection hot would put its reachability on the
 * request path. The policy is what an operator actually changes, so it lives
 * in the append-only `oidc_config` table and lands on every worker within the
 * poll interval the API reports.
 *
 * Roles are not part of it (ADR 0069): authorisation is a gateway fact, so an
 * administrator is made on the Users screen — never by a group claim. The
 * join gate stands aside for administrators for the same reason: a
 * misconfigured group mode must not be able to lock every admin out.
 */

import { Button, Card, Notice, Select, Spinner } from "@llmp/ui";
import { useEffect, useState } from "react";
import { useGroups, useOidcPolicy, usePolicyPreview, useUpdateOidcPolicy } from "../lib/admin";
import type { JoinMode, OidcPolicyInput } from "../lib/types";
import { FORM } from "../lib/layout";
import { useOptionalToast } from "../lib/toast";

const JOIN_MODES: { value: JoinMode; label: string; hint: string }[] = [
  {
    value: "open",
    label: "Open",
    hint: "Anyone who signs in through the identity provider gets an active account at once.",
  },
  {
    value: "approval",
    label: "Approval",
    hint: "A first-time sign-in creates a disabled account in the Users page's Awaiting-approval queue; you enable the people you want.",
  },
  {
    value: "invite",
    label: "Invite",
    hint: "Only people you invited (or a directory pushed) can sign in. Everyone else is refused.",
  },
  {
    value: "group",
    label: "Group",
    hint: "Only members of the required groups may sign in — checked at every sign-in and every API call.",
  },
];

export function ProvisioningPolicySection() {
  const policy = useOidcPolicy();
  const save = useUpdateOidcPolicy();
  const preview = usePolicyPreview();
  const groups = useGroups();
  const toast = useOptionalToast();

  // Local edit state per field: each saves on its own, as its own decision —
  // one knob, one row in the policy history — rather than a form that must be
  // submitted as a whole.
  const [joinMode, setJoinMode] = useState<JoinMode>("open");
  const [requiredGroups, setRequiredGroups] = useState<string[]>([]);
  const [groupFallback, setGroupFallback] = useState<"refuse" | "approval">("refuse");
  // Picking Group with nothing required would save a gate that refuses
  // everyone but the admins; said inline rather than allowed.
  const [needsGroups, setNeedsGroups] = useState(false);

  useEffect(() => {
    if (!policy.data) return;
    setJoinMode(policy.data.join_mode);
    setRequiredGroups(policy.data.required_groups);
    setGroupFallback(policy.data.group_fallback);
  }, [policy.data]);

  // A refused save must not leave the radio (or the checkboxes) showing a
  // mode the server declined: everything goes back to what is in force.
  const resetToPolicy = () => {
    if (!policy.data) return;
    setJoinMode(policy.data.join_mode);
    setRequiredGroups(policy.data.required_groups);
    setGroupFallback(policy.data.group_fallback);
    setNeedsGroups(false);
    preview.reset();
  };

  const put = (body: OidcPolicyInput, ok: string) =>
    save.mutate(body, {
      onSuccess: () => {
        toast?.add({ title: ok, type: "success" });
        preview.reset();
      },
      onError: (caught: unknown) => {
        toast?.add({
          title: caught instanceof Error ? caught.message : "Could not save",
          type: "error",
        });
        resetToPolicy();
      },
    });

  // Saving group mode (or changed groups inside it) asks the preview first:
  // "N people will lose access at their next request" is a sentence worth
  // reading before it becomes true. Other modes save straight away.
  const saveGroupMode = (mode: "group", groups: string[], fallback: "refuse" | "approval") => {
    preview.mutate(
      { join_mode: mode, required_groups: groups },
      {
        onSuccess: (result) => {
          if (result.would_be_refused === 0) {
            put(
              { join_mode: mode, required_groups: groups, group_fallback: fallback },
              "Join mode updated",
            );
          }
        },
        onError: (caught: unknown) =>
          toast?.add({
            title: caught instanceof Error ? caught.message : "Could not preview the change",
            type: "error",
          }),
      },
    );
  };

  const pickMode = (mode: JoinMode) => {
    setJoinMode(mode);
    setNeedsGroups(false);
    if (mode === "group") {
      if (requiredGroups.length === 0) {
        setNeedsGroups(true);
        return;
      }
      saveGroupMode("group", requiredGroups, groupFallback);
      return;
    }
    put(
      mode === "open"
        ? { join_mode: "open" }
        : mode === "approval"
          ? { join_mode: "approval" }
          : { join_mode: "invite" },
      "Join mode updated",
    );
  };

  // The checkboxes are local state: ticking one after another must not save
  // every intermediate set as its own policy row (or confirm a misleading
  // "N will lose access" for a gate nobody asked for). "Save access groups"
  // runs the preview/confirm for the whole set, once.
  const changeGroups = (names: string[]) => {
    setRequiredGroups(names);
    setNeedsGroups(names.length === 0 && joinMode === "group");
    preview.reset();
  };

  const saved = policy.data?.required_groups ?? [];
  const groupsDirty =
    joinMode === "group" &&
    (requiredGroups.length !== saved.length ||
      [...requiredGroups].sort().join("\n") !== [...saved].sort().join("\n"));

  const changeFallback = (fallback: "refuse" | "approval") => {
    setGroupFallback(fallback);
    if (joinMode === "group" && requiredGroups.length > 0) {
      put(
        { join_mode: "group", required_groups: requiredGroups, group_fallback: fallback },
        "Join mode updated",
      );
    }
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

  const sourceOf = (field: string) =>
    policy.data?.sources[field] === "console" ? "set here" : "set in .env";

  return (
    <div className={FORM}>
      {save.error ? (
        <Notice tone="danger" title="Could not save">
          {save.error instanceof Error ? save.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <fieldset>
        <legend className="p-0 text-sm">Who may join</legend>
        <div className="flex flex-col gap-2">
          {JOIN_MODES.map((mode) => (
            <label
              key={mode.value}
              className="flex cursor-pointer items-start gap-2 text-sm"
            >
              <input
                type="radio"
                name="join-mode"
                value={mode.value}
                checked={joinMode === mode.value}
                onChange={() => pickMode(mode.value)}
                className="mt-[0.2em] accent-accent"
              />
              <span>
                {mode.label}
                <span className="mt-0.5 block text-xs text-ink-faint">{mode.hint}</span>
              </span>
            </label>
          ))}
        </div>
        <p className="mt-1 text-xs text-ink-faint">
          Join mode {sourceOf("join_mode")}. Administrators are never refused by
          this gate, so a wrong group cannot lock you out.
        </p>
      </fieldset>

      {joinMode === "group" ? (
        <>
          {needsGroups ? (
            <Notice tone="warn" title="Pick at least one required group first">
              A group gate with no groups refuses everyone who is not an
              administrator — including the account that would fix it.
            </Notice>
          ) : null}
          <fieldset>
            <legend className="p-0 text-sm">Required groups</legend>
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {(groups.data?.items ?? []).map((group) => (
                <label key={group.id} className="inline-flex items-center gap-1.5 text-sm">
                  <input
                    type="checkbox"
                    checked={requiredGroups.includes(group.name)}
                    onChange={(event) =>
                      changeGroups(
                        event.target.checked
                          ? [...requiredGroups, group.name].sort()
                          : requiredGroups.filter((name) => name !== group.name),
                      )
                    }
                    className="accent-accent"
                  />
                  {group.name}
                </label>
              ))}
            </div>
            {(groups.data?.items ?? []).length === 0 ? (
              <p className="text-xs text-ink-faint">
                No groups yet. A directory name must first be imported or mapped
                on the Groups page.
              </p>
            ) : (
              <p className="text-xs text-ink-faint">
                Local group names. Required groups {sourceOf("required_groups")}.
              </p>
            )}
            {/* Saving is explicit for the set: no intermediate gate is ever
                stored, and the preview sees the whole choice. */}
            <div className="mt-2">
              <Button
                variant="primary"
                disabled={!groupsDirty}
                busy={preview.isPending || save.isPending}
                onClick={() => saveGroupMode("group", requiredGroups, groupFallback)}
              >
                Save access groups
              </Button>
            </div>
          </fieldset>

          <Select
            label="Someone without a required group"
            value={groupFallback}
            onChange={(event) => changeFallback(event.target.value as "refuse" | "approval")}
            hint={
              groupFallback === "refuse"
                ? "They are refused and told to ask an administrator."
                : "Their account is created disabled, waiting in the approval queue."
            }
          >
            <option value="refuse">Refuse them</option>
            <option value="approval">Hold them for approval</option>
          </Select>
        </>
      ) : null}

      {preview.data && preview.data.would_be_refused > 0 ? (
        <Notice
          tone="warn"
          title={`${preview.data.would_be_refused} ${
            preview.data.would_be_refused === 1 ? "person" : "people"
          } will lose access at their next request`}
        >
          {preview.data.admins_exempt}{" "}
          {preview.data.admins_exempt === 1 ? "administrator is" : "administrators are"} exempt.
          <div className="mt-2 flex gap-2">
            <Button
              variant="primary"
              busy={save.isPending}
              onClick={() =>
                put(
                  {
                    join_mode: "group",
                    required_groups: requiredGroups,
                    group_fallback: groupFallback,
                  },
                  "Join mode updated",
                )
              }
            >
              Save anyway
            </Button>
            <Button onClick={() => preview.reset()}>Go back</Button>
          </div>
        </Notice>
      ) : null}
    </div>
  );
}
