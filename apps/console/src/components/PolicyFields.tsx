import { Badge, Button, Input, Select } from "@llmp/ui";
import type { ReactNode } from "react";
import { useState } from "react";
import { MODES, entityLabel, entitySource, modeRank } from "../lib/entities";
import type { EntityMode, EntityPolicy, RedactionPolicy } from "../lib/types";
import styles from "./PolicyFields.module.css";

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
    <div className={styles.fields}>
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
        <p className={styles.muted}>No entity types to list. The default above still applies.</p>
      ) : (
        <>
          <div className={styles.entities}>
            {known.map((entity) => {
              const name = entityLabel(entity);
              const source = entitySource(entity, patternNames);
              // Nothing here knows this label, so `entityLabel` handed it back
              // unchanged. Printing it twice would read as a rendering fault.
              const named = name !== entity;
              return (
                <div key={entity} className={styles.entity}>
                  <ModeSelect
                    label={
                      <span className={styles.entityLabel}>
                        {named && name}
                        <code className={styles.code}>{entity}</code>
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
          <p className={styles.muted}>
            Turning a type off changes what is replaced, not how long detection takes.
          </p>
        </>
      )}

      <fieldset className={styles.patterns}>
        <legend className={styles.legend}>Custom patterns</legend>
        {policy.patterns.map((pattern, index) => (
          // Keyed by position: a name is edited character by character, so
          // keying on it would remount the field being typed into and lose
          // focus on every keystroke.
          <div key={index} className={styles.patternRow}>
            <Input
              label={`Pattern ${index + 1} name`}
              value={pattern.name}
              placeholder="TICKET_ID"
              onChange={(event) => setPattern(index, { name: event.target.value })}
            />
            <Input
              label={`Pattern ${index + 1} regex`}
              className={styles.code}
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
        <p className={styles.muted}>
          The name becomes the entity label. RE2 syntax: no backreferences, no lookaround.
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
 * A mode picker that cannot offer a mode the API would refuse.
 *
 * Weaker options are disabled rather than dropped: the floor is an
 * administrator's decision, and a select that silently omits three of five
 * choices reads as a bug rather than as a constraint.
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
