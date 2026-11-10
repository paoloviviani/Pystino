import { Badge, Button, Card, Dialog, Input, Notice, Select } from "@llmp/ui";
import type { ReactNode } from "react";
import { createContext, useContext, useEffect, useRef, useState } from "react";
import { MODES, entityLabel, modeRank } from "../lib/entities";
import { m } from "../paraglide/messages.js";
import { REDACTION_PATTERN_TEMPLATES } from "../lib/redactionTemplates";
import type {
  CustomEntity,
  EntityMode,
  EntityPolicy,
  KeywordList,
  PlaceholderStyle,
  PrivacyEngine,
  PrivacyEngines,
  RedactionPolicy,
} from "../lib/types";
import { CODE, FIELD_LABEL, FORM, MUTED } from "../lib/layout";
import { Tip } from "./Tip";

export interface PolicyFieldsProps {
  policy: RedactionPolicy;
  onChange: (policy: RedactionPolicy) => void;
  /**
   * What the redaction service reports: the pattern formats, and the NER engines
   * with the types each exposes. Undefined while it loads; the editor then shows
   * only what the policy already names.
   */
  engines?: PrivacyEngines | null;
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
  /** The gateway will not use this policy's allowlist, so the card says so. */
  allowListIgnored?: boolean;
  /** A stored policy: what to look for, with no mode (ADR 0114). */
  detectionOnly?: boolean;
}

/** The comma-separated words of a keyword list, as the list they stand for. */
export function parseWords(text: string): string[] {
  return text
    .split(",")
    .map((word) => word.trim())
    .filter(Boolean);
}

/**
 * The engine that reads this policy: the one it names, else the deployment's
 * default. Undefined when the service did not say.
 */
export function chosenEngine(
  policy: RedactionPolicy,
  engines: PrivacyEngines | null | undefined,
): PrivacyEngine | undefined {
  if (!engines) return undefined;
  return policy.engine
    ? engines.engines.find((engine) => engine.name === policy.engine)
    : engines.engines.find((engine) => engine.default);
}

/**
 * The capabilities as a policy may use them: a detector switched off on the
 * Detectors tab offers nothing (no formats, no engine, no entities), whatever the
 * service reports.
 */
export function usableEngines(engines: PrivacyEngines | null | undefined): PrivacyEngines | null {
  if (!engines) return null;
  return {
    ...engines,
    pattern_entities: engines.patterns_enabled === false ? [] : engines.pattern_entities,
    engines: engines.engines.map((engine) =>
      engine.enabled === false ? { ...engine, available: false } : engine,
    ),
  };
}

/**
 * Whether the redaction service has any NER engine that can find entities.
 * Undefined while the service has not answered (or cannot be reached): nothing is
 * hidden on a guess.
 */
export function hasNer(engines: PrivacyEngines | null | undefined): boolean | undefined {
  if (!engines || !engines.reachable) return undefined;
  return engines.engines.some(
    (engine) => engine.available && engine.enabled !== false && engine.entities.length > 0,
  );
}

/**
 * Formats the service no longer lists. The spaCy recogniser's label map once made
 * AGE, ID and EMAIL look like pattern formats; a policy that saved one keeps it.
 * A name an engine does find moves to the entities, any other stays in the
 * formats but is turned off: never dropped, and never left to act on nothing.
 */
export function migrateFormats(
  policy: RedactionPolicy,
  engines: PrivacyEngines | null | undefined,
): { policy: RedactionPolicy; moved: string[]; retired: string[] } {
  const none = { policy, moved: [] as string[], retired: [] as string[] };
  if (!engines || !engines.reachable || engines.pattern_entities.length === 0) return none;
  const known = new Set(engines.pattern_entities.map((name) => name.toUpperCase()));
  const engine = chosenEngine(policy, engines);
  const modelTypes = new Set((engine?.entities ?? []).map((name) => name.toUpperCase()));
  const moved: string[] = [];
  const retired: string[] = [];
  const formats: RedactionPolicy["formats"] = {};
  const entities = { ...policy.entities };
  let changed = false;
  for (const [name, entry] of Object.entries(policy.formats)) {
    if (known.has(name.toUpperCase())) {
      formats[name] = entry;
    } else if (modelTypes.has(name.toUpperCase()) && !(name in entities)) {
      entities[name] = entry;
      moved.push(name);
      changed = true;
    } else {
      formats[name] = entry.mode === "off" ? entry : { ...entry, mode: "off" };
      retired.push(name);
      changed = changed || entry.mode !== "off";
    }
  }
  if (!changed) return { policy, moved, retired };
  return { policy: { ...policy, formats, entities }, moved, retired };
}

