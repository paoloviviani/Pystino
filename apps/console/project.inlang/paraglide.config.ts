import { defineConfig } from "@inlang/paraglide-js";

/**
 * Compiler options for the console's message catalogue.
 *
 * They live here rather than in `vite.config.ts` because every surface that
 * compiles the catalogue reads this one file: the Vite plugin (dev and build)
 * and the `paraglide-js compile` that `typecheck` and `i18n:*` scripts run
 * before TypeScript or the checker ever see the generated output.
 *
 * `strategy` is the order the locale is picked in, and it is the console's
 * whole locale policy: an explicit choice the reader made (localStorage),
 * then the browser's own preference if the console has a catalogue for it
 * (custom-browser-language, defined in src/lib/locale.ts, which is the
 * console's single entry point to this and never re-implements the order),
 * then the base locale.
 *
 * The middle one is Paraglide's `preferredLanguage` with one change: a
 * pseudo-locale is not a language a browser may arrive with, so the custom
 * strategy refuses it and only an explicit choice can select it.
 *
 * No `cookie` and no `url`: the console is served from one address
 * (/console/), so a per-locale URL would be a second routing scheme for a
 * console that has no other reason for one, and a cookie would put a second
 * copy of the same choice in a place the console cannot clear from its own
 * storage settings.
 */
export default defineConfig({
  outdir: "./src/paraglide",
  /**
   * One output layout everywhere. The plugin otherwise switches to
   * `locale-modules` outside production, which moves every generated `.d.ts`
   * — and `tsc` reads those, not the `.js`, because this package does not
   * enable `allowJs`.
   */
  outputStructure: "message-modules",
  /**
   * `tsc` sees the catalogue through generated declarations. Without them
   * every `m["…"]` is an implicit `any` and a mistyped key is a type error
   * rather than a missing word on screen.
   */
  emitTsDeclarations: true,
  strategy: ["localStorage", "custom-browser-language", "baseLocale"],
  /** Read by `src/lib/locale.ts`; namespaced with the console like the rest. */
  localStorageKey: "console.locale",
});
