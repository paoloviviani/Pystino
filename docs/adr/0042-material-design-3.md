# 0042 — Material Design 3, tuned for reading

- Date: 2026-08-28
- Status: **accepted, built**
- Supersedes [0034](0034-bauhaus-design-language.md).
- Requested as: "I find the bauhaus style not very readable […] something more
  readable and less fancy", with a Material You specification supplied as "a
  loose direction".

## Context

ADR 0034 chose a Bauhaus / mid-century modernist language: square corners, 2px
near-black rules, a geometric typeface, tracked uppercase labels, no shadows,
three primaries and deliberately no green. Every one of those was an authentic
choice and they worked together.

They did not work on this application. It is pages of figures — tables of
tokens and money, quota bars, listings of thirty entity types — and a poster
idiom is a bad fit for that. Concretely:

- A 2px near-black rule around every card and under every row turned a listing
  into a grid of boxes, and the boxes competed with the numbers.
- Jost has a modest x-height. ADR 0034's own token file said so and nudged the
  scale up to compensate, which is the tell: a typeface needing compensation at
  UI sizes is the wrong typeface for UI.
- Tracked uppercase labels are measurably slower to read, and they were
  everywhere — every column heading, every field label, every stat caption.
- Dropping green cost the one colour pair every reader already knows. ADR 0034
  said this plainly and accepted it; it was the wrong trade for a screen whose
  job is to say whether a budget is fine.

## Decision

MD3's tonal system, type and geometry, and **not** its decoration.

### Taken

| | Instead of | Because |
|---|---|---|
| Tonal surfaces | 2px black rules | Depth from a few percent of luminance, so a page of cards is not a grid of boxes |
| `#1C1B1F` ink on a tinted ground | Near-black on grey | Still ~16:1, without the glare of a long column of figures |
| Roboto | Jost | Designed for screen UI at small sizes; large x-height; no compensation needed |
| 12–16px radii, pill buttons | Square everything | A rounded container reads as one object; a pill reads as pressable |
| Sentence-case labels | Tracked uppercase | Weight and muted ink distinguish a label from a value without costing reading speed |
| **Green for `ok`** | Blue | The pair every reader already knows, which 0034 gave up and named as a loss |

**The tonal ground is inverted from MD3's own default**, deliberately: the page
is tinted and a card is white. MD3 puts a tinted container on a lighter page,
which would place the tint under the numbers. This console is mostly tables, and
figures read best on white.

### Not taken

The supplied specification also asks for blurred organic shapes, glass-morphism,
radial-gradient auras, glow-on-hover, `hover:scale-[1.02]` and asymmetric
elevation. None of it is here. The brief was "more readable **and less fancy**",
and those are the fancy half — a console is read, not admired. Shadows are two
small steps used to lift a menu off the page, not an effect.

It is also written in Tailwind throughout, and this repository has no Tailwind
by decision ([0034](0034-bauhaus-design-language.md), and unchanged): the tokens
are plain custom properties so `packages/ui` needs no build tool from its
consumers.

## Consequences

- **The restyle was one file again, plus two components.** `tokens.css` carries
  the palette, type and geometry; `Badge.module.css` and the brand mark encoded
  Bauhaus-specific decisions and had to be told. Everything else — Button, Card,
  Table, Dialog, Meter — inherited the new look by referencing tokens, which is
  the second time that claim has been tested and held.
- Roboto is vendored under the **Apache License 2.0**, verified at the upstream
  repository, in two variable subsets (Latin, Latin Extended-A) at 43 KB and
  29 KB. Self-hosted, so the CSP and the no-CDN rule are untouched. Jost and its
  OFL are removed; they are a `git revert` away.
- The Badge collision ADR 0034 had to resolve is gone with the constraint that
  caused it. Classification badges took black because `ok` and `accent` were
  both the blue; with green back they are different hues and neither borrows the
  structural colour.
- Every token name is unchanged, so nothing outside these files had to move.
  That was the point of naming them by role.
