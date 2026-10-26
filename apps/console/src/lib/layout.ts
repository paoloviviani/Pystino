/**
 * The console's layout vocabulary, as Tailwind class strings.
 *
 * This replaces `Admin.module.css` and `Overview.module.css` (ADR 0047): the
 * same handful of page shapes — a column of cards, an auto-fit filter row, a
 * definition list, an engine card — kept in one place so the fifteen routes
 * cannot drift apart. Every value is a token (via the `@theme inline` mapping
 * in `@llmp/ui`'s tokens.css) or a structural quantity; a literal colour here
 * would bypass both the token file and the dark theme in one step.
 *
 * Grids use `auto-fit` rather than a fixed column count so filters and stats
 * sit in a row on a laptop and stack on a phone without a media query per
 * breakpoint. The one breakpoint that is written out is `max-[40rem]`, the
 * width the CSS Modules this replaces used to break at.
 */

/** The page column: a stack of cards. */
export const PAGE = "flex flex-col gap-5";

/** Filters / pickers above a listing. */
export const FILTERS =
  "grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(11rem,1fr))]";

/** Headline figures. */
export const STATS =
  "grid gap-5 [grid-template-columns:repeat(auto-fit,minmax(12rem,1fr))]";

/** The report's API-worded caveats, stacked under the figures. */
export const DISCLOSURES = "mt-5 flex flex-col gap-2";

/** A form. */
export const FORM = "flex flex-col gap-4";

/** Several fields on one row, stacking when there is no room. */
export const FORM_ROW =
  "grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(10rem,1fr))]";

/** Buttons at the end of a row (a dialog footer, a table's action column). */
export const ROW_ACTIONS = "flex items-center justify-end gap-2";

/**
 * Inline code, request ids, tokens: monospace at the size of the text around it. A long value
 * (a redirect URI, an id) breaks anywhere rather than running off a phone screen; a value that
 * fits is never broken.
 */
export const CODE = "font-mono [overflow-wrap:anywhere]";

/** Muted prose inside a cell ("—" for not applicable). */
export const MUTED = "text-ink-muted";

/** A run of small badges. */
export const CHIPS = "flex flex-wrap gap-1";

/**
 * A definition list for a read-only configuration: label on the left, value on
 * the right, collapsing to stacked pairs where there is no room for two
 * columns. `dl` rather than a table because these are not rows of like things —
 * each pair is a different kind of fact.
 */
export const DETAILS =
  "grid items-baseline gap-x-5 gap-y-2 [grid-template-columns:minmax(10rem,max-content)_1fr] " +
  "max-[40rem]:grid-cols-1 max-[40rem]:gap-x-0 max-[40rem]:gap-y-1";

export const DETAIL_LABEL =
  "text-dense font-medium tracking-[0.01em] text-ink-muted";

export const DETAIL_VALUE =
  "[overflow-wrap:anywhere] max-[40rem]:[&:not(:last-child)]:mb-3";

/** A checkbox grid for granting a model to groups. */
export const CHECK_LIST =
  "grid gap-2 [grid-template-columns:repeat(auto-fill,minmax(11rem,1fr))]";

export const CHECK_ITEM =
  "flex cursor-pointer items-center gap-2 rounded-md border border-line p-2 transition-colors hover:bg-sunken";

/**
 * The administration landing page. Two columns on a laptop, one on a phone —
 * six cards in three columns read as a toolbar rather than a set of places.
 */
export const SECTIONS =
  "grid gap-3 [grid-template-columns:repeat(auto-fit,minmax(20rem,1fr))]";

/**
 * A whole card that is one link. `flex` rather than `block` so the card fills
 * the link; the hover lands on the card, not the link — the card paints its own
 * surface, so a colour change underneath it does nothing at all.
 */
export const SECTION_LINK =
  "flex text-ink no-underline [&>*]:flex-1 [&>*]:transition-colors " +
  "hover:[&>*]:border-accent hover:[&>*]:shadow-sm " +
  "focus-visible:outline-none focus-visible:[&>*]:shadow-focus";

