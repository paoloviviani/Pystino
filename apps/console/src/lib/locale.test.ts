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

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = "en";
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
    vi.spyOn(navigator, "language", "get").mockReturnValue("qq-ZZ");
    expect(activeLocale()).toBe("en");
  });

  it("follows an explicit choice", () => {
    window.localStorage.setItem(KEY, "en-XA");
    expect(activeLocale()).toBe("en-XA");
  });

  it("never reaches the pseudo-locale from the browser alone", () => {
    // jsdom reports en-US, which resolves to the base locale; the point of the
    // assertion is that the pseudo-locale is only ever a deliberate choice.
    expect(activeLocale()).toBe("en");
    expect(realLocales()).not.toContain("en-XA");
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
