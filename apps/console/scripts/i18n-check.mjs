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
const CONVERTED_FILES = ["src/routes/NotFound.tsx", "src/routes/Login.tsx", "src/components/RequireAdmin.tsx", "src/components/OpencodeSetup.tsx", "src/routes/Admin.tsx", "src/components/Disclosures.tsx", "src/components/SubjectPicker.tsx", "src/components/CapabilityPicker.tsx", "src/routes/AdminReports.tsx", "src/routes/Reports.tsx", "src/routes/AdminRedactionRule.tsx", "src/routes/IdentityDirectory.tsx", "src/routes/AdminIdentity.tsx", "src/components/Shell.tsx", "src/components/PolicyFields.tsx", "src/routes/AdminProviders.tsx", "src/routes/AdminSearch.tsx", "src/routes/AdminQuotas.tsx"];

/** Props whose string value is read by a person, one way or another. */
const TEXT_PROPS = ["title", "placeholder", "aria-label", "alt", "label"];

/** JSX text with no letters in it is punctuation or an entity, not a word. */
const HAS_LETTERS = /[A-Za-z]/;

/**
 * A line that says it means to keep its text.
 *
 * Some words are not words: the product's own name, a file path somebody has
 * to type. Forcing those into the catalogue would be a translator's decision
 * made on their behalf, and leaving them inline would be this check failing a
 * build every time somebody moved the line — so a line can say so, and has to
 * say so out loud where the next reader sees it.
 */
const LITERAL = /i18n: literal/;

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

/**
 * A message's shape with the text taken out: its keys, its variant keys, how
 * many of them it has, and every `{placeholder}` and `{#tag}` in order.
 *
 * This is what "the pseudo-locale kept every placeholder" means, made into
 * something checkable. Comparing the generated file against the generator only
 * says the two agree — they agreed once while both wrapped a plural message's
 * declarations in accented brackets, and the compiler was the thing that
 * noticed. Comparing shape says the *wording* changed and nothing else did.
 */
function shape(value) {
  if (typeof value === "string") return tokens(value);
  if (Array.isArray(value)) return value.map(shape);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .filter(([key]) => key !== "$schema")
        .map(([key, entry]) => [key, shape(entry)]),
    );
  }
  return value;
}

/** Every placeholder and markup tag in a piece of text, in order. */
function tokens(text) {
  return text.match(/\{[^{}]*\}/g) ?? [];
}

/** Every message the console's own code asks for, and where it asks. */
async function usedKeys(files) {
  const used = new Map();
  // `m["not_found.body"]` — the form the compiler emits for a dotted key.
  const bracket = /\bm\[\s*"([^"]+)"\s*\]/g;
  // `m.something(...)` — a key with no dots, and the other shape a message can
  // be reached by. The call parenthesis is what keeps it apart from a lambda
  // parameter: `.filter((m) => m.kind === "search")` has an `m` in it, and
  // every file that imports the catalogue would then fail its own check.
  const dotted = /\bm\.([A-Za-z_][A-Za-z0-9_]*)\s*\(/g;
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
    if (LITERAL.test(line)) return;
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
    // Generated and in step is not the same as correct: the generator must
    // change the wording and nothing else.
    const pseudo = JSON.parse(current);
    const before = shape(base);
    const after = shape(pseudo);
    for (const key of keys) {
      if (!(key in after)) {
        failures.push(`messages/en-XA.json has no message for ${key}`);
        continue;
      }
      if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) {
        failures.push(
          `messages/en-XA.json changed more than the wording of ${key} — a placeholder, a tag or a variant key differs`,
        );
      }
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
