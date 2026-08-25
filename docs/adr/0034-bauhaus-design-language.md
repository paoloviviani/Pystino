# 0034 — Bauhaus as the design language, and a self-hosted geometric sans

- Date: 2026-08-24
- Status: **accepted, built**
- Requested as: "change the design language to something more bauhaus/mid-century,
  with bolder colors, and geometry" — grid systems, sans-serif, a red-blue-yellow
  palette, circles/triangles/squares, minimal text.
- Related: [0023](0023-admin-console.md) (the console, and the CSP the font had
  to fit), [0001](0001-licensing.md) (ground rule 1, which the font choice
  touches). There is no design-system ADR — the tokens' own reasoning has always
  lived in `packages/ui/src/tokens.css`, which is where it belongs and where this
  change mostly happened.

## Context

The console's look was deliberately quiet: cool off-white, slate text, one
steel-blue accent, 1px grey rules, soft shadows, rounded corners. It was chosen
for a page people read numbers off, "closer to a financial statement than to a
dashboard".

The request is a different register — functional modernism rather than corporate
neutrality. Which is a better fit than it first sounds: *form follows function* is
the argument this codebase already makes everywhere else, and a style whose colour
carries meaning suits a screen whose job is to say "this budget is over" more than
a style whose colour is brand decoration.

## Decision

### 1. Geometry is square, rules are 2px, nothing is shaded

`--radius-sm/md/lg` all become `0`. `--border-width` goes 1px → **2px** and
`--colour-border` goes light grey → near-black. `--shadow-sm/md` become `none`.

The radii and shadows are kept as tokens set to zero rather than deleted, because
every component references one of them; a component that hardcoded `0` would be a
component that could not be softened again.

`--radius-pill` stays a real radius for things that are genuinely circular — a
status dot, the spinner. **A circle is one of the three Bauhaus shapes; a rounded
rectangle is not.**

Two places the style is applied *functionally* rather than literally, and both
matter more than the rest of this list:

- **Table row lines stay 1px and grey** (`--border-width-quiet`,
  `--colour-border-quiet`). 2px of black under every row turns a listing into a
  grid of boxes and buries the data in its own scaffolding. The bold rules are
  reserved for separating *kinds* of thing: header from body, body from total.
- **Notices are a pale wash with a 5px coloured rule down the leading edge**, not
  a saturated panel. The first pass gave them the full cadmium yellow; four lines
  of prose on saturated yellow is hard to read, and a report whose disclosures all
  shout has no way left to say that one of them matters more.

Getting these two wrong is what "Bauhaus" looks like when it is applied as
decoration. Getting them right is the style's own argument.

### 2. Colour is a signal, on an achromatic ground

The three primaries are named as colours — `--colour-red`, `--colour-blue`,
`--colour-yellow` — because in this palette the colour is a fact about the design
and not only about a state: the brand mark uses all three, and a component that
wants "the yellow" should not have to ask for "the warning" to get it.

They are poster values rather than sRGB corners. Pure `#ff0000` and `#0000ff` are
screen artefacts, not pigments, and they vibrate against each other. Vermilion,
ultramarine, cadmium — what the printers of the period actually had.

The ground is **neutral grey, not paper-warm**. Period stock was warm and a warm
ground was tried here once before, on the theory that figures read calmer on
paper; in use it read as beige and was rejected. Grey is the right call twice
over — it also keeps the primaries at full strength, where a saturated red against
beige goes muddy.

Colour appears where it has a job and nowhere else. The nav is white with a black
rule and the current section is *underscored*, not filled: a filled tab is a block
of colour in the chrome, and a console whose chrome shouts is one where a red
warning does not.

### 3. There is no green, and that is a real loss

Bauhaus has three primaries and green is not one of them, so `ok` becomes the
blue. Worth stating plainly rather than presenting as a win: **green/red is the
most universally read pair in any interface, and blue/red is weaker.** A colourblind
reader is better served by blue/red than by green/red, which is a genuine gain, but
the everyday legibility of "green means fine" is gone.

What is gained is that one hue means "fine" everywhere — Active, a healthy service,
a budget in hand — and red and yellow keep all of their force for the two states an
operator scanning a quota page is looking for.

**A bug this caused, and how it was fixed.** With `ok` moved to blue and `accent`
already blue, the providers listing rendered its classification badge (ROUTER) and
its state badge (ACTIVE) in identical colours. Two different *kinds* of fact
wearing the same colour is worse than either colour being wrong. Resolved by making
`accent` **black**: `accent` classifies what a thing *is*, `ok` says how it is
*doing*, and black is the structural colour this whole design is already ruled in.
Found by looking at a screenshot, which is the only way it could have been found —
no test asserts on colour.

**Yellow is the one primary that has to invert.** Cadmium is the lightest of the
three: white type on it fails contrast badly and it is unreadable as type on white.
So `--colour-warn` is a dark yellow-brown for text and `--colour-yellow` is the
cadmium for fills. The period printers had the same problem and made the same
split.

### 4. Jost*, self-hosted, one variable file

A geometric sans carries most of the period signal on its own — Futura came out of
this movement — and the previous tokens deliberately used a system stack: "no
webfont request, no layout shift, and nothing to self-host for a licence to be
checked."

