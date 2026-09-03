# 0053 — Where model prices come from

- Date: 2026-09-03
- Status: **accepted, built** (OpenRouter and LiteLLM parsing; Tensorix manual)
- Builds on [0014](0014-model-catalogue-and-pricing.md) (the catalogue importer)
  and [0032](0032-provider-plugins.md).
- Requested as: "figure out how to get their prices" for OpenAI, Anthropic,
  OpenRouter, Nebius, Tensorix and Mistral.

## The research, verified at source on 2026-09-03

| Provider | Own pricing API? | Where the prices are |
|---|---|---|
| OpenRouter | **Yes** | `GET /api/v1/models`, public, no key: per-model `pricing.prompt` / `completion` / `input_cache_read` as decimal strings of **USD per token**, plus `context_length` and modalities |
| Cortecs | **Yes** | The original importer's source (ADR 0014) |
| OpenAI | No | `/v1/models` lists ids only; pricing lives on a marketing page |
| Anthropic | No | `/v1/models` lists ids, names, dates |
| Mistral | No | `/v1/models` lists models and capabilities |
| Nebius | No | `/v1/models` lists models |
| Tensorix | No public documentation found | manual entry |

## Decision

**Native catalogues where they exist; LiteLLM's community file for the
first-party APIs that publish nothing; manual entry for the rest.**

- **OpenRouter** gets a dedicated parser. This is not a nicety: the generic
  parser's keys *would match* OpenRouter's `pricing.prompt` and then read
  0.0000025 USD/token as €2.50 per **million** tokens — the exact
  off-by-a-million failure the generic parser's own docstring warns about.
  The ×1,000,000 is the unit conversion the token pricing implies, not a
  currency conversion. Free models (price `0`) import as free; non-text
  models with no token pricing are *named* in the unparsable report, because
  a model that silently fails to import looks exactly like a free one.
- **LiteLLM's `model_prices_and_context_window.json`** (3,500+ entries, MIT —
  licence-compatible per ADR 0001) is the pragmatic source the ecosystem
  converges on, and it covers every first-party API the gateway has a plugin
  for. `parse_litellm_catalogue` filters on `litellm_provider` (the
  provider row's plugin name is the tag), converts per-token USD to
  per-Mtok, maps `mode` onto the model kind, and is an **import source with
  a review step**, not a trusted authority: the figures land in the
  append-only price history through the same console flow as a hand-typed
  price, visibly and revertibly. The file is fetched fresh on each
  discovery — no dependency is adopted, only a URL read at admin request.
- The currency rule is unchanged and is the sharp edge: **LiteLLM prices in
  USD, and a gateway billing in EUR will skip every model** with
  "priced in USD, not EUR" rather than apply an exchange rate (ADR 0014's
  rule, restated because it will be the first thing an operator hits).
- **Tensorix**: no public documentation found; the plugin asserts no endpoint
  and the prices are typed by hand. The plugin's docstring records why it
  asserts nothing.

## Consequences

- The Models screen's discovery dialog gains a checkbox: "Use the community
  price catalogue (LiteLLM)". Off by default — a provider's own catalogue is
  the authority where one exists, and the checkbox is only needed for the
  APIs that publish nothing.
- Discovery and import both accept `catalogue=litellm`; the parser is chosen
  by the provider's plugin (`_catalogue_parser`), so adding a provider with
  its own catalogue dialect later is one function.
- A community file can be wrong. The defence is not trust but flow: discovery
  shows the figures before anything is adopted, imports are append-only and
  effective-dated, and a divergence surfaces in the reconciliation report
  exactly as a mis-typed manual price would.