/** Where the docs say how to install an NER engine. */
export const NER_DOCS_URL = "https://paoloviviani.github.io/Pystino/redaction/#deploying-less-of-it";

/**
 * The policy document as a form, in the two groups that never mix.
 *
 * Patterns (fast, deterministic): the service's built-in formats, regular
 * expressions an administrator wrote, and keyword lists. Entities (NER,
 * probabilistic): the types the policy's engine reads, and, for an engine that
 * takes labels in plain words, custom types.
 *
 * One component for the deployment's policies and a scoped rule because a policy
 * means the same thing in both and two editors would drift apart on the detail
 * that matters most: which mode is stronger than which.
 *
 * The lists come from the service rather than from here. Types belong to
 * whatever engine is installed (ADR 0026), so hard-coding them would mean a
 * console that silently omits a recogniser the engine gained last week.
 */
export function PolicyFields({
  policy,
  onChange,
  engines: reported = null,
  scoreThreshold = null,
  allowList = true,
  floor = null,
  allowListIgnored = false,
  detectionOnly = false,
}: PolicyFieldsProps) {
  const engines = usableEngines(reported);
  const [templatesOpen, setTemplatesOpen] = useState(false);
  const engine = chosenEngine(policy, engines);
  // Formats the service stopped offering are moved or turned off once, in the
  // open (a notice says so), rather than dropped or left acting on nothing.
  const migration = detectionOnly
    ? { policy, moved: [] as string[], retired: [] as string[] }
    : migrateFormats(policy, engines);
  const movedRef = useRef<string[]>([]);
  const [moved, setMoved] = useState<string[]>([]);
  useEffect(() => {
    if (migration.policy !== policy) {
      movedRef.current = [...movedRef.current, ...migration.moved];
      setMoved(movedRef.current);
      onChange(migration.policy);
    }
    // `migration` is derived from the two inputs listed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [policy, engines]);
  const noNer = hasNer(engines) === false;
  const entityItems = Object.keys(policy.entities).length + policy.custom_entities.length;
  const floorFor = (group: "formats" | "entities", name: string): EntityMode | null =>
    floor === null ? null : (floor[group][name]?.mode ?? floor.default_mode);

  const setPattern = (index: number, patch: Partial<RedactionPolicy["patterns"][number]>) =>
    onChange({
      ...policy,
      patterns: policy.patterns.map((pattern, at) =>
        at === index ? { ...pattern, ...patch } : pattern,
      ),
    });

  return (
    <DetectionOnly.Provider value={detectionOnly}>
    <div className="flex flex-col gap-5">
      {!detectionOnly && (
      <Card title={m["policy_fields.default_card"]()}>
        <div className={FORM}>
          {/* Applies to a recogniser the engine gains in a later release, which
              is why the safer direction is to protect by default rather than to
              let a new type through unnoticed. */}
          <div className="flex items-end gap-2">
            <ModeSegments
              name="default-mode"
              legend={m["policy_fields.default_mode"]()}
              visibleLegend
              value={policy.default_mode}
              floor={floor?.default_mode ?? null}
              onChange={(mode) => onChange({ ...policy, default_mode: mode })}
            />
            <Tip text={m["policy_fields.default_mode_hint"]()} />
          </div>
          <details className="text-body text-ink-muted">
            <summary className="cursor-pointer">{m["policy_fields.actions_key"]()}</summary>
            <ModeKey />
          </details>
        </div>
      </Card>
      )}

      {/* Group one: found by a pattern, so exact and fast, and the same in any
          language. */}
      <Card
        title={
          <span className="flex items-center gap-2">
            {m["policy_fields.patterns_group"]()}
            <Tip text={m["policy_fields.patterns_group_tip"]()} />
          </span>
        }
      >
        <div className="flex flex-col gap-5">
          {migration.retired.length > 0 && (
            <Notice tone="warn">
              {m["policy_fields.formats_retired"]({ names: migration.retired.join(", ") })}
            </Notice>
          )}
          {moved.length > 0 && (
            <Notice>{m["policy_fields.formats_moved"]({ names: moved.join(", ") })}</Notice>
          )}
          <TypeTable
            heading={m["policy_fields.formats_heading"]()}
            tip={m["policy_fields.formats_tip"]()}
            group="formats"
            known={engines?.pattern_entities ?? []}
            flagUnknown
            entries={policy.formats}
            defaultMode={policy.default_mode}
            floorFor={(name) => floorFor("formats", name)}
            scoreThreshold={null}
            onChange={(formats) => onChange({ ...policy, formats })}
          />

          <section className="flex flex-col gap-3 border-t border-line-quiet pt-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="m-0 flex items-center gap-2 text-body font-semibold">
                {m["policy_fields.patterns_card"]()}
                <Tip text={m["policy_fields.patterns_hint"]()} />
              </h3>
              <div className="flex flex-wrap gap-2">
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
              </div>
            </div>
            {policy.patterns.length > 0 && (
              // A fieldset because the legend is what names the group for a screen
              // reader. `min-w-0` because a fieldset defaults to min-content,
              // which breaks the grid.
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
                      label={m["policy_fields.pattern_name_label"]({ number: index + 1 })}
                      value={pattern.name}
                      placeholder={m["policy_fields.pattern_name_placeholder"]()}
                      onChange={(event) => setPattern(index, { name: event.target.value })}
                    />
                    <Input
                      label={m["policy_fields.pattern_regex_label"]({ number: index + 1 })}
                      className={CODE}
                      value={pattern.regex}
                      placeholder={m["policy_fields.pattern_regex_placeholder"]()}
                      onChange={(event) => setPattern(index, { regex: event.target.value })}
                    />
                    <ModeSelect
                      label={m["policy_fields.pattern_mode_label"]({ number: index + 1 })}
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
                    <div className="col-span-full max-w-md">
                      <PlaceholderSelect
                        label={m["policy_fields.pattern_placeholder_label"]({
                          number: index + 1,
                        })}
                        mode={pattern.mode}
                        value={pattern.placeholder ?? "tagged"}
                        onChange={(placeholder) => setPattern(index, { placeholder })}
                      />
                    </div>
                  </div>
                ))}
                <p className="m-0 text-small text-ink-faint">{m["policy_fields.pattern_note"]()}</p>
              </fieldset>
            )}
          </section>

          <section className="flex flex-col gap-3 border-t border-line-quiet pt-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="m-0 flex items-center gap-2 text-body font-semibold">
                {m["policy_fields.keywords_heading"]()}
                <Tip text={m["policy_fields.keywords_tip"]()} />
              </h3>
              <Button
                onClick={() =>
                  onChange({
                    ...policy,
                    keywords: [
                      ...policy.keywords,
                      { name: "", words: [], case_sensitive: false, mode: "redact" },
                    ],
                  })
                }
              >
                {m["policy_fields.keyword_add"]()}
              </Button>
            </div>
            {policy.keywords.length > 0 && (
              <fieldset className="m-0 grid min-w-0 gap-3 border-0 p-0">
                <legend className="sr-only">{m["policy_fields.keywords_legend"]()}</legend>
                {policy.keywords.map((keyword, index) => (
                  <KeywordRow
                    key={index}
                    number={index + 1}
                    keyword={keyword}
                    onChange={(patch) =>
                      onChange({
                        ...policy,
                        keywords: policy.keywords.map((k, at) =>
                          at === index ? { ...k, ...patch } : k,
                        ),
                      })
                    }
                    onRemove={() =>
                      onChange({
                        ...policy,
                        keywords: policy.keywords.filter((_, at) => at !== index),
                      })
                    }
                  />
                ))}
              </fieldset>
            )}
          </section>
        </div>
      </Card>
      <PatternTemplatesDialog
        open={templatesOpen}
        policy={policy}
        onClose={() => setTemplatesOpen(false)}
        onChange={onChange}
      />

      {/* Group two: read by a model, so probabilistic, and it depends on the
          language. Which model is the policy's own choice. */}
      <Card
        title={
          <span className="flex items-center gap-2">
            {m["policy_fields.entities_group"]()}
            <Tip text={m["policy_fields.entities_group_tip"]()} />
          </span>
        }
      >
        {noNer ? (
          <NoNerNotice
            items={entityItems}
            off={engines?.engines.some((e) => e.entities.length > 0 && e.enabled === false) ?? false}
          />
        ) : (
          <div className="flex flex-col gap-5">
            <EnginePicker
              value={policy.engine}
              engines={engines}
              onChange={(next) => onChange({ ...policy, engine: next })}
            />
            <TypeTable
              heading={m["policy_fields.types_heading"]()}
              tip={m["policy_fields.detection_note"]()}
              group="entities"
              known={engine?.entities ?? []}
              entries={policy.entities}
              defaultMode={policy.default_mode}
              floorFor={(name) => floorFor("entities", name)}
              scoreThreshold={scoreThreshold}
              onChange={(entities) => onChange({ ...policy, entities })}
            />
            {((engine?.available && engine?.custom_labels) || policy.custom_entities.length > 0) && (
              <CustomEntities
                policy={policy}
                usable={engine?.available === true && engine?.custom_labels === true}
                onChange={(custom_entities) => onChange({ ...policy, custom_entities })}
              />
            )}
          </div>
        )}
      </Card>

      {/* For values a detector is right about the shape of and wrong about the
          meaning of: a corporate domain is a URL, and it identifies nobody. */}
      {allowList && (
        <Card
          title={m["policy_fields.allowlist_card"]()}
          description={
            allowListIgnored
              ? m["policy_fields.allowlist_ignored"]()
              : m["policy_fields.allowlist_card_hint"]()
          }
        >
          <AllowList
            values={policy.allow_list}
            onChange={(allow_list) => onChange({ ...policy, allow_list })}
          />
        </Card>
      )}
    </div>
    </DetectionOnly.Provider>
  );
}

