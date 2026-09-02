import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

/**
 * `waitFor` gets a little longer than its 1s default.
 *
 * The search boxes debounce by 250ms before they fetch, so a test that types
 * and then waits for the request has already spent a quarter of the budget
 * before anything happens. Two seconds leaves room for that without hiding a
 * genuinely stuck query.
 *
 * This is a ceiling, not a delay: it costs nothing when tests pass. The reason
 * the search tests are fast at all is `userEvent.setup({ delay: null })` at
 * their call sites — userEvent's default waits a real tick between keystrokes,
 * which against a fifty-row table turns one typed word into a minute and a half
 * of re-rendering.
 */
configure({ asyncUtilTimeout: 2000 });

/* The `<dialog>` shim that used to live here is gone with the element: the
 * Dialog primitive is Base UI's now (ADR 0047), which renders ordinary DOM in
 * a portal and needs no jsdom prop to open. */
