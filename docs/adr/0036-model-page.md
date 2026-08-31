# 0036 — A model's price belongs on the model's page

- Date: 2026-08-25
- Status: **accepted, built**
- Requested as: "I find awkward from the UX perspective that the model pricing
  is a different tab. It should be configured from the the model tab. the 'edit'
  button in the 'models' tab should open a richer interface (maybe in a
  different page?) that includes also pricing. maybe accesible clicking also on
  the model name?"
- Related: [0023](0023-admin-console.md) (the console), [0008](0008-accounting-model.md)
  (why a price is append-only and effective-dated),
  [0031](0031-model-capabilities.md) (the capability vocabulary this page edits),
  [0014](0014-model-catalogue-and-pricing.md) (the catalogue and the importer).

## Context

The console had a **Models** screen and a **Pricing** screen. Pricing carried its
own model picker: to set a rate you left the catalogue, chose the model a second
time from a dropdown of every model in it, and worked in a page that showed
nothing else about it. Editing a model's capabilities was a third place — a
dialog on the listing — and granting access a fourth.

Four places, one subject. The cost was not only the clicking:

- **The dangerous state was off-screen.** A model that is catalogued, granted and
  unpriced serves happily and records a cost of zero. The listing flagged it with
  a badge; the page where you would fix it was somewhere else, reached by a menu
  item nobody had a reason to open.
- **The price was disconnected from who can spend it.** "What does this cost" and
  "who may use it" are the same decision, and they were two screens apart.
- **The API had fields no screen offered.** `cache_read_per_mtok` and
  `cache_write_per_mtok` have been accepted since the cache-accounting work and
  the Pricing form never had a box for them, so the only way to set them was
  `curl`. That is not a small omission: a provider that bills cache reads at a
  fraction of the input rate is billed *here* at the full input rate, so the
  ledger and the invoice diverge on exactly the requests the cache was meant to
  make cheaper (docs/cache-accounting-findings.md).

## Decision

**One page per model, at `/admin/models/:id`**, holding what it is, what it can
do, what it costs and who may reach it. The listing links to it twice — the name
is an anchor and the row's Edit button navigates — because the name is what
someone points at and the button is what the eye finds in a column of actions.

**A page, not a bigger dialog.** There is a price *history* here, and a dialog
that scrolls is a dialog that should have been a page. It also makes a model
addressable: a link in a ticket, a bookmark, a reload that lands where it left
off. That needed `GET /api/admin/models/{id}`, which did not exist — the listing
carries the same shape, but a deep link cannot page through a catalogue looking
for a row.

**The Pricing tab is gone, and `/admin/pricing` redirects** to the catalogue
rather than 404ing. It was in the navigation long enough to be bookmarked, and a
404 reads as a broken deployment rather than as a screen that moved.

**Access moved too**, from a dialog on the listing. Leaving it behind would have
half-fixed the complaint: still two places to configure one model, just a
different two.

**The append form gained the cache rates**, and the history a column for them.
Both render an em dash rather than a zero when unset — "not priced this way" and
"free" are different facts, and one of them is a decision somebody made.

What did **not** change: a price is still append-only and effective-dated, with
no edit and no delete. Putting it on a page with editable fields beside it makes
that more tempting to "fix", not less, so the section says so in its own
description. Re-pricing a model must never rewrite what a past request cost.

## Consequences

- `CapabilityPicker`, the known-value vocabularies and the capability badges moved
  to `components/CapabilityPicker.tsx`, because three screens now use them: the
  listing renders them, the Add dialog collects them, the model page edits them.
  A second copy of that vocabulary would be exactly the failure ADR 0031 is about
  — the capability worth hearing about is the one the provider added last week,
  and a duplicated list is the one that gets updated last.
- The capability tests moved with the editor, unchanged in what they assert. The
  listing keeps one new test in their place: that both the name and the button
  lead to the model's page.
- The nav is one item shorter, which is the point. A console whose menu grows an
  entry per *field* of a model ends up with a menu that has to be read.
