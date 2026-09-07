# 0055 — Document extraction as a `/v1` surface, with a local backend

- Date: 2026-09-07
- Status: **accepted, built** (upstream and local backends, response-side
  redaction; console and live script outstanding)
- Requested as: "cortecs supports also some ocr models (like Mistral ones), can
  we implement support for them too?", then widened — "markitdown can be served
  as a document extraction endpoint per se… we can offer markitdown as a built-in
  extractor for downstream services, other than just for redaction", and finally
  "go with the entire ocr surface".
- Extends [0028](0028-embeddings-and-served-model.md) and
  [0030](0030-more-surfaces.md), which set the pattern for adding a
  metered surface, and [0053](0053-model-pricing-sources.md), whose rule about
  where a price comes from applies to a page as much as to a token.
- **Amends [0019](0019-document-conversion.md)** — see "What this changes about
  0019" below. Its architecture stands; its default does not.

## What this changes about 0019

ADR 0019 decided document conversion in 2026-08-14, for Phase 3, and its central
claim was **"the endpoint is the architecture"**: conversion sits behind a
configurable HTTP endpoint rather than a hardcoded dependency, with
`docling-serve` as the reference implementation and default, and the contract to
be defined in `packages/shared-py` "in the same spirit as the redaction
contract".

Two of those three are honoured exactly. The contract is
`llmp_shared.documents`, a sibling of the detection contract. The endpoint is
configurable twice over — `GATEWAY_EXTRACTOR__ENDPOINT`, overridden per provider
row — and `/v1/ocr` generalises the idea further than 0019 imagined: the
*backend* is chosen per model, so one deployment can offer a local extractor and
a remote OCR model side by side and let a grant decide who may use which.

**The default changes, and the reason is measured rather than aesthetic.**
Resolved for Python 3.13: `docling` is 121 packages including torch,
torchvision, transformers and the NVIDIA CUDA stack — over a gigabyte of wheels
before cuBLAS and cuDNN — against `markitdown[docx,pdf,pptx,xls,xlsx]` at 37
with no ML at all. That weight buys layout and OCR *models*, which serve the
scanned case; it buys nothing for a `.docx`, which is a zip of XML. So the
built-in default reads what can be read without inference, and says plainly when
it cannot.

Docling is not displaced as an answer for the scanned case — it is displaced as
the *bundled* one. Pointing this surface at `docling-serve` needs a plugin for
its wire shape, which is the same one-function extension ADR 0032 describes and
is not written yet. 0019's open question — whether `docling-serve`'s own licence
differs from Docling core's — remains unverified, and is no longer on the
critical path precisely because nothing defaults to it.

## Context

Cortecs serves `POST /v1/ocr` with `mistral-ocr-4.1` behind it. Two facts about
it shaped this, both established at source on 2026-09-06:

* **The OCR models are in `/v1/models`, behind a default nobody mentioned.**
  This document first said the opposite, and the correction is worth keeping
  because the mistake is instructive: the catalogue's `tag` parameter
  **defaults to `['Instruct']`**, so a query that looks unfiltered is filtered.
  Asking plainly returns them — `tag=OCR` gives `mistral-ocr-4.1`, `4.0` and
  `2512`; `tag=Embedding` gives eleven embedding models that the same default
  had been hiding all along. An API whose default view is a subset, with no
  field in the response saying so, is a thing to check for rather than assume
  the absence of.
* **The response carries `usage_info` with `pages_processed` and `credits`.** The
  billable unit is a page. Whether `credits` is micro-EUR as their chat surface
  reports, or something else, is **unverified** — it needs one real call with a
  key, and until then no plugin reads it. Reporting a figure in the wrong unit
  is worse than reporting none (ADR 0032).
* **The price is published, per thousand pages.** The catalogue entry carries
  `pricing.ocr_cost`, documented as "Standard OCR cost per 1,000 processed
  pages" (and `ocr_annotated_cost` for annotated ones), with both token rates
  at zero. So `per_page` is `ocr_cost / 1000` — a divisor of a thousand sitting
  next to the token rates' million, which is the same class of error ADR 0053's
  OpenRouter parser exists to prevent, one order of magnitude down.

Then the scope widened, and the wider version is the better one: extraction is
worth exposing as a surface in its own right. A RAG pipeline that lets you point
its document extractor at Mistral can point it here instead, and then *which*
extractor runs — a third party, or this deployment's own — becomes an
administrator's grant rather than a setting in somebody else's tool.

## Decision

### 1. One surface, two backends, chosen by the provider

`POST /v1/ocr` speaks the Cortecs and Mistral shape, and the model's provider
decides who reads the document:

| | reads | document leaves the deployment? |
|---|---|---|
| `plugin = extractor` | our own service: Word, Excel, PowerPoint, text-layer PDF | **no** |
| any other plugin | the counterparty's `/ocr` | yes |

The discriminator is `providers.plugin`, which already answers "what kind of
thing is behind this row". `LocalExtractorPlugin` is registered like any other
counterparty type, so grants, prices, the console's provider selector and the
ledger all work unchanged on a model whose provider happens to be us. It reports
no cost — there is nobody to charge us — and carries no credential, because the
service is unreachable off the compose network and a credential it would ignore
is one somebody has to rotate.

This is what makes half of the confidentiality problem go away rather than be
managed: for a `.docx` or a text-layer PDF, nothing is sent anywhere.

