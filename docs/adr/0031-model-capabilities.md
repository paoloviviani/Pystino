# 0031 — Model capabilities

- Date: 2026-08-16
- Status: accepted
- Extends [0028](0028-embeddings-and-served-model.md), which introduced
  `models.kind`, and [0030](0030-more-surfaces.md), which added a third kind.

## Context

The catalogue knew one thing about what a model could do: its `kind`, which
decides the route. Everything else a caller needs — does it read images, does
it do tool calling, how much context does it have — was either absent or, in
the case of the context window, silently null on every imported model.

That last one is the shape of the problem. `parse_catalogue` looked for
`context_length`, `context_window` and `max_context`. The reference provider
sends **`context_size`**. So every model ever imported from it had no context
window, the console rendered a blank, and nothing failed — a nullable column
filled with nulls looks exactly like a model nobody has described yet.

Meanwhile a client picking a model has to guess. Ask a model that cannot do
tool calling to do tool calling and you find out from the provider's 400, after
paying for the request.

## Decision

### 1. Three lists, not a column of booleans

`models.input_modalities`, `models.output_modalities`,
`models.supported_features` — JSON string arrays.

The provider documents its feature set as **open**: "current values include
`json_mode`, `reasoning` and `tools`". A boolean column per feature would need
a migration every time a provider adds one, and — worse — a parser that
filtered to a vocabulary compiled today would silently discard exactly the
capability an operator most wants to hear about: the new one.

So unknown values are kept. What is enforced is shape, not vocabulary:
lower-cased, trimmed, deduplicated, sorted. Sorted matters more than it looks —
it makes re-importing a model visibly a no-op instead of a change nobody made.

**Empty means "not stated", never "cannot".** Migration 0006 backfills
modalities from `kind`, because a model catalogued as chat demonstrably took
and produced text, and leaves `supported_features` empty on every existing row,
because that genuinely was not known. Inferring "probably supports tools" from
a model's name would be a guess presented as a fact.

### 2. Discovery reports them before you import

The discover dialog shows kind, context window, input modalities and features
per offered model. "Does this one do tool calling" and "can it read an image"
are the questions asked at exactly that moment, and the alternative was
importing it to find out.

`kind` is still inferred from `output_modalities` — unchanged from
[ADR 0028](0028-embeddings-and-served-model.md), including the refusal to guess
"image" from a model's name.

### 3. And they are editable afterwards

A provider's catalogue is a **claim, not a contract**. A model advertised as
supporting tool calling may do it badly, or not at all through the router in
front of it. Without somewhere to record that, the only options are to believe
the catalogue or to stop importing.

`kind` is editable for the sharper version of the same problem: it is inferred,
inference can be wrong, and a wrong kind takes a model off the only route that
would serve it. Before this the fix was SQL. Correcting it does not make past
spend unreadable, because usage rows record the surface they actually went
through ([ADR 0030](0030-more-surfaces.md)).

Editing normalises the same way importing does, so a hand-typed `Tools` and an
imported `tools` are the same value.

### 4. Exposed on `/v1/models`

Alongside `kind`, which [0030](0030-more-surfaces.md) added. Not in OpenAI's
schema — theirs is one listing per endpoint, so the question does not arise —
but this gateway serves chat, embedding and image models from one catalogue,
and unknown fields are ignored by every OpenAI client.

## Consequences

Imported models finally have a context window. That is a visible change to
existing deployments: re-running discovery on models already catalogued does
not backfill them, because discovery only reports models we do *not* carry. An
operator who wants the numbers for existing rows edits them, or deletes and
re-imports.

Capabilities are descriptive, not enforced. The gateway does not refuse a
tool-calling request to a model whose `supported_features` omits `tools` — the
provider is the authority on that, and refusing on the strength of a possibly
stale claim would break working requests. The one capability that *is* enforced
is `kind`, because it decides routing.

## Alternatives considered

**A boolean per capability** (`supports_tools`, `supports_vision`, …). Cheaper
to query and to render, and wrong for an open set: every new provider feature
becomes a migration, and until that migration ships the feature is invisible.

**Deriving capabilities from `kind`.** Would have covered the modalities and
nothing else — the features are orthogonal to the route, which is the whole
reason they are worth recording separately.

**Refusing requests that the capability set says will fail.** Tempting, and it
would turn a provider 400 into a local one. Rejected: the claim can be stale or
wrong in either direction, and refusing a request the provider would have
served is a worse failure than forwarding one it refuses.
