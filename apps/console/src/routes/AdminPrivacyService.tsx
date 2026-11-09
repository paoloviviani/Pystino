import { Badge, Button, Card, Input, Notice, Select, Spinner } from "@llmp/ui";
import { useState } from "react";

import { PrivacyHeader } from "../components/PrivacyHeader";
import { useRedactionStatus } from "../lib/admin";
import { EngineDetails, EngineSettings } from "./AdminRedaction";
import { MUTED, PAGE } from "../lib/layout";
import { Tip } from "../components/Tip";
import {
  useCreatePrivacyProfile,
  useDeletePrivacyProfile,
  usePrivacyProfiles,
  usePrivacySettings,
  useSavePrivacySettings,
  useUpdatePrivacyProfile,
} from "../lib/privacy";
import type { PrivacyProfile, RedactionStatus } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * Detector profiles: the named ways a content condition can be decided.
 *
 * Compact cards; the long explanations are hints. A profile that is down or not
 * installed says so, and its fallback (refuse, which fails closed, or fall back
 * to a weaker profile, which is recorded) is set here, next to the health it
 * answers to, rather than on every rule.
 */

function StatusBadge({ profile }: { profile: PrivacyProfile }) {
  if (profile.status === "available") return <Badge tone="ok">{m["privacy.profile.available"]()}</Badge>;
  if (profile.status === "down") return <Badge tone="danger">{m["privacy.profile.down"]()}</Badge>;
  return <Badge tone="warn">{m["privacy.profile.not_installed"]()}</Badge>;
}