That position is overridden, and each of its three objections is answered rather
than ignored:

- **Licence.** [Jost*](https://github.com/indestructible-type/Jost) by Owen Earl
  is SIL Open Font License 1.1. **Verified at source** — `OFL.txt` was fetched from
  the upstream repository and from `google/fonts`' own `METADATA.pb`, not recalled
  from memory (ground rule 2). OFL is a free/libre licence, it imposes nothing on
  first-party EUPL-1.2 code because a font is a separate work rather than linked
  code, and its one real obligation — keep the notice with the font — is met by
  vendoring `fonts/OFL.txt` beside the file.
- **No external request.** Self-hosted, served by the gateway from its own origin.
  The console's CSP already said `font-src 'self'` and did not have to be loosened,
  which is the same direction ADR 0023 insisted on for the script policy — fit the
  build to the policy, not the policy to the build.
- **One file, 36 KB.** Google Fonts ships Jost as a *variable* font, so a single
  woff2 covers every weight from 100 to 900 — smaller than the two static faces it
  would otherwise take. Subset to Latin and Latin Extended-A: Italian needs the
  accents, and the rest of Unicode is 100 KB this deployment has no reader for.

`font-display: swap` rather than `block`: it is a same-origin asset that will
almost always be cached, and on the one cold load a readable page in the fallback
beats an invisible one. The fallback stack is metric-*similar* rather than
metric-compatible, so a cold load reflows slightly. That is the accepted cost of
not blocking the first paint.

The scale moved up one step, because Jost runs small for its point size as
geometric faces with a modest x-height do, and the display size moved up much
further: a Bauhaus page has a steep typographic hierarchy rather than an even grey.
Money and identifiers stay on the **mono** stack — digits lining up down a column
is a functional requirement, and "form follows function" is this style's own
argument rather than a slogan against it.

### 5. The mark is the three shapes, not a logo

Circle, triangle, square, in blue, yellow and red — Kandinsky's pairing from the
Bauhaus colour-shape questionnaire. Still **not a logo**: one is the foundation's
to supply, and inventing one here would only have to be removed. Drawn in CSS
rather than set as glyphs, because a real circle is a shape and a bullet character
is a font's opinion about one. `aria-hidden`, with the wordmark beside it carrying
the name, so it costs a screen reader nothing.

## Consequences

- **The tokens file's central claim held**, and this was the first real test of
  it. "Components reference tokens and never literal values. A component with
  `padding: 12px` in it is a component that cannot be restyled." The entire palette, geometry and type change was a change
  to `tokens.css`; the component edits that remained were all *deliberate*
  changes of idiom (a pill becoming a block, a panel becoming a ruled block), not
  literals that had to be hunted down. Two literals existed in the whole library
  and both were legitimate: the dialog backdrop tint and `border-radius: 50%` on
  the spinner.
- The label idiom — uppercase, tracked, medium weight — is now three tokens
  (`--label-transform`, `--label-tracking`, `--label-weight`) rather than the same
  two declarations copied into five stylesheets. That was already drift before
  this change; the restyle is what made it visible.
- `--focus-ring` moved from the accent to the **yellow**. Focus has to be visible
  on a blue button, and blue-on-blue is not.
- No dark theme, unchanged. It would be one block of token overrides here.
- The tokens file is now the load-bearing document for the look, and it says so at
  length. That is the intended place for it: a design decision explained in a
  component is a decision made in the wrong file.

## The font declaration, and a trap in it

`src: ... format("woff2")` — **not** `format("woff2-variations")`, which is what
this shipped with for one commit. The latter is the legacy syntax from before
variable fonts were finalised: Chromium accepts it and Safari rejects the whole
`@font-face` rule.

A rejected rule is **silent**. There is no error and no missing-font symptom,
because the fallback stack here is Futura and Century Gothic — Jost is a Futura
revival, so those are the metrically closest fallbacks, and they are also
installed on most machines and *wider*. So the failure does not look like "the
font is missing". It looks like "the layout is slightly wider than it should be",
which is far harder to attribute to its cause. It surfaced as a caption
overflowing its table cell on a reviewer's machine and not on the developer's.

Two lessons, and the second is the reusable one:

- The variable range is declared by `font-weight: 100 900`. The file being
  variable needs no announcing in `format()`.
- **A layout that depends on a font's metrics is a layout that breaks when the
  font does not load.** The fix for the symptom was not the font declaration; it
  was making the affected component's width demand independent of text width. See
  `Meter.module.css` — the caption moved under the bar so that the cell needs
  `max(bar, caption)` rather than `bar + gap + caption`, and no font metric can
  make one compete with the other.

## A bug found on the way, unrelated to the design

The self-hosted woff2 was served as `application/octet-stream` in the container
and `font/woff2` on a developer's machine. `StaticFiles` asks
`mimetypes.guess_type`, which consults the *operating system's* mime database —
present on a dev box, absent from the slim runtime image. Browsers load a font
regardless of its content type, so the symptom would have been nothing but a wrong
header, indefinitely. `console.py` now registers `.woff2` and `.woff` explicitly.

Same shape as the SQLite/PostgreSQL note in CLAUDE.md: works here, broken there,
and only a live check finds it.
