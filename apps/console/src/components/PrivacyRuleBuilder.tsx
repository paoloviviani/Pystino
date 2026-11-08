import { Button, Input, Select } from "@llmp/ui";
import { useState } from "react";

import {
  CONDITION_KEYS,
  FILETYPES,
  LANGUAGES,
  PATTERNS,
  READERS,
  VERDICTS,
  conditionLabel,
  conditionPhrase,
  filetypeLabel,
  isContentKey,
  languageLabel,
  readerLabel,
  verdictLabel,
} from "../lib/privacy";
import type {
  PrivacyAction,
  PrivacyCondition,
  PrivacyKind,
  PrivacyProfile,
} from "../lib/types";
import { MUTED } from "../lib/layout";
import { m } from "../paraglide/messages.js";

/**
 * The sentence-style rule builder: "When [chips], then [action]".
 *
 * Each condition is a chip that reads as part of the sentence; tapping it opens
 * its editor underneath (one at a time, so a phone keeps its place): is / is
 * not, the values, and for what the text contains, how that is decided. The
 * kind is fixed by the caller, so only what is valid for it is ever offered.
 */

export interface PrivacyRuleBuilderProps {
  kind: PrivacyKind;
  conditions: PrivacyCondition[];
  action: PrivacyAction;
  onConditions: (next: PrivacyCondition[]) => void;
  onAction: (next: PrivacyAction) => void;
  profiles: PrivacyProfile[];
  /** Group names the sign-in provider has produced, for the "who" picker. */
  groups: string[];
}

function toggle(values: string[], value: string): string[] {
  return values.includes(value) ? values.filter((v) => v !== value) : [...values, value];
}

/** The choices of a key that has a closed set; null for the ones typed in. */
function closedOptions(key: string): { value: string; label: string }[] | null {
  switch (key) {
    case "filetype":
      return FILETYPES.map((value) => ({ value, label: filetypeLabel(value) }));
    case "verdict":
      return VERDICTS.map((value) => ({ value, label: verdictLabel(value) }));
    case "lang":
      return LANGUAGES.map((value) => ({ value, label: languageLabel(value) }));
    default:
      return null;
  }
}

function Checks({
  name,
  options,
  values,
  onChange,
}: {
  name: string;
  options: { value: string; label: string }[];
  values: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <fieldset className="m-0 flex flex-wrap gap-x-4 gap-y-2 border-0 p-0">
      <legend className="sr-only">{name}</legend>
      {options.map((option) => (
        <label key={option.value} className="flex items-center gap-2 text-body">
          <input
            type="checkbox"
            checked={values.includes(option.value)}
            onChange={() => onChange(toggle(values, option.value))}
          />
          {option.label}
        </label>
      ))}
    </fieldset>
  );
}

/**
 * The patterns, one per line. The text is held here, not derived from the
 * values on every keystroke: a blank line (the one just typed after Enter) is
 * not a pattern, and rebuilding the text from the patterns would delete it and
 * make a second line impossible to start.
 */
function FilenameValues({
  values,
  onChange,
}: {
  values: string[];
  onChange: (values: string[]) => void;
}) {
  const [text, setText] = useState(values.join("\n"));
  return (
    <div className="flex flex-col gap-1">
      <label className="text-small font-medium" htmlFor="privacy-filename-values">
        {m["privacy.filename.label"]()}
      </label>
      <textarea
        id="privacy-filename-values"
        className="min-h-20 w-full rounded-lg border border-line bg-surface p-2 font-mono text-body"
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          onChange(
            event.target.value
              .split("\n")
              .map((line) => line.trim())
              .filter(Boolean),
          );
        }}
      />
      <p className={`m-0 text-small ${MUTED}`}>{m["privacy.filename.hint"]()}</p>
    </div>
  );
}

