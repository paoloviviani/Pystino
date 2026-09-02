import { Badge, Button, Input, Select } from "@llmp/ui";
import type { ReactNode } from "react";
import { useState } from "react";
import { MODES, entityLabel, entitySource, modeRank } from "../lib/entities";
import type { EntityMode, EntityPolicy, RedactionPolicy } from "../lib/types";
import { CODE, FIELD_LABEL, FORM, MUTED } from "../lib/layout";

export interface PolicyFieldsProps {
  policy: RedactionPolicy;
  onChange: (policy: RedactionPolicy) => void;
  /** Types the detector reports. Whatever the policy already names is added. */
  entityTypes?: string[];
  /** The deployment's threshold, shown as each row's placeholder. */
  scoreThreshold?: number | null;
  /** Hidden on a personal policy, where an allow-list is refused by the API. */
  allowList?: boolean;
  /**
   * A policy this one may not go below. Weaker modes are offered as disabled
   * options rather than removed, so a reader can see the floor and why the mode
   * they wanted is not available.
   */
  floor?: RedactionPolicy | null;
}

/**
 * The policy document as a form: default mode, per-type modes, patterns,
 * allow-list.
 *
 * One component for three callers — the deployment policy, a scoped rule, and a
 * person's own policy — because a policy means the same thing in all three and
 * three editors would drift apart on the detail that matters most: which mode
 * is stronger than which.
 *
 * The entity rows come from the detector rather than from a list here. Entity
 * labels belong to whatever engine is installed (ADR 0026), so hard-coding them
 * would mean a console that silently omits a recogniser the engine gained last
 * week — the same failure this feature exists to fix, one level up.
 */