/**
 * The one line that replaces the entities section when the redaction service has
 * no NER engine: what is missing and where to read how to install it. Items the
 * policy already lists are kept (and counted here), never dropped.
 */
export function NoNerNotice({ items, off = false }: { items: number; off?: boolean }) {
  return (
    <div className="flex flex-col gap-2" data-testid="no-ner">
      {off ? (
        // Installed but switched off: say so, and point at the switch, not at the docs.
        <p className={`m-0 text-body ${MUTED}`}>
          {m["policy_fields.no_ner_off"]()}{" "}
          <a href="/console/admin/privacy/detectors" className="underline">
            {m["policy_fields.no_ner_off_link"]()}
          </a>
        </p>
      ) : (
        <>
          <p className={`m-0 text-body ${MUTED}`}>
            {m["policy_fields.no_ner"]()}{" "}
            <a href={NER_DOCS_URL} target="_blank" rel="noreferrer" className="underline">
              {m["policy_fields.no_ner_docs"]()}
            </a>
          </p>
          <p className={`m-0 text-small ${MUTED}`}>{m["policy_fields.no_ner_how"]()}</p>
        </>
      )}
      {items > 0 && (
        <Notice tone="warn">{m["policy_fields.no_ner_kept"]({ count: items })}</Notice>
      )}
    </div>
  );
}

