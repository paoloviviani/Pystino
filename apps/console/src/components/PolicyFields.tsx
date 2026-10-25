import { Badge, Button, Card, Dialog, Input, Select } from "@llmp/ui";
import type { ReactNode } from "react";
import { useState } from "react";
import { MODES, entityLabel, entitySource, modeRank } from "../lib/entities";
import { m } from "../paraglide/messages.js";
import { REDACTION_PATTERN_TEMPLATES } from "../lib/redactionTemplates";
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
  const known = [...new Set([...entityTypes, ...Object.keys(policy.entities)])];
  // The rows are the types this policy says something about. The rest follow
  // the default, which is what an absent entry means to the gateway too, so
  // "remove the row" and "reset to the default" are the same act.
  const named = Object.keys(policy.entities);
  const unnamed = known
    .filter((entity) => !(entity in policy.entities))
    .sort((a, b) => entityLabel(a).localeCompare(entityLabel(b)));
  const patternNames = policy.patterns.map((pattern) => pattern.name);
  const [templatesOpen, setTemplatesOpen] = useState(false);

  const floorFor = (entity: string): EntityMode | null =>
    floor === null ? null : (floor.entities[entity]?.mode ?? floor.default_mode);

  const setEntity = (entity: string, patch: Partial<EntityPolicy>) =>
    onChange({
      ...policy,
      entities: {
        ...policy.entities,
        [entity]: {
          mode: patch.mode ?? policy.entities[entity]?.mode ?? policy.default_mode,
          threshold:
            patch.threshold !== undefined
              ? patch.threshold
              : (policy.entities[entity]?.threshold ?? null),
        },
      },
    });

  const removeEntity = (entity: string) => {
    const { [entity]: _gone, ...entities } = policy.entities;
    onChange({ ...policy, entities });
  };

  /**
   * A new row starts stricter than the default, because a row that repeated the
   * default would change nothing and the reason to add one is to treat the type
   * differently. Redact, or Block when the default is already that strong.
   */
  const addEntity = (entity: string) =>
    setEntity(entity, {
      mode: modeRank(policy.default_mode) < modeRank("redact") ? "redact" : "block",
    });

  const setPattern = (index: number, patch: Partial<RedactionPolicy["patterns"][number]>) =>
    onChange({
      ...policy,
      patterns: policy.patterns.map((pattern, at) =>
        at === index ? { ...pattern, ...patch } : pattern,
      ),
    });

  return (
    <div className="flex flex-col gap-5">
      <Card title={m["policy_fields.hide_card"]()} description={m["policy_fields.hide_hint"]()}>
        <div className={FORM}>
          {/* Applies to a recogniser the engine gains in a later release, which
              is why the safer direction is to protect by default rather than to
              let a new type through unnoticed. */}
          <div className="flex flex-col gap-1">
            <ModeSegments
              name="default-mode"
              legend={m["policy_fields.default_mode"]()}
              visibleLegend
              value={policy.default_mode}
              floor={floor?.default_mode ?? null}
              onChange={(mode) => onChange({ ...policy, default_mode: mode })}
            />
            <p className="m-0 text-small text-ink-faint">
              {m["policy_fields.default_mode_hint"]()}
            </p>
          </div>

          {known.length === 0 ? (
            <p className="m-0 text-body text-ink-muted">{m["policy_fields.no_entity_types"]()}</p>
          ) : (
            <section className="flex flex-col gap-3 border-t border-line-quiet pt-4">
              <h3 className="m-0 text-body font-semibold">{m["policy_fields.types_heading"]()}</h3>

              {named.length === 0 ? (
                <p className="m-0 text-body text-ink-muted">{m["policy_fields.types_empty"]()}</p>
              ) : (
                <ul className="m-0 flex list-none flex-col p-0">
                  {/* Column titles only where the columns are. On a phone each
                      row stacks, and a header over a stack names nothing. */}
                  <li
                    aria-hidden="true"
                    className={`${TYPE_ROW} border-b border-line pb-1 text-small font-medium text-ink-faint max-[40rem]:hidden`}
                  >
                    <span>{m["policy_fields.col_type"]()}</span>
                    <span>{m["policy_fields.col_action"]()}</span>
                    <span>{m["policy_fields.col_confidence"]()}</span>
                    <span />
                  </li>
                  {named.map((entity) => {
                    const name = entityLabel(entity);
                    const source = entitySource(entity, patternNames);
                    // Nothing here knows this label, so `entityLabel` handed it
                    // back unchanged. Printing it twice would read as a
                    // rendering fault.
                    const known_ = name !== entity;
                    return (
                      <li
                        key={entity}
                        className={`${TYPE_ROW} border-b border-line-quiet py-2 max-[40rem]:py-3`}
                      >
                        <div className="flex min-w-0 flex-col gap-0.5" title={entity}>
                          <span className="text-body font-medium">{known_ ? name : entity}</span>
                          <span className="flex flex-wrap items-center gap-1.5 text-small text-ink-faint">
                            {known_ && <code className={CODE}>{entity}</code>}
                            {source && (
                              <Badge>
                                {source === "pattern"
                                  ? m["policy_fields.source.pattern"]()
                                  : m["policy_fields.source.model"]()}
                              </Badge>
                            )}
                          </span>
                        </div>
                        <ModeSegments
                          name={`mode-${entity}`}
                          legend={m["policy_fields.mode_group"]({
                            name: known_ ? `${name} (${entity})` : entity,
                          })}
                          value={policy.entities[entity]?.mode ?? policy.default_mode}
                          floor={floorFor(entity)}
                          onChange={(mode) => setEntity(entity, { mode })}
                        />
                        <Input
                          label={m["policy_fields.confidence_for"]({ name })}
                          hideLabel
                          type="number"
                          min="0"
                          max="1"
                          step="0.05"
                          placeholder={
                            scoreThreshold === null
                              ? m["policy_fields.confidence_default"]()
                              : String(scoreThreshold)
                          }
                          title={
                            scoreThreshold === null
                              ? undefined
                              : m["policy_fields.confidence_default_value"]({
                                  value: scoreThreshold,
                                })
                          }
                          value={policy.entities[entity]?.threshold ?? ""}
                          onChange={(event) =>
                            setEntity(entity, {
                              threshold:
                                event.target.value === "" ? null : Number(event.target.value),
                            })
                          }
                        />
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={m["policy_fields.remove_type"]({ name })}
                          title={m["policy_fields.remove_type"]({ name })}
                          onClick={() => removeEntity(entity)}
                        >
                          {m["policy_fields.remove_type_short"]()}
                        </Button>
                      </li>
                    );
                  })}
                </ul>
              )}

              <div className="max-w-sm">
                <Select
                  label={m["policy_fields.add_type"]()}
                  value=""
                  disabled={unnamed.length === 0}
                  onChange={(event) => {
                    if (event.target.value !== "") addEntity(event.target.value);
                  }}
                >
                  <option value="">
                    {unnamed.length === 0
                      ? m["policy_fields.add_type_none"]()
                      : m["policy_fields.add_type_choose"]()}
                  </option>
                  {unnamed.map((entity) => (
                    <option key={entity} value={entity}>
                      {entityLabel(entity) === entity
                        ? entity
                        : `${entityLabel(entity)} (${entity})`}
                    </option>
                  ))}
                </Select>
              </div>

              <details className="text-body text-ink-muted">
                <summary className="cursor-pointer">{m["policy_fields.actions_key"]()}</summary>
                <ModeKey />
              </details>
              {/* Measured, and the opposite of what a list of switches implies:
                  the model pass runs over the whole prompt whichever types are
                  asked for. See docs/performance.md. */}
              <p className="m-0 text-small text-ink-faint">{m["policy_fields.detection_note"]()}</p>
            </section>
          )}
        </div>
      </Card>

      {/* No patterns is a card that is only its heading and its two buttons: the
          section is "collapsed" until there is something in it, and the
          buttons that fill it never move. */}
      <Card
        title={m["policy_fields.patterns_card"]()}
        description={m["policy_fields.patterns_hint"]()}
        actions={
          <>
            <Button
              onClick={() =>
                onChange({
                  ...policy,
                  // Redact rather than the gentlest mode: a pattern somebody
                  // wrote by hand is a value they went out of their way to name.
                  patterns: [...policy.patterns, { name: "", regex: "", mode: "redact" }],
                })
              }
            >
              {m["policy_fields.pattern_add"]()}
            </Button>
            <Button variant="secondary" onClick={() => setTemplatesOpen(true)}>
              {m["policy_fields.pattern_from_template"]()}
            </Button>
          </>
        }
      >
        {policy.patterns.length > 0 && (
          // A fieldset because the legend is what names the group for a screen
          // reader: three inputs called Name, Regex and Mode mean nothing
          // without "Custom patterns" attached to them. `min-w-0` because a
          // fieldset defaults to min-content, which breaks the grid.
          <fieldset className="m-0 grid min-w-0 gap-3 border-0 p-0">
            <legend className="sr-only">{m["policy_fields.patterns_legend"]()}</legend>
            {policy.patterns.map((pattern, index) => (
              // Keyed by position: a name is edited character by character, so
              // keying on it would remount the field being typed into and lose
              // focus on every keystroke.
              <div
                key={index}
                className="grid grid-cols-[minmax(8rem,1fr)_minmax(10rem,2fr)_minmax(9rem,1fr)_auto] items-end gap-3 border-b border-line-quiet pb-3 max-[56rem]:grid-cols-1"
              >
                <Input
                  label={m["policy_fields.pattern_name_label"]({
                    number: index + 1,
                  })}
                  value={pattern.name}
                  placeholder={m["policy_fields.pattern_name_placeholder"]()}
                  onChange={(event) => setPattern(index, { name: event.target.value })}
                />
                <Input
                  label={m["policy_fields.pattern_regex_label"]({
                    number: index + 1,
                  })}
                  className={CODE}
                  value={pattern.regex}
                  placeholder={m["policy_fields.pattern_regex_placeholder"]()}
                  onChange={(event) => setPattern(index, { regex: event.target.value })}
                />
                <ModeSelect
                  label={m["policy_fields.pattern_mode_label"]({
                    number: index + 1,
                  })}
                  value={pattern.mode}
                  floor={null}
                  onChange={(mode) => setPattern(index, { mode })}
                />
                <div>
                  <Button
                    variant="ghost"
                    onClick={() =>
                      onChange({
                        ...policy,
                        patterns: policy.patterns.filter((_, at) => at !== index),
                      })
                    }
                  >
                    {m["policy_fields.pattern_remove"]()}
                  </Button>
                </div>
              </div>
            ))}
            <p className="m-0 text-small text-ink-faint">{m["policy_fields.pattern_note"]()}</p>
          </fieldset>
        )}
      </Card>
      {/* Measured, not assumed: eight credential shapes were put through this
          deployment's detector and none was found. Two produced *wrong* hits —
          AWS_SECRET_ACCESS_KEY as a LOCATION, the word "token" as a PERSON.

          Those shapes are therefore offered as templates rather than seeded
          into every new rule. An empty pattern list is a deliberate choice; a
          seeded list would make it impossible to tell that choice from an
          omission. */}
      <PatternTemplatesDialog
        open={templatesOpen}
        policy={policy}
        onClose={() => setTemplatesOpen(false)}
        onChange={onChange}
      />

      {/* For values a detector is right about the shape of and wrong about the
          meaning of: a corporate domain is a URL, and it identifies nobody. */}
      {allowList && (
        <Card
          title={m["policy_fields.allowlist_card"]()}
          description={m["policy_fields.allowlist_card_hint"]()}
        >
          <AllowList
            values={policy.allow_list}
            onChange={(allow_list) => onChange({ ...policy, allow_list })}
          />
        </Card>
      )}
    </div>
  );
}

