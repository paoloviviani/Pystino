import { Badge, Input } from "@llmp/ui";
import type { ReactNode } from "react";
import { useState } from "react";
import type { AdminModel } from "../lib/types";
import styles from "../routes/Admin.module.css";

/**
 * Everything about *describing* a model's capabilities, shared by the three
 * places that do it: the catalogue listing renders them, the Add dialog collects
 * them, and the model page edits them.
 *
 * Extracted when the model page arrived. Two copies of the vocabulary would be
 * the bug this whole design is meant to avoid — ADR 0031's point is that the
 * capability worth hearing about is the one the provider added last week, and a
 * second list is one that gets updated last.
 */

/**
 * What a model accepts, produces and can do.
 *
 * Three lists rather than a column of yes/no flags, because the provider
 * documents its feature set as open — "current values include json_mode,
 * reasoning and tools" — and a fixed set of checkboxes would silently hide
 * whatever it added last week (ADR 0031).
 *
 * An empty set reads "not stated", never "cannot". Nobody has described most
 * of these models yet, and rendering that as a row of crosses would turn an
 * absence of information into a claim.
 */
export function Capabilities({ model }: { model: AdminModel }) {
  const inputs = model.input_modalities.filter((item) => item !== "text");
  const shown = [...inputs, ...model.supported_features];

  if (shown.length === 0) {
    return <span className={styles.muted}>not stated</span>;
  }
  return (
    <div className={styles.chips}>
      {shown.map((item) => (
        <Badge key={item}>{item.replace(/_/g, " ")}</Badge>
      ))}
    </div>
  );
}

export const asList = (value: string): string[] =>
  value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);

export interface Vocabulary {
  value: string;
  /** A plain-English name for a value whose own name does not give it away. */
  gloss?: string;
}

/**
 * The values that get a checkbox.
 *
 * Not a design opinion: this is every value the reference provider's catalogue
 * actually uses, counted across all 107 models it offers. `input_modalities`
 * is never anything but text, image or audio; `supported_features` is never
 * anything but tools, json_mode or reasoning. A checkbox for a value no
 * provider emits is clutter, and one missing for a value they do emit is the
 * bug this is meant to avoid.
 *
 * The two extra output modalities come from this gateway's own model kinds
 * rather than from the catalogue — nothing in it produces embeddings or
 * images, but migration 0006 backfills both and an operator cataloguing such a
 * model by hand needs to say so.
 *
 * These lists are a convenience, never a filter. Anything outside them is
 * typed into the Other box and kept verbatim, which is the whole point of
 * ADR 0031: the capability most worth hearing about is the one the provider
 * added last week, and a vocabulary compiled today would discard exactly that.
 */
export const KNOWN_INPUTS: readonly Vocabulary[] = [
  { value: "text" },
  { value: "image", gloss: "vision" },
  { value: "audio" },
];

export const KNOWN_OUTPUTS: readonly Vocabulary[] = [
  { value: "text" },
  { value: "image" },
  { value: "embeddings" },
];

export const KNOWN_FEATURES: readonly Vocabulary[] = [
  { value: "tools", gloss: "function calling" },
  { value: "json_mode", gloss: "structured output" },
  { value: "reasoning" },
];

/**
 * A capability set: checkboxes for the values providers actually use, and a
 * comma-separated box for everything else.
 *
 * The Other box is not a fallback nobody is expected to reach. It is seeded
 * with whatever the import found that has no checkbox, so an unrecognised
 * capability is *visible and editable* rather than quietly absent — a value
 * with nowhere to render would be dropped by the first save, and the operator
 * would have destroyed information by opening a dialog and clicking Save.
 */
export function CapabilityPicker({
  label,
  otherLabel,
  hint,
  known,
  value,
  onChange,
}: {
  label: string;
  otherLabel: string;
  hint: ReactNode;
  known: readonly Vocabulary[];
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const vocabulary = known.map((entry) => entry.value);

  // Seeded once, then owned by the input. Deriving this from `value` on every
  // render would fight the person typing: splitting on the comma they just
  // pressed and joining the result back removes it before they type the next
  // word. The dialog remounts this component when it loads a different model.
  const [other, setOther] = useState(() =>
    value.filter((item) => !vocabulary.includes(item)).join(", "),
  );

  // Set union rather than concatenation: typing a value into Other that also
  // has a checkbox should tick it, not list it twice.
  const emit = (ticked: string[], extras: string) =>
    onChange([...new Set([...ticked, ...asList(extras)])]);

  return (
    <fieldset className={styles.capabilities}>
      <legend className={styles.capabilitiesLegend}>{label}</legend>
      <div className={styles.checkList}>
        {known.map((entry) => (
          <label key={entry.value} className={styles.checkItem}>
            <input
              type="checkbox"
              checked={value.includes(entry.value)}
              onChange={(event) =>
                emit(
                  vocabulary.filter((item) =>
                    item === entry.value ? event.target.checked : value.includes(item),
                  ),
                  other,
                )
              }
            />
            <span>
              {entry.value}
              {entry.gloss && <span className={styles.muted}> ({entry.gloss})</span>}
            </span>
          </label>
        ))}
      </div>
      <Input
        label={otherLabel}
        value={other}
        onChange={(event) => {
          setOther(event.target.value);
          emit(
            value.filter((item) => vocabulary.includes(item)),
            event.target.value,
          );
        }}
        placeholder="comma separated"
        hint={hint}
      />
    </fieldset>
  );
}
