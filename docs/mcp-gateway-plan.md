# An MCP gateway: what LiteLLM built, and what this one should be

- Date: 2026-09-04
- Status: **proposal, nothing built**
- Asked as: "I'm considering implementing an MCP gateway. Review how LiteLLM
  implements it and propose a plan."
- Reading order if you are short of time: §2 (the one thing we already have that
  LiteLLM does not), §5 (the security rule that shapes the design), §7 (the
  sequence).

Everything quoted from LiteLLM and from the MCP specification was read at source
on 2026-09-04; the URLs are in §8. Where a claim is theirs and unverified by us,
it says so.

## 1. What LiteLLM built

LiteLLM's MCP gateway is a **reverse proxy for MCP servers**, exactly analogous
to what this gateway is for LLM endpoints: many upstream servers behind one
address, one credential at the front, and a policy layer in the middle.

**The surface.** A JSON-RPC MCP endpoint at `/mcp`, plus `/{server_name}/mcp`
for one named server, and a REST pair — `/mcp-rest/tools/list` and
`/mcp-rest/tools/call` — for calling a tool without an LLM in the loop.
Management lives at `GET /v1/mcp/server`. Three transports are documented:
Streamable HTTP, SSE, and stdio.

**Registration** is config-file shaped, under `mcp_servers`:

```yaml
mcp_servers:
  - server_name: internal-db
    url: http://db-mcp.internal:8000/mcp
    transport: http
    available_on_public_internet: false
```

**Namespacing.** Tools from many servers are flattened into one list and
prefixed: `{server_prefix}{separator}{upstream_tool_name}`, separator `-` by
default, so `github_mcp-search_issues`. A client narrows the list with an
`x-mcp-servers` header, or by URL (`/github_mcp,zapier/mcp`).

**Client auth** is their own proxy key — "the same auth (LiteLLM API key)" that
protects the LLM routes, `Authorization: Bearer sk-...`.

**Upstream auth** is per server, with `api_key`, `bearer_token`, `basic`,
`authorization`, `oauth2` and `aws_sigv4` supported, and an
`upstream_token_header` for counterparties that want the credential somewhere
other than `Authorization`. A client may also pass a credential through with
`x-mcp-auth` or `x-mcp-{server_alias}-{header_name}`.

**Permissions** are the most developed part, and worth studying rather than
copying wholesale. They attach to six kinds of subject — keys, teams, end users,
agents, internal users, organisations — with server-level grants
(`mcp_servers`, `mcp_access_groups`, `allow_all_keys`), tool-level filtering
(`mcp_tool_permissions` as `Dict[server_id, List[tool_name]]`,
`allowed_tools`/`disallowed_tools` at registration), parameter-level control
(`allowed_params` as `Dict[tool_name, List[param_names]]`), and a per-server
`mcp_rpm_limit`. Resolution is an intersection — "most-restrictive wins" — with
the organisation as a ceiling, and a `no-mcp-servers` sentinel to deny a key
everything.

**Cost** is a fixed price per call, configured per server:

```yaml
mcp_servers:
  zapier_server:
    mcp_info:
      mcp_server_cost_info:
        default_cost_per_query: 0.01
        tool_name_to_cost_per_query:
          send_email: 0.05
```

The figure lands in their log as `response_cost`. A `CustomMCPCostTracker` class
can compute it from the response instead. Their documentation does not say what
happens when no price is configured — which matters, and §4 takes a position on
it.

## 2. What we would be building that they are not

Their gateway routes and permits. This one **accounts**. That difference is the
whole reason to write a plan rather than start typing.

Three properties this repository already holds that an MCP feature must not
break, each of which their design does not have to think about:

1. **A price is a decision with provenance.** `model_prices` is append-only and
   effective-dated, and every row records who supplied the figure —
   `manual`, `catalogue`, `community` (ADR 0053). A per-call tool price is a
   price and belongs in that history, not in a config file that the next
   deployment overwrites silently.
2. **Nothing is billed from a table nobody maintains.** An unpriced model
   reserves nothing and therefore has no cost ceiling at all;
   `unpriced_model_count` on the provider listing is the standing warning. The
   same rule has to hold for tools, and §4 says what it means.
3. **The counterparty's figure and ours are both recorded, always.** `cost`,
   `computed_cost`, `upstream_cost` and `cost_source` (ADR 0032) exist so that a
   divergence is reconstructable. An MCP server that reports no cost is the
   normal case, so `own_prices` is the only mode that applies — but the columns
   should be populated on the same terms, not left null because "MCP is
   different".

## 3. The metering shape, and the precedent that makes it cheap

**A tool call is one unit, priced like an image.** This is not an analogy
invented for the plan: `accounting/cost.py:383` already does
`counts.images * price.per_image`, added by ADR 0030 for a surface that produces
no tokens at all. The one place in the codebase permitted to multiply a count by
a rate already knows how to charge per unit rather than per token.

