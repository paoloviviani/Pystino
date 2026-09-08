# 0058 — Provider-side web search is a billable unit, priced per search

- Date: 2026-09-08
- Status: **accepted, built** (`per_search`, `search_count`, the cap, the
  report). Phase 1 of the plan in
  [docs/web-search-plan.md](../web-search-plan.md); phases 2 and 3 are not
  started.
- Requested as "can we replicate this functionality (native search support and
  search provider support…)", then "go with phase 1".
- Extends [0030](0030-more-surfaces.md) (per-image pricing, the first non-token
  unit) and [0055](0055-ocr-surface.md) (per-page, the second), and applies
  [0053](0053-model-pricing-sources.md)'s rule about where a price may come
  from to a third.

## The hole this closes

`/v1/chat/completions`, `/v1/responses` and `/v1/messages` are passthrough
surfaces: `extra="allow"` forwards any field the caller sends, tools included.
So **provider-side web search already worked** — a caller passing Anthropic's
`web_search` tool got searches, today, on an unmodified deployment.

It was billed as zero. `accounting/cost.py` multiplied tokens, images and
pages by rates, and there was no rate for a search. Anthropic charges **$10 per
1,000 searches on top of token costs** and reports the count as
`usage.server_tool_use.web_search_requests` (verified against their live
documentation on 2026-09-08). Three consequences, none of them visible on any
screen:

- every search was a real charge recorded as nothing;
- the quota engine reserved nothing for it, so search spend had **no ceiling at
  all** — the `unpriced_model_count` hazard reached by another route;
- and nothing in the ledger said a search had happened, so the gap could only
  be found on the provider's invoice.

## The decision

`model_prices.per_search`, per **one** search, alongside the token rates rather
than instead of them: a searching chat model incurs both, every time.

`usage_records.search_count`, the billable count, recorded **whether or not a
rate existed to price it**. That last part is the design: an unpriced search
charges nothing (no invented rates — the rule this codebase applies to unpriced
models), but it leaves a trace, so the gap is findable here rather than on an
invoice six weeks later.

`TokenCounts.searches` and `CostBreakdown.search_cost` carry it through the
arithmetic, and the component stays separate through currency conversion like
every other, so the parts keep summing to the total.

## Where the count comes from, and where it does not

**Only from the counterparty's reported usage.** Never from counting
`server_tool_use` blocks in the response, and the reason is in Anthropic's own
documentation: *"If an error occurs during web search, the web search will not
be billed."* An errored search produces a block. Counting blocks would bill the
failures.

One reader, `_search_requests`, is used on all three surfaces, and that needs
justifying against this module's own rule that the surfaces must not share a
tolerant parser. The rule is about **meaning**: `prompt_tokens` and
`input_tokens` differ in what they *include*, so merging them produces wrong
numbers. `server_tool_use.web_search_requests` means one thing wherever it
appears. This is the `_CACHE_WRITE_KEYS` case — one quantity, several places it
can turn up — not the prompt case.

**OpenAI's own web search is deliberately not priced here.** It is billed per
call, but its documentation states no usage field for the count and its rate
varies by `search_context_size`. A count inferred from `web_search_call` output
items would bill the calls that failed and would still not know the rate. Same
refusal as `usage_info.credits` on the OCR surface, for the same reason: a
figure in an unverified unit is worse than none. What a deployment gets today
is the count from any counterparty that reports the field, which includes a
router proxying Anthropic.

## Two mechanisms, and which one is the ceiling

This is the part worth reading carefully, because the first draft of the tests
assumed the wrong one.

**`max_uses`, written into the outgoing tool, bounds *this* request.** When a
caller asks for search without capping it, the deployment's
`default_max_web_searches` (5) is both reserved *and* written into the tool
definition, so the provider itself refuses the sixth search. A number we
reserve against but do not send is a figure that looks like a ceiling and is
not one — the OCR surface is stuck with exactly that shape, because a document
has no page-count field to write into, and this surface is not. Both numbers
come out of one call to `bound_web_search`, which is what stops them drifting
apart.

It is written **only** into the date-versioned Anthropic tool family, which is
where `max_uses` exists. OpenAI's `web_search` takes `search_context_size` and
`filters`; injecting an unknown field there risks a 400 on a request that would
otherwise have worked, and refusing somebody's request to protect a reservation
is the wrong trade. On that tool the reservation degrades to a floor, and
`WebSearchBound.enforced` says so.

**The reservation makes search spend count immediately**, so a cost ceiling
engages for what comes next. It cannot refuse a request for its own cost: the
quota engine subtracts a request's own contribution before judging, which is
its documented overrun policy — refused *at* the limit, an admitted request may
overshoot by its own usage. Three searching requests against a tight ceiling
therefore go 200, 200, 429, and that is the tested behaviour.

A caller who sets their own `max_uses` is taken at their word: it is what gets
reserved and nothing is rewritten.

## What is deliberately absent

- **Nothing enables search that did not ask for it.** The gateway never adds
  the tool, only caps one that is present.
- **No per-context-size rates.** OpenAI prices by `search_context_size`; one
  rate per model here, like per-image pricing, whose per-size variant the user
  deferred.
- **No `search_cost` column on the ledger.** Migration 0023 refused a
  `page_cost` column and the reasoning holds: a second place for a number
  already implied by `cost`, free to disagree with it. The **count** is a
  different thing — it is the unit an invoice is itemised by, and it cannot be
  recovered from the money, least of all when the money is zero because no rate
  existed.
- **No catalogue parsing.** No provider catalogue publishes a per-search rate,
  so `CataloguePrice` and the discovery row do not carry one. It is entered by
  hand, in the console.
- **`openrouter:web_search` is not recognised.** That is their namespace for a
  tool *they* execute. Nothing here executes a search — see the plan's phase 2.

## A bug this fixed on the way past

`_with_units` in the recorder rebuilt `TokenCounts` field by field, so every
billable unit added after it was written was silently dropped — which is how a
page-counted request first recorded a cost of zero, and would have been how a
searched request recorded one too. `TokenCounts.from_image_usage` had the same
shape. Both now use `dataclasses.replace`, which cannot forget a field. The
trap was documented in a comment for a year; the comment did not stop it
recurring, and a copy that cannot be wrong does.

## Tested

`test_cost.py` — the arithmetic: charged per search and not per million,
charged on top of the tokens, nothing charged when unpriced, nothing charged
when no search happened, and the component converting with everything else.
Plus the reader: the Anthropic surface, a router passing the same key through
an OpenAI shape, the Responses surface, absent and malformed objects, and an
image request keeping its units through the reader that used to drop them.

`test_web_search.py` — the bound (no tools reserves nothing; the caller's cap
taken as given; the default written in; nothing written into a tool with no
such field; two tools summed; a foreign namespace ignored; junk and
non-positive caps), the ledger (charged and counted, counted-but-unpriced, no
searches on a request that did not search, the cap reaching the provider, a
caller's own cap not overridden, and no tools added to a request that asked for
none), and the ceiling (search spend closing the window on later requests, the
no-search control, and an unpriced search reserving nothing).

`test_reporting.py` — the count summed across rows and disclosed, and no
disclosure in a month where nothing searched.
