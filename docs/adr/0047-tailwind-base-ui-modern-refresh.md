# 0047 — A modern design language on Tailwind CSS v4 and Base UI

- Date: 2026-09-01
- Status: **accepted, being built**
- Supersedes the **styling-toolchain stance** of [0023](0023-admin-console.md)
  ("CSS Modules and custom properties, not a utility framework") and
  [0042](0042-material-design-3.md) ("no Tailwind by decision"). It does **not**
  supersede 0042's readability judgements — they carry over; what changes is the
  design language they are expressed in and the machinery that expresses it.
- Requested as: "The UI was created in a bit artisanal way. I want to adopt a
  modern UI framework and make it good looking and modern."

## Context

The console's primitives have been hand-built since 0023: fifteen components in
`packages/ui`, CSS Modules beside each, one `tokens.css` owning the look. Two
restyles (Bauhaus, then MD3) each landed as a change to that one file plus a
couple of components — the token discipline works, and nothing here argues
against it.

What the hand-built layer does not give, and after two design passes still does
not have, is the **interaction layer**: a menu, a combobox, a toast, a tooltip,
a skeleton. The user menu in the Shell is a hand-rolled disclosure with
document-level listeners; a select is the native element; errors are inline
paragraphs because there is nowhere else to put them. Each of those is exactly
the "a few hundred lines and a bug" that the Dialog component's own comment
warns about, and each is solved, tested and accessible in headless component
libraries.

The decision this ADR replaces had one reason: components carrying Tailwind
classes force every future consumer to run Tailwind, and CSS Modules left the
Phase 3 chat toolchain open. The chat application is no longer a plan of record
(ADR 0016 stays on its branch; the operator has stated it may never happen), so
"keep the toolchain open for a hypothetical second consumer" is now weighting a
guess above a real deficit. The operator also asked for a dark theme, which the
hand-rolled layer would grow one module at a time.

## Options considered

