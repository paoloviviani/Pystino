/**
 * Shared test plumbing.
 *
 * Only what more than one test file needs, and only where getting it wrong in
 * one file would make that file agree with a screen that is broken.
 */

/**
 * A JSON response in the shape the gateway actually sends.
 *
 * Listing endpoints answer with a pagination envelope. Fixtures stay as plain
 * arrays — that is what a test is about — and are wrapped here, so the envelope
 * is described once rather than copied into every mock. A non-array payload is
 * passed through untouched, which is what a single-object route returns.
 */
export function jsonResponse(payload: unknown, status = 200): Response {
  const body = Array.isArray(payload)
    ? { items: payload, total: payload.length, limit: 50, offset: 0 }
    : payload;
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/**
 * Choose an item from a table row's "⋯" menu, as a person does: open the
 * row's menu, then pick the item. `row` is the name the menu button was given
 * ("Actions for <row>"); without it, the first row's menu is used.
 */
export async function chooseRowAction(
  user: { click: (element: Element) => Promise<void> },
  item: string | RegExp,
  row?: string | RegExp,
): Promise<void> {
  const { screen } = await import("@testing-library/react");
  const triggers = await screen.findAllByRole("button", {
    name: row === undefined ? /^Actions for / : typeof row === "string" ? `Actions for ${row}` : row,
  });
  await user.click(triggers[0]!);
  await user.click(await screen.findByRole("menuitem", { name: item }));
}
