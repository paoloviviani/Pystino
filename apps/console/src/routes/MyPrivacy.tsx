import { Badge, Button, Card, Dialog, EmptyState, Input, Notice, Select, Spinner } from "@llmp/ui";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { HowItWorks } from "../components/HowItWorks";
import { PageHeader } from "../components/PageHeader";
import { PipelineCard } from "../components/PipelineCard";
import { PrivacyTestBox } from "../components/PrivacyTest";
import { Tip } from "../components/Tip";
import { entityLabel, modeLabel } from "../lib/entities";
import { FORM, FORM_ROW, MUTED, PAGE } from "../lib/layout";
import {
  KINDS,
  kindLabel,
  useDeleteMyPolicy,
  useDeletePipeline,
  useInForce,
  useMyPolicies,
  usePipelines,
  usePrivacyOptions,
  useSaveMyPolicy,
  useSavePipeline,
} from "../lib/privacy";
import type { PrivacyPipeline, UserPolicy, UserPolicyEntry } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * A user's own privacy pipelines (ADR 0113): theirs alone, and they can only add
 * protection. Under them, the administrator's pipelines that already apply to the
 * user (read only), and the user's own policies, which pipelines can apply.
 *
 * A value an administrator's policy hides stays hidden whatever is written here:
 * the combination takes the highest level and the strictest mode, so nothing a
 * user adds can lower either. The gateway enforces that; this page only avoids
 * offering what would be refused.
 */

const USER_MODES = ["anonymise_restore", "anonymise", "redact", "block"] as const;

type Tab = "pipelines" | "test";

