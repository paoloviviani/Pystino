import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Table } from "@llmp/ui";
import { AboutServer } from "./AboutServer";

describe("AboutServer", () => {
  it("keeps developer detail closed until it is opened", async () => {
    render(
      <AboutServer>
        <p>Console build abc1234</p>
      </AboutServer>,
    );
    const details = screen.getByText("About this server").closest("details");
    expect(details).not.toBeNull();
    expect(details?.open).toBe(false);
    await userEvent.click(screen.getByText("About this server"));
    expect(details?.open).toBe(true);
    expect(screen.getByText("Console build abc1234")).toBeVisible();
  });
});

describe("Table stack", () => {
  const columns = [
    { key: "name", header: "Name", render: (row: { name: string }) => row.name },
    { key: "act", header: "", label: "", render: () => <button>Edit</button> },
  ];

  it("labels each cell so a phone can draw the row as a card", () => {
    render(
      <Table stack columns={columns} rows={[{ name: "Ada" }]} rowKey={(r) => r.name} />,
    );
    const cell = screen.getByText("Ada").closest("td");
    expect(cell?.getAttribute("data-label")).toBe("Name");
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });

  it("stays a plain table without the flag", () => {
    render(<Table columns={columns} rows={[{ name: "Ada" }]} rowKey={(r) => r.name} />);
    expect(screen.getByText("Ada").closest("td")?.hasAttribute("data-label")).toBe(false);
  });
});