/**
 * One row of the types table: the kind of data, its action, its confidence,
 * and the reset button. On a phone the four stack into two lines rather than
 * shrinking, because five segments and a number field do not fit in 360px.
 */
const TYPE_ROW =
  "grid grid-cols-[minmax(8rem,1fr)_max-content_5.5rem_auto] items-center gap-x-3 gap-y-2 " +
  "max-[40rem]:grid-cols-[1fr_auto] max-[40rem]:[&>*:nth-child(2)]:col-span-2 " +
  "max-[40rem]:[&>*:nth-child(2)]:row-start-2 max-[40rem]:[&>*:nth-child(3)]:row-start-3 " +
  "max-[40rem]:[&>*:nth-child(3)]:col-span-2";

/**
 * The template catalogue, offered rather than seeded.
 *
 * A dialog lists the shapes the detector cannot see, grouped into credentials
 * and identifiers. Each credential template blocks because sending a key is
 * itself the incident; identifier templates redact because the surrounding
 * request is still useful without the value. Added rows remain editable.
 */
function PatternTemplatesDialog({
  open,
  policy,
  onClose,
  onChange,
}: {
  open: boolean;
  policy: RedactionPolicy;
  onClose: () => void;
  onChange: (policy: RedactionPolicy) => void;
}) {
  return (
    <Dialog
      open={open}
      title={m["policy_fields.pattern_from_template"]()}
      onClose={onClose}
      footer={
        <Button variant="secondary" onClick={onClose}>
          {m["policy_fields.templates.done"]()}
        </Button>
      }
    >
      <div className="flex flex-col gap-3">
        <p className="m-0 text-body text-ink-muted">{m["policy_fields.templates_note"]()}</p>
        {REDACTION_PATTERN_TEMPLATES.map((template) => {
          const added = policy.patterns.some(
            (pattern) => pattern.name === template.name && pattern.regex === template.regex,
          );
          return (
            <div
              key={template.name}
              className="flex items-start justify-between gap-4 border-b border-line-quiet pb-3"
            >
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <strong>{template.name}</strong>
                  <Badge>
                    {template.kind === "credential"
                      ? m["policy_fields.templates.kind.credential"]()
                      : m["policy_fields.templates.kind.identifier"]()}
                  </Badge>
                  <Badge>
                    {template.mode === "block"
                      ? m["policy_fields.templates.mode.block"]()
                      : m["policy_fields.templates.mode.redact"]()}
                  </Badge>
                </div>
                <p className={`m-0 text-body ${MUTED}`}>{template.description()}</p>
                <code className={CODE}>{template.regex}</code>
              </div>
              <Button
                variant="secondary"
                aria-label={m["policy_fields.templates.add_aria"]({
                  name: template.name,
                })}
                disabled={added}
                onClick={() =>
                  onChange({
                    ...policy,
                    patterns: [
                      ...policy.patterns,
                      {
                        name: template.name,
                        regex: template.regex,
                        mode: template.mode,
                      },
                    ],
                  })
                }
              >
                {added ? m["policy_fields.templates.added"]() : m["policy_fields.templates.add"]()}
              </Button>
            </div>
          );
        })}
      </div>
    </Dialog>
  );
}

