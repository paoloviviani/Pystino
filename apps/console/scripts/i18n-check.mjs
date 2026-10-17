#!/usr/bin/env node
/**
 * The console's i18n guard.
 *
 * Three things go wrong when a catalogue is being filled in by hand, and each
 * of them is silent at the moment it happens:
 *
 * 1. a screen calls `m["users.invite.button"]` and the key is not in en.json —
 *    nothing throws, and the reader sees `undefined` where a button was;
 * 2. en.json grows a message and the committed en-XA.json does not, so the
 *    pseudo-locale stops showing the very string it exists to reveal;
 * 3. a screen that was declared converted still spells a word out in JSX,
 *    which no type can catch and which a translation will leave behind.
 *
 *     pnpm --filter @llmp/console i18n:check
 *
 * The checks are deliberately cheap and textual. A proper AST extractor would
 * find more, and would also break on every formatter upgrade; the point here
 * is to fail the build on the mistakes that actually happen, and to be readable
 * when it does.
 */

import { readFile, readdir } from "node:fs/promises";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { buildPseudoLocale, serialise } from "./pseudo-locale.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = resolve(HERE, "..");
const SRC = join(APP, "src");
const MESSAGES = join(APP, "messages");

/**
 * Screens that have been through the conversion, and so may not carry
 * hard-coded user-visible text.
 *
 * A list rather than a rule over the whole tree because the rule cannot be
 * right: `aria-label="Close"` on a dialog and `data-testid="users-row"` are
 * both string props, and only one of them is a word a translator needs. The
 * list is the honest version — a file is converted when somebody says so, in
 * the same commit that converts it — and the heuristic below is there to catch
 * the text that slips back in afterwards.
 */
const CONVERTED_FILES = ["src/routes/NotFound.tsx"];

/** Props whose string value is read by a person, one way or another. */
const TEXT_PROPS = ["title", "placeholder", "aria-label", "alt", "label"];

/** JSX text with no letters in it is punctuation or an entity, not a word. */
const HAS_LETTERS = /[A-Za-z]/;

async function walk(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  const files = await Promise.all(
    entries.map(async (entry) => {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) return walk(path);
      return /\.tsx?$/.test(entry.name) && !entry.name.endsWith(".d.ts") ? [path] : [];
    }),
  );
  return files.flat();
}

/**
 * Flattens a catalogue to `key -> message`, however it is nested.
 *
 * The inlang plugin reads nested objects and dotted keys the same way — both
 * become `users.invite.button` — so this may not care which was written.
 */
function flatten(catalogue, prefix = "", out = {}) {
  for (const [key, value] of Object.entries(catalogue)) {
    if (key === "$schema") continue;
    const name = prefix === "" ? key : `${prefix}.${key}`;
    if (typeof value === "string" || Array.isArray(value)) out[name] = value;
    else if (value && typeof value === "object") flatten(value, name, out);
  }
  return out;
}

/** Every message the console's own code asks for, and where it asks. */
async function usedKeys(files) {
  const used = new Map();
  // `m["not_found.body"]` — the form the compiler emits for a dotted key.
  const bracket = /\bm\[\s*"([^"]+)"\s*\]/g;
  // `m.something` — a key with no dots, and the only other shape a message can
  // be reached by.
  const dotted = /\bm\.([A-Za-z_][A-Za-z0-9_]*)/g;
  // `m` is a common enough name for a lambda parameter — `(m) => m.kind` in a
  // table filter reads exactly like a message — so only a file that imported
  // the catalogue is searched at all.
  const importsMessages = /from\s+["'][^"']*paraglide\/messages(\.js)?["']/;
  for (const file of files) {
    const source = await readFile(file, "utf8");
    if (!importsMessages.test(source)) continue;
    for (const [pattern, group] of [
      [bracket, 1],
      [dotted, 0],
    ]) {
      for (const match of source.matchAll(pattern)) {
        const key = match[group];
        const where = relative(APP, file);
        if (!used.has(key)) used.set(key, new Set());
        used.get(key).add(where);
      }
    }
  }
  return used;
}

/** Hard-coded user-visible text in a file that has been converted. */
function hardCoded(source, file) {
  const problems = [];
  // Comments first, and block comments keep their newlines so the line numbers
  // below still point at the line a person would look at. This file's own
  // header, and every header in the console, quote English and angle brackets
  // freely; a check that read them would report the explanation as the mistake.
  const code = source
    .replace(/\/\*[\s\S]*?\*\//g, (comment) => comment.replace(/[^\n]/g, " "))
    .replace(/(^|[^:"'`\\])\/\/.*$/gm, "$1");

  code.split("\n").forEach((line, index) => {
    const lineNumber = index + 1;
    // JSX text nodes: what sits between a tag's `>` and the next `<`, when it
    // carries a letter. Punctuation and an entity are not a missing message.
    for (const node of line.match(/>([^<>{}]+)</g) ?? []) {
      const value = node.slice(1, -1).trim();
      if (HAS_LETTERS.test(value)) problems.push({ file, lineNumber, value, kind: "JSX text" });
    }
    // String literals in the props a person reads.
    for (const prop of TEXT_PROPS) {
      const pattern = new RegExp(`\\b${prop}=(?:"([^"]*)"|\\{\`([^\`]*)\`\\})`, "g");
      for (const match of line.matchAll(pattern)) {
        const value = (match[1] ?? match[2] ?? "").trim();
        if (!HAS_LETTERS.test(value)) continue;
        // `label={`${m.a()} ${m.b()}`}` is already the catalogue's job.
        if (/\bm\.[A-Za-z_]|\bm\[/.test(value)) continue;
        problems.push({ file, lineNumber, value, kind: prop });
      }
    }
  });

  return problems;
}

async function main() {
  const failures = [];
  const base = JSON.parse(await readFile(join(MESSAGES, "en.json"), "utf8"));
  const keys = new Set(Object.keys(flatten(base)));

  // 1. A key used in code that the catalogue does not have.
  const files = await walk(SRC);
  const used = await usedKeys(files);
  const missing = [...used.keys()].filter((key) => !keys.has(key)).sort();
  for (const key of missing) {
    failures.push(`used in code but not in en.json: ${key} (${[...used.get(key)].join(", ")})`);
  }

  // 2. The pseudo-locale, which is generated and must be regenerated.
  let current = null;
  try {
    current = await readFile(join(MESSAGES, "en-XA.json"), "utf8");
  } catch {
    failures.push("messages/en-XA.json is missing — run `pnpm i18n:pseudo`");
  }
  if (current !== null) {
    const expected = serialise(buildPseudoLocale(base));
    if (current !== expected) {
      failures.push(
        "messages/en-XA.json is out of date with messages/en.json — run `pnpm i18n:pseudo`",
      );
    }
  }

  // 3. Text left hard-coded in a converted screen.
  for (const file of CONVERTED_FILES) {
    const path = join(APP, file);
    let source;
    try {
      source = await readFile(path, "utf8");
    } catch {
      failures.push(`converted file is missing: ${file}`);
      continue;
    }
    for (const problem of hardCoded(source, file)) {
      failures.push(`${problem.file}:${problem.lineNumber} hard-coded ${problem.kind}: ${problem.value}`);
    }
  }

  if (failures.length > 0) {
    console.error(`i18n:check failed (${failures.length}):`);
    for (const failure of failures) console.error(`  - ${failure}`);
    process.exit(1);
  }
  console.log(
    `i18n:check passed: ${keys.size} message(s), ${used.size} used, ${CONVERTED_FILES.length} converted file(s)`,
  );
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
