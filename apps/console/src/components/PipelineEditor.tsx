import { Badge, Button, Card, Input, Notice, Spinner, Switch } from "@llmp/ui";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { FIELD_LABEL, FORM, MUTED, PAGE } from "../lib/layout";
import {
  DEFAULT_READER,
  EXTRACT_LEVELS,
  FILETYPES,
  KINDS,
  MODEL_TYPES,
  RULE_MODES,
  conditionHint,
  conditionKeys,
  conditionLabel,
  filetypeLabel,
  kindHint,
  kindLabel,
  levelHint,
  levelLabel,
  modelTypeLabel,
  ruleBody,
  ruleModeLabel,
  ruleSentence,
  useCheckPipeline,
  usePipelines,
  usePrivacyOptions,
  useSavePipeline,
  type Area,
  type Cond,
} from "../lib/privacy";
import { useOptionalToast } from "../lib/toast";
import type {
  PrivacyAction,
  PrivacyIssue,
  PrivacyKind,
  PrivacyModelType,
  PrivacyOptions,
  PrivacyPipelineInput,
  PrivacyPipelineUpdate,
} from "../lib/types";
import { m } from "../paraglide/messages.js";
import { PageHeader } from "./PageHeader";
import { Tip } from "./Tip";

/**
 * The pipeline editor, one for an administrator and for a user (ADR 0113, 0114).
 *
 * The same object, so the same form; what differs is what is offered. A user's
 * who is fixed to them and `classified` is not offered. Each rule is one sentence,
 * "When [policies] + [conditions] -> [mode]", with the per-type overrides collapsed.
 */

