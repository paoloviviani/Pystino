import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import type { RedactionPolicy } from "../lib/types";
import { PolicyFields, seededPolicy } from "./PolicyFields";

/**
 * The policy form in isolation: what each section shows for a policy, and what
 * each control writes back. The page's own tests cover it wired to the API.
 */

function Harness({
  initial = seededPolicy(),
  ...props
}: Partial<React.ComponentProps<typeof PolicyFields>> & { initial?: RedactionPolicy }) {
  const [policy, setPolicy] = useState(initial);
  return (
    <>
      <PolicyFields
        policy={policy}
        onChange={setPolicy}
        entityTypes={["PERSON", "EMAIL_ADDRESS", "URL"]}
        {...props}
      />
      <output data-testid="policy">{JSON.stringify(policy)}</output>
    </>
  );
}

const written = (): RedactionPolicy =>
  JSON.parse(screen.getByTestId("policy").textContent ?? "{}") as RedactionPolicy;

describe("PolicyFields", () => {
  it("lists the sections in the order people think, with plain headings", () => {
    render(<Harness />);
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual([
      "What to hide",
      "Your own patterns",
      "Never hide",
    ]);
  });

  it("changes the default action from a segmented control", async () => {
    const user = userEvent.setup({ delay: null });
    render(<Harness />);

    const control = screen.getByRole("group", { name: "Default action" });
    expect(within(control).getAllByRole("radio")).toHaveLength(5);
    await user.click(within(control).getByRole("radio", { name: "Block" }));
    expect(written().default_mode).toBe("block");
  });

  it("shows a row for each type the policy names, in plain words with the id kept", () => {
    render(
      <Harness
        initial={{
          ...seededPolicy(),
          entities: { EMAIL_ADDRESS: { mode: "block", threshold: 0.7 } },
        }}
      />,
    );

    expect(screen.getByText("Email address")).toBeInTheDocument();
    expect(screen.getByText("EMAIL_ADDRESS")).toBeInTheDocument();
    const row = screen.getByRole("group", { name: /EMAIL_ADDRESS/ });
    expect(within(row).getByRole("radio", { name: "Block" })).toBeChecked();
    expect(screen.getByLabelText("Confidence for Email address")).toHaveValue(0.7);
    // The other types are not rows; they are on offer.
    expect(screen.queryByRole("group", { name: /PERSON/ })).not.toBeInTheDocument();
  });

  it("starts an added type one step above the default and removes it again", async () => {
    const user = userEvent.setup({ delay: null });
    render(<Harness />);

    await user.selectOptions(screen.getByLabelText("Add a kind of data"), "URL");
    expect(written().entities).toEqual({ URL: { mode: "redact", threshold: null } });

    await user.click(screen.getByRole("button", { name: "Reset Web address to the default" }));
    expect(written().entities).toEqual({});
  });

  it("starts an added type at Block when the default is already Redact", async () => {
    const user = userEvent.setup({ delay: null });
    render(<Harness initial={{ ...seededPolicy(), default_mode: "redact" }} />);

    await user.selectOptions(screen.getByLabelText("Add a kind of data"), "PERSON");
    expect(written().entities.PERSON?.mode).toBe("block");
  });

  it("disables the modes below a floor rather than hiding them", () => {
    render(
      <Harness
        initial={{ ...seededPolicy(), entities: { PERSON: { mode: "block", threshold: null } } }}
        floor={{ ...seededPolicy(), default_mode: "redact" }}
      />,
    );

    const row = screen.getByRole("group", { name: /PERSON/ });
    expect(within(row).getByRole("radio", { name: "Off" })).toBeDisabled();
    expect(within(row).getByRole("radio", { name: "Redact" })).toBeEnabled();
  });

  it("is only a heading and two buttons while there are no patterns", async () => {
    const user = userEvent.setup({ delay: null });
    render(<Harness />);

    expect(screen.queryByLabelText("Pattern 1 name")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add pattern" }));
    expect(screen.getByLabelText("Pattern 1 name")).toBeInTheDocument();
    expect(written().patterns).toEqual([{ name: "", regex: "", mode: "redact" }]);

    await user.click(screen.getByRole("button", { name: "Remove" }));
    expect(screen.queryByLabelText("Pattern 1 name")).not.toBeInTheDocument();
  });

  it("parses the allowlist as it is typed", async () => {
    const user = userEvent.setup({ delay: null });
    render(<Harness />);

    await user.type(screen.getByLabelText("Allowlist"), "ilpost.it, example.org");
    expect(written().allow_list).toEqual(["ilpost.it", "example.org"]);
  });

  it("leaves the allowlist out where the API refuses one", () => {
    render(<Harness allowList={false} />);
    expect(screen.queryByText("Never hide")).not.toBeInTheDocument();
  });

  describe("placeholder style", () => {
    it("is offered for a type that writes a token, and written back when changed", async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <Harness
          initial={{
            ...seededPolicy(),
            entities: { EMAIL_ADDRESS: { mode: "anonymise_restore", threshold: null } },
          }}
        />,
      );

      const select = screen.getByLabelText("Placeholder for Email address");
      expect(select).toHaveValue("tagged");
      await user.selectOptions(select, "same_length");
      expect(written().entities.EMAIL_ADDRESS).toMatchObject({
        mode: "anonymise_restore",
        placeholder: "same_length",
      });
    });

    it("is not offered where there is no token to shape", () => {
      render(
        <Harness
          initial={{
            ...seededPolicy(),
            entities: {
              EMAIL_ADDRESS: { mode: "redact", threshold: null },
              URL: { mode: "block", threshold: null },
            },
          }}
        />,
      );
      expect(screen.queryByLabelText(/^Placeholder for/)).not.toBeInTheDocument();
    });

    it("keeps the choice when the mode changes", async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <Harness
          initial={{
            ...seededPolicy(),
            entities: {
              EMAIL_ADDRESS: {
                mode: "anonymise_restore",
                threshold: null,
                placeholder: "same_length",
              },
            },
          }}
        />,
      );
      const row = screen.getByRole("group", { name: /EMAIL_ADDRESS/ });
      await user.click(within(row).getByRole("radio", { name: "Block" }));
      expect(written().entities.EMAIL_ADDRESS?.placeholder).toBe("same_length");
    });

    it("can be chosen for a custom pattern", async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <Harness
          initial={{
            ...seededPolicy(),
            patterns: [
              { name: "BADGE", regex: "B-[0-9]+", mode: "anonymise_restore", placeholder: "tagged" },
            ],
          }}
        />,
      );
      await user.selectOptions(screen.getByLabelText("Pattern 1 placeholder"), "same_length");
      expect(written().patterns[0]?.placeholder).toBe("same_length");
    });
  });
});
