# An MCP gateway: authentication, access, confidentiality

- Date: 2026-09-04
- Status: **proposal, nothing built, not approved to build**
- Asked as: "I'm considering implementing an MCP gateway. Review how LiteLLM
  implements it and propose a plan", then narrowed: *"I care about
  authorization, confidentiality, access control"*, *"we need also passthrough
  OIDC"*, *"we need to support exposing stdio mcps as endpoints too"*, and
  finally the case in §5 — a service whose only way in is a browser flow.
- **Billing and audit are out of scope by instruction.** §8 says what that
  leaves undone, in two sentences, so nobody re-derives it as an oversight.
- Everything attributed to LiteLLM or to the MCP specification was read at
  source on 2026-09-04; URLs in §10. The first version of this document led with
  accounting, which is what this repository is about but not what was asked;
  this one is organised around the three properties above.

## 1. What it is, in one paragraph

Pystino becomes the single MCP endpoint that clients point at — Claude Desktop,
Claude Code, Cursor, an agent — instead of pointing at real MCP servers. The
real servers are registered behind it. Clients never reach them directly and,
in three of the four upstream modes in §4, never hold their credentials. The
gateway decides **who is calling** (§3), **which tools they can see** (§6),
**what credential reaches the upstream** (§4, §5), and **what leaves in the
arguments** (§7).

## 2. What LiteLLM built, factually

A reverse proxy for MCP servers, structurally what this gateway already is for
LLM endpoints.

- **Surface.** JSON-RPC at `/mcp` and `/{server_name}/mcp`; a REST pair
  `/mcp-rest/tools/list` and `/mcp-rest/tools/call` for calling a tool with no
  LLM involved; management at `GET /v1/mcp/server`.
- **Registration** in `config.yaml` under `mcp_servers`, with `server_name`,
  `url`, `transport`, and `available_on_public_internet` to keep a server off
  the public surface. Internal ranges are named with `mcp_internal_ip_ranges`.
- **Namespacing.** Tools from every server are flattened into one list and
  prefixed `{server_prefix}{separator}{upstream_tool_name}`, separator `-`, so
  `github_mcp-search_issues`. A client narrows with an `x-mcp-servers` header or
  by URL (`/github_mcp,zapier/mcp`).
- **Permissions** attach to six subject kinds — keys, teams, end users, agents,
  internal users, organisations — with server-level grants (`mcp_servers`,
  `mcp_access_groups`, `allow_all_keys`), tool-level filtering
  (`mcp_tool_permissions` as `Dict[server_id, List[tool_name]]`,
  `allowed_tools`/`disallowed_tools` at registration), parameter-level
  (`allowed_params` as `Dict[tool_name, List[param_names]]`), and a per-server
  `mcp_rpm_limit`. Resolution is an intersection, "most-restrictive wins", with
  the organisation as a ceiling and a `no-mcp-servers` sentinel to deny a key
  everything.
- **Transports:** Streamable HTTP, SSE, and stdio (see §9).

Worth knowing as a thing to avoid rather than copy: their issue #29800 reports
`serverInfo.name` hardcoded to `litellm-mcp-server` for every `/mcp/{alias}`
endpoint — what happens when the aggregate identity is written once and each
server's own identity is an afterthought.

## 3. Inbound: who is calling

**Most of this already exists.** ADR 0040 put OIDC access tokens on `/v1`
alongside API keys, gated by `GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE` — set the
audience and tokens naming it are accepted. An MCP endpoint reuses that same
principal resolver and inherits both credentials for nothing.

Four inbound cases, in the order I would build them:

1. **`gwk_` API key** in `Authorization: Bearer`. Works with any client that
   lets you set a header. Revoking it cuts off LLM and tool access together,
   which is the property worth having.
2. **OIDC access token**, audience-gated, exactly as `/v1` accepts it today.
   This is what makes a per-user linked credential (§5) possible without
   inventing a second identity system.