/**
 * The engine that reads this policy.
 *
 * Only an engine the service reports as available can be chosen (the server
 * refuses the rest). A service that does not answer leaves only "Deployment
 * default": nothing else can be checked, and a policy that names an engine the
 * deployment cannot run would find nothing.
 */
function EnginePicker({
  value,
  engines,
  onChange,
}: {
  value: RedactionPolicy["engine"];
  engines: PrivacyEngines | null;
  onChange: (engine: RedactionPolicy["engine"]) => void;
}) {
  const reachable = engines?.reachable ?? false;
  // Only an engine the service can run is offered; one a policy already names
  // stays on screen below, flagged, so saving does not silently change it.
  const list = (engines?.engines ?? []).filter((engine) => engine.available);
  const current = value ? list.find((engine) => engine.name === value) : undefined;
  const effective = current ?? (value ? undefined : list.find((engine) => engine.default));
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-[14rem]">
          <Select
            label={m["policy_fields.engine_label"]()}
            value={value ?? ""}
            onChange={(event) =>
              onChange(event.target.value === "" ? null : (event.target.value as "spacy" | "gliner"))
            }
          >
            <option value="">
              {m["policy_fields.engine_default"]()}
              {reachable && list.find((engine) => engine.default)
                ? ` (${list.find((engine) => engine.default)?.name})`
                : ""}
            </option>
            {list.map((engine) => (
              <option key={engine.name} value={engine.name} disabled={!reachable}>
                {engine.name}
                {engine.default ? ` (${m["policy_fields.engine_is_default"]()})` : ""}
              </option>
            ))}
            {/* A policy that names an engine the service no longer reports keeps
                it on screen, so saving does not silently change it. */}
            {value && !list.some((engine) => engine.name === value) && (
              <option value={value} disabled>
                {value} ({m["policy_fields.engine_unavailable"]()})
              </option>
            )}
          </Select>
        </div>
        <Tip text={m["policy_fields.engine_tip"]()} />
      </div>
      {engines && !engines.reachable && (
        <Notice tone="warn" title={m["policy_fields.engine_unreachable"]()}>
          {/* i18n: server text, the redaction service's own reason. */}
          {engines.detail}
        </Notice>
      )}
      {effective && (
        <p className={`m-0 text-small ${MUTED}`}>
          {effective.ms_per_token !== null &&
            `${m["policy_fields.engine_speed"]({ ms: String(effective.ms_per_token) })} · `}
          {m["policy_fields.engine_languages"]({ languages: effective.languages.join(", ") })}
        </p>
      )}
    </div>
  );
}