function ValuesEditor({
  condition,
  onChange,
  groups,
}: {
  condition: PrivacyCondition;
  onChange: (values: string[]) => void;
  groups: string[];
}) {
  const [typed, setTyped] = useState("");
  const closed = closedOptions(condition.key);
  const label = conditionLabel(condition.key);
  if (closed) {
    return <Checks name={label} options={closed} values={condition.values} onChange={onChange} />;
  }
  if (condition.key === "filename") {
    return <FilenameValues values={condition.values} onChange={onChange} />;
  }
  // who and target: a few fixed choices and a field to add a typed one.
  const fixed =
    condition.key === "target"
      ? [
          { value: "cloud", label: m["privacy.target.cloud"]() },
          { value: "local", label: m["privacy.target.local"]() },
        ]
      : groups.map((name) => ({ value: `group:${name}`, label: m["privacy.who.group"]({ name }) }));
  const fixedValues = new Set(fixed.map((o) => o.value));
  const extra = condition.values.filter((v) => !fixedValues.has(v));
  const placeholder =
    condition.key === "target" ? m["privacy.target.placeholder"]() : m["privacy.who.placeholder"]();
  const add = () => {
    const value = typed.trim();
    if (!value) return;
    const prefixed =
      condition.key === "who" && !/^(user|group):/.test(value) ? `user:${value}` : value;
    if (!condition.values.includes(prefixed)) onChange([...condition.values, prefixed]);
    setTyped("");
  };
  return (
    <div className="flex flex-col gap-2">
      <Checks name={label} options={fixed} values={condition.values} onChange={onChange} />
      {extra.length > 0 && (
        <ul className="m-0 flex list-none flex-wrap gap-2 p-0">
          {extra.map((value) => (
            <li key={value}>
              <button
                type="button"
                className="cursor-pointer rounded-lg bg-sunken px-2 py-1 text-small"
                aria-label={m["privacy.value.remove"]({ value })}
                onClick={() => onChange(condition.values.filter((v) => v !== value))}
              >
                {value} ×
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex gap-2">
        <Input
          label={placeholder}
          hideLabel
          placeholder={placeholder}
          value={typed}
          onChange={(event) => setTyped(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              add();
            }
          }}
        />
        <Button type="button" onClick={add}>
          {m["privacy.value.add"]()}
        </Button>
      </div>
    </div>
  );
}

function DecideEditor({
  condition,
  profiles,
  onChange,
}: {
  condition: PrivacyCondition;
  profiles: PrivacyProfile[];
  onChange: (decide: string) => void;
}) {
  const value = condition.decide ?? PATTERNS;
  const chosen = profiles.find((p) => p.id === value);
  return (
    <Select
      label={m["privacy.decide.label"]()}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      hint={
        chosen && !chosen.decides
          ? m["privacy.decide.unavailable"]({
              name: chosen.name,
              outcome:
                chosen.fallback === "fallback_to"
                  ? m["privacy.decide.outcome_fallback"]()
                  : m["privacy.decide.outcome_refuse"](),
            })
          : value === PATTERNS
            ? m["privacy.decide.patterns_hint"]()
            : m["privacy.decide.cost"]({ cost: chosen?.cost_hint ?? "" })
      }
    >
      <option value={PATTERNS}>{m["privacy.decide.patterns"]()}</option>
      {profiles.map((profile) => (
        <option key={profile.id} value={profile.id}>
          {m["privacy.decide.model"]({ name: profile.name, cost: profile.cost_hint })}
        </option>
      ))}
    </Select>
  );
}

export function PrivacyRuleBuilder({
  kind,
  conditions,
  action,
  onConditions,
  onAction,
  profiles,
  groups,
}: PrivacyRuleBuilderProps) {
  const [open, setOpen] = useState<number | null>(null);
  const names = new Map(profiles.map((p) => [p.id, p.name]));
  const unused = CONDITION_KEYS[kind].filter((key) => !conditions.some((c) => c.key === key));

  const change = (index: number, patch: Partial<PrivacyCondition>) =>
    onConditions(conditions.map((c, i) => (i === index ? { ...c, ...patch } : c)));

  const addCondition = (key: string) => {
    const first = closedOptions(key)?.[0]?.value;
    const created: PrivacyCondition = {
      key,
      values: first ? [first] : [],
      negate: false,
      ...(isContentKey(key) ? { decide: PATTERNS } : {}),
    };
    onConditions([...conditions, created]);
    setOpen(conditions.length);
  };

  const actions: { type: string; label: string }[] =
    kind === "read"
      ? [
          { type: "reader", label: m["privacy.action_type.reader"]() },
          { type: "refuse", label: m["privacy.action.refuse_read"]() },
        ]
      : [
          { type: "refuse", label: m["privacy.action.refuse_embed"]() },
          { type: "local", label: m["privacy.action.local"]() },
        ];

  return (
    <div className="flex flex-col gap-4">
      <section aria-labelledby="privacy-when" className="flex flex-col gap-2">
        <h3 id="privacy-when" className="m-0 text-small font-semibold uppercase tracking-wide text-ink-muted">
          {m["privacy.builder.when"]()}
        </h3>
        {conditions.length === 0 && (
          <p className={`m-0 text-body ${MUTED}`}>{m["privacy.builder.no_conditions"]()}</p>
        )}
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {conditions.map((condition, index) => (
            <li key={condition.key} className="flex flex-col gap-2">
              <button
                type="button"
                aria-expanded={open === index}
                className="w-fit max-w-full cursor-pointer rounded-lg border border-line bg-surface px-3 py-1.5 text-left text-body hover:bg-sunken"
                onClick={() => setOpen(open === index ? null : index)}
              >
                {index > 0 && <span className={MUTED}>{m["privacy.and_chip"]()} </span>}
                {conditionPhrase(condition, names)}
              </button>
              {open === index && (
                <div className="flex flex-col gap-3 rounded-lg border border-line-quiet bg-sunken p-3">
                  <Select
                    label={conditionLabel(condition.key)}
                    value={condition.negate ? "not" : "is"}
                    onChange={(event) => change(index, { negate: event.target.value === "not" })}
                  >
                    <option value="is">{m["privacy.negate.is"]()}</option>
                    <option value="not">{m["privacy.negate.not"]()}</option>
                  </Select>
                  <ValuesEditor
                    condition={condition}
                    groups={groups}
                    onChange={(values) => change(index, { values })}
                  />
                  {isContentKey(condition.key) && (
                    <DecideEditor
                      condition={condition}
                      profiles={profiles}
                      onChange={(decide) => change(index, { decide })}
                    />
                  )}
                  <div>
                    <Button
                      type="button"
                      onClick={() => {
                        onConditions(conditions.filter((_, i) => i !== index));
                        setOpen(null);
                      }}
                    >
                      {m["privacy.condition.remove"]()}
                    </Button>
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
        {unused.length > 0 && (
          <Select
            label={m["privacy.condition.add"]()}
            hideLabel
            value=""
            onChange={(event) => event.target.value && addCondition(event.target.value)}
          >
            <option value="">{m["privacy.condition.add"]()}</option>
            {unused.map((key) => (
              <option key={key} value={key}>
                {conditionLabel(key)}
              </option>
            ))}
          </Select>
        )}
      </section>

      <section aria-labelledby="privacy-then" className="flex flex-col gap-3">
        <h3 id="privacy-then" className="m-0 text-small font-semibold uppercase tracking-wide text-ink-muted">
          {m["privacy.builder.then"]()}
        </h3>
        <Select
          label={m["privacy.action.label"]()}
          value={action.type}
          onChange={(event) =>
            onAction(
              event.target.value === "reader"
                ? { type: "reader", reader: "markitdown" }
                : { type: event.target.value },
            )
          }
        >
          {actions.map((a) => (
            <option key={a.type} value={a.type}>
              {a.label}
            </option>
          ))}
        </Select>
        {action.type === "reader" && (
          <Select
            label={m["privacy.reader.label"]()}
            value={action.reader ?? "markitdown"}
            onChange={(event) =>
              onAction({ type: "reader", reader: event.target.value as PrivacyAction["reader"] })
            }
            hint={m["privacy.reader.hint"]()}
          >
            {READERS.map((reader) => (
              <option key={reader} value={reader}>
                {readerLabel(reader)}
              </option>
            ))}
          </Select>
        )}
        {action.type === "local" && (
          <Input
            label={m["privacy.local.model"]()}
            value={action.model ?? ""}
            onChange={(event) =>
              onAction({ type: "local", ...(event.target.value ? { model: event.target.value } : {}) })
            }
            hint={m["privacy.local.hint"]()}
          />
        )}
      </section>
    </div>
  );
}
