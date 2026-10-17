import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { activeLocale, applyLocale, realLocales, rememberLocale, storedLocale } from "./locale";

/**
 * The console's language is decided in one place, and the order is
 * configuration rather than code: an explicit choice, then the browser's
 * preference if the console has that language, then English. What has to hold
 * here is that a storage failure costs the preference and not the page, and
 * that the pseudo-locale is reachable but never offered.
 */

const KEY = "console.locale";

/** What the browser says it speaks, in order — the second strategy's input. */
function speaks(languages: string[]): void {
  Object.defineProperty(window.navigator, "languages", { value: languages, configurable: true });
  Object.defineProperty(window.navigator, "language", {
    value: languages[0] ?? "",
    configurable: true,
  });
}

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = "en";
  speaks(["en-US"]);
});

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("storedLocale", () => {
  it("returns an explicit choice the catalogue has", () => {
    window.localStorage.setItem(KEY, "en-XA");
    expect(storedLocale()).toBe("en-XA");
  });

  it("ignores a stored locale this build has no catalogue for", () => {
    // A console that has been downgraded, or a hand-edited key: the English
    // catalogue must win over a language that no longer exists.
    window.localStorage.setItem(KEY, "de");
    expect(storedLocale()).toBeUndefined();
  });

  it("answers undefined when storage access is denied", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("The operation is insecure.", "SecurityError");
    });
    expect(storedLocale()).toBeUndefined();
  });
});

describe("activeLocale", () => {
  it("is English when nothing has been chosen and the browser asks for nothing known", () => {
    speaks(["qq-ZZ"]);
    expect(activeLocale()).toBe("en");
  });

  it("follows the browser's own language when the console has that language", () => {
    // `en-GB` has no catalogue of its own; its base language does.
    speaks(["en-GB", "qq-ZZ"]);
    expect(activeLocale()).toBe("en");
  });

  it("follows an explicit choice over the browser", () => {
    speaks(["qq-ZZ"]);
    window.localStorage.setItem(KEY, "en-XA");
    expect(activeLocale()).toBe("en-XA");
  });

  it("refuses the pseudo-locale when a browser asks for it", () => {
    // The pseudo-locale is a lens, not a language: a deployment must never be
    // shown accented English because somebody's browser says so.
    speaks(["en-XA"]);
    expect(activeLocale()).toBe("en");
    expect(realLocales()).not.toContain("en-XA");
  });

  it("still offers the pseudo-locale to a reader who asks for it by hand", () => {
    speaks(["en-US"]);
    rememberLocale("en-XA");
    expect(activeLocale()).toBe("en-XA");
  });
});

describe("rememberLocale", () => {
  it("stores the choice and publishes it to the document", () => {
    rememberLocale("en-XA");
    expect(window.localStorage.getItem(KEY)).toBe("en-XA");
    expect(document.documentElement.lang).toBe("en-XA");
  });

  it("still changes the document when storage access is denied", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Quota exceeded.", "QuotaExceededError");
    });
    expect(() => rememberLocale("en-XA")).not.toThrow();
    expect(document.documentElement.lang).toBe("en-XA");
  });
});

describe("applyLocale", () => {
  it("sets <html lang>, which is what a screen reader announces", () => {
    applyLocale("en-XA");
    expect(document.documentElement.lang).toBe("en-XA");
  });
});