3. **OAuth, driven by the client** — the spec's own model. MCP servers **MUST**
   implement RFC 9728 Protected Resource Metadata and return
   `WWW-Authenticate` on a 401 pointing at it; clients **MUST** use it for
   discovery. This is not politeness: Claude Desktop, Claude Code and Cursor
   drive OAuth themselves and have nowhere to type a custom header, so without
   it they cannot connect at all. LiteLLM's `dcr_bridge: true` exists for
   exactly these clients. **Until this exists we are usable but not conformant
   on authorization, and the doc should keep saying so.**
4. **Nothing of ours** — `true_passthrough`, §4.

## 4. Upstream: what credential reaches the server

Four modes, per registered server. LiteLLM has all of them and the matrix is
worth copying wholesale.

| Mode | Client sends | Upstream receives | Do we know the user? |
|---|---|---|---|
| **Static** | our key or OIDC token | the server's own stored secret | yes |
| **Delegated** (`oauth_delegate`) | admission in one header, upstream token in `Authorization` | the client's upstream token, uninspected | yes |
| **Exchanged** (`oauth2_token_exchange`) | our key or OIDC token | a token exchanged at the IdP for that server's audience | yes |
| **Linked** (§5) | our key or OIDC token | that user's own credential, collected once via a browser | yes |
| **Passthrough** (`true_passthrough`) | nothing of ours | the client's token verbatim | **no** |

Notes that matter:

- **Static** is what we already do for LLM providers: one credential, encrypted
  with `SecretBox` (ADR 0027), rotated centrally, never seen by users. It is
  also a service account — every user gets the upstream's full authority — which
  is why §5 exists.
- **Exchanged** is RFC 8693 OAuth 2.0 Token Exchange: the caller's token is the
  `subject_token`, posted to the IdP's exchange endpoint with the server's
  `audience`, and only the exchanged token reaches the upstream. Config keys, in
  LiteLLM's spelling: `auth_type: oauth2_token_exchange`,
  `token_exchange_endpoint`, `client_id`, `client_secret`, `audience`, `scopes`.
  Entra ID needs `token_exchange_profile: entra_obo`, which uses the RFC 7523
  `jwt-bearer` grant with the caller's token as `assertion` instead.
  **This is "passthrough OIDC" done properly**, and it is the mode that lets the
  upstream do its own per-user authorization.
- **Passthrough** relays blindly and, in LiteLLM's own words, *"spend tracking,
  per-key rate limits, and any guardrail depending on `user_api_key_auth.user_id`
  do not run"*, with the warning to *"only enable it on servers whose upstream
  OAuth issuer you trust to enforce access control"*. It is a TCP relay with
  OAuth discovery attached. Worth having; worth labelling in the console as the
  mode where our access control does not apply.

**On the specification's prohibition, because the first version of this document
overstated it.** The spec says an MCP server *"**MUST NOT** pass through the
token it received from the MCP client"* to an upstream API. That forbids
relaying a token *issued for us* — it does not forbid acting on behalf of a
user. Exchange (RFC 8693) and a separately-obtained upstream token (delegated,
linked) are the sanctioned ways, which is why the matrix above has four rows and
not one.

## 5. The linked-account browser flow

**The case:** a service whose own authentication is a browser flow, with no
bearer token to hold and nothing to exchange. We need an endpoint that triggers
that flow and ends with the gateway holding *that user's* credential.

### The precondition that decides whether this is buildable

Does the service's browser flow **end by redirecting to a URL we register**,
handing us a code or a token?

- **Yes** — even if they never say "OAuth" — then we are its client and
  everything below works.
- **No**, it ends with a session cookie in the user's own browser and hands
  nothing to a third party — then the credential physically never reaches us.
  The only remaining shapes are a headless browser driven with the user's
  password, which should not be built, or letting the client talk to that
  service directly and leaving it out of the gateway. **This is the open
  question in §11 and it gates the work.**

### The pieces

**A per-user credential row**, keyed `(user_id, mcp_server_id)`, holding the
encrypted credential, its expiry and its scopes. Distinct from the server's
static secret and from an exchanged token: this one is collected once,
interactively, and belongs to a person.

