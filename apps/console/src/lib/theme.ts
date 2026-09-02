/**
 * The reader's theme, and where it is remembered.
 *
 * Light/dark is a property of how one person reads one browser, like the
 * exact-figures preference beside it in the menu — so `localStorage`, not the
 * server. A browser with site data blocked throws on access rather than
 * returning null, and a console that will not load because it could not read a
 * display preference is worse than a preference that does not stick.
 *
 * The choice is a *class on `<html>`*, because that is the whole mechanism the
 * tokens file needs: one `.dark` block of custom-property overrides (ADR 0047).
 * Nothing else in the app knows a dark theme exists.
 */

const THEME_KEY = "llmp.console.theme";

export type Theme = "light" | "dark";

export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
}

/** The stored preference, or the OS's if none was made. */
export function storedTheme(): Theme {
  try {
    const value = window.localStorage.getItem(THEME_KEY);
    if (value === "light" || value === "dark") return value;
  } catch {
    // Not sticking across reloads is a small loss; failing the read is not.
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/**
 * Apply the theme before the first paint this app controls.
 *
 * CSP is `script-src 'self'` with no `'unsafe-inline'`, so there is no
 * pre-hydration script in index.html to do this earlier; a reader who chose
 * dark may see the light ground for the few milliseconds before React mounts.
 * Accepted rather than worked around: loosening the CSP to remove a sub-second
 * flash on one page is the wrong trade.
 */
export function initTheme(): void {
  applyTheme(storedTheme());
}

export function rememberTheme(theme: Theme): void {
  try {
    window.localStorage.setItem(THEME_KEY, theme);
  } catch {
    // See `storedTheme`.
  }
}