function ProfileCard({
  profile,
  all,
  redaction,
}: {
  profile: PrivacyProfile;
  all: PrivacyProfile[];
  /** The redaction engine's state, shown here when this profile is the Presidio engine. */
  redaction?: RedactionStatus;
}) {
  const update = useUpdatePrivacyProfile();
  const remove = useDeletePrivacyProfile();
  const others = all.filter((p) => p.id !== profile.id);
  const error = update.error ?? remove.error;
  return (
    <Card
      title={
        <span className="flex flex-wrap items-center gap-2">
          {/* i18n: server text, the name an administrator gave the profile. */}
          {profile.name}
          <StatusBadge profile={profile} />
          {profile.is_default && <Badge tone="accent">{m["privacy.profile.default"]()}</Badge>}
        </span>
      }
      description={profile.status_detail}
    >
      <div className="flex flex-col gap-3">
        <p className={`m-0 text-small ${MUTED}`}>
          {m["privacy.profile.engine"]({ engine: profile.engine })}
          {" · "}
          {m["privacy.profile.cost"]({ cost: profile.cost_hint })}
          {" · "}
          {m["privacy.profile.used"]({ count: profile.rules_using })}
        </p>
        {profile.labels.length > 0 && (
          <p className="m-0 text-small">{m["privacy.profile.labels"]({ labels: profile.labels.join(", ") })}</p>
        )}
        <div className="grid gap-3 [grid-template-columns:repeat(auto-fit,minmax(12rem,1fr))]">
          <Select
            label={m["privacy.profile.fallback"]()}
            value={profile.fallback}
            hint={
              profile.fallback === "refuse"
                ? m["privacy.profile.fallback_refuse_hint"]()
                : m["privacy.profile.fallback_weaker_hint"]()
            }
            onChange={(event) => {
              const next = event.target.value as PrivacyProfile["fallback"];
              update.mutate({
                id: profile.id,
                body:
                  next === "refuse"
                    ? { fallback: "refuse", fallback_profile_id: null }
                    : { fallback: "fallback_to", fallback_profile_id: others[0]?.id ?? null },
              });
            }}
          >
            <option value="refuse">{m["privacy.profile.fallback_refuse"]()}</option>
            <option value="fallback_to" disabled={others.length === 0}>
              {m["privacy.profile.fallback_to"]()}
            </option>
          </Select>
          {profile.fallback === "fallback_to" && (
            <Select
              label={m["privacy.profile.fallback_target"]()}
              value={profile.fallback_profile_id ?? ""}
              onChange={(event) =>
                update.mutate({
                  id: profile.id,
                  body: { fallback: "fallback_to", fallback_profile_id: event.target.value },
                })
              }
            >
              {others.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </Select>
          )}
        </div>
        {profile.fallback === "fallback_to" && (
          <Notice tone="warn">{m["privacy.profile.weakens"]()}</Notice>
        )}
        <label className="flex items-center gap-2 text-body">
          <input
            type="checkbox"
            checked={profile.user_available}
            onChange={(event) =>
              update.mutate({ id: profile.id, body: { user_available: event.target.checked } })
            }
          />
          {m["privacy.profile.user_available"]()}
          <Tip text={m["privacy.profile.user_available_tip"]()} />
        </label>
        {redaction && <EngineSettings status={redaction} />}
        {error && (
          <Notice tone="danger">
            {error instanceof Error ? error.message : m["privacy.error.unknown"]()}
          </Notice>
        )}
        {!profile.is_default && (
          <div>
            <Button type="button" disabled={remove.isPending} onClick={() => remove.mutate(profile.id)}>
              {m["privacy.profile.delete"]()}
            </Button>
          </div>
        )}
      </div>
    </Card>
  );
}

function AddProfile() {
  const create = useCreatePrivacyProfile();
  const [name, setName] = useState("");
  const [engine, setEngine] = useState("gliner");
  const [labels, setLabels] = useState("");
  return (
    <Card title={m["privacy.profile.add"]()} description={m["privacy.profile.add_hint"]()}>
      <div className="flex flex-col gap-3">
        <div className="grid gap-3 [grid-template-columns:repeat(auto-fit,minmax(12rem,1fr))]">
          <Input
            label={m["privacy.profile.name"]()}
            value={name}
            maxLength={64}
            onChange={(event) => setName(event.target.value)}
          />
          <Select
            label={m["privacy.profile.engine_label"]()}
            value={engine}
            onChange={(event) => setEngine(event.target.value)}
          >
            <option value="gliner">GLiNER</option>{/* i18n: literal */}
            <option value="presidio">Presidio</option>{/* i18n: literal */}
            <option value="patterns">{m["privacy.profile.engine_patterns"]()}</option>
          </Select>
          <Input
            label={m["privacy.profile.labels_label"]()}
            value={labels}
            hint={m["privacy.profile.labels_hint"]()}
            onChange={(event) => setLabels(event.target.value)}
          />
        </div>
        {create.error && (
          <Notice tone="danger">
            {create.error instanceof Error ? create.error.message : m["privacy.error.unknown"]()}
          </Notice>
        )}
        <div>
          <Button
            type="button"
            variant="primary"
            disabled={!name.trim() || create.isPending}
            onClick={() =>
              create.mutate(
                {
                  name: name.trim(),
                  engine,
                  labels: labels
                    .split(",")
                    .map((l) => l.trim())
                    .filter(Boolean),
                },
                {
                  onSuccess: () => {
                    setName("");
                    setLabels("");
                  },
                },
              )
            }
          >
            {m["privacy.profile.add_button"]()}
          </Button>
        </div>
      </div>
    </Card>
  );
}

/**
 * What users may add to their own policies: the entity types they may name
 * (patterns are always theirs). Blank means every type the detector offers.
 */
function UserTypes() {
  const settings = usePrivacySettings();
  const save = useSavePrivacySettings();
  const [text, setText] = useState<string | null>(null);
  const current = settings.data?.user_entity_types ?? null;
  const shown = text ?? (current ? current.join(", ") : "");
  return (
    <Card title={m["privacy.user_types.title"]()} description={m["privacy.user_types.hint"]()}>
      <div className="flex flex-col gap-3">
        <Input
          label={m["privacy.user_types.label"]()}
          hint={m["privacy.user_types.field_hint"]()}
          value={shown}
          onChange={(event) => setText(event.target.value)}
        />
        {save.error && (
          <Notice tone="danger">
            {/* i18n: server text, the API's own message. */}
            {save.error instanceof Error ? save.error.message : m["privacy.error.unknown"]()}
          </Notice>
        )}
        <div>
          <Button
            variant="primary"
            busy={save.isPending}
            onClick={() => {
              const types = shown
                .split(",")
                .map((t) => t.trim())
                .filter(Boolean);
              save.mutate(
                { user_entity_types: types.length ? types : null },
                { onSuccess: () => setText(null) },
              );
            }}
          >
            {m["privacy.user_types.save"]()}
          </Button>
        </div>
      </div>
    </Card>
  );
}

export function AdminPrivacyProfiles() {
  const profiles = usePrivacyProfiles();
  const redaction = useRedactionStatus();
  const items = profiles.data?.items ?? [];
  // The profile that is the Presidio engine hosts the engine choice and the
  // detection families: the default "Fast" profile is that engine.
  const presidio = items.find((p) => p.engine === "presidio" && p.is_default) ?? items.find((p) => p.engine === "presidio");
  return (
    <div className={PAGE}>
      <PrivacyHeader tab="detectors" />
      <p className={`m-0 text-small ${MUTED}`}>{m["privacy.profiles.subtitle"]()}</p>
      {profiles.isPending && <Spinner label={m["privacy.loading"]()} />}
      {profiles.error ? (
        <Notice tone="danger" title={m["privacy.error.load"]()}>
          {profiles.error instanceof Error ? profiles.error.message : m["privacy.error.unknown"]()}
        </Notice>
      ) : null}
      {items.map((profile) => (
        <ProfileCard
          key={profile.id}
          profile={profile}
          all={items}
          redaction={profile.id === presidio?.id ? redaction.data : undefined}
        />
      ))}
      {redaction.error && presidio ? (
        <Notice tone="danger" title={m["admin_redaction.load_error.title"]()}>
          {/* i18n: server text — the API's own error message. */}
          {redaction.error instanceof Error ? redaction.error.message : m["admin_redaction.unknown_error"]()}
        </Notice>
      ) : null}
      {profiles.data && <AddProfile />}
      {profiles.data && <UserTypes />}
      {redaction.data && <EngineDetails status={redaction.data} />}
    </div>
  );
}