**Two endpoints**, deliberately the same shape as the sign-in machinery that
already exists:

- `GET /mcp/connect/{server}` — begins the upstream flow: PKCE, `state` in a
  signed cookie, redirect to the service's authorize URL.
- `GET /mcp/connect/{server}/callback` — the service lands here; verify `state`,
  exchange the code using our registered client credentials, encrypt and store
  against that user, render "connected, return to your client".

Copy `/auth/callback/{provider_name}` rather than reinventing it: namespaced per
server so that, as that route's comment puts it, "two directories cannot deliver
a code to the wrong flow", with `state` as an anti-CSRF nonce in a signed flow
cookie.

**How the agent learns it must happen**, in this order:

1. If the client declared the `elicitation` capability, send
   `elicitation/create` with the connect URL in `message` and a boolean
   acknowledgement field, then retry the call. Note the hard constraint: the
   spec says *"Servers **MUST NOT** use elicitation to request sensitive
   information"*, so this hands over a URL and takes a confirmation — it never
   collects a credential.
2. Otherwise, and this is the universal path since few clients implement
   elicitation yet, **fail the tool call with an error whose text carries the
   connect URL.** The human sees it in the transcript and clicks it.

**The link is one-time, short-lived and user-bound.** An agent will put that URL
in a transcript, a log, possibly a shared channel. So: an opaque token, minutes
long, bound to `(user, server, nonce)`, consumed on first use — whoever opens it
is about to have a credential attached to their account.

### Two consequences to accept before choosing this for a service

- **If the service issues no refresh token**, every expiry is another browser
  trip. "Reconnect your account" twice a day is a product decision, not a bug.
- **List the tools even when the user has not connected.** Hiding them teaches
  the agent the capability does not exist, so it never triggers the flow. A
  grant controls visibility; connection state controls success.

## 6. Access control: tools are an allowlist

Copy the models rule, which is already the house pattern: *"a model is invisible
to callers until a group is granted it."* So a tool is invisible until granted,
and `tools/list` returns **only** what this caller may call.

That is a security property, not a tidiness one: an agent cannot attempt what it
cannot see, and a prompt injection cannot name a tool that was never in the
list. Refusing at call time instead would still leak the inventory.

- Grants per server **and** per tool within a server, union of group and user
  grants, as `access.py` already computes for models.
- **Separate state-changing tools from read-only ones.** "Search Jira" and
  "close a ticket" should not arrive on the same grant. This wants a flag per
  tool, set at registration and re-checked when the upstream's tool list
  changes.
- Use the **models** precedence rule, not the other two this codebase has:
  quotas are *all rules must pass*, redaction is *any applicable scope requiring
  it wins*, model access is *granted or invisible*. A tool is a capability, so
  it is the third.

A tool list is not static — servers add and rename tools. So a newly appeared
tool must default to **not granted**, and the console needs to show that a
server is offering something nobody has approved. The provider-catalogue drift
warning is the shape to copy.

## 7. Confidentiality: what leaves in the arguments

Tool arguments are user-written text going to a third party, which is what
prompts are, so ADR 0037's machinery applies to them. Redaction scopes today are
provider, model, group, user and API key (ADR 0038) — none of which name an MCP
server, so this needs `RedactionScope.MCP_SERVER`.

Two hazards specific to tools:

- **A tool result is not assistant text.** It is structured, typed content
  blocks, often JSON. The restore step walks assistant-text fields today
  (`protocols.py:rewrite_whole`); MCP needs a new reader there — which is the
  right home, since that file's job is "where the interesting fields live in a
  frame".
- **A placeholder in an argument may break the tool.** A redacted address passed
  to `send_email` is not a smaller privacy problem, it is a failed call. The
  per-entity `block` mode may be the right default for some arguments, which
  makes this a per-tool policy decision an operator has to be able to make. The
  first version applies the catch-all rule like everywhere else and records this
  as a known sharp edge.

