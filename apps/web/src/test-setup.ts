import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

/** Room for the streaming assertions, which wait on several ticks of SSE. */
configure({ asyncUtilTimeout: 2000 });

/**
 * jsdom implements neither of these, and assistant-ui's viewport uses both.
 *
 * Shimmed rather than worked around in the component: auto-scrolling a
 * transcript is exactly what a chat viewport is for, and a real browser has had
 * `ResizeObserver` since 2020. What is given up is only the behaviour jsdom
 * could not exercise anyway — nothing here has a layout to observe.
 */
if (!("ResizeObserver" in globalThis)) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {};
}

/* jsdom implements neither, and the thread viewport's auto-scroll calls both on
   every message. Unhandled, they fail the run while every test passes — which
   is the confusing shape of failure worth shimming away. */
if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function scrollTo() {};
}

if (!Element.prototype.scrollBy) {
  Element.prototype.scrollBy = function scrollBy() {};
}