/**
 * A new policy starts empty.
 *
 * Empty is now a deliberate starting point rather than an omission: the
 * credential shapes live in the template dialogue above, where adding one is
 * explicit and remains visible as a row that can be edited or removed.
 */
export function seededPolicy(): RedactionPolicy {
  return {
    default_mode: "off",
    entities: {},
    patterns: [],
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
      label={m["policy_fields.allowlist.label"]()}
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
      placeholder={m["policy_fields.allowlist.placeholder"]()}
      hint={m["policy_fields.allowlist.hint"]()}
    />
  );
}

/**
 * The five modes as one segmented control, in the weakest-to-strongest order
 * they are ranked in.
 *
 * Radios underneath, so the keyboard and a screen reader get what they would
 * from any radio group: arrow keys move the choice, and the fieldset's legend
 * names the group. The segments are the visible half. A select was here first
 * and, repeated down a table, it was the densest thing on the screen — every
 * row a closed control that had to be opened to be read. Segments put the
 * current answer and the alternatives in one line, so a row is read rather than
 * operated.
 *
 * The labels are short because the meaning is given once, in `ModeKey`; each
 * segment still carries the long form as a `title`, so the short word is never
 * the only thing available.
 *
 * Weaker options are disabled rather than dropped: the floor is an
 * administrator's decision, and a control that silently omits three of five
 * choices reads as a fault rather than as a constraint.
 */
