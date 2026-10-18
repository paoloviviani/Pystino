#!/usr/bin/env node
/**
 * Generates `messages/en-XA.json` from `messages/en.json`.
 *
 * A pseudo-locale is the one translation nobody has to write: every message
 * wrapped in brackets with accented letters, so a string the console still
 * spells out in code shows up on screen bracketed next to the ones that went
 * through the catalogue. Brackets rather than accent marks alone, because a
 * missing accent is invisible in a diff of a screenshot and a missing bracket
 * is not.
 *
 * `en-XA` is a developer tool. It is never a default: it is not in the
 * browser's language list, so `preferredLanguage` cannot reach it, and
 * selecting it means writing `localStorage["console.locale"] = "en-XA"` in the
 * console's own devtools session. A deployment that served it by accident
 * would look broken in a way nobody could file a useful bug about.
 *
 * Run it by hand after editing en.json, or let `i18n:check` tell you the
 * committed file is stale:
 *
 *     pnpm --filter @llmp/console i18n:pseudo
 */

import { readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = resolve(HERE, "..");

/**
 * Latin letters accented the way a real translation of English usually is:
 * the vowel carries the mark, the consonant does not. Chosen so that no two
 * source letters map to the same accented letter where a reader could confuse
 * two *different* words — `i` and `ı` are kept apart because a Turkish
 * dotless one looks like an `i` at reading size.
 */
const ACCENTED = {
  a: "á", b: "ƀ", c: "ć", d: "ď", e: "é", f: "ƒ", g: "ǧ", h: "ȟ",
  i: "í", j: "ĵ", k: "ķ", l: "ľ", m: "ḿ", n: "ń", o: "ó", p: "þ",
  q: "ǫ", r: "ŕ", s: "ś", t: "ť", u: "ú", v: "ǚ", w: "ŵ", x: "ẋ",
  y: "ý", z: "ź",
  A: "Á", B: "Ɓ", C: "Ć", D: "Ď", E: "É", F: "Ƒ", G: "Ǧ", H: "Ȟ",
  I: "Í", J: "Ĵ", K: "Ķ", L: "Ĺ", M: "Ḿ", N: "Ń", O: "Ó", P: "Þ",
  Q: "Ǫ", R: "Ŕ", S: "Ś", T: "Ť", U: "Ú", V: "Ǚ", W: "Ŵ", X: "Ẋ",
  Y: "Ý", Z: "Ź",
};

/**
 * One placeholder or markup tag: `{count}`, `{count: number style=percent}`,
 * `{#link}`, `{/link}`, `{#icon/}`, and the `\{` that stands for a literal
 * brace. Captured whole so it can be put back exactly as it was — a
 * pseudo-locale message whose placeholder has been accented would fail to
 * compile, which is the one thing this file must never produce.
 */
const TOKEN = /(\{[^{}]*\})/;

/** Accents one run of literal text, leaving every token between them alone. */
function accent(text) {
  return text.replace(/[A-Za-z]/g, (letter) => ACCENTED[letter] ?? letter);
}

function pseudo(message) {
  // Nothing to mark: an empty or whitespace-only message would become "[]",
  // which reads as a message rather than as an absent one.
  if (message.trim() === "") return message;
  // `split` with a capturing group returns the literal runs at the even
  // indexes and the tokens at the odd ones.
  const parts = message.split(TOKEN).map((part, index) => (index % 2 === 0 ? accent(part) : part));
  return `[${parts.join("")}]`;
}

/**
 * Rewrites a catalogue, whatever shape it has.
 *
 * Simple messages are strings; a message with variants or pluralization is an
 * array of `{ declarations, selectors, match }` objects, and a catalogue may
 * nest either one. Walking the structure instead of the file's top level is
 * what lets a plural message be pseudo-localized as well as a plain one —
 * otherwise the first count-dependent message added would silently come out
 * untranslated.
 */
function pseudoValue(value) {
  if (typeof value === "string") return pseudo(value);
  if (Array.isArray(value)) return value.map(pseudoVariant);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .filter(([key]) => key !== "$schema")
        .map(([key, entry]) => [key, pseudoValue(entry)]),
    );
  }
  return value;
}

/**
 * One variant of a message that has variants: `{ declarations, selectors,
 * match }`.
 *
 * Only `match` holds text. `declarations` and `selectors` are the *syntax* of a
 * plural or a formatted placeholder — `"local countPlural = count: plural"` is
 * read by the compiler, not by a reader — and accenting them does not produce a
 * word with accents, it produces a catalogue that will not compile at all. That
 * is a loud failure, which is the good kind, but it is still a failure this
 * script caused.
 */
function pseudoVariant(variant) {
  if (Array.isArray(variant) || typeof variant !== "object" || variant === null) {
    return pseudoValue(variant);
  }
  return Object.fromEntries(
    Object.entries(variant).map(([key, value]) => [
      key,
      key === "match" && value && typeof value === "object"
        ? Object.fromEntries(Object.entries(value).map(([match, pattern]) => [match, pseudo(pattern)]))
        : value,
    ]),
  );
}

export const BASE_LOCALE = "en";
export const PSEUDO_LOCALE = "en-XA";
export const MESSAGES = join(APP, "messages");

/** The pseudo-locale catalogue `i18n:check` compares the committed file with. */
export function buildPseudoLocale(catalogue) {
  return {
    $schema: "https://inlang.com/schema/inlang-message-format",
    ...pseudoValue(catalogue),
  };
}

export function serialise(catalogue) {
  return `${JSON.stringify(catalogue, null, 2)}\n`;
}

async function main() {
  const source = join(MESSAGES, `${BASE_LOCALE}.json`);
  const target = join(MESSAGES, `${PSEUDO_LOCALE}.json`);
  const catalogue = JSON.parse(await readFile(source, "utf8"));
  const generated = buildPseudoLocale(catalogue);
  await writeFile(target, serialise(generated));
  const count = Object.keys(generated).filter((key) => key !== "$schema").length;
  console.log(`${PSEUDO_LOCALE}.json: ${count} message(s) from ${BASE_LOCALE}.json`);
}

// Only when run as a script: `i18n:check` imports the generator to learn what
// the committed file should be, and writing the file out from an import would
// make a read-only check repair what it is checking.
if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
