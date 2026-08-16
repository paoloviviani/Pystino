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
 * which against a fifty-row table turns one typed word into a minute and a
 * half of re-rendering.
 */
configure({ asyncUtilTimeout: 2000 });

/**
 * jsdom does not implement `<dialog>`'s modal methods (as of jsdom 30).
 *
 * Shimmed here rather than avoided in the components: the native element is the
 * right production choice — the browser supplies the focus trap, Escape to
 * close, inertness of the page behind, and the correct role for a screen reader,
 * all of which are otherwise a few hundred lines and a bug. Losing that to suit
 * a test environment would be the tail wagging the dog.
 *
 * What this shim gives up is exactly the behaviour jsdom cannot exercise anyway
 * (focus management, the backdrop). Open and close, which is what the tests
 * assert on, behave the same.
 */
const dialog = globalThis.HTMLDialogElement?.prototype;
if (dialog && typeof dialog.showModal !== "function") {
  dialog.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true;
  };
  dialog.show = function show(this: HTMLDialogElement) {
    this.open = true;
  };
  dialog.close = function close(this: HTMLDialogElement, returnValue?: string) {
    this.open = false;
    if (returnValue !== undefined) this.returnValue = returnValue;
    this.dispatchEvent(new Event("close"));
  };
}
