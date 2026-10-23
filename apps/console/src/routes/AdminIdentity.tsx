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
import {
  useGroups,
  useOidcPolicy,
  usePolicyPreview,
  useUpdateOidcPolicy,
} from "../lib/admin";
import type { JoinMode, OidcPolicyInput } from "../lib/types";
import { FORM } from "../lib/layout";
import { m } from "../paraglide/messages.js";
import { useOptionalToast } from "../lib/toast";

/** Message functions, not strings: this list is built once, at import. */
const JOIN_MODES: { value: JoinMode; label: () => string; hint: () => string }[] = [
  {
    value: "open",
    label: m["admin_identity.join_mode.open.label"],
    hint: m["admin_identity.join_mode.open.hint"],
  },
  {
    value: "approval",
    label: m["admin_identity.join_mode.approval.label"],
    hint: m["admin_identity.join_mode.approval.hint"],
  },
  {
    value: "invite",
    label: m["admin_identity.join_mode.invite.label"],
    hint: m["admin_identity.join_mode.invite.hint"],
  },
  {
    value: "group",
    label: m["admin_identity.join_mode.group.label"],
    hint: m["admin_identity.join_mode.group.hint"],
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
  const [groupFallback, setGroupFallback] = useState<"refuse" | "approval">(
    "refuse",
  );
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
  };

  const put = (body: OidcPolicyInput, ok: string) =>
    save.mutate(body, {
      onSuccess: () => {
        toast?.add({ title: ok, type: "success" });
        preview.reset();
      },
      onError: (caught: unknown) => {
        toast?.add({
          // i18n: server text — the gateway's own error message.
          title: caught instanceof Error ? caught.message : m["admin_identity.toast.save_failed"](),
          type: "error",
        });
        resetToPolicy();
      },
    });

  // Saving group mode (or changed groups inside it) asks the preview first:
  // "N people will lose access at their next request" is a sentence worth
  // reading before it becomes true. Other modes save straight away.
  const saveGroupMode = (
    mode: "group",
    groups: string[],
    fallback: "refuse" | "approval",
  ) => {
    preview.mutate(
      { join_mode: mode, required_groups: groups },
      {
        onSuccess: (result) => {
          if (result.would_be_refused === 0) {
            put(
              {
                join_mode: mode,
                required_groups: groups,
                group_fallback: fallback,
              },
              m["admin_identity.toast.join_mode_updated"](),
            );
          }
        },
        onError: (caught: unknown) =>
          toast?.add({
            // i18n: server text — the gateway's own error message.
            title:
              caught instanceof Error
                ? caught.message
                : m["admin_identity.toast.preview_failed"](),
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
      m["admin_identity.toast.join_mode_updated"](),
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
        {
          join_mode: "group",
          required_groups: requiredGroups,
          group_fallback: fallback,
        },
        m["admin_identity.toast.join_mode_updated"](),
      );
    }
  };

  if (policy.isPending || policy.error) {
    return (
      <Card>
        {policy.isPending ? (
          <Spinner label={m["admin_identity.loading"]()} />
        ) : (
          <Notice tone="danger" title={m["admin_identity.load_error.title"]()}>
            {/* i18n: server text — the API's own error message. */}
            {policy.error instanceof Error
              ? policy.error.message
              : m["admin_identity.load_error.unknown"]()}
          </Notice>
        )}
      </Card>
    );
  }

  const sourceOf = (field: string) =>
    policy.data?.sources[field] === "console"
      ? m["admin_identity.source.here"]()
      : m["admin_identity.source.env"]();

  return (
    <div className={FORM}>
      {save.error ? (
        <Notice tone="danger" title={m["admin_identity.toast.save_failed"]()}>
          {/* i18n: server text — the gateway's own error message. */}
          {save.error instanceof Error ? save.error.message : m["admin_identity.load_error.unknown"]()}
        </Notice>
      ) : null}

      <fieldset>
        <legend className="p-0 text-body">{m["admin_identity.who_may_join"]()}</legend>
        <div className="flex flex-col gap-2">
          {JOIN_MODES.map((mode) => (
            <label
              key={mode.value}
              className="flex cursor-pointer items-start gap-2 text-body"
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
                {mode.label()}
                <span className="mt-0.5 block text-dense text-ink-faint">
                  {mode.hint()}
                </span>
              </span>
            </label>
          ))}
        </div>
        <p className="mt-1 text-small text-ink-faint">
          {m["admin_identity.join_mode_note"]({ source: sourceOf("join_mode") })}
        </p>
      </fieldset>

      {joinMode === "group" ? (
        <>
          {needsGroups ? (
            <Notice tone="warn" title={m["admin_identity.needs_groups.title"]()}>
              {m["admin_identity.needs_groups.body"]()}
            </Notice>
          ) : null}
          <fieldset>
            <legend className="p-0 text-body">{m["admin_identity.required_groups"]()}</legend>
            <div className="flex flex-col gap-1.5">
              {(groups.data?.items ?? []).map((group) => {
                // The mapping-only rule: a claimed name reaches the group only
                // through a mapping. A required group nothing maps is one the
                // gate can only admit by hand — say so where it is picked.
                const mapped = (policy.data?.group_mappings ?? []).some(
                  (rule) => rule.local === group.name,
                );
                const chosen = requiredGroups.includes(group.name);
                return (
                  <div key={group.id}>
                    <label className="inline-flex items-center gap-1.5 text-body">
                      <input
                        type="checkbox"
                        checked={chosen}
                        onChange={(event) =>
                          changeGroups(
                            event.target.checked
                              ? [...requiredGroups, group.name].sort()
                              : requiredGroups.filter(
                                  (name) => name !== group.name,
                                ),
                          )
                        }
                        className="accent-accent"
                      />
                      {group.name}
                    </label>
                    {chosen && !mapped ? (
                      <p className="text-small text-ink-faint">
                        {m["admin_identity.group.unmapped"]()}
                      </p>
                    ) : null}
                  </div>
                );
              })}
            </div>
            {(groups.data?.items ?? []).length === 0 ? (
              <p className="text-small text-ink-faint">{m["admin_identity.group.none"]()}</p>
            ) : (
              <p className="text-small text-ink-faint">
                {m["admin_identity.group.local_note"]({ source: sourceOf("required_groups") })}
              </p>
            )}
            {/* Saving is explicit for the set: no intermediate gate is ever
                stored, and the preview sees the whole choice. */}
            <div className="mt-2">
              <Button
                variant="primary"
                disabled={!groupsDirty}
                busy={preview.isPending || save.isPending}
                onClick={() =>
                  saveGroupMode("group", requiredGroups, groupFallback)
                }
              >
                {m["admin_identity.save_groups"]()}
              </Button>
            </div>
          </fieldset>

          <Select
            label={m["admin_identity.fallback.label"]()}
            value={groupFallback}
            onChange={(event) =>
              changeFallback(event.target.value as "refuse" | "approval")
            }
            hint={
              groupFallback === "refuse"
                ? m["admin_identity.fallback.hint.refuse"]()
                : m["admin_identity.fallback.hint.approval"]()
            }
          >
            <option value="refuse">{m["admin_identity.fallback.refuse"]()}</option>
            <option value="approval">{m["admin_identity.fallback.approval"]()}</option>
          </Select>
        </>
      ) : null}

      {preview.data && preview.data.would_be_refused > 0 ? (
        <Notice
          tone="warn"
          title={m["admin_identity.preview.title"]({ count: preview.data.would_be_refused })}
        >
          {m["admin_identity.preview.exempt"]({ count: preview.data.admins_exempt })}
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
                  m["admin_identity.toast.join_mode_updated"](),
                )
              }
            >
              {m["admin_identity.preview.save_anyway"]()}
            </Button>
            <Button onClick={() => preview.reset()}>{m["admin_identity.preview.go_back"]()}</Button>
          </div>
        </Notice>
      ) : null}
    </div>
  );
}
