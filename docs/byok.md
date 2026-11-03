# Bring your own key

A person holding the `byok` capability can add **their own** providers (with
their own API key) and models, exactly the way an administrator adds the
deployment's, and use them privately. Nobody else sees or uses them. Their usage
is **recorded for their own information** and nothing else: it is never counted
by a quota and never appears in a group, billing or administrator report.

This page has two halves: [for administrators](#for-administrators) (the
capability, the settings, the network guard, what is excluded) and
[the API](#the-api) (endpoints, payloads, naming, errors), written for whoever
builds a client or a screen against it.

## For administrators

### The capability

`byok` is a per-group flag, managed exactly like `api_keys` and
`local_connectors`: a person holds it as an administrator, or through an active
membership in an active group with the flag on ([Identity](identity.md#chat-only-people)).
It needs no chat (unlike `code_agents`): it is reported on `/v1/me` and
`/api/me` either way.

| Where | What |
|---|---|
| Console, **Groups** | the per-group `byok_allowed` flag (`PATCH /api/admin/groups/{id}`) |
| Console, **Settings** | the default new groups get, and a bulk on/off (`POST /api/admin/groups/capabilities` with `"capability": "byok"`) |
| `ACCESS_BYOK` / `GATEWAY_ACCESS__BYOK` | the default `byok` flag new groups are created with: `on` or `off`. **Default `off`**, unlike the other capabilities: it lets a person send traffic, with their own credential, to a host of their choosing, which is a decision for the deployment. Existing groups are never changed by it. The console's Settings page decides once it saves (`byok_mode` on `PUT /api/admin/oidc/policy`) |

A person holding the capability needs **no billing group** to use their own
models: those bill nothing, so a missing, disabled or no-longer-member default
group does not stop `my/...`. Deployment models still need one and are refused as
before, and `/v1/models` lists only their own models while they have none.

Switching it off for a person does **not delete** anything. Their private
providers and models stay in the database and stop working (`my/...` answers
403 `byok_not_allowed`, and the models drop out of `/v1/models`); switching it
back on restores them as they were.

### The network guard

A private provider's base URL is typed by a member, and the gateway then calls
it with a credential and a body they chose. Unguarded that is a way to reach
everything the gateway can reach: the cloud metadata address
(`http://169.254.169.254/`), `http://postgres:5432`, `http://gateway:8000/api/admin`,
a laptop on the office network. So for a **private** provider (deployment
providers are untouched) the host must resolve **only to public addresses**.

Refused: loopback, private (RFC 1918 and `fc00::/7`), link-local (including
`169.254.169.254` and `fe80::/10`), carrier-grade NAT (`100.64.0.0/10`),
multicast, unspecified, reserved and every other not-globally-routable range;
IPv4-mapped IPv6, NAT64 and 6to4 forms are judged by the IPv4 address inside;
this deployment's service names (`gateway`, `postgres`, `valkey`, `redaction`,
`authelia`, `caddy`, `extractor`, `cerea`, ...), any single-label host,
`localhost` and the `.local`, `.internal`, `.lan`, `.home.arpa` and `.localhost`
suffixes. A URL with credentials in it, or a scheme other than `http`/`https`,
is refused too.

It is enforced three times: at **create and update** (a 400,
`provider_url_blocked`, naming the rule and the address), before **every
request and probe** (so the caller is told which rule applied), and at **connect
time**, which is the one that cannot be raced. Connect-time works by pinning:
the gateway resolves the host itself, requires *every* address it got to be
allowed (a mixed answer is refused outright, not filtered), and opens the socket
to the validated IP, not to the name. The name is never resolved a second time,
so a DNS answer that changes between "check" and "connect" (rebinding) has no
window to act in. TLS is unaffected: the handshake still uses the real hostname
for SNI and certificate verification. Redirects are never followed by any
provider client, and `HTTP_PROXY` in the environment is ignored for these calls
so a proxy cannot route around the guard. A catalogue URL override
(`?url=`) is refused for a private provider.

**`byok_private_networks`** (`ACCESS_BYOK_PRIVATE_NETWORKS` /
`GATEWAY_ACCESS__BYOK_PRIVATE_NETWORKS`, or **Settings**; default **off**) lifts
the guard for private providers, for a deployment whose members legitimately run
endpoints on its network (a lab's vLLM). It never opens this deployment itself:
loopback, link-local (the cloud metadata address), this stack's service names,
and every subnet the gateway's own interfaces are on (in a container, the compose
networks: its database, cache and siblings) stay refused, so a member can reach
their LAN but never the gateway's own services. It applies to the very next request,
and turning it back off refuses the next request even over a connection opened
while it was on. It never needs setting for providers on the public internet.

### What a member may add

Language-model providers only: a plugin of kind `provider` or `router` speaking
the OpenAI or Anthropic protocol. Not a search backend, not the document
extractor, not a decision provider. Models of kind `chat`, `embedding`, `image`
and `transcription`; not `ocr`, `decision` or `search`. Never public, never
granted to a group, never the target of an alias or a route. A member may hold
at most 25 providers and 500 models. Their credential is encrypted exactly like
a deployment provider's and **never returned**, by any endpoint, to anyone.

### What is excluded, and where

BYOK usage is written to the ledger (tokens, cost at the **member's own price
rows**; an unpriced private model has no cost) with `usage_records.byok = true`,
the member's `user_id` and **no billing group**, under the model name
`my/<name>`. From there:

- **Quotas.** No rule is evaluated and no counter moves: the request reserves
  nothing (an empty reservation, so every exit still settles). A group cap of
  zero does not stop a BYOK request. The ledger-derived counters (cold-cache
  rebuilds, quota health, reconcile) skip BYOK rows, so a rebuild cannot
  resurrect usage the live path never counted. `x-bill-to` is irrelevant.
- **Reports.** `GET /api/admin/reports/usage` (and its CSV), `/api/admin/usage`,
  `/api/admin/requests` (and its CSV), the billing-group totals and the
  reconciliation block all leave BYOK rows out.
- **Where it does show.** In the owner's own usage only: `GET /v1/pystino/usage`
  and `GET /api/me/usage` carry it as a separate `usage.own_keys` total (every
  existing field keeps its meaning), and `GET /api/me/reports/usage` and
  `/api/me/requests` take `own_keys=true` to report only it. Administrators see
  per-provider and per-model totals on [their own view](#administrators-view).
- **Redaction applies.** BYOK traffic goes through the same redaction as any
  request, under the same effective policy. There is no exemption.

### Administrators' view

`GET /api/admin/byok/providers` and `/models` list every user's private rows:
owner (email and name), the provider's name, type and **host** (never the full
URL, whose path can carry a token), models, enabled, last used and usage totals.
**Never the credential**: not the key, its hint, its ciphertext or header
values. Rows can be held and released:

| Call | Effect |
|---|---|
| `POST /api/admin/byok/providers/{id}/disable` / `/enable` | hold or release a provider |
| `POST /api/admin/byok/models/{id}/disable` / `/enable` | hold or release a model |

A hold is its own flag (`admin_disabled`), separate from the owner's own
`is_active`, so the owner cannot undo it; the row is kept. Both calls are
written to the [audit log](console.md#the-administrator-audit-log) (`byok.provider.disable`,
`byok.provider.enable`, `byok.model.disable`, `byok.model.enable`). A member's
own edits are not: the audit log records administrators' acts, and would
otherwise carry one person's private endpoint names to everyone who reads it.

The ordinary admin catalogue routes (`/api/admin/providers`, `/models`, grants,
aliases) see deployment rows only; a private id is a 404 there.

Deleting a user deletes their private models and providers with them (the
delete preview counts them), and a merge **drops** the merged-away account's.
The ledger keeps its rows either way, like every other usage row.

## The API

Everything under `/api/me/` is **cookie-session only**, like the rest of `/api`.
`/v1` is where the models are *used*, with an API key or an access token.

### Capability and errors

`GET /v1/me` and `GET /api/me` report `capabilities.byok` (boolean).

Every `/api/me/providers*`, `/api/me/models*`, `/api/me/provider-plugins` and
`/api/me/provider-catalogue` call needs it: without it the answer is
**403 `byok_not_allowed`** ("Bring-your-own-key is not enabled for this
account..."). Using a `my/...` model on `/v1` without it is the same 403. A row
that is not the caller's (another user's, or a deployment's) is **404**, never
403. Other codes:

| Status / code | When |
|---|---|
| 400 `provider_url_blocked` | the base URL fails the [network guard](#the-network-guard) |
| 400 `byok_plugin_not_allowed` | the provider type is not a language-model provider |
| 400 `byok_kind_not_allowed` | provider kind other than `provider`/`router`, or a model kind other than `chat`, `embedding`, `image`, `transcription` |
| 400 `byok_header_not_allowed` | `extra_headers` names `Host`, `Content-Length`, `Transfer-Encoding`, `Connection`, `X-Forwarded-*` and similar |
| 400 `byok_not_shareable` | `is_public: true` |
| 400 `byok_catalogue_url` | a catalogue `url=` override |
| 400 `byok_limit_reached` | 25 providers or 500 models already |
| 400 | a model `name` starting with `my/` (reserved) |
| 409 | a name the caller already uses (provider names and model names are unique **per owner**), or deleting a provider that still has models |

### Endpoints

Same operations and payloads as the administrators' (`ProviderCreateRequest`,
`ProviderUpdateRequest`, `ProviderResponse`, `ModelCreateRequest`,
`ModelUpdateRequest`, `ModelAdminResponse`, `PriceCreateRequest`,
`PriceResponse`, `CatalogueDiscoveryResponse`, `ModelImportRequest` ... in
`openapi.json`), on the caller's own rows:

| Call | |
|---|---|
| `GET /api/me/provider-plugins` | the provider types a member may pick: language-model types only |
| `GET /api/me/provider-catalogue` | the deployment's catalogue entries a member could use, to prefill type and endpoint |
| `GET /api/me/providers` | the caller's providers (paginated envelope: `items`, `total`, `limit`, `offset`) |
| `POST /api/me/providers` | create. `name`, `base_url` (omit when the type has a default), `api_key` (write-only), `plugin`, `kind` (`provider`/`router`), `extra_headers`, `prefix`, `billing_mode`, `catalogue_id` |
| `PATCH /api/me/providers/{id}` | change. `api_key`: omitted keeps, a value rotates, `""` clears |
| `DELETE /api/me/providers/{id}` | 204; 409 while models use it |
| `POST /api/me/providers/{id}/test` | probe the provider with its stored key; always 200, `ok` says how it went |
| `GET /api/me/models` | the caller's models (`?q=`, `?provider_id=`, `?include_inactive=`) |
| `POST /api/me/models` | create. `name` (**without** `my/`), `upstream_model`, `provider_id` (one of the caller's), `kind`, limits, modalities |
| `GET` / `PATCH` / `DELETE /api/me/models/{id}` | read, change, remove (prices go with it; the ledger keeps its rows) |
| `GET /api/me/models/{id}/prices` | price history, newest first |
| `POST /api/me/models/{id}/prices` | append a price row (never edited). Cost is computed at these rows; none means no cost |
| `GET /api/me/models/discover?provider_id=` | compare the provider's own `/models` with the caller's models; `fill_missing_prices`, `tag` as for administrators; **no `url`** |
| `GET /api/me/models/tags?provider_id=` | the provider's catalogue tags, where it has any |
| `POST /api/me/models/import?provider_id=` | adopt selected models (with the listed price or a `manual_price`); only the allowed kinds import |

Credentials: `ProviderResponse` carries `has_api_key` and a masked
`api_key_hint` and never the key.

### Naming on `/v1`

A private model is addressed **`my/<name>`** on every `/v1` surface (chat
completions, responses, messages, embeddings, images, transcriptions,
`/v1/models`, `/v1/models/{id}`), and the response's `model` is the name the
caller sent. The prefix belongs to the API and is not stored: the same row is
`name` in the owner's console. `my/<name>` always means the caller's own model,
so it can never shadow a deployment one (and a stored name may not begin with
`my/`).

`GET /v1/models` lists the caller's own models with `"byok": true` (deployment
entries carry `false`); older clients that ignore or `.nullish()`-parse the field
are unaffected. Another user's private models are never listed.

### Usage

`GET /v1/pystino/usage` and `GET /api/me/usage`: the existing figures are what
counts against quotas and group spend; `usage.own_keys` is `{window_seconds,
requests, total_tokens, cost, currency, estimated_requests}` for the caller's own
keys. `GET /api/me/reports/usage?own_keys=true` and
`GET /api/me/requests?own_keys=true` (and `.csv`) report only that usage; without
the flag it is left out. Rows are named `my/<name>`.

### Administrators

`GET /api/admin/byok/providers`, `GET /api/admin/byok/models`
(`?q=`, `?owner_id=`, models also `?provider_id=`), and
`POST /api/admin/byok/{providers,models}/{id}/{disable,enable}`: see
[the administrators' view](#administrators-view). The admin group, user and
policy payloads gain `byok_allowed` (groups, users) and `byok_mode`,
`byok_private_networks` (policy).