/** One category of the administration landing page: its label and its cards. */
export const SECTION_GROUP = "flex flex-col gap-3";

/**
 * The category label itself. Small caps in faint ink — a signpost, not a
 * heading that wants to compete with the card titles under it.
 */
export const SECTION_GROUP_HEADING =
  "text-small font-medium uppercase tracking-[0.08em] text-ink-faint";

/* -- the sidebar ----------------------------------------------------------- */

/**
 * A navigation row in the sidebar: icon and label on one line. The collapsed
 * rail re-dresses the same row as a centred icon button (the label is then on
 * a tooltip, and the narrow width is Shell's to apply), so one element serves
 * both widths and a test finds exactly one link per screen.
 */
export const SIDEBAR_LINK =
  "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-control font-medium tracking-[0.01em] no-underline " +
  "text-ink-muted transition-colors hover:bg-sunken hover:text-ink " +
  "focus-visible:outline-none focus-visible:shadow-focus";

export const SIDEBAR_LINK_ACTIVE =
  "bg-accent-subtle text-accent hover:bg-accent-subtle hover:text-accent";

/** Desktop-only: the rail centres the icon and drops the row's padding. */
export const SIDEBAR_LINK_COLLAPSED = "min-[40rem]:justify-center min-[40rem]:px-0";

/**
 * The category label in the sidebar — the same voice as the landing page's
 * group headings (SECTION_GROUP_HEADING), because the two are meant to read as
 * one vocabulary, not two.
 */
export const SIDEBAR_GROUP = SECTION_GROUP_HEADING;

/* -- the redaction engine list ------------------------------------------- */

/**
 * A list of choices rather than a table: each row carries a paragraph of
 * description and one action, which is the shape a table renders badly. Stacked
 * on a narrow screen: the action under the description reads better than a
 * button squeezed against the right edge.
 */
export const ENGINE =
  "flex items-start justify-between gap-4 rounded-md border border-line bg-surface p-4 " +
  "max-[40rem]:flex-col max-[40rem]:items-stretch";

/** The engine in force is the one fact this list must make unmissable. */
export const ENGINE_ACTIVE =
  "border-accent [box-shadow:inset_3px_0_0_0_var(--colour-accent)]";

export const ENGINE_BODY = "flex min-w-0 flex-col gap-1";
export const ENGINE_NAME = "flex flex-wrap items-center gap-2 font-semibold";
export const ENGINE_ACTION = "shrink-0";

/** The engine's description and notes: a compact list, so the dense step. */
export const ENGINE_TEXT = "text-dense text-ink-muted";

/**
 * Short labels that must not break mid-phrase. "per 24 / hours" split across
 * two lines reads as a layout fault rather than as prose; a long *name*
 * wrapping does not, which is why this is opt-in per cell rather than set on
 * the table.
 */
export const NOWRAP = "whitespace-nowrap";

/* -- the preview box (redaction rule editor) ------------------------------ */

/**
 * A labelled block that is not one of the `@llmp/ui` fields: the sample is a
 * textarea and the result is a paragraph, and neither belongs in that package
 * until a second screen needs one. The label is deliberately identical to the
 * field labels in `@llmp/ui`, so the sample box reads as one more control
 * rather than as a section of its own.
 */
export const FIELD = "flex flex-col gap-1";
export const FIELD_LABEL = "text-dense font-medium tracking-[0.01em] text-ink-muted";

export const TEXTAREA =
  "w-full resize-y rounded-md border border-line bg-surface px-3 py-2 text-body max-[40rem]:text-[16px] " +
  "transition-shadow hover:border-line-strong focus-visible:outline-none focus-visible:shadow-focus";

/** The rewritten prompt: monospace, pre-wrap — the substitutions are the point. */
export const SAMPLE =
  "rounded-md border border-line bg-sunken p-3 font-mono whitespace-pre-wrap [overflow-wrap:anywhere]";

/* -- Overview -------------------------------------------------------------- */

