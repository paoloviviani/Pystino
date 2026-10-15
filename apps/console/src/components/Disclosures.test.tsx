/**
 * The usage caveats, collapsed to one line.
 *
 * A long report shows several paragraphs above the numbers on every visit;
 * the summary names the count and carries the first note, and the choice
 * sticks per page. The notes themselves stay in the DOM either way — search
 * and assistive technology must not lose a caveat to a collapsed section —
 * so what is pinned is which half the accessibility tree sees.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Disclosures } from "./Disclosures";

const NOTE_ONE = "9 request(s) are charged from our price table: their provider reported none.";
const NOTE_TWO = "Searches through the gateway's own backends are counted and never priced.";

function renderNotes(notes: string[] = [NOTE_ONE, NOTE_TWO], storageKey = "disclosures-test") {
  return render(<Disclosures notes={notes} storageKey={storageKey} />);
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Disclosures", () => {
  it("collapses to one line by default: the count and the first note", () => {
    renderNotes();

    const toggle = screen.getByRole("button", { name: /2 notes about these figures/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    // The first note rides along after the count (the browser clips it to
    // the line; the text itself stays whole).
    expect(toggle.textContent).toContain(NOTE_ONE);
    // …and names the region it controls.
    const region = document.getElementById(toggle.getAttribute("aria-controls") ?? "");
    expect(region).not.toBeNull();
    expect(region).toHaveAttribute("hidden");
    // Hidden from assistive technology too: the summary is the one note the
    // tree sees until it opens.
    expect(screen.getAllByRole("note")).toHaveLength(1);
  });

  it("says note, singular, for one", () => {
    renderNotes([NOTE_ONE]);

    expect(
      screen.getByRole("button", { name: /1 note about these figures/ }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /notes about these figures/ })).toBeNull();
  });

  it("opens to the notes exactly as the API worded them", async () => {
    renderNotes();
    const user = userEvent.setup();
    const toggle = screen.getByRole("button", { name: /2 notes about these figures/ });

    await user.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const region = document.getElementById(toggle.getAttribute("aria-controls") ?? "");
    expect(region).not.toHaveAttribute("hidden");
    // The API's own wording, verbatim: restating a caveat is how the UI and
    // the API end up disagreeing about what the number means.
    const notes = screen.getAllByRole("note");
    expect(notes.map((note) => note.textContent)).toContain(NOTE_ONE);
    expect(notes.map((note) => note.textContent)).toContain(NOTE_TWO);
  });

  it("remembers being opened across a remount", async () => {
    const user = userEvent.setup();
    const first = renderNotes([NOTE_ONE, NOTE_TWO], "disclosures-remember-open");
    await user.click(screen.getByRole("button", { name: /2 notes/ }));
    expect(window.localStorage.getItem("disclosures-remember-open")).toBe("true");
    first.unmount();

    renderNotes([NOTE_ONE, NOTE_TWO], "disclosures-remember-open");

    expect(
      screen.getByRole("button", { name: /2 notes/ }),
    ).toHaveAttribute("aria-expanded", "true");
  });

  it("remembers being closed again across a remount", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem("disclosures-remember-closed", "true");
    const first = renderNotes([NOTE_ONE, NOTE_TWO], "disclosures-remember-closed");
    await user.click(screen.getByRole("button", { name: /2 notes/ }));
    expect(window.localStorage.getItem("disclosures-remember-closed")).toBe("false");
    first.unmount();

    renderNotes([NOTE_ONE, NOTE_TWO], "disclosures-remember-closed");

    expect(
      screen.getByRole("button", { name: /2 notes/ }),
    ).toHaveAttribute("aria-expanded", "false");
  });

  it("renders nothing with no disclosures", () => {
    const { container } = renderNotes([]);

    expect(container).toBeEmptyDOMElement();
  });

  it("renders and toggles when storage throws", async () => {
    // A browser with site data blocked throws on access rather than
    // returning null; a usage page that will not show its caveats because
    // it could not remember a display preference has it backwards.
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const user = userEvent.setup();
    renderNotes([NOTE_ONE, NOTE_TWO], "disclosures-no-storage");

    const toggle = screen.getByRole("button", { name: /2 notes/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
  });
});
