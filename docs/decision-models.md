# Decision models

A **decision model** does not write text. It takes a piece of text and typed
questions about it and answers with calibrated probabilities, which makes
routing, triage, moderation and classification much cheaper and faster than
asking a chat model for a verdict and parsing its prose. Cloudflare's Clef and
Convai's Laya are examples. Pystino serves them on two routes, behind the same
keys, groups, quotas and reports as everything else (ADR 0100).

The first provider with a Decisions API is Cortecs (in beta when this was
written). Nothing in the route is specific to it: what is, lives in the provider
plugin ([below](#providers)).

## Setting one up

1. Have a provider of a type that supports decisions. In the console's provider
   form the type list says which do; Cortecs does, the generic OpenAI-compatible
   type does not.
2. Create the model by hand under **Models**, with kind **Decision**, the
   provider's model id as the upstream name (`laya`, `clef-flash`) and its price
   per million input and output tokens. Cortecs does not list its decision models
   in `GET /models`, so discovery will not find them. Creating a decision model
   on a provider type with no decision support is refused with a message saying
   so.
3. Grant it to a group, or make it public, as for any model. An
   [alias](model-aliases.md) works too, and a routing policy applies to it.

`GET /v1/models` lists it with `"kind": "decision"`. A chat client's picker
should filter on `kind`, as it already does for embedding and OCR models. A
decision model is refused on the chat, embeddings and other routes, and a chat
model is refused on these two.

## The routes

| Route | Takes | Answers |
|---|---|---|
| `POST /v1/decisions` | the `input` form, or the System One form | in the format it was sent |
| `POST /v1/systemone` | the System One form only | System One |

The **`input` form**: `model`, `input` (a string, or messages whose content is a
string or `input_text` parts) and `questions`, an array of `predicate`, `choice`
and `score` questions. The answer's `answers` is an array, with `probability`
for a predicate, `choice` and `score` with `confidence` and `probabilities` for
the others, and possibly a `refusal`.

The **System One form**: `model`, `state` (a string, object or array) and
`questions`, an object keyed by your own ids, with `noul`, `choice` and `score`
questions. The answer's `answers` is an object keyed by the same ids.

Send one form per request, with a non-empty `questions` of the matching type
(array for `input`, object for `state`); anything else is a 400 before any
accounting. Beyond that the gateway does not read the questions or the answers.
Question bodies, option lists and any field it does not know go to the provider
as sent and the answer comes back as the provider wrote it, so a new question
type the provider adds works without a gateway release. The `model` in the answer
is the name you sent.

```bash
curl https://gateway.example/v1/decisions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "triage",
    "input": "The cold-storage room is at 9 degrees, the maximum is 5.",
    "questions": [
      {"type": "predicate", "name": "inspect", "instructions": "Needs inspection?"},
      {"type": "choice", "name": "team", "instructions": "Which team?",
       "choices": [{"value": "facilities"}, {"value": "billing"}]}
    ],
    "preference": "cost"
  }'
```

## Routing belongs to the deployment

The body fields that choose providers, regions, quantisations or fallbacks are
the provider row's settings, as for chat, and are refused with a 400 that names
the field. For Cortecs they are `allowed_providers`, `eu_native`,
`provider_order`, `quantizations`, `allow_zero_data_retention`,
`enable_model_fallback` and `fallbacks`. A caller who could send `eu_native:
false` could pick a provider the deployment excluded. A field sent as `null`
counts as absent and is dropped. `preference` (`speed`, `cost`, `balanced`) is the
caller's and passes through.

## Redaction

`input` and `state` are text sent to an upstream, so the deployment's
[redaction policy](redaction.md) applies as it does to a chat message: every
string in `input`, and every string value anywhere inside a `state` object or
array. Object keys and the questions themselves are not redacted: keys are your
structure, and the questions are yours, so do not put personal data in
`instructions`. A policy that blocks refuses the request with a 403 and records
a `blocked` row. Answers hold probabilities, choices and scores, so there is
nothing to restore afterwards; a refusal's reason is passed through as written.

## Metering

The same path as embeddings: access by key, token or group grant; quota reserved
before the call and settled after; one usage record per request, with the
surface `decisions` for both routes.

- **Reservation.** Input tokens only: an answer is numbers, and every response
  measured reported zero output tokens. The estimate counts the redacted text
  and the questions, because the provider tokenises both.
- **Cost.** The price table (input and output rate per million tokens) is both
  the reservation estimate and the fallback. In `provider_reported` billing the
  provider's own figure is the charge, with the table's figure kept beside it,
  as for chat ([accounting](accounting-and-quotas.md)).
- **Served model.** Recorded as the provider reports it (for Cortecs the
  `x-cortecs-provider` and `x-cortecs-model` response headers), so a substituted
  model is disclosed on the row rather than priced as the one you asked for.
- **Errors.** A provider error passes through with its status and body (Cortecs
  uses 400, 401 and 404 beyond its documented 400 and 500), the row is recorded
  as an upstream error and nothing is charged.
- No streaming: the API has none.

## Providers

A provider plugin declares decision support (`DecisionSupport` in
`gateway/plugins/base.py`): the upstream path for each route and the names of
its routing fields. A plugin that does not declare it cannot serve a `decision`
model. The Cortecs plugin declares `/decisions` and `/systemone`, and reads
`usage.cost`, an integer in **micro-EUR rounded up per request**: 143 input
tokens at 0.042 EUR per million is 6.006 micro-EUR, reported as 7. The reported
figure is therefore at most a micro-EUR above the table's, which is not a
mismatch. Another provider's plugin owns its own unit (OpenRouter reports a
decimal credit figure, for example), path and routing vocabulary.

The two request forms and the pass-through of everything else are the contract
with callers and do not change with the provider.
