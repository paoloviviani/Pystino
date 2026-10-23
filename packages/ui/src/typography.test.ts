import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The type scale is ONE scale (tokens.css: title, heading, body, control,
 * dense, small). Tailwind's own `text-sm`, `text-lg` and friends are cleared in
 * the theme, so a stray one renders as nothing, silently, and a page quietly
 * drifts from its neighbours. This is what stops it: no source file may use a
 * size utility that is not a step of the scale.
 */
const ROOTS = [join(__dirname), join(__dirname, "../../../apps/console/src")];
const STRAY = /(?:^|[\s"'`:])text-(?:xs|sm|base|md|lg|xl|[2-9]xl|smd)(?![\w-])/;
// The one sanctioned arbitrary size: 16px on a phone, so iOS does not zoom into a field.
const ARBITRARY = /text-\[[^\]]*\]/g;

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (name === "node_modules" || name === "paraglide") return [];
    if (statSync(path).isDirectory()) return sources(path);
    return /\.(tsx?|css)$/.test(name) && !/\.test\./.test(name) && name !== "tokens.css" ? [path] : [];
  });
}

describe("type scale", () => {
  const files = ROOTS.flatMap(sources);

  it("finds the sources it is meant to guard", () => {
    expect(files.length).toBeGreaterThan(40);
  });

  it("uses no size utility outside the scale", () => {
    const offenders = files.flatMap((file) =>
      readFileSync(file, "utf8")
        .split("\n")
        .map((line, i) => ({ file, line: i + 1, text: line }))
        .filter(({ text }) => STRAY.test(text)),
    );
    expect(offenders.map((o) => `${o.file}:${o.line}`)).toEqual([]);
  });

  it("allows one arbitrary size only: 16px on a phone", () => {
    const found = files.flatMap((file) =>
      (readFileSync(file, "utf8").match(ARBITRARY) ?? []).filter((c) => c !== "text-[16px]"),
    );
    expect(found).toEqual([]);
  });
});
