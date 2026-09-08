# 0059 — Restoring a placeholder moves the citations that point past it

- Date: 2026-09-08
- Status: **accepted, built**
- Requested as "go ahead then, now it's already buggy", after the alternative —
  dropping the citations from any response we rewrote — was offered and
  refused: "no one is using this, there is no API stability to respect… the
  effort is all yours".
- Fixes a defect found while writing
  [docs/web-search-plan.md](../web-search-plan.md), and corrects a *different*
  defect that plan claimed and did not have.

## The bug

A provider that searches the web returns citations saying which characters of
its answer a source supports. OpenAI's `url_citation` is a pair of character
offsets, `start_index` and `end_index`, into the assistant's message.

Those offsets were computed against the text the provider generated, and that
text contained our placeholders. Restoring `<PERSON_K3QF7RZM2A>` to `Paolo
Viviani` is 13 characters where there were 18, so **every character after it
moves five to the left** — and the offsets did not move with it. Nothing in the
gateway read `annotations` at all.

The result is not an error. The citation still parses, still points somewhere,
and still looks right. It just quotes the wrong words. And it is worse than
uniformly wrong: only citations positioned *after* the first substitution are
affected, so a response with five citations can have the first two correct and
the rest silently off.

## What this replaces

The plan document said Anthropic's `encrypted_content` requirement and our
redaction were "in direct conflict", with a 400 waiting for anyone using
multi-turn native search. That was inferred from Anthropic's documentation and
never checked against this codebase, and it is wrong: `_message_texts` collects
a content part's `text` string and nothing else, and `MessagesReader
.rewrite_whole` rewrites `block["text"]` and nothing else. A
`web_search_tool_result` block has no `text` key — the results sit under
`content`, with `encrypted_content` inside them — so neither direction touches
it and the blocks go back byte for byte.

Checking that is what turned up the real one, one field over.

## The decision

Restoration reports **where** it wrote, and each surface moves the offsets it
knows about.

`PlaceholderMap.restore_with_edits` returns a `Restored`: the text, plus a
`TextEdit` per substitution in the coordinates of the text *before* it. From
those, `Restored.shift(index)` answers "where did this character end up".
`restore` stays as the one-line wrapper for callers with no offsets to fix.

One left-to-right pass replaces the old chain of `str.replace` calls, because a
chain cannot say where it wrote. The semantics are preserved exactly, including
the property that mattered: candidates are ranked longest-first, so a
placeholder that is a prefix of another is never substituted inside it.

`SurfaceProtocol.shift_citations(payload, shift)` is the new seam, implemented
by all five readers, because *where the offsets live* is precisely the kind of
per-surface knowledge `protocols.py` exists to hold:

| Surface | What it moves |
|---|---|
| Chat completions | `choices[].message.annotations[]` and `choices[].delta.annotations[]` |
| Responses | `output[].content[].annotations[]`, the `response.completed` envelope that repeats it, and the lone `annotation` on a streamed annotation event |
| Messages | nothing — see below |
| Images, OCR | nothing; neither has cited text |

Anthropic needs nothing moved. Its web-search citations carry an
`encrypted_index` and their own copy of the `cited_text`, so rewriting our copy
of the answer cannot invalidate them. Its *document* citations do use character
indices — but into the document the caller supplied, not into the answer, so
restoring the answer leaves them alone too.

## Why the shift takes a choice index

`Shift` is `(choice index, offset) -> offset`, not `(offset) -> offset`, and
that is not symmetry for its own sake. Restoration uses one placeholder map for
the whole request, but the *positions* it edits differ per choice, because each
choice is different text. A request with `n: 2` needs two maps, and shifting
the second choice by the first choice's edits would be a new bug in the same
place. The first draft of this change had exactly that shape, and passed the
whole body to a reader that expected a single choice — so it silently shifted
nothing at all.

## Streaming, which is the hard half

A streamed citation's offsets are into the **whole accumulated message**, not
into the frame carrying them. So nothing less than the whole answer can place
them, and the buffering rewriter cannot help: it deliberately keeps its
unreleased remainder *already transformed*, so its buffer is a mix of
coordinate systems.

The rewriter therefore keeps the provider's own text per choice, as it arrived,
and builds the offset map from that. Two properties make the cost acceptable:
the map is computed **lazily**, only for a frame that actually carries offsets
— a handful per response — and it is **cached** until more text arrives, so a
frame with several annotations pays for one pass and a run of text frames pays
for none. Accumulating is a list append of strings already in memory, next to a
recorder that accumulates the same text for the ledger.

`TextRewriteStage.edits_in` is how a rewriter opts in. The default returns
`None`, meaning "this transform never moves a character", and then no offset is
touched and no text is remembered to no purpose.

## When this cannot fire

`Restored.moved` is false when nothing was substituted **and** when every
substitution happened to be the same length as what it replaced. In both cases
the provider's offsets are still right and no work is done. That covers the
case this deployment is in: with redaction off, or with a prompt containing
nothing to redact, the response comes back byte for byte and the citations were
never wrong. Which is why this was a latent defect and not an outage.

## An offset inside a replaced run

It has no exact answer: the placeholder it pointed into no longer exists. It
lands on the start of the value that replaced it. That only arises if a
provider cited half of a placeholder, which would mean citing half of a name.

## Rejected

- **Dropping `annotations` from any response we rewrote.** A few lines, and
  offered first because it is honest — nothing points anywhere wrong. Refused
  by the reviewer, and rightly: it trades a fixable bug for a permanent
  feature loss, and the only argument for it was the size of the change.
- **Recomputing the map on every frame.** O(n²) over a stream, on the path
  `docs/performance.md` says is already 90% of the CPU.
- **Shifting by a running total of the length changes so far.** Simple, and
  wrong whenever an edit lands between the cited span and the current stream
  position — which is a real ordering, just an uncommon one. An
  approximately-correct citation is the thing this ADR is fixing.

## Tested

`apps/gateway/tests/test_citations.py` — 27 tests. The arithmetic (offsets
before, after and inside an edit; several edits accumulating; the same
placeholder twice; a prefix placeholder losing to the one containing it; an
equal-length replacement reporting nothing moved; an unknown placeholder not
counting as an edit). The shapes (both OpenAI surfaces, each of the three
Responses spellings, a streamed delta, and the three surfaces that must move
nothing). Per-choice maps. The streaming rewriter's memory, its cache
invalidation, its per-choice separation, and a rewriter that opts out. And end
to end through `/v1/chat/completions` with a real redactor: the caller gets the
real name *and* a citation that still quotes the same words — plus the control,
that with nothing redacted the provider's offsets arrive untouched.