## 8. Billing and audit: deliberately out of scope

Not wanted, and cheap to add later if that changes: a tool call is one unit, and
`accounting/cost.py:383` already multiplies an image count by `per_image` for a
surface with no tokens, so a `per_call` rate would be a third case in the one
file allowed to compute money. Nothing else in the plan depends on it.

## 9. stdio servers: where they run

**In LiteLLM, inside the proxy's own container.** It spawns the subprocess and
manages its lifecycle:

```yaml
mcp_servers:
  circleci_mcp:
    transport: "stdio"
    command: "npx"
    args: ["-y", "@circleci/mcp-server-circleci"]
    env:
      CIRCLECI_TOKEN: "your-circleci-token"
```

It can also map request headers into the child's environment with
`${X-HEADER_NAME}` — `X-GITHUB_PERSONAL_ACCESS_TOKEN` becoming
`GITHUB_PERSONAL_ACCESS_TOKEN` in the process.

**I would not run them in the gateway container**, for one specific reason
rather than taste: that container holds the PostgreSQL credentials and the
`SecretBox` key that decrypts every stored provider credential. `npx -y
@vendor/thing` there is third-party code, fetched from a registry at start,
running with the gateway's filesystem, network position and secrets. On the
smaller development host it is also 3 GB of RAM shared with PostgreSQL, Valkey
and Presidio.

**Proposed shape:** a separate `mcp-runner` service in the compose stack — no
gateway secrets in its environment, its own memory and CPU limits, reachable
only on the compose network — with the gateway speaking to it over HTTP and the
runner owning the stdio subprocesses. One image, one place to reason about what
third-party code can touch.

**The isolation question to settle inside the runner**, because it is a
confidentiality bug if got wrong: environment is per *process*, so a long-lived
subprocess shared between callers gives the second caller the first caller's
token in its environment. Per-caller credentials therefore require a process per
caller or per session, with a lifecycle and a cap on concurrent processes.
LiteLLM's documentation does not say which they do; their code would have to be
read before trusting either answer.

## 10. Sources, read 2026-09-04

- LiteLLM: [MCP deployment](https://docs.litellm.ai/docs/mcp_deployment),
  [overview](https://docs.litellm.ai/docs/mcp),
  [permission management](https://docs.litellm.ai/docs/mcp_control),
  [OAuth passthrough](https://docs.litellm.ai/docs/mcp_oauth_passthrough),
  [on-behalf-of auth](https://docs.litellm.ai/docs/mcp_obo_auth),
  [cost tracking](https://docs.litellm.ai/docs/mcp_cost),
  [REST API](https://docs.litellm.ai/docs/mcp_rest_api),
  issue [#29800](https://github.com/BerriAI/litellm/issues/29800).
- MCP specification 2025-06-18:
  [transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports),
  [authorization](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization),
  [elicitation](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation).
- Ours: [ADR 0040](adr/0040-bearer-tokens-on-v1.md) (OIDC access tokens on
  `/v1`), [ADR 0027](adr/0027-inference-providers.md) (credentials encrypted at
  rest), [ADR 0037](adr/0037-redaction-policy.md) and
  [ADR 0038](adr/0038-scoped-redaction.md) (redaction policy and scopes),
  [ADR 0051](adr/0051-settings-identity-and-email.md) (per-connection OIDC
  callbacks, the shape §5 copies).

## 11. Open questions, in the order they block work

1. **Does the browser-only service's flow redirect back to a URL we register,
   with a code or token?** §5 is buildable if yes and mostly not if no. Nothing
   else in §5 matters until this is answered.
2. **Which inbound credentials must work on day one?** If Claude Desktop or
   Cursor are targets, RFC 9728 discovery (§3.3) is not optional and is the
   largest single piece of the authentication work.
3. **Per-caller stdio processes, or shared?** Decides the runner's design and,
   if got wrong, leaks one user's credential into another's process (§9).
4. **Does a state-changing tool need approval per call**, or is the grant the
   whole control (§6)?