interface DraftRule {
  /** Client-only, for React keys; the saved id is `id`. */
  uid: string;
  id?: string;
  name: string;
  enabled: boolean;
  /** A warning the migration left on a rule it switched off; kept as it came. */
  note: string | null;
  policies: string[];
  match: "all" | "any";
  conditions: Cond[];
  mode: string;
  overrides: Record<string, string>;
  /** Extract rules only. */
  action: PrivacyAction | null;
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
  match,
  onChange,
  onMatch,
  own,
  classifiers,
  warnNameOnly,
}: {
  kind: PrivacyKind;
  conditions: Cond[];
  match: "all" | "any";
  onChange: (next: Cond[]) => void;
  onMatch: (next: "all" | "any") => void;
  /** A user's own editor: no `classified`. */
  own: boolean;
  classifiers: { id: string; name: string }[];
  warnNameOnly: boolean;
}) {
  // A key may appear once, except `classified`, which names one classifier each.
  const available = conditionKeys(kind, own).filter(
    (k) => k === "classified" || !conditions.some((c) => c.key === k),
  );
  const set = (index: number, patch: Partial<Cond>) =>
    onChange(conditions.map((c, i) => (i === index ? { ...c, ...patch } : c)));
  const flip = (index: number, c: Cond) => (
    <Button
      size="sm"
      aria-label={m["privacy.cond.flip"]({ name: conditionLabel(c.key) })}
      onClick={() => set(index, { negate: !c.negate })}
    >
      {c.key === "filename"
        ? c.negate
          ? m["privacy.cond.not_matches"]()
          : m["privacy.cond.matches"]()
        : c.negate
          ? m["privacy.cond.is_not"]()
          : m["privacy.cond.is"]()}
    </Button>
  );
  return (
    <div className="flex flex-col gap-2">
      <div className={`${FIELD_LABEL} flex flex-wrap items-center gap-2`}>
        {m["privacy.edit.conditions"]()}
        <Tip text={m["privacy.edit.conditions_tip"]()} />
        {conditions.length > 1 && (
          <Segmented
            label={m["privacy.rule.match"]()}
            value={match}
            options={[
              { value: "all", label: m["privacy.rule.match_all"]() },
              { value: "any", label: m["privacy.rule.match_any"]() },
            ]}
            onChange={onMatch}
          />
        )}
      </div>
      {conditions.map((c, index) => {
        const name = conditionLabel(c.key);
        return (
          <div
            key={`${c.key}-${index}`}
            className="flex flex-wrap items-center gap-2 rounded-lg border border-line-quiet p-2"
          >
            <strong className="text-dense" title={conditionHint(c.key)}>
              {name}
            </strong>
            {flip(index, c)}
            {c.key === "filetype" && (
              <Chips
                label={name}
                options={FILETYPES.map((v) => ({ value: v, label: filetypeLabel(v) }))}
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
            {c.key === "finds" && (
              <span className="flex items-center gap-1 text-dense">
                <input
                  type="number"
                  min={1}
                  max={1000}
                  className="w-16 rounded-md border border-line bg-surface px-2 py-1 text-body"
                  aria-label={m["privacy.cond.finds_count"]()}
                  value={c.count ?? ""}
                  onChange={(event) =>
                    // Empty while it is being retyped; the save sends 1 for it.
                    set(index, {
                      count:
                        event.target.value === ""
                          ? undefined
                          : Math.min(1000, Math.max(1, Math.trunc(Number(event.target.value)) || 1)),
                    })
                  }
                />
                {m["privacy.cond.items"]()}
              </span>
            )}
            {c.key === "classified" && (
              <>
                <select
                  className="rounded-md border border-line bg-surface px-2 py-1 text-body"
                  aria-label={m["privacy.cond.classified_pick"]()}
                  value={c.values[0] ?? ""}
                  onChange={(event) => set(index, { values: event.target.value ? [event.target.value] : [] })}
                >
                  <option value="">{m["privacy.cond.choose_classifier"]()}</option>
                  {classifiers.map((x) => (
                    <option key={x.id} value={x.id}>
                      {x.name}
                    </option>
                  ))}
                </select>
                <Tip text={m["privacy.cond.classified_warning"]()} />
                {classifiers.length === 0 && (
                  <span className={`text-small ${MUTED}`}>
                    {m["privacy.cond.no_classifiers"]()}{" "}
                    <Link to="/admin/privacy/classifiers">{m["privacy.cond.classifiers_link"]()}</Link>
                  </span>
                )}
              </>
            )}
            <Button
              size="sm"
              variant="ghost"
              aria-label={m["privacy.cond.remove"]({ name })}
              onClick={() => onChange(conditions.filter((_, i) => i !== index))}
            >
              ×
            </Button>
          </div>
        );
      })}
      {available.length > 0 && (
        <div className="flex items-center gap-2">
          <select
            className="w-fit rounded-md border border-line bg-surface px-2 py-1 text-body"
            aria-label={m["privacy.cond.add"]()}
            value=""
            onChange={(event) => {
              const key = event.target.value;
              if (!key) return;
              onChange([
                ...conditions,
                { key, values: [], negate: false, ...(key === "finds" ? { count: 1 } : {}) },
              ]);
            }}
          >
            <option value="">{m["privacy.cond.add_option"]()}</option>
            {available.map((k) => (
              <option key={k} value={k}>
                {conditionLabel(k)}
              </option>
            ))}
          </select>
          {kind === "extract" && <Tip text={m["privacy.cond.read_hint"]()} />}
        </div>
      )}
      {warnNameOnly && (
        <Notice tone="warn" title={m["privacy.cond.renamed_title"]()}>
          {m["privacy.cond.renamed"]()}
        </Notice>
      )}
    </div>
  );
}

// -- policies, mode and overrides ------------------------------------------------

function PoliciesEditor({
  kind,
  rule,
  onChange,
  options,
  prefix,
}: {
  kind: PrivacyKind;
  rule: DraftRule;
  onChange: (patch: Partial<DraftRule>) => void;
  options: PrivacyOptions;
  prefix: string;
}) {
  const policies = [...options.policies, ...options.own_policies];
  const nameOf = (id: string) => policies.find((p) => p.id === id)?.name ?? "?";
  const free = policies.filter((p) => !rule.policies.includes(p.id));
  const overrides = Object.entries(rule.overrides);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <div className={`${FIELD_LABEL} flex items-center gap-2`}>
          {kind === "extract" ? m["privacy.rule.hide_extract"]() : m["privacy.rule.policies"]()}
          <Tip text={m["privacy.rule.policies_tip"]()} />
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {rule.policies.map((id) => (
            <span key={id} className={`${chip(true)} flex items-center gap-1`}>
              {nameOf(id)}
              <button
                type="button"
                className="cursor-pointer"
                aria-label={m["privacy.rule.remove_policy"]({ name: nameOf(id) })}
                onClick={() => onChange({ policies: rule.policies.filter((p) => p !== id) })}
              >
                ×
              </button>
            </span>
          ))}
          {free.length > 0 && (
            <select
              className="rounded-md border border-line bg-surface px-2 py-1 text-body"
              aria-label={m["privacy.rule.add_policy"]()}
              value=""
              onChange={(event) =>
                event.target.value && onChange({ policies: [...rule.policies, event.target.value] })
              }
            >
              <option value="">{m["privacy.rule.add_policy"]()}</option>
              {free.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          )}
          {policies.length === 0 && (
            <span className={`text-small ${MUTED}`}>{m["privacy.rule.no_policies_available"]()}</span>
          )}
          {kind === "content" && rule.policies.length === 0 && policies.length > 0 && (
            <span className={`text-small ${MUTED}`}>{m["privacy.rule.no_policies"]()}</span>
          )}
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${prefix}-mode`} className={`${FIELD_LABEL} flex items-center gap-2`}>
          {m["privacy.rule.mode"]()}
          <Tip text={m["privacy.rule.mode_tip"]()} />
        </label>
        <select
          id={`${prefix}-mode`}
          className="w-fit rounded-md border border-line bg-surface px-2 py-1 text-body"
          value={rule.mode}
          onChange={(event) => onChange({ mode: event.target.value })}
        >
          {RULE_MODES.filter((mode) => kind === "content" || mode !== "block").map((mode) => (
            <option key={mode} value={mode}>
              {ruleModeLabel(mode)}
            </option>
          ))}
        </select>
      </div>
      <details className="rounded-lg border border-line-quiet p-2" open={overrides.length > 0}>
        <summary className={`cursor-pointer ${FIELD_LABEL}`}>
          {m["privacy.rule.overrides"]()}
          {overrides.length > 0 && ` (${overrides.length})`}
        </summary>
        <div className="mt-2 flex flex-col gap-2">
          <span className={`text-small ${MUTED}`}>{m["privacy.rule.overrides_tip"]()}</span>
          {overrides.map(([type, mode]) => (
            <div key={type} className="flex flex-wrap items-center gap-2">
              <code className="text-dense">{type}</code>
              <select
                className="rounded-md border border-line bg-surface px-2 py-1 text-body"
                aria-label={m["privacy.rule.override_mode"]({ type })}
                value={mode}
                onChange={(event) =>
                  onChange({ overrides: { ...rule.overrides, [type]: event.target.value } })
                }
              >
                {RULE_MODES.filter((x) => kind === "content" || x !== "block").map((x) => (
                  <option key={x} value={x}>
                    {ruleModeLabel(x)}
                  </option>
                ))}
              </select>
              <Button
                size="sm"
                variant="ghost"
                aria-label={m["privacy.rule.override_remove"]({ type })}
                onClick={() => {
                  const next = { ...rule.overrides };
                  delete next[type];
                  onChange({ overrides: next });
                }}
              >
                ×
              </Button>
            </div>
          ))}
          <OverrideAdd onAdd={(type) => onChange({ overrides: { ...rule.overrides, [type]: "redact" } })} />
        </div>
      </details>
    </div>
  );
}

function OverrideAdd({ onAdd }: { onAdd: (type: string) => void }) {
  const [type, setType] = useState("");
  return (
    <div className="flex items-center gap-2">
      <input
        className="w-48 rounded-md border border-line bg-surface px-2 py-1 font-mono text-body"
        aria-label={m["privacy.rule.override_type"]()}
        placeholder={m["privacy.rule.override_placeholder"]()}
        value={type}
        onChange={(event) => setType(event.target.value)}
      />
      <Button
        size="sm"
        disabled={!type.trim()}
        onClick={() => {
          onAdd(type.trim().toUpperCase().replace(/[^A-Z0-9_]+/g, "_"));
          setType("");
        }}
      >
        {m["privacy.rule.override_add"]()}
      </Button>
    </div>
  );
}

function ExtractActionEditor({
  action,
  onChange,
  options,
  prefix,
}: {
  action: PrivacyAction;
  onChange: (next: PrivacyAction) => void;
  options: PrivacyOptions;
  prefix: string;
}) {
  return (
    <div className="flex flex-col gap-3">
      <div className={`${FIELD_LABEL} flex items-center gap-2`}>{m["privacy.rule.extract_action"]()}</div>
      <div role="radiogroup" aria-label={m["privacy.rule.extract_action"]()} className="flex flex-col gap-1">
        {EXTRACT_LEVELS.map((level) => (
          <label
            key={level}
            className={`flex cursor-pointer items-start gap-2 rounded-lg border p-2 ${
              action.level === level ? "border-accent bg-sunken" : "border-line"
            }`}
          >
            <input
              type="radio"
              name={`${prefix}-level`}
              checked={action.level === level}
              onChange={() =>
                onChange({
                  level,
                  ...(level === "extract_with" ? { reader: action.reader ?? DEFAULT_READER } : {}),
                })
              }
            />
            <span>
              <strong>{levelLabel(level)}</strong>
              <span className={`block text-small ${MUTED}`}>{levelHint(level)}</span>
            </span>
          </label>
        ))}
      </div>
      {action.level === "extract_with" && (
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
                aria-pressed={(action.reader ?? DEFAULT_READER) === r.id}
                className={`${chip((action.reader ?? DEFAULT_READER) === r.id)} cursor-pointer`}
                onClick={() => onChange({ level: "extract_with", reader: r.id })}
              >
                {r.name}
              </button>
            ))}
            {options.readers.length === 0 && (
              <span className={`text-small ${MUTED}`}>{m["privacy.edit.no_readers"]()}</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// -- the page ------------------------------------------------------------------

/** An update body: the create body without the kind, which the gateway refuses to change. */
function updateBody(body: PrivacyPipelineInput): PrivacyPipelineUpdate {
  const { kind: _kind, ...rest } = body;
  return rest;
}

function newRule(index: number, kind: PrivacyKind): DraftRule {
  return {
    uid: uid(),
    name: m["privacy.edit.rule_default_name"]({ number: String(index + 1) }),
    enabled: true,
    note: null,
    policies: [],
    match: "all",
    conditions: [],
    mode: kind === "content" ? "anonymise_restore" : "off",
    overrides: {},
    action: kind === "extract" ? { level: "default" } : null,
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
  const save = useSavePipeline(area);
  const check = useCheckPipeline(area);
  const existing = pipelines.data?.items.find((p) => p.id === id) ?? null;

  const initialKind = (KINDS.find((k) => k === search.get("kind")) ?? "content") as PrivacyKind;
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<PrivacyKind>(initialKind);
  const [enabled, setEnabled] = useState(true);
  const [whoMode, setWhoMode] = useState<"all" | "users" | "groups" | "keys">("all");
  const [whoIds, setWhoIds] = useState<string[]>([]);
  const [targetMode, setTargetMode] = useState<"all" | "only" | "except">("all");
  const [providers, setProviders] = useState<string[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [modelTypes, setModelTypes] = useState<PrivacyModelType[]>([]);
  const [rules, setRules] = useState<DraftRule[]>([]);
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
    setModelTypes([...(existing.target.model_types ?? [])]);
    setRules(
      existing.rules.map((r) => {
        const body = ruleBody(r);
        return {
          uid: uid(),
          id: body.id ?? undefined,
          name: body.name,
          enabled: body.enabled ?? true,
          note: body.note ?? null,
          policies: [...(body.policies ?? [])],
          match: (body.match ?? "all") as "all" | "any",
          conditions: (body.conditions ?? []).map((c) => ({
            key: c.key,
            values: c.values ?? [],
            negate: c.negate ?? false,
            ...(c.key === "finds" ? { count: c.count ?? 1 } : {}),
          })),
          mode: body.mode ?? "off",
          overrides: { ...(body.overrides ?? {}) },
          action: body.action ?? null,
        };
      }),
    );
  }

  const body: PrivacyPipelineInput = useMemo(
    () => ({
      kind,
      name,
      enabled,
      who: { mode: whoMode, ids: whoIds },
      target: { mode: targetMode, providers, models, model_types: kind === "content" ? modelTypes : [] },
      rules: rules.map((r) => ({
        ...(r.id ? { id: r.id } : {}),
        name: r.name,
        enabled: r.enabled,
        note: r.note,
        policies: r.policies,
        match: r.match,
        mode: r.mode,
        overrides: r.overrides,
        conditions: r.conditions.map((c) => ({
          key: c.key,
          values: c.values,
          negate: c.negate,
          ...(c.key === "finds" ? { count: c.count ?? 1 } : {}),
        })),
        ...(r.action ? { action: r.action } : {}),
      })),
    }),
    [kind, name, enabled, whoMode, whoIds, targetMode, providers, models, modelTypes, rules],
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
    kind === "extract"
      ? x.kind === "ocr"
      : modelTypes.length
        ? modelTypes.includes(x.kind as PrivacyModelType)
        : x.kind === "chat" || x.kind === "embedding" || x.kind === "rerank",
  );
  const whoOptions =
    whoMode === "users" ? opts.users : whoMode === "groups" ? opts.groups : whoMode === "keys" ? opts.keys : [];
  const classifierList = opts.classifiers;

  const changeKind = (next: PrivacyKind) => {
    setKind(next);
    setRules([]);
    setProviders([]);
    setModels([]);
    setModelTypes([]);
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
  for (const p of [...opts.policies, ...opts.own_policies, ...opts.classifiers]) labels[p.id] = p.name;

  // The kind is fixed once saved: only a new pipeline sends it.
  const submit = () =>
    save.mutate(
      { id, body: existing ? updateBody(body) : body },
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
              <Tip text={kind === "extract" ? m["privacy.edit.target_tip_read"]() : m["privacy.edit.target_tip"]()} />
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
          {kind === "content" && (
            <div>
              <div className={`${FIELD_LABEL} flex items-center gap-2`}>
                {m["privacy.edit.model_type"]()}
                <Tip text={m["privacy.edit.model_type_tip"]()} />
              </div>
              <Chips
                label={m["privacy.edit.model_type"]()}
                options={MODEL_TYPES.map((t) => ({ value: t, label: modelTypeLabel(t) }))}
                selected={modelTypes}
                onToggle={(v) => setModelTypes(toggled(modelTypes, v))}
              />
              {modelTypes.length === 0 && (
                <span className={`text-small ${MUTED}`}>{m["privacy.edit.model_type_any"]()}</span>
              )}
            </div>
          )}
          <div className="flex items-center gap-2 text-body">
            <Switch label={m["privacy.edit.enabled"]()} checked={enabled} onChange={setEnabled} />
            <span aria-hidden>{m["privacy.edit.enabled"]()}</span>
          </div>
        </div>
      </Card>

      <section className="flex flex-col gap-2" aria-label={m["privacy.edit.rules"]()}>
        <div className="flex flex-wrap items-baseline gap-2">
          <h2 className="m-0 text-heading font-semibold">{m["privacy.edit.rules"]()}</h2>
        </div>
        <p className={`m-0 text-small ${MUTED}`}>{m["privacy.edit.first_match_tip"]()}</p>
        <ol className="m-0 flex list-none flex-col gap-2 p-0">
          {rules.map((rule, index) => {
            const isOpen = open === rule.uid;
            const nameOnly =
              kind === "extract" &&
              rule.conditions.length > 0 &&
              rule.conditions.every((c) => c.key === "filename" || c.key === "filetype") &&
              (rule.action?.level ?? "default") !== "default";
            return (
              <li key={rule.uid} className="rounded-lg border border-line bg-surface p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`w-5 text-right text-small ${MUTED}`} aria-hidden="true">
                    {index + 1}
                  </span>
                  <span className="min-w-0 flex-1">
                    <strong>{rule.name}</strong>
                    <span className={`block text-dense ${MUTED}`}>
                      {ruleSentence(kind, rule, labels)}
                    </span>
                    {rule.note && (
                      <span className="mt-1 flex flex-wrap items-center gap-2 text-dense">
                        <Badge tone="warn">{m["privacy.rule.note_badge"]()}</Badge>
                        {/* i18n: server text, left by the migration on this rule. */}
                        {rule.note}
                      </span>
                    )}
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
                    {rule.note && (
                      <Notice tone="warn" title={m["privacy.rule.note_title"]()}>
                        {/* i18n: server text, left by the migration on this rule. */}
                        {rule.note}
                      </Notice>
                    )}
                    {kind === "content" && (
                      <PoliciesEditor
                        kind={kind}
                        rule={rule}
                        onChange={(patch) => patchRule(rule.uid, patch)}
                        options={opts}
                        prefix={rule.uid}
                      />
                    )}
                    <ConditionsEditor
                      kind={kind}
                      conditions={rule.conditions}
                      match={rule.match}
                      onChange={(conditions) => patchRule(rule.uid, { conditions })}
                      onMatch={(match) => patchRule(rule.uid, { match })}
                      own={own}
                      classifiers={classifierList}
                      warnNameOnly={nameOnly}
                    />
                    {kind === "extract" && rule.action && (
                      <>
                        <ExtractActionEditor
                          action={rule.action}
                          onChange={(action) => patchRule(rule.uid, { action })}
                          options={opts}
                          prefix={rule.uid}
                        />
                        <PoliciesEditor
                          kind={kind}
                          rule={rule}
                          onChange={(patch) => patchRule(rule.uid, patch)}
                          options={opts}
                          prefix={`${rule.uid}-hide`}
                        />
                      </>
                    )}
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