/** The minted API-key secret: a long unbroken token, readable on a phone. */
export const SECRET_ROW = "flex flex-wrap items-center gap-3";
export const SECRET =
  "min-w-48 flex-1 rounded-md border border-line bg-sunken p-3 font-mono [overflow-wrap:anywhere]";
export const SECRET_DETAIL = "text-dense text-ink-muted";

/** Quotas, one per row: a meter is read left to right, and two side by side
 * invite a comparison between different metrics that does not mean anything. */
export const QUOTA_LIST = "grid gap-4";
export const QUOTA = "grid gap-1";
export const QUOTA_HEAD = "flex flex-wrap items-center gap-2";
export const QUOTA_NAME = "font-medium";
export const QUOTA_DETAIL = "m-0 text-dense text-ink-muted";

/* -- Login ------------------------------------------------------------------ */

/**
 * The sign-in door both products send people to (the chat's OAuth flow lands
 * here when there is no session). Drawn as a card in the chat's idiom —
 * rounded-2xl, quiet border, raised surface — rather than bare form fields on
 * the page ground, so the first screen a chat user ever sees already reads as
 * the same product. Shared with the password-reset screens, which are the
 * same door from the other side.
 */
export const LOGIN_CENTRE = "flex min-h-[60vh] items-center justify-center p-5";
export const LOGIN_CARD =
  "flex w-96 max-w-full flex-col gap-4 rounded-2xl border border-line-quiet bg-surface p-6 shadow-sm";
export const FORM_STACK = "flex flex-col gap-4";

/* -- page width -------------------------------------------------------------- */

/**
 * Listing and reporting screens (usage, summaries), which opt in to the wide layout. A table of six or seven
 * columns plus its actions wants more than the 76rem that keeps a form's lines
 * readable, so on a wide screen these routes raise `--layout-max-width` (the
 * header and the content both read it, so they stay aligned). Forms, settings
 * and detail pages are not listed and keep the default.
 */
const WIDE_ROUTES = new Set([
  "/",
  "/reports",
  "/requests",
  "/admin",
  "/admin/reports",
  "/admin/models",
  "/admin/routing",
  "/admin/providers",
  "/admin/search",
  "/admin/groups",
  "/admin/quotas",
  "/admin/redaction",
  "/admin/requests",
  "/admin/audit",
  "/admin/users",
]);

/** The width cap for listing screens, in place of the token's default. */
export const WIDE_LAYOUT_MAX_WIDTH = "104rem";

/**
 * One rule's page (new, edit, clone) is not a listing, but it is wide for the
 * listing's reason: the rule form sits beside a summary and a test box, and two
 * columns need more than the 76rem that keeps a single column's lines readable.
 */
const WIDE_PATTERNS = [/^\/admin\/redaction\/rules\/[^/]+$/];

export function isWideRoute(pathname: string): boolean {
  const path = pathname.replace(/\/+$/, "") || "/";
  return WIDE_ROUTES.has(path) || WIDE_PATTERNS.some((pattern) => pattern.test(path));
}

/**
 * Secondary text in a table cell — a description, a URL, an upstream name — on
 * one line, cut with an ellipsis. Pair it with a `title` carrying the full
 * text: the cell stays one line tall and nothing is lost to a mouse or a
 * screen reader. The cap is what lets `truncate` work in an auto-width table,
 * where a cell otherwise grows to fit its longest line.
 */
export const CLAMP = "block max-w-[min(26rem,70vw)] truncate";

/** A short fingerprint of a secret: one line, never broken mid-token. */
export const FINGERPRINT = "whitespace-nowrap font-mono";

/** The tail of a key hint ("sk-a…p4dA" -> "…p4dA"); the full hint is the title. */
export function shortHint(hint: string | null | undefined): string {
  if (!hint) return "";
  const at = hint.indexOf("\u2026");
  return at >= 0 ? hint.slice(at) : hint.length > 5 ? `\u2026${hint.slice(-4)}` : hint;
}