/**
 * The rows for one dictionary of types (formats or entities): the types this
 * policy says something about, an add control for the rest.
 *
 * The rows are the types the policy names. The rest follow the default, which is
 * what an absent entry means to the gateway too, so "remove the row" and "reset
 * to the default" are the same act. Whatever the policy already names is shown
 * even when the service does not report it: dropping it from the screen would
 * silently delete it on the next save.
 */
function TypeTable({
  heading,
  tip,
  group,
  known,
  flagUnknown = false,
  entries,
  defaultMode,
  floorFor,
  scoreThreshold,
  onChange,
}: {
  heading: string;
  tip: string;
  group: "formats" | "entities";
  known: string[];
  /** Mark (and lock) a row whose type the service does not list. */
  flagUnknown?: boolean;
  entries: Record<string, EntityPolicy>;
  defaultMode: EntityMode;
  floorFor: (name: string) => EntityMode | null;
  /** Null for a group that has no confidence (formats). */
  scoreThreshold: number | null;
  onChange: (entries: Record<string, EntityPolicy>) => void;
}) {
  const named = Object.keys(entries);
  const unnamed = [...new Set(known)]
    .filter((name) => !(name in entries))
    .sort((a, b) => entityLabel(a).localeCompare(entityLabel(b)));
  const withThreshold = group === "entities";

  const set = (name: string, patch: Partial<EntityPolicy>) => {
    const old = entries[name];
    const placeholder = patch.placeholder ?? old?.placeholder;
    onChange({
      ...entries,
      [name]: {
        mode: patch.mode ?? old?.mode ?? defaultMode,
        threshold: patch.threshold !== undefined ? patch.threshold : (old?.threshold ?? null),
        // Only written once somebody has chosen: an absent field means
        // tagged to the gateway, and policies saved before the option existed
        // should round-trip unchanged.
        ...(placeholder !== undefined && { placeholder }),
      },
    });
  };
  const remove = (name: string) => {
    const { [name]: _gone, ...rest } = entries;
    onChange(rest);
  };
  /**
   * A new row starts stricter than the default, because a row that repeated the
   * default would change nothing and the reason to add one is to treat the type
   * differently. Redact, or Block when the default is already that strong.
   */
  const add = (name: string) =>
    set(name, { mode: modeRank(defaultMode) < modeRank("redact") ? "redact" : "block" });

  const rowClass = withThreshold ? TYPE_ROW : TYPE_ROW_PLAIN;
  return (
    <section className="flex flex-col gap-3" aria-label={heading}>
      <h3 className="m-0 flex items-center gap-2 text-body font-semibold">
        {heading}
        <Tip text={tip} />
      </h3>
      {named.length === 0 ? (
        <p className="m-0 text-body text-ink-muted">{m["policy_fields.types_empty"]()}</p>
      ) : (
        <ul className="m-0 flex list-none flex-col p-0">
          <li
            aria-hidden="true"
            className={`${rowClass} border-b border-line pb-1 text-small font-medium text-ink-faint max-[40rem]:hidden`}
          >
            <span>{m["policy_fields.col_type"]()}</span>
            <span>{m["policy_fields.col_action"]()}</span>
            {withThreshold && <span>{m["policy_fields.col_confidence"]()}</span>}
            <span />
          </li>
          {named.map((entity) => {
            const name = entityLabel(entity);
            // Nothing here knows this label, so `entityLabel` handed it back
            // unchanged. Printing it twice would read as a rendering fault.
            const labelled = name !== entity;
            const mode = entries[entity]?.mode ?? defaultMode;
            const unlisted =
              flagUnknown && known.length > 0 && !known.some((k) => k.toUpperCase() === entity);
            return (
              <li
                key={entity}
                className={`${rowClass} border-b border-line-quiet py-2 max-[40rem]:py-3`}
              >
                <div className="flex min-w-0 flex-col gap-0.5" title={entity}>
                  <span className="text-body font-medium">{labelled ? name : entity}</span>
                  {labelled && (
                    <span className="flex flex-wrap items-center gap-1.5 text-small text-ink-faint">
                      <code className={CODE}>{entity}</code>
                    </span>
                  )}
                  {unlisted && (
                    <span className="text-small text-warn">
                      {m["policy_fields.format_unlisted"]()}
                    </span>
                  )}
                </div>
                <ModeSegments
                  name={`mode-${group}-${entity}`}
                  legend={m["policy_fields.mode_group"]({
                    name: labelled ? `${name} (${entity})` : entity,
                  })}
                  value={mode}
                  floor={floorFor(entity)}
                  locked={unlisted}
                  onChange={(next) => set(entity, { mode: next })}
                />
                {withThreshold && (
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
                        : m["policy_fields.confidence_default_value"]({ value: scoreThreshold })
                    }
                    value={entries[entity]?.threshold ?? ""}
                    onChange={(event) =>
                      set(entity, {
                        threshold: event.target.value === "" ? null : Number(event.target.value),
                      })
                    }
                  />
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={m["policy_fields.remove_type"]({ name })}
                  title={m["policy_fields.remove_type"]({ name })}
                  onClick={() => remove(entity)}
                >
                  {m["policy_fields.remove_type_short"]()}
                </Button>
                {/* Only the two modes that write a derived token have a form to
                    choose; redact writes the bare label. */}
                <div className="col-span-full max-w-md">
                  <PlaceholderSelect
                    label={m["policy_fields.placeholder_for"]({ name })}
                    mode={mode}
                    value={entries[entity]?.placeholder ?? "tagged"}
                    onChange={(placeholder) => set(entity, { placeholder })}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      )}
      <div className="max-w-sm">
        <Select
          label={
            group === "formats" ? m["policy_fields.add_format"]() : m["policy_fields.add_type"]()
          }
          value=""
          disabled={unnamed.length === 0}
          onChange={(event) => {
            if (event.target.value !== "") add(event.target.value);
          }}
        >
          <option value="">
            {unnamed.length === 0
              ? m["policy_fields.add_type_none"]()
              : m["policy_fields.add_type_choose"]()}
          </option>
          {unnamed.map((entity) => (
            <option key={entity} value={entity}>
              {entityLabel(entity) === entity ? entity : `${entityLabel(entity)} (${entity})`}
            </option>
          ))}
        </Select>
      </div>
    </section>
  );
}

/**
 * One keyword list: a name, the words as one comma-separated line, whether
 * capitals and accents count, and the mode.
 *
 * The typed line is state of its own, as the allow-list's is: parsing on every
 * keystroke would delete the comma at the moment it is typed. It is dropped for
 * the list's own words whenever the two stop agreeing, which is what happens
 * when another row is removed and this one takes its place.
 */
function KeywordRow({
  number,
  keyword,
  onChange,
  onRemove,
}: {
  number: number;
  keyword: KeywordList;
  onChange: (patch: Partial<KeywordList>) => void;
  onRemove: () => void;
}) {
  const [text, setText] = useState(() => keyword.words.join(", "));
  const parsed = parseWords(text);
  if (parsed.length !== keyword.words.length || parsed.some((w, i) => w !== keyword.words[i])) {
    setText(keyword.words.join(", "));
  }
  return (
    <div className="grid grid-cols-[minmax(8rem,1fr)_minmax(10rem,2fr)_minmax(9rem,1fr)_auto] items-end gap-3 border-b border-line-quiet pb-3 max-[56rem]:grid-cols-1">
      <Input
        label={m["policy_fields.keyword_name_label"]({ number })}
        value={keyword.name}
        placeholder={m["policy_fields.keyword_name_placeholder"]()}
        onChange={(event) => onChange({ name: event.target.value })}
      />
      <Input
        label={m["policy_fields.keyword_words_label"]({ number })}
        value={text}
        placeholder={m["policy_fields.keyword_words_placeholder"]()}
        onChange={(event) => {
          setText(event.target.value);
          onChange({ words: parseWords(event.target.value) });
        }}
      />
      <ModeSelect
        label={m["policy_fields.keyword_mode_label"]({ number })}
        value={keyword.mode}
        floor={null}
        onChange={(mode) => onChange({ mode })}
      />
      <div>
        <Button variant="ghost" onClick={onRemove}>
          {m["policy_fields.pattern_remove"]()}
        </Button>
      </div>
      <label className="col-span-full flex items-center gap-2 text-body">
        <input
          type="checkbox"
          checked={keyword.case_sensitive ?? false}
          onChange={(event) => onChange({ case_sensitive: event.target.checked })}
        />
        {m["policy_fields.keyword_case"]({ number })}
        <Tip text={m["policy_fields.keyword_case_tip"]()} />
      </label>
      <div className="col-span-full max-w-md">
        <PlaceholderSelect
          label={m["policy_fields.keyword_placeholder_label"]({ number })}
          mode={keyword.mode}
          value={keyword.placeholder ?? "tagged"}
          onChange={(placeholder) => onChange({ placeholder })}
        />
      </div>
    </div>
  );
}

/**
 * Custom entity types, named in plain words, for an engine that reads labels
 * (GLiNER). A name is how the type is stored, counted and logged, so it is
 * UPPER_SNAKE; the label is what the model is asked to look for.
 */
function CustomEntities({
  policy,
  usable,
  onChange,
}: {
  policy: RedactionPolicy;
  usable: boolean;
  onChange: (list: CustomEntity[]) => void;
}) {
  const list = policy.custom_entities;
  const patch = (index: number, change: Partial<CustomEntity>) =>
    onChange(list.map((item, at) => (at === index ? { ...item, ...change } : item)));
  return (
    <section
      className="flex flex-col gap-3 border-t border-line-quiet pt-4"
      aria-label={m["policy_fields.custom_heading"]()}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="m-0 flex items-center gap-2 text-body font-semibold">
          {m["policy_fields.custom_heading"]()}
          <Tip text={m["policy_fields.custom_tip"]()} />
        </h3>
        {usable && (
          <Button
            onClick={() =>
              onChange([...list, { name: "", label: "", mode: "redact", threshold: null }])
            }
          >
            {m["policy_fields.custom_add"]()}
          </Button>
        )}
      </div>
      {!usable && (
        <Notice tone="warn">{m["policy_fields.custom_unused"]()}</Notice>
      )}
      {list.map((item, index) => (
        <div
          key={index}
          className="grid grid-cols-[minmax(8rem,1fr)_minmax(10rem,2fr)_minmax(9rem,1fr)_6rem_auto] items-end gap-3 border-b border-line-quiet pb-3 max-[56rem]:grid-cols-1"
        >
          <Input
            label={m["policy_fields.custom_name_label"]({ number: index + 1 })}
            className={CODE}
            value={item.name}
            placeholder={m["policy_fields.custom_name_placeholder"]()}
            onChange={(event) =>
              patch(index, {
                name: event.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, "_"),
              })
            }
          />
          <Input
            label={m["policy_fields.custom_label_label"]({ number: index + 1 })}
            value={item.label}
            placeholder={m["policy_fields.custom_label_placeholder"]()}
            onChange={(event) => patch(index, { label: event.target.value })}
          />
          <ModeSelect
            label={m["policy_fields.custom_mode_label"]({ number: index + 1 })}
            value={item.mode}
            floor={null}
            onChange={(mode) => patch(index, { mode })}
          />
          <Input
            label={m["policy_fields.custom_threshold_label"]({ number: index + 1 })}
            type="number"
            min="0"
            max="1"
            step="0.05"
            value={item.threshold ?? ""}
            onChange={(event) =>
              patch(index, {
                threshold: event.target.value === "" ? null : Number(event.target.value),
              })
            }
          />
          <div>
            <Button variant="ghost" onClick={() => onChange(list.filter((_, at) => at !== index))}>
              {m["policy_fields.pattern_remove"]()}
            </Button>
          </div>
          <div className="col-span-full max-w-md">
            <PlaceholderSelect
              label={m["policy_fields.custom_placeholder_label"]({ number: index + 1 })}
              mode={item.mode}
              value={item.placeholder ?? "tagged"}
              onChange={(placeholder) => patch(index, { placeholder })}
            />
          </div>
        </div>
      ))}
    </section>
  );
}

/**
 * One row of the types table: the kind of data, its action, its confidence,
 * and the reset button. On a phone the four stack into two lines rather than
 * shrinking, because six segments and a number field do not fit in 360px.
 */
const TYPE_ROW =
  "grid grid-cols-[minmax(8rem,1fr)_max-content_5.5rem_auto] items-center gap-x-3 gap-y-2 " +
  "max-[40rem]:grid-cols-[1fr_auto] max-[40rem]:[&>*:nth-child(2)]:col-span-2 " +
  "max-[40rem]:[&>*:nth-child(2)]:row-start-2 max-[40rem]:[&>*:nth-child(3)]:row-start-3 " +
  "max-[40rem]:[&>*:nth-child(3)]:col-span-2";

/** The same row for a group with no confidence (the pattern formats). */
const TYPE_ROW_PLAIN =
  "grid grid-cols-[minmax(8rem,1fr)_max-content_auto] items-center gap-x-3 gap-y-2 " +
  "max-[40rem]:grid-cols-[1fr_auto] max-[40rem]:[&>*:nth-child(2)]:col-span-2 " +
  "max-[40rem]:[&>*:nth-child(2)]:row-start-2";

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
    engine: null,
    formats: {},
    patterns: [],
    keywords: [],
    entities: {},
    custom_entities: [],
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
 * The six modes as one segmented control, in the weakest-to-strongest order
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
  detect_only: "peer-checked:bg-ink/10 peer-checked:font-semibold peer-checked:text-ink",
  anonymise_restore:
    "peer-checked:bg-accent-solid peer-checked:font-semibold peer-checked:text-white",
  anonymise: "peer-checked:bg-accent-solid peer-checked:font-semibold peer-checked:text-white",
  redact: "peer-checked:bg-accent-solid peer-checked:font-semibold peer-checked:text-white",
  block: "peer-checked:bg-danger peer-checked:font-semibold peer-checked:text-ink-inverse",
};

function ModeSegments(props: Parameters<typeof ModeSegmentsInner>[0]) {
  return useContext(DetectionOnly) ? null : <ModeSegmentsInner {...props} />;
}

function ModeSegmentsInner({
  name,
  legend,
  visibleLegend = false,
  value,
  floor,
  locked = false,
  onChange,
}: {
  name: string;
  legend: ReactNode;
  /** Show the legend as a label above the control (the default action). */
  visibleLegend?: boolean;
  value: EntityMode;
  floor: EntityMode | null;
  /** Nothing can be chosen: the row's type is not one the service offers. */
  locked?: boolean;
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
          const disabled = locked || modeRank(mode.value) < least;
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
 * What the six words mean, said once.
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

/** Whether a mode writes a derived token, which is what a placeholder style shapes. */
/**
 * A policy only says what to look for (ADR 0114): what to do with what it finds is a rule's.
 * In that mode the editor shows no mode selector and no default mode, and the placeholder
 * style is always offered (it applies wherever a rule writes a derived token).
 */
const DetectionOnly = createContext(false);

export function writesToken(mode: EntityMode): boolean {
  return mode === "anonymise" || mode === "anonymise_restore";
}

/**
 * How a placeholder is written, shown only where a value is replaced by a
 * derived token (the two Anonymise actions). Redact writes the bare label
 * (`opaque_placeholder`) and block refuses the request, so the style would change
 * nothing there. The same select serves every row of every group.
 */
function PlaceholderSelect({
  label,
  mode,
  value,
  onChange,
}: {
  label: ReactNode;
  mode: EntityMode;
  value: PlaceholderStyle;
  onChange: (value: PlaceholderStyle) => void;
}) {
  const detection = useContext(DetectionOnly);
  if (!detection && !writesToken(mode)) return null;
  return (
    <Select
      label={label}
      value={value}
      hint={m["policy_fields.placeholder_hint"]()}
      onChange={(event) => onChange(event.target.value as PlaceholderStyle)}
    >
      <option value="tagged">{m["policy_fields.placeholder_tagged"]()}</option>
      <option value="same_length">{m["policy_fields.placeholder_same_length"]()}</option>
    </Select>
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
  const detection = useContext(DetectionOnly);
  const least = floor === null ? -1 : modeRank(floor);
  if (detection) return null;
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