So the arithmetic is a third case beside tokens and images — `per_call` on the
price row — and `accounting/cost.py` stays the only file that computes money.
Nothing in the plugin layer prices anything, per ADR 0032's load-bearing rule.

What that leaves to decide, and both answers are cheap:

- **Do tool arguments and results count tokens?** They are text, and an
  argument blob can be large. My proposal: **no token metering in the first
  version**, and `total_tokens = 0` on the row. A count we cannot tie to a rate
  anyone charges is a number that invites arithmetic nobody asked for. Revisit
  only if a real MCP counterparty prices by size.
- **What does `usage_source` say?** `measured` is wrong — nothing measured
  tokens. Either a new member or reuse of `estimated` with the disclosure
  reworded. This wants an ADR sentence, not a guess, because
  `_disclosures` in `reporting.py` reads that field to tell an operator how much
  of a total is inferred — and it already has one bug of exactly this kind (it
  blames the provider for client disconnects).

## 4. Pricing, and the unpriced case

Follow the model rule rather than LiteLLM's: **an unpriced tool is visible,
callable only if an administrator says so, and reserves nothing.** LiteLLM's
docs are silent here; silence resolves to "free", and a tool call that is
actually billed by Zapier while our ledger says €0 is the exact failure the
`own_prices_fallback` machinery exists to make impossible for models.

Concretely:

- `per_call` lands on `model_prices` (or its MCP sibling — see §6) through the
  same append-only, effective-dated write as any other price, stamped
  `manual` since no MCP server publishes a price catalogue today.
- An **unpriced tool count** per server, mirroring `unpriced_model_count`, on
  the listing the console renders. The absence has to be visible, because the
  absence is the risk.
- Quota admission for an unpriced tool reserves nothing, which means **no cost
  ceiling** — identical to an unpriced model, and worth stating in the console
  in the same words rather than new ones.

## 5. The security rule that shapes everything: no token passthrough

The MCP specification is explicit, and it is a **MUST NOT**:

> "If the MCP server makes requests to upstream APIs, it may act as an OAuth
> client to them. The access token used at the upstream API is a separate token,
> issued by the upstream authorization server. The MCP server **MUST NOT** pass
> through the token it received from the MCP client."

We are in exactly the position that rule is about: a client presents a
credential to us, and we call a third party on their behalf. So:

- The caller's `gwk_` key authenticates them **to us** and is never forwarded.
- Each MCP server carries its **own** credential, encrypted at rest with the
  same `SecretBox` that holds provider keys (ADR 0027). This is the shape we
  already have, so the compliant design is also the cheap one.
- LiteLLM's `x-mcp-auth` — a client supplying its own upstream credential —
  is the feature to **leave out of the first version**. It is useful, and it is
  a confused-deputy hazard the spec devotes a section to. If it lands later it
  needs its own ADR, and the reason it was deferred belongs in it.

**Client authentication.** Two options, and they are not exclusive:

1. **Our bearer key on the MCP endpoint** (ADR 0040 already put bearer tokens on
   `/v1`, ADR 0046 issues them locally). Works today with any client that lets
   you set a header. This is what LiteLLM does, and it is the first version.
2. **The spec's OAuth flow.** The spec says MCP servers **MUST** implement
   RFC 9728 Protected Resource Metadata and return `WWW-Authenticate` on 401
   pointing at it, and clients **MUST** use it for discovery. A client with a
   built-in OAuth flow and no header field will not connect without this. We
   have an OIDC stack and per-connection callbacks already, so it is reachable —
   but it is a second ADR, not a paragraph in the first one, and the honest
   framing is that **without it we are not spec-conformant on authorization**,
   only usable.

**Transport.** Streamable HTTP only, first version: one endpoint path serving
both POST and GET, `Mcp-Session-Id` echoed on every subsequent request,
`MCP-Protocol-Version` validated (400 on unsupported), and the `Origin` header
validated — the spec calls out DNS rebinding by name. stdio is a client-side
transport for subprocess servers and is not something a hosted gateway serves;
supporting stdio *upstream* (we launch a subprocess) is a separate question and
my proposal is not to, because a subprocess per session inside the gateway
container is a resource model this deployment has no answer for.

Three things we get for free and should not re-solve: Caddy already streams with
`flush_interval -1` for every `/v1` surface, our SSE pipeline exists, and
`FORWARDED_ALLOW_IPS` is already set so forwarded headers are believed (ADR
0035).

## 6. Where it goes in the data model

**A new table, not `providers` with a third kind.** `providers.kind` already
distinguishes `provider` from `router` (ADR 0032), and a third value is
tempting, but the fields do not overlap enough: an MCP server has a transport, a
session, a tool list that changes under it, and no notion of a served model. The
cost of a shared table is that every provider screen and every provider plugin
grows a branch for a thing that is not an LLM endpoint.

What to reuse instead, deliberately:

| Concern | Reuse |
|---|---|
| Credential at rest | `SecretBox`, as `providers.api_key_encrypted` does |
| Access | the **allowlist** shape: a tool is invisible until a group or user is granted it, union of the two, as `access.py` does for models |
| Prices | append-only + effective-dated + `PriceSource`, as `model_prices` does |
| Listing | `pagination.py`'s envelope, like every other management route |
| Vendor quirks | a plugin per auth scheme, returning facts and never money |

**Tool naming.** Copy LiteLLM's prefix, because a flattened list needs it and
their separator choice is already what clients see: `{server}-{tool}`. Note
their live bug as a thing to avoid — issue #29800 reports `serverInfo.name`
hardcoded to `litellm-mcp-server` for every `/mcp/{alias}` endpoint, which is
what happens when the aggregate identity is written once and the per-server
identity is an afterthought.

**Access precedence.** Use the models rule (allowlist, union of group and user
grants), **not** the quota rule and **not** the redaction rule. All three exist
here and they differ on purpose: quotas are *all rules must pass*, redaction is
*any applicable scope requiring it wins*, and model access is *granted or
invisible*. A tool is a capability, so it is the third.

## 7. Redaction is the part that will bite

Tool arguments are prompt-shaped text sent to a third party. Everything ADR 0037
established about prompts applies to them, and the machinery is scoped by
provider, model, group, user and API key (ADR 0038) — none of which name an MCP
server.

So an MCP gateway needs `RedactionScope.MCP_SERVER`, and the request path needs
the detect-and-substitute step applied to **arguments on the way out** and the
restore step applied to **results on the way back**. The `_metered` pipeline
already sequences that for five surfaces; this is a sixth, and the honest
estimate is that it is the largest single piece of work in the plan.

Two hazards specific to tools:

- **A tool result is not assistant text.** It is structured content, often JSON.
  The restore step walks assistant-text fields today
  (`protocols.py:rewrite_whole`); for MCP it has to walk a content array of
  typed blocks. That is a new reader in `protocols.py`, which is the right home
  — it is exactly "where the interesting fields live in a frame".
- **Placeholders in an argument may break the tool.** A redacted email address
  passed to `send_email` is not a smaller privacy problem, it is a failed call.
  The per-entity `block` mode exists for values whose presence is the incident;
  for tools, `block` may be the *right default on some arguments*, and that is a
  policy decision an operator must be able to make per tool. First version:
  the catch-all rule applies as it does everywhere else, and this is recorded as
  a known sharp edge rather than solved.

## 8. Sequence, and what each step buys

1. **The transport and one server, no policy.** Streamable HTTP at
   `/mcp`, our bearer key, one registered server, tools listed and callable,
   nothing billed. Buys: the protocol is right, verified against a real client.
2. **The ledger row.** A tool call writes a `UsageRecord` with `per_call` cost,
   zero tokens, and its own `cost_source`. Buys: the thing that makes this our
   gateway rather than a proxy. Needs tests specifically, per ground rule 3.
3. **Access and the console.** Registration screen, tool allowlist, grants,
   unpriced-tool warning. Buys: an administrator can run it.
4. **Quotas.** Admission on cost, which mostly falls out of step 2 if the row is
   right.
5. **Redaction.** The new scope, the argument path, the typed-content restorer.
6. **Later, each with its own ADR:** RFC 9728 + OAuth discovery for clients that
   need it; `x-mcp-auth` client-supplied upstream credentials; per-tool
   parameter allowlists; stdio upstream.

Steps 1–2 are the ones worth doing before deciding whether the rest is wanted:
they answer "does this deployment want to be in the MCP path at all" with a
working thing rather than an argument.

## 9. What this plan does not do

- **No agent/team model.** LiteLLM permits on six subject kinds including agents
  and organisations. We have users, groups and keys, and adding subject kinds to
  serve MCP would be the tail wagging the dog.
- **No `mcp_rpm_limit`.** Rate limiting per server is a real need and we have no
  request-rate quota metric at all today — only cost and tokens. It is a quota
  feature that happens to be wanted here, so it belongs in the quota engine on
  its own merits, not bolted to MCP.
- **No claim of spec conformance on authorization** until §5's option 2 exists.
- **No stdio, no client-supplied upstream credentials, no parameter filtering**
  in the first version.

## 10. Sources, read 2026-09-04

- LiteLLM: [MCP deployment](https://docs.litellm.ai/docs/mcp_deployment),
  [MCP overview](https://docs.litellm.ai/docs/mcp),
  [permission management](https://docs.litellm.ai/docs/mcp_control),
  [cost tracking](https://docs.litellm.ai/docs/mcp_cost),
  [REST API](https://docs.litellm.ai/docs/mcp_rest_api),
  and issue [#29800](https://github.com/BerriAI/litellm/issues/29800) for the
  `serverInfo.name` bug.
- MCP specification 2025-06-18:
  [transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports),
  [authorization](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization).