const SEGMENT_ON: Record<EntityMode, string> = {
  off: "peer-checked:bg-ink/10 peer-checked:font-semibold peer-checked:text-ink",
  anonymise_restore:
    "peer-checked:bg-accent-solid peer-checked:font-semibold peer-checked:text-white",
  anonymise: "peer-checked:bg-accent-solid peer-checked:font-semibold peer-checked:text-white",
  redact: "peer-checked:bg-accent-solid peer-checked:font-semibold peer-checked:text-white",
  block: "peer-checked:bg-danger peer-checked:font-semibold peer-checked:text-ink-inverse",
};

function ModeSegments({
  name,
  legend,
  visibleLegend = false,
  value,
  floor,
  onChange,
}: {
  name: string;
  legend: ReactNode;
  /** Show the legend as a label above the control (the default action). */
  visibleLegend?: boolean;
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
      <legend className={visibleLegend ? `mb-1 p-0 ${FIELD_LABEL}` : "sr-only"}>{legend}</legend>
      <div className="grid grid-flow-col auto-cols-auto overflow-hidden rounded-md border border-line bg-surface min-[40rem]:w-max min-[40rem]:grid-flow-col">
        {MODES.map((mode) => {
          const disabled = modeRank(mode.value) < least;
          return (
            // Disabled because a stricter policy above sets a floor. Dimmed
            // rather than hidden: the reader is meant to see that the option
            // exists and is not theirs to choose.
            <label
              key={mode.value}
              className="relative cursor-pointer border-l border-line first:border-l-0 has-disabled:cursor-not-allowed"
              title={m["redaction_mode.title"]({
                label: mode.label(),
                hint: mode.hint(),
              })}
            >
              <input
                type="radio"
                name={name}
                value={mode.value}
                checked={value === mode.value}
                disabled={disabled}
                onChange={() => onChange(mode.value)}
                className="peer sr-only"
              />
              <span
                className={`block px-2 py-1.5 text-center text-dense whitespace-nowrap text-ink-muted transition-colors hover:bg-sunken peer-focus-visible:shadow-focus peer-disabled:text-ink-faint peer-disabled:line-through peer-disabled:hover:bg-transparent max-[40rem]:px-1.5 max-[40rem]:text-small ${SEGMENT_ON[mode.value]}`}
              >
                {mode.short()}
              </span>
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
    <dl className="m-0 mt-2 grid gap-x-4 gap-y-1 bg-sunken p-3 text-body [grid-template-columns:repeat(auto-fit,minmax(14rem,1fr))]">
      {MODES.map((mode) => (
        <div key={mode.value} className="flex items-baseline gap-2">
          <dt className="font-medium whitespace-nowrap">{mode.short()}</dt>
          <dd className={`m-0 ${MUTED}`}>{mode.hint()}</dd>
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
          {mode.label()}
        </option>
      ))}
    </Select>
  );
}