export function PolicyFields({
  policy,
  onChange,
  entityTypes = [],
  scoreThreshold = null,
  allowList = true,
  floor = null,
}: PolicyFieldsProps) {
  // Every type the detector holds, plus anything the policy already names — a
  // rule about a type this engine does not report is still a rule, and dropping
  // it from the screen would silently delete it on the next save.
  const known = [...new Set([...entityTypes, ...Object.keys(policy.entities)])].sort();
  const patternNames = policy.patterns.map((pattern) => pattern.name);

  const modeOf = (entity: string): EntityMode =>
    policy.entities[entity]?.mode ?? policy.default_mode;

  const floorFor = (entity: string): EntityMode | null =>
    floor === null ? null : (floor.entities[entity]?.mode ?? floor.default_mode);

  const setEntity = (entity: string, patch: Partial<EntityPolicy>) =>
    onChange({
      ...policy,
      entities: {
        ...policy.entities,
        [entity]: {
          mode: patch.mode ?? modeOf(entity),
          threshold:
            patch.threshold !== undefined
              ? patch.threshold
              : (policy.entities[entity]?.threshold ?? null),
        },
      },
    });

  const setPattern = (index: number, patch: Partial<RedactionPolicy["patterns"][number]>) =>
    onChange({
      ...policy,
      patterns: policy.patterns.map((pattern, at) =>
        at === index ? { ...pattern, ...patch } : pattern,
      ),
    });

  return (
    <div className={FORM}>
      {/* Applies to a recogniser the engine gains in a later release, which is
          why the safer direction is to protect by default rather than to let a
          new type through unnoticed. */}
      <ModeSelect
        label="Default mode"
        value={policy.default_mode}
        floor={floor?.default_mode ?? null}
        hint="Applies to anything not listed below, including types added later."
        onChange={(mode) => onChange({ ...policy, default_mode: mode })}
      />

      {known.length === 0 ? (
        <p className="text-sm text-ink-muted">
          No entity types to list. The default above still applies.
        </p>
      ) : (
        <>
          <ModeKey />
          {/* Two columns, not three.

              `auto-fill` at 20rem gave three or four columns on a wide screen,
              and with a five-option control in each cell that is where the
              screen stopped being readable: the eye has no column to run down,
              and every row is a different width. Two fixed columns at a wider
              minimum keep each row on one line and give the radios room to sit
              beside their label rather than under it. One column below 64rem,
              because five radios and a number field do not fit in half a
              laptop. */}
          <div className="grid gap-x-6 gap-y-3 [grid-template-columns:repeat(2,minmax(0,1fr))] max-[64rem]:grid-cols-1">
            {known.map((entity) => {
              const name = entityLabel(entity);
              const source = entitySource(entity, patternNames);
              // Nothing here knows this label, so `entityLabel` handed it back
              // unchanged. Printing it twice would read as a rendering fault.
              const named = name !== entity;
              return (
                <div
                  key={entity}
                  className="grid grid-cols-[1fr_minmax(5rem,6rem)] items-end gap-2 border-b border-line-quiet pb-2"
                >
                  {/* A hairline per row. With two columns the rows no longer
                      line up by accident, and a reader following one across
                      needs to be told where it ends. */}
                  <ModeRadios
                    name={`mode-${entity}`}
                    legend={
                      <span className="flex flex-wrap items-center gap-2">
                        {named && name}
                        <code className={CODE}>{entity}</code>
                        {source && <Badge>{source === "pattern" ? "Pattern" : "Model"}</Badge>}
                      </span>
                    }
                    value={modeOf(entity)}
                    floor={floorFor(entity)}
                    onChange={(mode) => setEntity(entity, { mode })}
                  />
                  <Input
                    label={`Confidence for ${name}`}
                    hideLabel
                    type="number"
                    min="0"
                    max="1"
                    step="0.05"
                    placeholder={scoreThreshold === null ? "default" : `${scoreThreshold} (default)`}
                    value={policy.entities[entity]?.threshold ?? ""}
                    onChange={(event) =>
                      setEntity(entity, {
                        threshold: event.target.value === "" ? null : Number(event.target.value),
                      })
                    }
                  />
                </div>
              );
            })}
          </div>
          {/* Measured, and the opposite of what a list of switches implies: the
              model pass runs over the whole prompt whichever types are asked
              for. See docs/performance.md. */}
          <p className="text-sm text-ink-muted">
            Turning a type off changes what is replaced, not how long detection takes.
          </p>
        </>
      )}

      {/* A fieldset because the legend is what names the group for a screen
          reader: three inputs called Name, Regex and Mode mean nothing without
          "Custom patterns" attached to them. `min-w-0` because a fieldset
          defaults to min-content, which breaks the grid. */}
      <fieldset className="m-0 grid min-w-0 gap-3 border-0 p-0">
        {/* Deliberately identical to the field labels in `@llmp/ui` — a legend
            that styled itself differently would read as a section heading
            rather than as the label of the controls under it. */}
        <legend className={`p-0 ${FIELD_LABEL}`}>Custom patterns</legend>
        {policy.patterns.map((pattern, index) => (
          // Keyed by position: a name is edited character by character, so
          // keying on it would remount the field being typed into and lose
          // focus on every keystroke.
          <div
            key={index}
            className="grid grid-cols-[minmax(8rem,1fr)_minmax(12rem,2fr)_minmax(10rem,1fr)_auto] items-end gap-3 max-[48rem]:grid-cols-1"
          >
            <Input
              label={`Pattern ${index + 1} name`}
              value={pattern.name}
              placeholder="TICKET_ID"
              onChange={(event) => setPattern(index, { name: event.target.value })}
            />
            <Input
              label={`Pattern ${index + 1} regex`}
              className={CODE}
              value={pattern.regex}
              placeholder="LINKS-[0-9]{4,}"
              onChange={(event) => setPattern(index, { regex: event.target.value })}
            />
            <ModeSelect
              label={`Pattern ${index + 1} mode`}
              value={pattern.mode}
              floor={null}
              onChange={(mode) => setPattern(index, { mode })}
            />
            <Button
              variant="ghost"
              onClick={() =>
                onChange({
                  ...policy,
                  patterns: policy.patterns.filter((_, at) => at !== index),
                })
              }
            >
              Remove
            </Button>
          </div>
        ))}
        <div>
          <Button
            onClick={() =>
              onChange({
                ...policy,
                // Redact rather than the gentlest mode: a pattern somebody wrote
                // by hand is a value they went out of their way to name.
                patterns: [...policy.patterns, { name: "", regex: "", mode: "redact" }],
              })
            }
          >
            Add pattern
          </Button>
        </div>
        <p className="text-sm text-ink-muted">
          The name becomes the entity label. RE2 syntax: no backreferences, no lookaround.
        </p>

        {/* Measured, not assumed: eight credential shapes were put through this
            deployment's detector and none was found. Two produced *wrong* hits —
            AWS_SECRET_ACCESS_KEY as a LOCATION, the word "token" as a PERSON.

            So the patterns above are seeded with the published prefixes rather
            than offered behind a button. An operator who has just configured
            thirty entity types would otherwise be entitled to assume the list
            covers credentials. It does not. */}
        <p className="text-sm text-ink-muted">
          The engine detects no credentials. The seeded patterns above cover the
          published prefixes; a bespoke format needs its own.
        </p>
      </fieldset>

      {/* For values a detector is right about the shape of and wrong about the
          meaning of: a corporate domain is a URL, and it identifies nobody. */}
      {allowList && (
        <AllowList
          values={policy.allow_list}
          onChange={(allow_list) => onChange({ ...policy, allow_list })}
        />
      )}
    </div>
  );
}

