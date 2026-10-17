import { baseLocale, getLocale, isLocale, locales, setLocale } from "../paraglide/runtime.js";
import type { Locale } from "../paraglide/runtime.js";

/**
 * The console's language, and where a choice of it is remembered.
 *
 * The *order* the locale is picked in is configuration, not code: it lives in
 * `project.inlang/paraglide.config.ts` as Paraglide's strategy list —
 * `localStorage` (an explicit choice), `preferredLanguage` (the browser's own
 * preference, honoured only for a language the console has a catalogue for),
 * `baseLocale` (English). This module is the console's single door onto that
 * resolution, so there is one answer to "which language is this in" and no
 * second one to keep in step with it.
 *
 * English is the only real language today. The machinery is here so that
 * adding one is a file, not a change to the console.
 */

const LOCALE_KEY = "console.locale";

/**
 * Locales that exist to be looked at rather than read.
 *
 * `en-XA` is the pseudo-locale (scripts/pseudo-locale.mjs): English with every
 * message bracketed and accented, so an untranslated string is visible on
 * screen. It is named here, not inferred from the tag, because "a locale whose
 * name contains XA" is a guess and a guess in a filter that decides what a
 * reader may choose is the wrong place for one.
 */
const PSEUDO_LOCALES: readonly string[] = ["en-XA"];

/**
 * Every locale the catalogue carries, including `en-XA`.
 *
 * Re-exported so that nothing else in the console has to know the generated
 * module's path. Anything a reader may choose goes through `realLocales`.
 */
export { locales };

/** Locales a reader may choose: everything the console can actually ship. */
export function realLocales(): Locale[] {
  return locales.filter((locale) => !PSEUDO_LOCALES.includes(locale));
}

/**
 * The stored choice, if it names a locale this build has a catalogue for.
 *
 * `localStorage` throws rather than returning null where site data is blocked,
 * and a console that will not start because it could not read a language
 * preference is worse than a preference that does not stick. So the read is
 * guarded and the browser's own language is the answer instead.
 */
export function storedLocale(): Locale | undefined {
  try {
    const value = window.localStorage.getItem(LOCALE_KEY);
    if (value !== null && isLocale(value)) return value;
  } catch {
    // See above: not sticking across reloads is a small loss, failing to load is not.
  }
  return undefined;
}

/**
 * The language the console is in right now: the stored choice if there is one,
 * otherwise the browser's if the console speaks it, otherwise English.
 *
 * Never throws. Paraglide's own `getLocale()` raises when no strategy
 * resolves, which cannot happen with `baseLocale` in the list — but this is
 * read while every screen renders, and a locale read that can take the console
 * down is not a locale read.
 */
export function activeLocale(): Locale {
  try {
    return getLocale();
  } catch {
    return baseLocale;
  }
}

/**
 * Publish the active locale to the document.
 *
 * `<html lang>` is what a screen reader announces, what a browser uses to pick
 * a hyphenation and line-breaking dictionary, and what `Intl` falls back to
 * for anything that does not name a locale. index.html carries `lang="en"`
 * because the console is served as a static file — this sets the attribute
 * before the first render so nothing has to correct it.
 */
export function applyLocale(locale: Locale): void {
  document.documentElement.lang = locale;
}

/** Apply the active locale to the document. Called once, before the first render. */
export function initLocale(): void {
  applyLocale(activeLocale());
}

/**
 * Change the console's language, and remember the choice.
 *
 * Persists first, then hands the locale to Paraglide, then republishes the
 * document attribute — in that order, so an exception in the second step
 * leaves a stored choice that survives a reload rather than a page whose
 * language and stored preference disagree. The write is this module's own even
 * though the `localStorage` strategy would repeat it, because a preference
 * that sticks because of the strategy list is a preference that a future
 * change to that list silently takes away.
 *
 * `reload: false` is the documented narrow escape hatch, and this is the
 * surface it was written for: a client-rendered console with no localized
 * URLs and nothing server-rendered to tear down. It does not re-render, so a
 * caller changing the language for a session that has already painted wants a
 * reload; a menu added later can either reload or re-render on its own.
 */
export function rememberLocale(locale: Locale): void {
  try {
    window.localStorage.setItem(LOCALE_KEY, locale);
  } catch {
    // See `storedLocale`.
  }
  setLocale(locale, { reload: false });
  applyLocale(locale);
}