| | Look ownership | Behaviour layer | Licence | Notes |
|---|---|---|---|---|
| **react-admin** | MUI's, theming away the default is the known "default is dull" trap | full framework + its own dataProvider | MIT core, paid EE (open-core vendor) | Material Design by construction; wrong look, and ADR 0001 friction |
| **Refine headless** | ours (headless core) | framework conventions on React Query | MIT | Fits, but tenancy in a framework grammar for plumbing the console already owns and tests |
| **Mantine 8** | the library's, overridden | included | MIT | Fast to a nice screen; every divergence becomes an override against a moving baseline |
| **Tailwind v4 + Base UI, owned in `@llmp/ui`** | **ours — tokens file, no baseline** | headless, a11y-tested | MIT / MIT | Styling labour is ours; accepted (the labour is the author's) |
| Polish in place | ours | still hand-rolled | — | Restyles again without buying the interaction layer |

Framework-shaped options were rejected for the reason above: their benefit
concentrates in uniform CRUD over resources, which is the *smaller* half of this
console, and their cost — permanent conventions and an upgrade cycle — is paid
everywhere. The structure they would impose is instead captured as owned
patterns (list page, form page) built on the primitives.

## Decision

**Tailwind CSS v4 as the styling engine, Base UI as the headless behaviour
layer, components still owned and exported by `@llmp/ui` with their public
props unchanged, and a new neutral design language with a dark theme.**

Verified at source on 2026-09-01, not from memory:

- `tailwindcss` **4.3.3** and `@tailwindcss/vite` **4.3.3** — MIT; the Vite
  plugin's peer range is `vite ^5.2.0 || ^6 || ^7 || ^8`, and the console runs
  Vite 8.
- **`@base-ui/react` 1.7.0** — MIT (MUI team); React peer `^17 || ^18 || ^19`.
  The former package name `@base-ui-components/react` is **deprecated at
  1.0.0-rc.0** ("Package was renamed to @base-ui/react") — the old name must not
  be installed.
- Roboto remains vendored under the Apache License 2.0 (unchanged from 0042).

### The new design language

A neutral greyscale with a single indigo accent, in the contemporary
"product console" idiom — tonal surfaces, 1px borders, restrained shadows, pill
buttons kept. 0042's readability rules are retained **as requirements, not as
palette values**: sentence-case labels, tabular figures for numbers, no blur,
no glass, no glow, no scale-on-hover, soft ink that clears 4.5:1 in **both**
themes, green/red as the ok/danger pair. What changes is the palette those
rules are met with (zinc neutrals, indigo accent, self-hosted Roboto kept).

### Dark theme

One `.dark` block of token overrides in the same file — the "one block away"
that `tokens.css` promised when it declined the dark theme. The reader's choice
is remembered in `localStorage`, defaults to following `prefers-color-scheme`,
and `color-scheme` follows so form controls and scrollbars match. The inversion
0042 records (tinted page, white cards) is kept in light; in the dark theme it
flips naturally (near-black page, raised cards), which is the same rule —
figures on the cleaner ground.

### How the toolchain reversal is contained

- **Tokens stay plain custom properties.** `tokens.css` still defines every
  value in `:root` / `.dark` and remains importable by a consumer with no
  Tailwind at all — the `@theme inline` block and the `dark` custom variant are
  Tailwind at-rules that a plain-CSS consumer simply ignores. The one-file
  property of the look is preserved.
- **The components' classes are Tailwind's.** That is the accepted coupling: a
  consumer of the *components* now needs the Tailwind build. In this repo both
  consumers compile through Vite and the package stays source-only, so the cost
  is one Vite plugin line and one CSS import — not a build step in `packages/ui`.
- **Tailwind's content detection scans the repo from its git root**, so classes
  used inside `packages/ui/src` are picked up by the console's build with no
  content list to maintain.
- CSP and the no-CDN rule are untouched: Tailwind emits ordinary stylesheets
  through Vite (`cssCodeSplit` stays on), fonts stay self-hosted, and no
  inline styles are introduced.

### What is kept, unchanged

- **`Money`, `formatMoney`, `MoneyPrecision`** — domain logic, and the
  string-end-to-end rule (CLAUDE.md) that no display refactor may touch.
- **Public props of every primitive.** Routes swap styling, not signatures;
  tests assert behaviour, not class names.
- The native `<select>` for simple option lists (styled now), because it is the
  best mobile and assistive-technology control for that job; Base UI's Select
  and Combobox exist as primitives for when a screen needs searchable or
  rich options.
- Accessibility wiring the components already do themselves: `aria-busy`,
  `aria-describedby`, `role="meter"`, the labelled-by-construction fields.

## Consequences

- **CSS Modules leave the package.** The fifteen `.module.css` files are
  deleted; `css-modules.d.ts` in `packages/ui` goes with them. Route-level
  modules migrate during the one-pass restyle and are deleted as each route
  moves.
- **The Dialog test shim in the console's `test-setup.ts` is retired** — it
  existed to prop up the native `<dialog>` under jsdom; Base UI's Dialog brings
  its own behaviour and needs no shim. New primitives (Menu, Toast) get a
  `renderIntoContainer` convention for tests rather than per-file mocks.
- **New primitives join the package**: Menu (replaces the hand-rolled user
  menu), Combobox, Tooltip, Toast, Skeleton. They exist because screens needed
  them, not speculatively — same rule as 0023's "nothing arrives until a real
  screen needs it", which still holds.
- **Every future screen is styled in utilities against tokens.** A literal
  colour/size in a component is now a *bigger* sin than before: it bypasses both
  the token file and the theme mapping in one step.
- Upgrading Tailwind or Base UI can move the compiled output; the look itself
  moves only when the tokens file does. That is the trade 0042 made in reverse,
  and it is the same bargain with the dependency doing what the hand-rolled
  layer did.
- The dark theme doubles the palette surface that must keep its contrast floor;
  both blocks are checked together when either changes.