/**
 * Patterns for the credential shapes the detector cannot see.
 *
 * Deliberately few and deliberately boring. Each one matches a published,
 * documented prefix rather than trying to be clever about entropy: a regex that
 * guesses at "looks secret" fires on git hashes and base64 payloads, and a
 * redaction rule that cries wolf is turned off within the week.
 *
 * `block` rather than `redact`, alone among the defaults offered anywhere in
 * this console. A leaked key is not a privacy problem to be papered over with a
 * placeholder — sending it at all is the incident, and the request should not
 * reach a provider. The operator can weaken it in the row above; the default
 * should not be the weak one.
 *
 * RE2, so no backreferences and no lookaround — see the note above the field.
 */
const SECRET_PATTERNS = [
  { name: "OPENAI_KEY", regex: "sk-[A-Za-z0-9_-]{16,}", mode: "block" as const },
  { name: "AWS_ACCESS_KEY_ID", regex: "A(KIA|SIA)[0-9A-Z]{16}", mode: "block" as const },
  {
    name: "GITHUB_TOKEN",
    regex: "gh[pousr]_[A-Za-z0-9]{36,}",
    mode: "block" as const,
  },
  { name: "SLACK_TOKEN", regex: "xox[baprs]-[A-Za-z0-9-]{10,}", mode: "block" as const },
  {
    name: "PRIVATE_KEY",
    regex: "-----BEGIN [A-Z ]*PRIVATE KEY-----",
    mode: "block" as const,
  },
  {
    name: "BEARER_TOKEN",
    // A JWT by shape: three dot-separated base64url segments, the header
    // beginning with the encoding of `{"alg"`.
    regex: "eyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}",
    mode: "block" as const,
  },
];

/**
 * A new policy, carrying the secret patterns from the start.
 *
 * Seeded rather than offered: the detector finds no credentials at all, so a
 * blank pattern list is a policy that silently does not cover them. An operator
 * can delete any of these in the rows above — that is a decision — but it should
 * not be one taken by default and by omission.
 */
export function seededPolicy(): RedactionPolicy {
  return {
    default_mode: "off",
    entities: {},
    patterns: [...SECRET_PATTERNS],
    allow_list: [],
  };
}

/**
 * The allow-list, edited as text.
 *
 * The typed string is state of its own rather than `values.join(", ")`: parsing
 * on every keystroke deletes the comma at the moment it is typed, and the field
 * becomes impossible to type a second entry into.
 */
function AllowList({
  values,
  onChange,
}: {
  values: string[];
  onChange: (values: string[]) => void;
}) {
  const [text, setText] = useState(() => values.join(", "));

  return (
    <Input
      label="Allowlist"
      value={text}
      onChange={(event) => {
        setText(event.target.value);
        onChange(
          event.target.value
            .split(",")
            .map((item) => item.trim())
            .filter(Boolean),
        );
      }}
      placeholder="ilpost.it, example.org"
      hint="Comma separated. Matched exactly, case-insensitively."
    />
  );
}