function MyNav({ tab }: { tab: Tab }) {
  const items: { tab: Tab; to: string; label: string }[] = [
    { tab: "pipelines", to: "/privacy", label: m["my_privacy.tab.pipelines"]() },
    { tab: "test", to: "/privacy/test", label: m["my_privacy.tab.test"]() },
  ];
  return (
    <nav aria-label={m["privacy.tab.label"]()} className="border-b border-line">
      <ul className="m-0 flex list-none gap-1 p-0">
        {items.map((item) => (
          <li key={item.tab}>
            <Link
              to={item.to}
              aria-current={item.tab === tab ? "page" : undefined}
              className={`-mb-px inline-block border-b-2 px-3 py-2 text-body no-underline ${
                item.tab === tab
                  ? "border-accent font-semibold text-ink"
                  : "border-transparent text-ink-muted hover:text-ink"
              }`}
            >
              {item.label}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}

interface EntryForm {
  kind: "pattern" | "mode";
  name: string;
  regex: string;
  mode: string;
}

function PolicyDialog({
  policy,
  onClose,
}: {
  /** null: a new policy. */
  policy: UserPolicy | null;
  onClose: () => void;
}) {
  const save = useSaveMyPolicy();
  const options = usePrivacyOptions("me");
  const [name, setName] = useState(policy?.name ?? "");
  const [profile, setProfile] = useState(policy?.profile_id ?? "");
  const [entries, setEntries] = useState<EntryForm[]>(
    (policy?.entries ?? []).map((e) => ({
      kind: e.kind,
      name: e.name,
      regex: e.regex ?? "",
      mode: e.mode,
    })),
  );
  const types = options.data?.entity_types ?? null;
  const patch = (index: number, change: Partial<EntryForm>) =>
    setEntries(entries.map((e, i) => (i === index ? { ...e, ...change } : e)));
  return (
    <Dialog
      open
      title={policy ? m["my_privacy.policy.edit"]() : m["my_privacy.policy.new"]()}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["privacy.delete.cancel"]()}</Button>
          <Button
            variant="primary"
            busy={save.isPending}
            disabled={!name.trim()}
            onClick={() =>
              save.mutate(
                {
                  id: policy?.id,
                  body: {
                    name: name.trim(),
                    profile_id: profile || null,
                    entries: entries.map(
                      (e): UserPolicyEntry => ({
                        kind: e.kind,
                        name: e.name,
                        mode: e.mode,
                        regex: e.kind === "pattern" ? e.regex : null,
                      }),
                    ),
                  },
                },
                { onSuccess: onClose },
              )
            }
          >
            {m["my_privacy.policy.save"]()}
          </Button>
        </>
      }
    >
      <div className={FORM}>
        {save.error && (
          <Notice tone="danger" title={m["privacy.save_error"]()}>
            {/* i18n: server text, the API's own message (it names what RE2 refused). */}
            {save.error.message}
          </Notice>
        )}
        <Input
          label={m["my_privacy.policy.name"]()}
          value={name}
          maxLength={120}
          onChange={(event) => setName(event.target.value)}
        />
        {(options.data?.profiles.length ?? 0) > 0 && (
          <Select
            label={m["privacy.policy.profile"]()}
            hint={m["privacy.policy.profile_later"]()}
            title={m["privacy.policy.profile_later"]()}
            disabled
            value={profile}
            onChange={(event) => setProfile(event.target.value)}
          >
            <option value="">{m["privacy.policy.profile_default"]()}</option>
            {options.data?.profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        )}
        {entries.map((e, index) => (
          <div key={index} className="flex flex-col gap-2 rounded-lg border border-line-quiet p-2">
            <div className={FORM_ROW}>
              <Select
                label={m["my_redaction.form.kind"]()}
                value={e.kind}
                onChange={(event) => patch(index, { kind: event.target.value as EntryForm["kind"] })}
              >
                <option value="mode">{m["my_redaction.form.kind_mode"]()}</option>
                <option value="pattern">{m["my_redaction.form.kind_pattern"]()}</option>
              </Select>
              {e.kind === "mode" && types ? (
                <Select
                  label={m["my_redaction.form.type_name"]()}
                  value={e.name}
                  onChange={(event) => patch(index, { name: event.target.value })}
                >
                  <option value="">{m["my_privacy.policy.choose_type"]()}</option>
                  {types.map((t) => (
                    <option key={t} value={t}>
                      {entityLabel(t)}
                    </option>
                  ))}
                </Select>
              ) : (
                <Input
                  label={
                    e.kind === "pattern"
                      ? m["my_redaction.form.pattern_name"]()
                      : m["my_redaction.form.type_name"]()
                  }
                  value={e.name}
                  maxLength={64}
                  onChange={(event) => patch(index, { name: event.target.value })}
                />
              )}
              <Select
                label={m["my_redaction.form.mode"]()}
                value={e.mode}
                onChange={(event) => patch(index, { mode: event.target.value })}
              >
                {USER_MODES.map((mode) => (
                  <option key={mode} value={mode}>
                    {modeLabel(mode)}
                  </option>
                ))}
              </Select>
            </div>
            {e.kind === "pattern" && (
              <Input
                label={m["my_redaction.form.regex"]()}
                value={e.regex}
                maxLength={512}
                hint={m["my_redaction.form.regex_hint"]()}
                className="font-mono"
                onChange={(event) => patch(index, { regex: event.target.value })}
              />
            )}
            <div>
              <Button size="sm" variant="ghost" onClick={() => setEntries(entries.filter((_, i) => i !== index))}>
                {m["my_redaction.rules.remove"]()}
              </Button>
            </div>
          </div>
        ))}
        <div>
          <Button
            onClick={() =>
              setEntries([...entries, { kind: "mode", name: "", regex: "", mode: "redact" }])
            }
          >
            {m["my_privacy.policy.add_entry"]()}
          </Button>
        </div>
        <p className={`m-0 text-small ${MUTED}`}>{m["my_redaction.form.only_stricter"]()}</p>
      </div>
    </Dialog>
  );
}

function MyPolicies() {
  const policies = useMyPolicies();
  const remove = useDeleteMyPolicy();
  const [editing, setEditing] = useState<UserPolicy | "new" | null>(null);
  const items = policies.data?.items ?? [];
  return (
    <section className="flex flex-col gap-3" aria-label={m["my_privacy.policies.title"]()}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="m-0 flex items-center gap-2 text-heading font-semibold">
          {m["my_privacy.policies.title"]()}
          <Tip text={m["my_privacy.policies.tip"]()} />
        </h2>
        <Button onClick={() => setEditing("new")}>{m["my_privacy.policy.new"]()}</Button>
      </div>
      {remove.error && (
        <Notice tone="danger" title={m["privacy.save_error"]()}>
          {/* i18n: server text, the API's own message. */}
          {remove.error.message}
        </Notice>
      )}
      {policies.data && items.length === 0 && (
        <p className={`m-0 ${MUTED}`}>{m["my_privacy.policies.empty"]()}</p>
      )}
      {items.map((p) => (
        <Card key={p.id}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-col gap-1">
              {/* i18n: server text, the name the user gave the policy. */}
              <strong>{p.name}</strong>
              <span className={`text-small ${MUTED}`}>
                {p.entries.length === 0
                  ? m["my_privacy.policies.no_entries"]()
                  : p.entries
                      .map((e) => `${e.kind === "mode" ? entityLabel(e.name) : e.name} · ${modeLabel(e.mode)}`)
                      .join(" · ")}
              </span>
              {p.used_by.length > 0 && (
                <span className={`text-small ${MUTED}`}>
                  {m["privacy.policies.used_by_names"]({ names: p.used_by.join(", ") })}
                </span>
              )}
            </div>
            <div className="flex gap-1">
              <Button size="sm" onClick={() => setEditing(p)}>
                {m["privacy.row.edit"]()}
              </Button>
              <Button
                size="sm"
                variant="danger"
                disabled={remove.isPending}
                onClick={() => remove.mutate(p.id)}
              >
                {m["privacy.row.delete"]()}
              </Button>
            </div>
          </div>
        </Card>
      ))}
      {editing && (
        <PolicyDialog
          key={editing === "new" ? "new" : editing.id}
          policy={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
        />
      )}
    </section>
  );
}

export function MyPrivacy() {
  const mine = usePipelines("me");
  const inForce = useInForce();
  const save = useSavePipeline("me");
  const remove = useDeletePipeline("me");
  const navigate = useNavigate();
  const [deleting, setDeleting] = useState<PrivacyPipeline | null>(null);
  const items = mine.data?.items ?? [];
  return (
    <div className={PAGE}>
      <PageHeader
        title={m["my_privacy.title"]()}
        subtitle={m["my_privacy.subtitle"]()}
        actions={
          <>
            <HowItWorks own />
            <Button variant="primary" onClick={() => navigate("/privacy/pipelines/new")}>
              {m["privacy.pipelines.new"]()}
            </Button>
          </>
        }
      />
      <MyNav tab="pipelines" />
      <p className={`m-0 flex items-center gap-2 text-small ${MUTED}`}>
        {m["my_privacy.lead"]()}
        <Tip text={m["my_privacy.lead_tip"]()} />
      </p>
      {mine.isPending && <Spinner label={m["privacy.loading"]()} />}
      {Boolean(mine.error) && (
        <Notice tone="danger" title={m["privacy.load_error"]()}>
          {/* i18n: server text, the API's own message. */}
          {mine.error instanceof Error ? mine.error.message : m["privacy.unknown_error"]()}
        </Notice>
      )}
      {Boolean(save.error || remove.error) && (
        <Notice tone="danger" title={m["privacy.save_error"]()}>
          {/* i18n: server text, the API's own message. */}
          {((save.error ?? remove.error) as Error).message}
        </Notice>
      )}
      {mine.data && items.length === 0 && (
        <EmptyState title={m["my_privacy.empty"]()} detail={m["my_privacy.empty_hint"]()} />
      )}
      {KINDS.filter((k) => items.some((p) => p.kind === k)).map((kind) => (
        <section key={kind} className="flex flex-col gap-3" aria-label={kindLabel(kind)}>
          <h2 className="m-0 text-heading font-semibold">{kindLabel(kind)}</h2>
          {items
            .filter((p) => p.kind === kind)
            .map((p) => (
              <PipelineCard
                key={p.id}
                pipeline={p}
                own
                editTo={`/privacy/pipelines/${p.id}`}
                onToggle={(enabled) => save.mutate({ id: p.id, body: { enabled } })}
                onDelete={() => setDeleting(p)}
              />
            ))}
        </section>
      ))}

      <MyPolicies />

      <section className="flex flex-col gap-3" aria-label={m["my_privacy.admin.title"]()}>
        <div className="flex flex-wrap items-baseline gap-2">
          <h2 className="m-0 text-heading font-semibold">{m["my_privacy.admin.title"]()}</h2>
          <span className={`flex items-center gap-1 text-small ${MUTED}`}>
            <Badge tone="neutral">{m["my_privacy.admin.read_only"]()}</Badge>
            <Tip text={m["my_privacy.admin.tip"]()} />
          </span>
        </div>
        {inForce.data && inForce.data.items.length === 0 && (
          <p className={`m-0 ${MUTED}`}>{m["my_privacy.admin.none"]()}</p>
        )}
        {inForce.data?.items.map((p) => <PipelineCard key={p.id} pipeline={p} />)}
      </section>

      <Dialog
        open={deleting !== null}
        title={m["privacy.delete.title"]({ name: deleting?.name ?? "" })}
        onClose={() => setDeleting(null)}
        footer={
          <>
            <Button onClick={() => setDeleting(null)}>{m["privacy.delete.cancel"]()}</Button>
            <Button
              variant="danger"
              busy={remove.isPending}
              onClick={() => {
                if (deleting) remove.mutate(deleting.id, { onSuccess: () => setDeleting(null) });
              }}
            >
              {m["privacy.delete.confirm"]()}
            </Button>
          </>
        }
      >
        <p className="m-0">{m["privacy.delete.body"]()}</p>
      </Dialog>
    </div>
  );
}

export function MyPrivacyTest() {
  return (
    <div className={PAGE}>
      <PageHeader title={m["my_privacy.title"]()} subtitle={m["my_privacy.subtitle"]()} actions={<HowItWorks own />} />
      <MyNav tab="test" />
      <PrivacyTestBox area="me" />
    </div>
  );
}