### 2. A page is a unit, and the count has a source

`model_prices.per_page` beside `per_image`, `TokenCounts.pages`, and one more
branch in `accounting/cost.py` — still the only file that multiplies a count by
a rate. The count comes from the counterparty's `pages_processed`, or from the
document itself when extraction was local. **Never from the length of the
returned text**, which would be a billing figure with no source.

`TokenCounts.from_ocr_usage` is its own reader for the reason the Anthropic one
is: the field is named differently *and* means something else, and there are no
tokens at all on this surface — a tolerant reader would report zero for a
request that really cost money.

**A bug this surfaced, worth recording because it was invisible:**
`resolve_counts` decided whether the counterparty's usage was usable by testing
`counts.total`, i.e. prompt plus completion. With no tokens, an exact page count
fell through to the estimation path, where the rebuild dropped it, and the
request recorded a cost of **zero**. Found by the first test of the surface, not
by an invoice. `_with_images` is now `_with_units` and carries both non-token
quantities, with a docstring saying that it rebuilds a frozen dataclass and
anything it forgets is lost silently.

### 3. The reservation is a floor, and the cost of that is stated

Every other surface can bound its bill before the call: chat has `max_tokens`,
images have `n`. A document's page count is unknown until it has been read.
Admission therefore reserves what it can prove — the caller's `pages` selection
if there is one, else a single page — and settles the real count afterwards.

**A caller close to their ceiling can exceed it with one large document.** That
is the trade, and it is deliberate: reserving a pessimistic maximum would refuse
ordinary requests that would have fitted, and the ceiling is checked again at
settle. If it ever needs to be tighter, the honest fix is a per-model maximum
page count, not a bigger guess.

### 4. Redaction runs on the way back

This is the surface where the request is not the sensitive half. With
`document_url` the provider fetches the document itself, so the bytes never
reach us and there is nothing to inspect — a property of the request, not a gap
to close. What returns is the document *as text*, which is the moment a scanned
identity card stops being a picture and becomes searchable, indexable data.

So `pages[].markdown` goes through detect-and-substitute under the caller's
effective policy, on both backends — the local path is not a shortcut past the
policy. Two consequences:

* **A blocked response does not un-charge the request.** The counterparty read
  the document and will invoice for it whatever we then decide to hand over. The
  row stays `completed` with its real cost and records which rule refused.
  Reversing the charge would be a nicer story and a false one.
* **Redaction facts are discovered after the row is created**, so
  `observe_redaction` sets them and `finalise` writes them. A row reporting
  "0 entities redacted" for a request that had forty replaced is worse than one
  reporting nothing.

### 5. A document that was not read is an error, never an empty success

`no_text_layer`, `unsupported`, `too_large`, `unreadable` — each becomes a 422
with a message naming the next step ("this is a scan; use an OCR model"), and
nothing is charged. An empty `pages` array is indistinguishable from a blank
document, and a pipeline that indexed it would have indexed nothing without
knowing.

The same rule governs what the local backend's response does *not* invent:
no pagination for a `.docx` (there are no pages until something renders it, so
one page rather than markdown split at a guess), no `images`, bounding boxes or
confidence scores (absent rather than present-and-empty, which would claim a
page had no pictures), and `pages_processed` of zero for a format without pages
rather than one-for-tidiness.

### 6. A URL with the local backend is refused

Fetching a caller-supplied address from the gateway is server-side request
forgery on a network holding the database, the other compose services and, on a
cloud host, the metadata endpoint. So the local extractor reads documents that
travelled with the request, and the refusal says which model to use instead. A
test points at `169.254.169.254` and asserts nothing was fetched.

## Consequences

- **markitdown, not four libraries or a framework.** One upstream and one API,
  MIT, and the numbers that decided it (resolved for Python 3.13):
  `markitdown[docx,pdf,pptx,xls,xlsx]` is 37 packages; `unstructured` is 71 and
  brings a second spaCy alongside Presidio's; `docling` is 121 and pulls torch,
  transformers and the CUDA stack — over a gigabyte of wheels, for layout models
  that serve the scanned case this build does not do. `[xls]` earns its place by
  reading the pre-2007 format the per-format libraries cannot.
- **Two containers from one image.** The extractor is the detection image with
  `REDACTION_NLP_ENGINE=disabled` (~150 MB, no model). Not one container,
  because ingestion is bursty and payload-heavy while detection is already ~90%
  of this deployment's CPU; not one image each, because they differ only in
  which endpoint is called.
- **Scanned documents remain unsolved, and the reason is circular.** Knowing
  whether a scan contains personal data requires reading it, which requires the
  OCR model that the document is being sent to in the first place. This surface
  narrows the problem to exactly that case — office formats and text-layer PDFs
  no longer leave — but does not resolve it. The options, none taken: a cheaper
  self-hosted OCR sufficient for *detection* rather than extraction; a rule
  about which groups may send documents to which providers at all; or accepting
  that the returned text is where the control lives, which is what §4 does.
- **`credits` is not read.** Until one real call establishes its unit, the
  Cortecs plugin reports no cost for this surface and billing is from our own
  price row. Silence rather than a figure in a unit nobody verified.
- **Still outstanding:** the console has no OCR-specific screen (a `per_page`
  field on the price form, and the kind badge), and there is no live script yet.
  Both are listed in CLAUDE.md's open items rather than implied as done.
