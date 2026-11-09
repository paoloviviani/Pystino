import { Badge, Button, Card, Input, Notice, Spinner, Switch } from "@llmp/ui";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";

import { FIELD_LABEL, FORM, MUTED, PAGE } from "../lib/layout";
import {
  BOOL_KEYS,
  BUILTIN,
  CONDITION_KEYS,
  CONTENT_KEYS,
  ENGINES,
  FILETYPES,
  KINDS,
  LADDER,
  LANGUAGES,
  PATTERNS,
  actionBody,
  baseLevel,
  conditionHint,
  conditionLabel,
  engineLabel,
  filetypeLabel,
  kindHint,
  kindLabel,
  languageLabel,
  levelHint,
  levelLabel,
  ruleBody,
  ruleSentence,
  useCheckPipeline,
  usePipelines,
  usePrivacyOptions,
  usePrivacyProfiles,
  useSavePipeline,
  type Area,
  type Cond,
} from "../lib/privacy";
import { useOptionalToast } from "../lib/toast";
import type {
  PrivacyAction,
  PrivacyIssue,
  PrivacyKind,
  PrivacyOptions,
  PrivacyPipelineInput,
} from "../lib/types";
import { m } from "../paraglide/messages.js";
import { PageHeader } from "./PageHeader";
import { Tip } from "./Tip";

/**
 * The pipeline editor, one for an administrator and for a user (ADR 0113).
 *
 * The same object, so the same form; what differs is what is offered. A user's
 * who is fixed to them, and the "nothing" level of the ladder is not offered as
 * a rule's action (what happens anyway is not an addition). Everything a user
 * may not do is also refused by the gateway: the form only avoids offering it.
 */

interface DraftRule {
  /** Client-only, for React keys; the saved id is `id`. */
  uid: string;
  id?: string;
  name: string;
  enabled: boolean;
  observe_only: boolean;
  conditions: Cond[];
  action: PrivacyAction;
}

let counter = 0;
const uid = () => `r${++counter}`;

function chip(on: boolean): string {
  return `rounded-full border px-3 py-1 text-dense ${
    on ? "border-accent bg-sunken font-semibold text-ink" : "border-line bg-surface text-ink-muted"
  }`;
}

