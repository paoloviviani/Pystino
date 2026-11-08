import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import type { PrivacyAction, PrivacyCondition, PrivacyKind, PrivacyProfile } from "../lib/types";
import { PrivacyRuleBuilder } from "./PrivacyRuleBuilder";

/**
 * The sentence builder: only what is valid for the kind is offered, a chip
 * opens its own editor, and a content condition says how it is decided.
 */

const MEDICAL = {
  id: "p-medical",
  name: "Medical",
  engine: "gliner",
  status: "not_installed",
  decides: false,
  cost_hint: "slow",
  fallback: "refuse",
} as unknown as PrivacyProfile;

function Harness({
  kind,
  initial = [],
  initialAction,
  onChange,
}: {
  kind: PrivacyKind;
  initial?: PrivacyCondition[];
  initialAction?: PrivacyAction;
  onChange?: (state: { conditions: PrivacyCondition[]; action: PrivacyAction }) => void;
}) {
  const [conditions, setConditions] = useState(initial);
  const [action, setAction] = useState<PrivacyAction>(
    initialAction ?? (kind === "read" ? { type: "reader", reader: "markitdown" } : { type: "refuse" }),
  );
  onChange?.({ conditions, action });
  return (
    <PrivacyRuleBuilder
      kind={kind}
      conditions={conditions}
      action={action}
      onConditions={setConditions}
      onAction={setAction}
      profiles={[MEDICAL]}
      groups={["Clinic", "Staff"]}
    />
  );
}

function optionsOf(label: string | RegExp): string[] {
  return within(screen.getByLabelText(label)).getAllByRole("option").map((o) => o.textContent ?? "");
}

describe("the rule builder", () => {
  it("offers only the conditions and actions of the kind", () => {
    const { unmount } = render(<Harness kind="read" />);
    expect(optionsOf("Add a condition")).toEqual(["Add a condition", "File type", "File name", "Who sent it"]);
    expect(optionsOf("Action")).toEqual(["Choose the reader", "refuse to read it"]);
    unmount();

    render(<Harness kind="embed" />);
    expect(optionsOf("Add a condition")).toEqual([
      "Add a condition",
      "What it contains",
      "Language",
      "Who sent it",
      "Where it is going",
    ]);
    expect(optionsOf("Action")).toEqual(["refuse to embed it", "embed it with a local embedding model"]);
  });

  it("adds a condition, opens it and lets the values and is-not be changed", async () => {
    const user = userEvent.setup();
    let latest: PrivacyCondition[] = [];
    render(<Harness kind="read" onChange={(s) => (latest = s.conditions)} />);

    await user.selectOptions(screen.getByLabelText("Add a condition"), "filetype");
    // The new chip reads as part of the sentence, and its editor is open.
    expect(screen.getByRole("button", { name: "it is a PDF" })).toHaveAttribute("aria-expanded", "true");

    await user.click(screen.getByLabelText("a Word file"));
    await user.selectOptions(screen.getByLabelText("File type"), "not");
    expect(latest).toEqual([{ key: "filetype", values: ["pdf", "word"], negate: true }]);
    expect(screen.getByRole("button", { name: "it is not a PDF or a Word file" })).toBeInTheDocument();
  });

  it("takes file name patterns one per line", async () => {
    const user = userEvent.setup();
    let latest: PrivacyCondition[] = [];
    render(
      <Harness
        kind="read"
        initial={[{ key: "filename", values: ["referto*"], negate: false }]}
        onChange={(s) => (latest = s.conditions)}
      />,
    );
    await user.click(screen.getByRole("button", { name: "its name matches referto*" }));
    const box = screen.getByLabelText("File name patterns, one per line");
    await user.clear(box);
    await user.type(box, "referto*{enter}esame*");
    expect(latest[0]?.values).toEqual(["referto*", "esame*"]);
  });

  it("offers the groups, and a typed user or provider, for who and where", async () => {
    const user = userEvent.setup();
    let latest: PrivacyCondition[] = [];
    render(
      <Harness
        kind="embed"
        initial={[{ key: "who", values: [], negate: false }]}
        onChange={(s) => (latest = s.conditions)}
      />,
    );
    await user.click(screen.getByRole("button", { name: "it is from" }));
    await user.click(screen.getByLabelText("group Clinic"));
    await user.type(screen.getByLabelText("Add a user's email, or group:Name"), "ana@example.org{enter}");
    expect(latest[0]?.values).toEqual(["group:Clinic", "user:ana@example.org"]);
  });

  it("says how a content condition is decided, and what an unavailable model does", async () => {
    const user = userEvent.setup();
    let latest: PrivacyCondition[] = [];
    render(<Harness kind="embed" onChange={(s) => (latest = s.conditions)} />);

    await user.selectOptions(screen.getByLabelText("Add a condition"), "verdict");
    // The default is the quick check.
    expect(latest[0]).toMatchObject({ key: "verdict", values: ["health"], decide: "patterns" });
    expect(screen.getByLabelText("How to decide")).toHaveValue("patterns");
    expect(screen.getByText(/can miss plain wording/)).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("How to decide"), "p-medical");
    expect(latest[0]?.decide).toBe("p-medical");
    expect(screen.getByText(/Medical is not available now, so the rule takes its protective action/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /decided by Medical/ })).toBeInTheDocument();
  });

  it("does not offer how-to-decide for a file condition", async () => {
    const user = userEvent.setup();
    render(
      <Harness kind="read" initial={[{ key: "filetype", values: ["pdf"], negate: false }]} />,
    );
    await user.click(screen.getByRole("button", { name: "it is a PDF" }));
    expect(screen.queryByLabelText("How to decide")).toBeNull();
  });

  it("removes a condition", async () => {
    const user = userEvent.setup();
    render(<Harness kind="read" initial={[{ key: "filetype", values: ["pdf"], negate: false }]} />);
    await user.click(screen.getByRole("button", { name: "it is a PDF" }));
    await user.click(screen.getByRole("button", { name: "Remove this condition" }));
    expect(screen.queryByRole("button", { name: "it is a PDF" })).toBeNull();
    expect(screen.getByText(/No conditions: the rule applies to every request/)).toBeInTheDocument();
  });

  it("shows the reader choice only for a reader action", async () => {
    const user = userEvent.setup();
    let latest: PrivacyAction = { type: "refuse" };
    render(<Harness kind="read" onChange={(s) => (latest = s.action)} />);
    expect(screen.getByLabelText("Reader")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Reader"), "docling");
    expect(latest).toEqual({ type: "reader", reader: "docling" });
    await user.selectOptions(screen.getByLabelText("Action"), "refuse");
    expect(latest).toEqual({ type: "refuse" });
    expect(screen.queryByLabelText("Reader")).toBeNull();
  });
});
