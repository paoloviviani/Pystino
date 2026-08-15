import "@testing-library/jest-dom/vitest";

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