/**
 * The five modes as radios, in the weakest-to-strongest order they are ranked in.
 *
 * A select was here first, and repeated across thirty entity types it was the
 * densest thing on the screen: every row a closed control that had to be opened
 * to read, and the answer written out in full — "Anonymise, restore in the
 * answer" — thirty times over. Radios put the current answer and the four
 * alternatives on one line, so a row is read rather than operated, and the whole
 * policy can be scanned down a column.
 *
 * The labels are short because the meaning is given once, in `ModeKey` above the
 * list, rather than thirty times inside it. Each still carries the long form as
 * a `title`, so the short word is never the only thing available.
 *
 * Weaker options are disabled rather than dropped, for the same reason the
 * select disabled them: the floor is an administrator's decision, and a control
 * that silently omits three of five choices reads as a fault rather than as a
 * constraint.
 */
function ModeRadios({
  name,
  legend,
  value,
  floor,
  onChange,
}: {
  name: string;
  legend: ReactNode;
  value: EntityMode;
  floor: EntityMode | null;
  onChange: (mode: EntityMode) => void;
}) {
  const least = floor === null ? -1 : modeRank(floor);
  return (
    // A real fieldset, because five radios need one accessible name between
    // them; without it a screen reader announces "Off" five times over with
    // nothing saying which type they belong to. No border or padding: it is a
    // row of controls and not a section.
    <fieldset className="m-0 grid min-w-0 gap-1 border-0 p-0">
      <legend className="p-0 text-sm">{legend}</legend>
      <div className="flex flex-wrap gap-x-3 gap-y-1">
        {MODES.map((mode) => {
          const disabled = modeRank(mode.value) < least;
          return (
            // Disabled because a stricter policy above sets a floor. Dimmed
            // rather than hidden: the reader is meant to see that the option
            // exists and is not theirs to choose.
            <label
              key={mode.value}
              className="inline-flex cursor-pointer items-center gap-[0.35em] text-sm whitespace-nowrap data-disabled:cursor-not-allowed data-disabled:text-ink-faint"
              title={`${mode.label} — ${mode.hint}`}
              data-disabled={disabled || undefined}
            >
              <input
                type="radio"
                name={name}
                value={mode.value}
                checked={value === mode.value}
                disabled={disabled}
                onChange={() => onChange(mode.value)}
                className="m-0 accent-accent"
              />
              {mode.short}
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

/**
 * What the five words mean, said once.
 *
 * This is the half of the density fix that matters: the labels below can only
 * be short because this is here. Without it the screen would be terser and less
 * legible, which is not the same thing as less dense.
 */
function ModeKey() {
  return (
    <dl className="m-0 grid gap-x-4 gap-y-1 bg-sunken p-3 text-sm [grid-template-columns:repeat(auto-fit,minmax(14rem,1fr))]">
      {MODES.map((mode) => (
        <div key={mode.value} className="flex items-baseline gap-2">
          <dt className="font-medium whitespace-nowrap">{mode.short}</dt>
          <dd className={`m-0 ${MUTED}`}>{mode.hint}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * A mode picker that cannot offer a mode the API would refuse.
 *
 * Kept as a select for the two places that carry one control rather than a
 * column of them — the default, and a custom pattern's row — where a
 * five-radio group would be wider than the thing it configures.
 */
function ModeSelect({
  label,
  value,
  floor,
  hint,
  onChange,
}: {
  label: ReactNode;
  value: EntityMode;
  floor: EntityMode | null;
  hint?: ReactNode;
  onChange: (mode: EntityMode) => void;
}) {
  const least = floor === null ? -1 : modeRank(floor);
  return (
    <Select
      label={label}
      value={value}
      hint={hint}
      onChange={(event) => onChange(event.target.value as EntityMode)}
    >
      {MODES.map((mode) => (
        <option key={mode.value} value={mode.value} disabled={modeRank(mode.value) < least}>
          {mode.label}
        </option>
      ))}
    </Select>
  );
}
