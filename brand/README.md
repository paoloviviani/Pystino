# Brand

The mark is a pair of glasses with two ridged brows above the lenses. Each brow
rises to a peak and drops inward, so it reads twice: as the frown of a
nitpicker — *pistino*, the one who checks every last detail — and as a mountain
ridge with two summits, the same device as Cerea's. Two lenses, two peaks, one
stroke weight.

## Files

| File | Use |
| --- | --- |
| `pystino-mark.svg` | The mark in ink. The default, on light backgrounds. |
| `pystino-mark-reversed.svg` | Paper on ink, for dark backgrounds. |
| `pystino-mark-pine.svg` | Pine, for quieter chrome and for print on light stock. |
| `pystino-mark-clay.svg` | Clay, for accents and one-off material. |
| `pystino-favicon.svg` | The small cut: no temples, heavier strokes, a square canvas. 16 to 32 px. |
| `pystino-lockup.svg` | Mark with the wordmark, horizontal. |

Every file is one colour, with no gradients and no embedded rasters, and each
carries its own viewBox. Replace the stroke colours with `currentColor` to make
the mark inherit the text colour where it is inlined.

## Palette

| Name | Value |
| --- | --- |
| Ink | `#14181C` |
| Paper | `#F6F3EC` |
| Pine | `#0E3B33` |
| Clay | `#A6462B` |
| Clay on dark | `#E0A07F` |

## Clear space and sizes

Half a lens radius — 29 units on the mark's grid, about one fifteenth of its
width — on every side, and nothing else inside it. The mark holds from 24 px
wide; below that use `pystino-favicon.svg`, which is drawn for 16 px.

## Don't

- colour one lens or one brow on its own;
- change the stroke weights, or square the caps;
- rotate, skew, outline or add a shadow;
- set the mark on a background it does not clear by at least 3:1.

## Geometry

Lenses r 58, centres 176 apart; glasses stroke 22, brows 26, each brow peak 30
above its outer end and 42 above its inner; bridge 13 over a 62 gap; canvas
432 × 248 with 22 units of padding. The favicon cut: r 62, stroke 30, brows 34,
no temples.

The wordmark in `pystino-lockup.svg` is live text in a serif stack, not
outlines: outline it, or replace it with the face Cerea is set in, before the
lockup goes anywhere it cannot fall back.