function Chips<T extends string>({
  options,
  selected,
  onToggle,
  label,
}: {
  options: { value: T; label: string; disabled?: boolean }[];
  selected: readonly T[];
  onToggle: (value: T) => void;
  label: string;
}) {
  return (
    <div role="group" aria-label={label} className="flex flex-wrap gap-1.5">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={selected.includes(o.value)}
          disabled={o.disabled}
          className={`${chip(selected.includes(o.value))} ${o.disabled ? "opacity-50" : "cursor-pointer"}`}
          onClick={() => onToggle(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function toggled<T>(list: readonly T[], value: T): T[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex gap-1">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={value === o.value}
          className={`${chip(value === o.value)} cursor-pointer`}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

// -- conditions ----------------------------------------------------------------

function ConditionsEditor({
  kind,
  conditions,
  onChange,
  profiles,
  warnNameOnly,
}: {
  kind: PrivacyKind;
  conditions: Cond[];
  onChange: (next: Cond[]) => void;
  profiles: { id: string; name: string }[];
  warnNameOnly: boolean;
}) {
  const unused = CONDITION_KEYS[kind].filter((k) => !conditions.some((c) => c.key === k));
  const set = (index: number, patch: Partial<Cond>) =>
    onChange(conditions.map((c, i) => (i === index ? { ...c, ...patch } : c)));
  return (
    <div className="flex flex-col gap-2">
      <div className={`${FIELD_LABEL} flex items-center gap-2`}>
        {m["privacy.edit.conditions"]()}
        <Tip text={m["privacy.edit.conditions_tip"]()} />
      </div>
      {conditions.map((c, index) => {
        const bool = BOOL_KEYS.includes(c.key);
        const content = CONTENT_KEYS.includes(c.key);
        return (
          <div
            key={c.key}
            className="flex flex-wrap items-center gap-2 rounded-lg border border-line-quiet p-2"
          >
            <strong className="text-dense" title={conditionHint(c.key)}>
              {conditionLabel(c.key)}
            </strong>
            <Button
              size="sm"
              aria-label={m["privacy.cond.flip"]({ name: conditionLabel(c.key) })}
              onClick={() => set(index, { negate: !c.negate })}
            >
              {c.key === "filename"
                ? c.negate
                  ? m["privacy.cond.not_matches"]()
                  : m["privacy.cond.matches"]()
                : bool && c.key !== "scan"
                  ? c.negate
                    ? m["privacy.cond.not_contains"]()
                    : m["privacy.cond.contains"]()
                  : c.negate
                    ? m["privacy.cond.is_not"]()
                    : m["privacy.cond.is"]()}
            </Button>
            {c.key === "filetype" && (
              <Chips
                label={conditionLabel(c.key)}
                options={FILETYPES.map((v) => ({ value: v, label: filetypeLabel(v) }))}
                selected={c.values}
                onToggle={(v) => set(index, { values: toggled(c.values, v) })}
              />
            )}
            {c.key === "lang" && (
              <Chips
                label={conditionLabel(c.key)}
                options={LANGUAGES.map((v) => ({ value: v, label: languageLabel(v) }))}
                selected={c.values}
                onToggle={(v) => set(index, { values: toggled(c.values, v) })}
              />
            )}
            {c.key === "filename" && (
              <input
                className="min-w-[12rem] flex-1 rounded-md border border-line bg-surface px-2 py-1 font-mono text-body"
                aria-label={m["privacy.cond.glob_label"]()}
                placeholder={m["privacy.cond.glob_placeholder"]()}
                value={c.values.join(", ")}
                onChange={(event) =>
                  set(index, {
                    values: event.target.value
                      .split(",")
                      .map((v) => v.trim())
                      .filter(Boolean),
                  })
                }
              />
            )}
            {content && (
              <>
                <select
                  className="rounded-md border border-line bg-surface px-2 py-1 text-body"
                  aria-label={m["privacy.cond.decide_label"]({ name: conditionLabel(c.key) })}
                  value={c.decide ?? PATTERNS}
                  onChange={(event) => set(index, { decide: event.target.value })}
                >
                  <option value={PATTERNS}>{m["privacy.cond.quick"]()}</option>
                  {profiles.map((p) => (
                    <option key={p.id} value={p.id}>
                      {m["privacy.cond.model"]({ name: p.name })}
                    </option>
                  ))}
                </select>
                <Tip text={m["privacy.cond.decide_tip"]()} />
              </>
            )}
            <Button
              size="sm"
              variant="ghost"
              aria-label={m["privacy.cond.remove"]({ name: conditionLabel(c.key) })}
              onClick={() => onChange(conditions.filter((_, i) => i !== index))}
            >
              ×
            </Button>
          </div>
        );
      })}
      {unused.length > 0 && (
        <select
          className="w-fit rounded-md border border-line bg-surface px-2 py-1 text-body"
          aria-label={m["privacy.cond.add"]()}
          value=""
          onChange={(event) => {
            const key = event.target.value;
            if (!key) return;
            onChange([
              ...conditions,
              {
                key,
                values: [],
                negate: false,
                ...(CONTENT_KEYS.includes(key) ? { decide: PATTERNS } : {}),
              },
            ]);
          }}
        >
          <option value="">{m["privacy.cond.add_option"]()}</option>
          {unused.map((k) => (
            <option key={k} value={k}>
              {conditionLabel(k)}
            </option>
          ))}
        </select>
      )}
      {warnNameOnly && (
        <Notice tone="warn" title={m["privacy.cond.renamed_title"]()}>
          {m["privacy.cond.renamed"]()}
        </Notice>
      )}
    </div>
  );
}

// -- actions -------------------------------------------------------------------

function ActionEditor({
  kind,
  action,
  onChange,
  options,
  own,
  last,
  prefix,
}: {
  kind: PrivacyKind;
  action: PrivacyAction;
  onChange: (next: PrivacyAction) => void;
  options: PrivacyOptions;
  own: boolean;
  /** The fixed last rule: a user's may add nothing. */
  last: boolean;
  prefix: string;
}) {
  const ladder = LADDER[kind];
  const policies = [...options.policies, ...options.own_policies];
  return (
    <div className="flex flex-col gap-3">
      <div>
        <div className={`${FIELD_LABEL} flex items-center gap-2`}>
          {m["privacy.edit.where"]()}
          <Tip
            text={m["privacy.edit.where_tip"]({
              ladder: ladder.map((l) => levelLabel(l)).join(" < "),
            })}
          />
          {kind !== "read" && (
            <span className={`text-small ${MUTED}`}>
              {m["privacy.edit.provider_stays"]()}
              <Tip
                text={
                  kind === "embed"
                    ? m["privacy.edit.provider_stays_embed"]()
                    : m["privacy.edit.provider_stays_chat"]()
                }
              />
            </span>
          )}
        </div>
        <div role="radiogroup" aria-label={m["privacy.edit.where"]()} className="mt-1 flex flex-col gap-1">
          {ladder.map((level) => {
            const noop = own && !last && level === baseLevel(kind);
            return (
              <label
                key={level}
                title={noop ? m["privacy.edit.noop_tip"]() : undefined}
                className={`flex items-start gap-2 rounded-lg border p-2 ${
                  action.level === level && !noop ? "border-accent bg-sunken" : "border-line"
                } ${noop ? "opacity-60" : "cursor-pointer"}`}
              >
                <input
                  type="radio"
                  name={`${prefix}-level`}
                  checked={action.level === level}
                  disabled={noop && action.level !== level}
                  onChange={() =>
                    onChange({
                      level,
                      ...(level === "refuse" ? {} : action.redact_with ? { redact_with: action.redact_with } : {}),
                      ...(level === "read_with" ? { reader: action.reader ?? BUILTIN } : {}),
                    })
                  }
                />
                <span>
                  <strong>{levelLabel(level)}</strong>
                  <span className={`block text-small ${MUTED}`}>{levelHint(level)}</span>
                </span>
              </label>
            );
          })}
        </div>
      </div>
      {kind === "read" && action.level === "read_with" && (
        <div className="flex flex-col gap-2">
          <div className={`${FIELD_LABEL} flex items-center gap-2`}>
            {m["privacy.edit.reader"]()}
            <Tip text={m["privacy.edit.reader_tip"]()} />
          </div>
          <div role="group" aria-label={m["privacy.edit.reader"]()} className="flex flex-wrap gap-1.5">
            {options.readers.map((r) => (
              <button
                key={r.id}
                type="button"
                aria-pressed={(action.reader ?? BUILTIN) === r.id}
                className={`${chip((action.reader ?? BUILTIN) === r.id)} cursor-pointer`}
                onClick={() =>
                  onChange({
                    level: "read_with",
                    reader: r.id,
                    ...(action.redact_with ? { redact_with: action.redact_with } : {}),
                    ...(r.id === BUILTIN && action.engine ? { engine: action.engine } : {}),
                  })
                }
              >
                {r.id === BUILTIN ? m["privacy.reader.builtin"]() : r.name}
              </button>
            ))}
            {options.readers.length === 0 && (
              <span className={`text-small ${MUTED}`}>{m["privacy.edit.no_readers"]()}</span>
            )}
          </div>
          {(action.reader ?? BUILTIN) === BUILTIN && (
            <div className="flex items-center gap-2">
              <label className="text-dense" htmlFor={`${prefix}-engine`}>
                {m["privacy.edit.engine"]()}
              </label>
              <select
                id={`${prefix}-engine`}
                className="rounded-md border border-line bg-surface px-2 py-1 text-body"
                value={action.engine ?? ""}
                onChange={(event) =>
                  onChange(
                    actionBody({
                      level: "read_with",
                      reader: BUILTIN,
                      redact_with: action.redact_with,
                      engine: event.target.value || undefined,
                    }),
                  )
                }
              >
                <option value="">{m["privacy.engine.default"]()}</option>
                {ENGINES.map((e) => (
                  <option key={e} value={e}>
                    {engineLabel(e)}
                  </option>
                ))}
              </select>
              <Tip text={m["privacy.edit.engine_tip"]()} />
            </div>
          )}
        </div>
      )}
      {action.level === "refuse" ? (
        <p className={`m-0 text-small ${MUTED}`}>{m["privacy.edit.refused_no_policy"]()}</p>
      ) : (
        <div className="flex flex-col gap-1">
          <div className={`${FIELD_LABEL} flex items-center gap-2`}>
            <label htmlFor={`${prefix}-policy`}>
              {kind === "read"
                ? m["privacy.edit.redact_with_read"]()
                : kind === "embed"
                  ? m["privacy.edit.redact_with_embed"]()
                  : m["privacy.edit.redact_with_chat"]()}
            </label>
            <Tip text={m["privacy.edit.redact_with_tip"]()} />
          </div>
          <select
            id={`${prefix}-policy`}
            className="w-fit rounded-md border border-line bg-surface px-2 py-1 text-body"
            value={action.redact_with ?? ""}
            onChange={(event) =>
              onChange(
                actionBody({
                  ...action,
                  redact_with: event.target.value || undefined,
                }),
              )
            }
          >
            <option value="">{m["privacy.edit.no_policy"]()}</option>
            {policies.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>
      )}
    </div>
  );
}

// -- the page ------------------------------------------------------------------

function newRule(index: number, kind: PrivacyKind): DraftRule {
  return {
    uid: uid(),
    name: m["privacy.edit.rule_default_name"]({ number: String(index + 1) }),
    enabled: true,
    observe_only: false,
    conditions: [],
    action: { level: kind === "read" ? "default" : "pass" },
  };
}

export function PipelineEditorPage({ area }: { area: Area }) {
  const own = area === "me";
  const params = useParams();
  const [search] = useSearchParams();
  const navigate = useNavigate();
  const toast = useOptionalToast();
  const back = own ? "/privacy" : "/admin/privacy";
  const id = params.id === "new" ? undefined : params.id;

  const pipelines = usePipelines(area);
  const options = usePrivacyOptions(area);
  const profiles = usePrivacyProfiles();
  const save = useSavePipeline(area);
  const check = useCheckPipeline(area);
  const existing = pipelines.data?.items.find((p) => p.id === id) ?? null;

  const initialKind = (KINDS.find((k) => k === search.get("kind")) ?? "embed") as PrivacyKind;
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<PrivacyKind>(initialKind);
  const [enabled, setEnabled] = useState(true);
  const [whoMode, setWhoMode] = useState<"all" | "users" | "groups" | "keys">("all");
  const [whoIds, setWhoIds] = useState<string[]>([]);
  const [targetMode, setTargetMode] = useState<"all" | "only" | "except">("all");
  const [providers, setProviders] = useState<string[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [rules, setRules] = useState<DraftRule[]>([]);
  const [otherwise, setOtherwise] = useState<PrivacyAction>({ level: baseLevel(initialKind) });
  const [open, setOpen] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<PrivacyIssue[]>([]);
  const [saved, setSaved] = useState(false);

  // Keyed seeding, as the other editors do: without it the fields keep the
  // previous pipeline's values when the list resolves.
  if (existing && loadedFor !== existing.id) {
    setLoadedFor(existing.id);
    setName(existing.name);
    setKind(existing.kind as PrivacyKind);
    setEnabled(existing.enabled);
    setWhoMode(existing.who.mode);
    setWhoIds([...existing.who.ids]);
    setTargetMode(existing.target.mode);
    setProviders([...existing.target.providers]);
    setModels([...existing.target.models]);
    setRules(
      existing.rules.map((r) => {
        const body = ruleBody(r);
        return {
          uid: uid(),
          id: body.id ?? undefined,
          name: body.name,
          enabled: body.enabled ?? true,
          observe_only: body.observe_only ?? false,
          conditions: (body.conditions ?? []).map((c) => ({
            key: c.key,
            values: c.values ?? [],
            negate: c.negate ?? false,
            decide: c.decide ?? null,
          })),
          action: body.action,
        };
      }),
    );
    setOtherwise(actionBody(existing.otherwise.action));
  }

  const body: PrivacyPipelineInput = useMemo(
    () => ({
      kind,
      name,
      enabled,
      who: { mode: whoMode, ids: whoIds },
      target: { mode: targetMode, providers, models },
      rules: rules.map((r) => ({
        ...(r.id ? { id: r.id } : {}),
        name: r.name,
        enabled: r.enabled,
        observe_only: r.observe_only,
        conditions: r.conditions,
        action: r.action,
      })),
      otherwise,
    }),
    [kind, name, enabled, whoMode, whoIds, targetMode, providers, models, rules, otherwise],
  );

  // The warnings are asked for as the pipeline is written, never to block saving.
  const signature = JSON.stringify(body);
  const { mutate: runCheck } = check;
  useEffect(() => {
    if (!name.trim()) return;
    const timer = setTimeout(
      () =>
        runCheck(body, {
          onSuccess: (result) =>
            setWarnings(
              result.error
                ? [{ code: "error", message: result.error, rule_id: null, rule_name: null }]
                : result.issues,
            ),
        }),
      500,
    );
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  if (id && pipelines.isPending) {
    return (
      <div className={PAGE}>
        <Spinner label={m["privacy.loading"]()} />
      </div>
    );
  }
  if (id && !existing) {
    return (
      <div className={PAGE}>
        <Notice tone="danger" title={m["privacy.edit.missing"]()}>
          <a href={back}>{m["privacy.edit.back"]()}</a>
        </Notice>
      </div>
    );
  }
  const opts = options.data;
  if (!opts) {
    return (
      <div className={PAGE}>
        {options.error ? (
          <Notice tone="danger" title={m["privacy.load_error"]()}>
            {/* i18n: server text, the API's own message. */}
            {options.error instanceof Error ? options.error.message : m["privacy.unknown_error"]()}
          </Notice>
        ) : (
          <Spinner label={m["privacy.loading"]()} />
        )}
      </div>
    );
  }

  const targetModels = opts.models.filter((x) =>
    kind === "read" ? x.kind === "ocr" : kind === "embed" ? x.kind === "embedding" || x.kind === "rerank" : x.kind === "chat",
  );
  const whoOptions =
    whoMode === "users" ? opts.users : whoMode === "groups" ? opts.groups : whoMode === "keys" ? opts.keys : [];
  const profileList = (profiles.data?.items ?? []).map((p) => ({ id: p.id, name: p.name }));

  const changeKind = (next: PrivacyKind) => {
    setKind(next);
    setRules([]);
    setOtherwise({ level: baseLevel(next) });
    setProviders([]);
    setModels([]);
    setOpen(null);
  };
  const patchRule = (u: string, patch: Partial<DraftRule>) =>
    setRules(rules.map((r) => (r.uid === u ? { ...r, ...patch } : r)));
  const moveRule = (index: number, delta: -1 | 1) => {
    const to = index + delta;
    if (to < 0 || to >= rules.length) return;
    const next = [...rules];
    [next[index], next[to]] = [next[to]!, next[index]!];
    setRules(next);
  };
  const labels: Record<string, string> = {};
  for (const p of [...opts.policies, ...opts.own_policies]) labels[p.id] = p.name;

  const submit = () =>
    save.mutate(
      { id, body },
      {
        onSuccess: () => {
          setSaved(true);
          toast?.add({ title: m["privacy.edit.saved"](), type: "success" });
          navigate(back);
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
        title={name.trim() || (own ? m["privacy.edit.title_new_own"]() : m["privacy.edit.title_new"]())}
        subtitle={kindHint(kind)}
        actions={saved ? <Badge tone="ok">{m["privacy.edit.saved"]()}</Badge> : undefined}
      />
      {save.error && (
        <Notice tone="danger" title={m["privacy.save_error"]()}>
          {/* i18n: server text, the gateway's own error message. */}
          {save.error.message}
        </Notice>
      )}
      <Card>
        <div className={FORM}>
          <Input
            label={m["privacy.edit.name"]()}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
          <div>
            <div className={`${FIELD_LABEL} flex items-center gap-2`}>
              {m["privacy.edit.kind"]()}
              {existing && <Tip text={m["privacy.edit.kind_fixed"]()} />}
            </div>
            {existing ? (
              <Badge tone="accent">{kindLabel(kind)}</Badge>
            ) : (
              <Segmented
                label={m["privacy.edit.kind"]()}
                value={kind}
                options={KINDS.map((k) => ({ value: k, label: kindLabel(k) }))}
                onChange={changeKind}
              />
            )}
          </div>
          <div>
            <div className={`${FIELD_LABEL} flex items-center gap-2`}>
              {m["privacy.edit.who"]()}
              <Tip text={own ? m["privacy.edit.who_tip_own"]() : m["privacy.edit.who_tip"]()} />
            </div>
            {own ? (
              <Badge tone="neutral">{m["privacy.scope.me"]()}</Badge>
            ) : (
              <div className="flex flex-col gap-2">
                <Segmented
                  label={m["privacy.edit.who"]()}
                  value={whoMode}
                  options={[
                    { value: "all", label: m["privacy.who.all"]() },
                    { value: "users", label: m["privacy.who.users"]() },
                    { value: "groups", label: m["privacy.who.groups"]() },
                    { value: "keys", label: m["privacy.who.keys"]() },
                  ]}
                  onChange={(mode) => {
                    setWhoMode(mode);
                    setWhoIds([]);
                  }}
                />
                {whoMode !== "all" && (
                  <Chips
                    label={m["privacy.edit.who"]()}
                    options={whoOptions.map((o) => ({ value: o.id, label: o.name }))}
                    selected={whoIds}
                    onToggle={(v) => setWhoIds(toggled(whoIds, v))}
                  />
                )}
              </div>
            )}
          </div>
          <div>
            <div className={`${FIELD_LABEL} flex items-center gap-2`}>
              {m["privacy.edit.target"]()}
              <Tip text={kind === "read" ? m["privacy.edit.target_tip_read"]() : m["privacy.edit.target_tip"]()} />
            </div>
            <div className="flex flex-col gap-2">
              <Segmented
                label={m["privacy.edit.target"]()}
                value={targetMode}
                options={[
                  { value: "all", label: m["privacy.target.all"]() },
                  { value: "only", label: m["privacy.target.only"]() },
                  { value: "except", label: m["privacy.target.except"]() },
                ]}
                onChange={(mode) => {
                  setTargetMode(mode);
                  if (mode === "all") {
                    setProviders([]);
                    setModels([]);
                  }
                }}
              />
              {targetMode !== "all" && (
                <>
                  <Chips
                    label={m["privacy.edit.providers"]()}
                    options={opts.providers.map((p) => ({ value: p.id, label: p.name }))}
                    selected={providers}
                    onToggle={(v) => setProviders(toggled(providers, v))}
                  />
                  <Chips
                    label={m["privacy.edit.models"]()}
                    options={targetModels.map((x) => ({
                      value: x.id,
                      label: `${x.provider ?? ""} / ${x.name}`,
                    }))}
                    selected={models}
                    onToggle={(v) => setModels(toggled(models, v))}
                  />
                </>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2 text-body">
            <Switch label={m["privacy.edit.enabled"]()} checked={enabled} onChange={setEnabled} />
            <span aria-hidden>{m["privacy.edit.enabled"]()}</span>
          </div>
        </div>
      </Card>

      <section className="flex flex-col gap-2" aria-label={m["privacy.edit.rules"]()}>
        <div className="flex flex-wrap items-baseline gap-2">
          <h2 className="m-0 text-heading font-semibold">{m["privacy.edit.rules"]()}</h2>
          <span className={`flex items-center gap-1 text-small ${MUTED}`}>
            {m["privacy.edit.first_match"]()}
            <Tip text={m["privacy.edit.first_match_tip"]()} />
          </span>
        </div>
        <ol className="m-0 flex list-none flex-col gap-2 p-0">
          {rules.map((rule, index) => {
            const isOpen = open === rule.uid;
            const nameOnly =
              kind === "read" &&
              rule.conditions.length > 0 &&
              rule.conditions.every((c) => c.key === "filename" || c.key === "filetype") &&
              rule.action.level !== baseLevel(kind);
            return (
              <li key={rule.uid} className="rounded-lg border border-line bg-surface p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`w-5 text-right text-small ${MUTED}`} aria-hidden="true">
                    {index + 1}
                  </span>
                  <span className="min-w-0 flex-1">
                    <strong>{rule.name}</strong>
                    <span className={`block text-dense ${MUTED}`}>
                      {ruleSentence(kind, { ...rule, conditions: rule.conditions }, labels)}
                    </span>
                  </span>
                  <Button
                    size="sm"
                    aria-label={m["privacy.row.up"]({ name: rule.name })}
                    disabled={index === 0}
                    onClick={() => moveRule(index, -1)}
                  >
                    ↑
                  </Button>
                  <Button
                    size="sm"
                    aria-label={m["privacy.row.down"]({ name: rule.name })}
                    disabled={index === rules.length - 1}
                    onClick={() => moveRule(index, 1)}
                  >
                    ↓
                  </Button>
                  <Button size="sm" onClick={() => setOpen(isOpen ? null : rule.uid)}>
                    {isOpen ? m["privacy.edit.close"]() : m["privacy.row.edit"]()}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={m["privacy.edit.delete_rule"]({ name: rule.name })}
                    onClick={() => setRules(rules.filter((r) => r.uid !== rule.uid))}
                  >
                    ×
                  </Button>
                </div>
                {isOpen && (
                  <div className="mt-3 flex flex-col gap-3 border-t border-line-quiet pt-3">
                    <Input
                      label={m["privacy.edit.rule_name"]()}
                      value={rule.name}
                      onChange={(event) => patchRule(rule.uid, { name: event.target.value })}
                    />
                    <ConditionsEditor
                      kind={kind}
                      conditions={rule.conditions}
                      onChange={(conditions) => patchRule(rule.uid, { conditions })}
                      profiles={profileList}
                      warnNameOnly={nameOnly}
                    />
                    <ActionEditor
                      kind={kind}
                      action={rule.action}
                      onChange={(action) => patchRule(rule.uid, { action })}
                      options={opts}
                      own={own}
                      last={false}
                      prefix={rule.uid}
                    />
                    <label className="flex items-center gap-2 text-body">
                      <input
                        type="checkbox"
                        checked={rule.observe_only}
                        onChange={(e) => patchRule(rule.uid, { observe_only: e.target.checked })}
                      />
                      {m["privacy.edit.observe"]()}
                      <Tip text={m["privacy.edit.observe_tip"]()} />
                    </label>
                    <div className="flex items-center gap-2 text-body">
                      <Switch
                        size="sm"
                        label={m["privacy.edit.rule_enabled"]()}
                        checked={rule.enabled}
                        onChange={(next) => patchRule(rule.uid, { enabled: next })}
                      />
                      <span aria-hidden>{m["privacy.edit.rule_enabled"]()}</span>
                    </div>
                  </div>
                )}
              </li>
            );
          })}
          <li className="rounded-lg border border-dashed border-line bg-surface p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`w-5 text-right text-small ${MUTED}`} aria-hidden="true">
                ∗
              </span>
              <span className="min-w-0 flex-1">
                <strong>{m["privacy.edit.else"]()}</strong>
                <Tip text={m["privacy.edit.else_tip"]()} />
                <span className={`block text-dense ${MUTED}`}>
                  {ruleSentence(kind, { conditions: [], action: otherwise, observe_only: false }, labels)}
                </span>
              </span>
              <Button size="sm" onClick={() => setOpen(open === "else" ? null : "else")}>
                {open === "else" ? m["privacy.edit.close"]() : m["privacy.row.edit"]()}
              </Button>
            </div>
            {open === "else" && (
              <div className="mt-3 border-t border-line-quiet pt-3">
                <ActionEditor
                  kind={kind}
                  action={otherwise}
                  onChange={setOtherwise}
                  options={opts}
                  own={own}
                  last
                  prefix="else"
                />
              </div>
            )}
          </li>
        </ol>
        <div>
          <Button
            onClick={() => {
              const rule = newRule(rules.length, kind);
              setRules([...rules, rule]);
              setOpen(rule.uid);
            }}
          >
            {m["privacy.edit.add_rule"]()}
          </Button>
        </div>
      </section>

      {warnings.length > 0 && (
        <Notice tone="warn" title={m["privacy.edit.warnings"]()}>
          <ul className="m-0 list-disc pl-5">
            {warnings.map((w, index) => (
              // i18n: server text, the gateway's own warning.
              <li key={`${w.code}-${index}`}>{w.message}</li>
            ))}
          </ul>
        </Notice>
      )}

      <div
        role="region"
        aria-label={m["privacy.edit.savebar"]()}
        className="sticky bottom-0 z-10 flex flex-wrap items-center justify-end gap-2 rounded-lg border border-line bg-surface px-4 py-3 shadow-md"
      >
        <Button onClick={() => navigate(back)}>{m["privacy.edit.back"]()}</Button>
        <Button variant="primary" busy={save.isPending} disabled={!name.trim()} onClick={submit}>
          {existing ? m["privacy.edit.save"]() : m["privacy.edit.create"]()}
        </Button>
      </div>
    </div>
  );
}

/** The route element for an administrator's editor. */
export function AdminPipeline() {
  return <PipelineEditorPage area="admin" />;
}

export function MyPipeline() {
  return <PipelineEditorPage area="me" />;
}

export type { PrivacyOptions };
