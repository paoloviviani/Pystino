# Model aliases

An alias is a model name **you** choose that resolves to one model, or to
several that a [routing policy](#routing-several-targets-per-alias) chooses
between. Callers send it like any other name and never learn what is behind it.

The reason to want one is that access to a model and access to the name it is
sold under do not have to be the same thing. Without aliases, anyone who can use
`gpt-4o-mini` can use it. With them, you can say: everyone in the company may
use `fast`, and only Research may call the model behind it.

Names like `fast`, `default-chat` and `eu-reasoning` also stop being something
every client has to be told about when the model behind them changes. Retargeting
is one edit in the console and takes effect on the next request — there is no
cache to invalidate, and nothing a client has to change.

An alias is a row in the same `models` table as everything else, which is what
lets all of this work without any of it being special:

- **One name namespace.** A name is unique across models and aliases alike, and
  a caller cannot tell the two apart.
- **One access mechanism.** `is_public`, group grants and personal grants
  (`group_model_access`, `user_model_access`) apply to an alias exactly as they
  apply to a model, with the same console controls.
- **One catalogue.** Aliases appear in `GET /v1/models` and in a client's model
  picker with no second source.

## What access to an alias does and does not give

**Access is decided on the name the caller sent, and only on that name.** If
`fast` is granted to a group, that group can use `fast` — whether or not anyone
ever granted them the model behind it. That is the whole point: an alias is the
grant.

The corollary is worth being explicit about, because it is the part that is easy
to get wrong when you are reading a ledger: **billing follows the target.** The
usage row records the model that served the request, so prices, quota rules and
reports behave as if the target had been called. A quota rule on
`gpt-4o-mini` counts every request that arrived as `fast`, and `fast` has no
price of its own to differ by.

The ledger keeps both names. Every usage row carries the served model in
`model_id` and `model_name`, and the name the caller sent in
`requested_model` — null for an ordinary request. Reports show a "via alias"
count beside each row, so you can see how much of a model's spend arrived under
a name callers chose. It is a copy of a string rather than a foreign key, so a
retarget or a deletion never rewrites what a past request was.

## The card a caller sees

An alias appears in `GET /v1/models` like any other model:

- `id` is the alias's name;
- `display_name` and `description` are the alias's own, and null when you did not
  write them;
- the capabilities — `kind`, modalities, supported features, context window,
  output and input limits — are the **target's**, because the target is what
  will run the request and a card that under-promises is a request that fails.

Nothing in the card names the target: no upstream name, no provider, no id.
Retargeting has to be invisible to clients by design, and the model behind an
alias may be one the caller cannot see at all.

The one thing the target's provider decides is the `local` flag, which says
whether bytes stay on this server. That is a statement about where data goes
rather than an identity, and answering "no" for an alias onto this deployment's
own extractor would tell a client to send a document off-box that never had to
leave.

## When an alias cannot serve

An alias is **unavailable** when none of its targets can serve: every target
was deleted, is inactive, or sits behind an inactive provider. (An alias with
several targets stays available while any one of them can serve; the Routing tab
marks the ones that cannot.) A request naming an unavailable alias is refused
with:

```
503
code: alias_unavailable
"The model alias 'fast' has no available target."
```

It is 503 and not 404 on purpose. The caller's access was already decided
against the name they sent and it is fine; the alias is reachable and its target
is not. Answering 404 would tell a client to give up on a model that is there.

An unavailable alias is also left out of `GET /v1/models` — a card is a promise
that the name works — and shown as **Unavailable** with its reason in the
console, where it stays visible so you can fix it.

An alias with **one** target is never quietly pointed at something else: it
fails with the 503 above. Falling back to another model is what you opt into by
giving an alias several targets, and the ledger then records which model served
each attempt (see below).

Deleting a model does **not** delete the aliases pointing at it. Each one keeps
its name and its grants and becomes unavailable with the reason
`target_deleted`; you can then retarget it or remove it.

## Routing: several targets per alias

An alias may point at several models. Three things decide what happens to a
request, in this order.

### 1. The selection rule

Set per alias. It puts the *available* targets (active, behind an active
provider, of the right kind for the route) in the order they will be tried; the
first is tried and the rest are the fallback order.

| Rule | Order |
|---|---|
| `first_available` (default) | The order you listed the targets in. |
| `weighted` | A random draw in proportion to each target's weight (1 to 1000). The draw also sets the fallback order: the next target is drawn from what remains with the same odds. |
| `cheapest` | Lowest list price first: input rate × the alias's input price weight plus output rate × its output price weight, per million tokens (1:1 by default, which adds them; 10:1 suits long prompts with short answers), or the per-image or per-page rate for models priced that way. Prices in another currency are converted at the day's rate. A target with **no price** is tried last, never first, because an unpriced model reserves nothing and bills nothing. Ties keep your order. |

The rules are one interface in `gateway/routing.py` (`SelectionRule`): given the
available targets, return them in preference order, possibly fewer. A rule that
asks a decision model, or one that excludes targets by region or retention, is
another implementation of the same thing; stickiness, quota admission and
fallback are applied around any rule and never inside it.

### 2. Session stickiness

A conversation should keep hitting one upstream (its prompt cache lives there).
When a request carries a **session key**, the target that served it is
remembered, and later requests with the same key go to that target first for as
long as it is still available and admitted.

The session key is the first of these that is present:

1. the `x-session-id` header;
2. the `x-session-affinity` header (opencode 1.19.3 sends this, and `X-Session-Id`
   with its session id, to providers other than opencode's own; verified, so a
   coding agent on opencode is sticky with no configuration);
3. `prompt_cache_key` in the request body;
4. `user` in the request body.

A request with none of them is not sticky: the rule decides every time. There is
no shared bucket for requests without a key.

The memory is a row per `(alias, session)` in the database (`route_sessions`),
so it survives a restart and is shared by every gateway process. Details that
matter:

- **The key is hashed together with the caller's id** before it is stored. The
  table holds no raw session id, and one person cannot reach another's memory by
  guessing an id.
- **The window slides.** Each alias has a stickiness window (default 60 minutes;
  `0` turns stickiness off). A use renews it, written at most once per half
  window so a busy conversation does not cost a write per request.
- **A pin never blocks service.** If the pinned target is deleted, inactive, not
  admitted by quota, or fails, the request moves on like any other and the
  session is **re-pinned** to the target that serves it. A pinned target that
  recovers is not returned to; the session stays where it landed until the window
  ends.
- A pin is made only by an answer that was actually served (status below 400). A
  request the upstream refuses for being malformed does not pin anything.
- Deleting an alias, or a target model, forgets the sessions pinned to it.

Stickiness applies to aliases with **more than one** target, even while all but
one are down, so a session pinned to B while A was out is still on B when A
returns.

### 3. Fallback

If an attempt fails for the target's own reasons, the next target in the order is
tried, up to **three attempts** per request.

| What happened | Falls back? |
|---|---|
| connection refused or reset, DNS failure | yes |
| timeout | yes |
| upstream `5xx` (including 529 "overloaded") | yes |
| upstream `429` | yes (the provider is refusing *our* key or quota, not judging the request) |
| upstream `408` | yes |
| upstream `401` or `402` | yes (the caller already authenticated to the gateway, so this is *our* credential or balance at that provider; one provider answers "Insufficient Balance." with a 401). Logged at warning level naming the provider |
| upstream `403` whose body says auth, billing, quota, balance, credit or an API key | yes, with the same warning |
| a provider whose credential cannot be read (misconfigured) | yes |
| quota refusal for this attempt | yes (see below) |
| any other upstream `4xx` (400, a `403` that says nothing about our account, 404, 413, 422 ...) | **no**: the answer is returned as is |
| a request blocked by redaction policy | **no**: it is the same on every target |
| quota state unavailable | **no**: 503, as for any request |

The reasoning for the `4xx` line: those say the **request** is wrong, and another
target would be asked the same question and either refuse it too (doubling what a
bad request costs) or accept what the first correctly refused. `403` is judged by
its body because a content-policy refusal is the request's and a suspended
account is ours. If the last attempt fails this way, the caller gets that answer
in the same form as for a direct call.

**Streaming.** The fallback window closes when the upstream's stream has opened
with a success status. Once the first byte of a stream is on its way to the
client, a failure ends that stream and the client sees it end; the gateway never
splices a second model's text onto a half-sent answer. A stream that cannot even
be opened (connection error, 5xx, 429) does fall back.

**Quota admission is per attempt.** Each attempt is admitted on its own and
reserves its own worst case, priced for that target. An attempt that is not
admitted writes no ledger row and moves on to the next target. If no attempt is
admitted, the caller gets the last `429`. A failed attempt settles at what it
used, which is nothing, so it hands its reservation back before the next begins.
One thing to know: a **requests** limit counts attempts, because each attempt is
an upstream call and a ledger row, and counters are rebuilt from the ledger. A
failover therefore uses two of the caller's requests.

**Redaction runs per attempt**, under the policy of the target being tried, so a
target with a stricter rule is never sent text redacted for a laxer one.

**If every attempt fails**, the caller gets the last attempt's failure, in the
same form as for a direct call.

### What the ledger and the request log say

Billing follows the target that **served**. A request that fell back leaves one
row per attempt, all under one request id: the failed attempts with status
`upstream_error` and cost zero against the target that failed, the last against
the target that served. Every row carries the alias in `requested_model` and its
position in `route_attempt` (`1` is the first target chosen). `route_attempt` is
null for a direct call and for an alias with one target. The request log shows
it as a "try n" badge and as the `attempt` column in the CSV.

### Where routing applies

Selection, stickiness and fallback apply to chat completions, responses,
messages, embeddings, decisions and image generation. OCR and search serve an alias from
the rule's first choice and do not fall back.

### Rules that stay

Two rules follow from "targets of one alias must be interchangeable":

- **an alias cannot point at another alias** (a chain). It is refused when you
  save it, and again at request time for anything that got in another way;
- **all targets of an alias are of one kind.** A chat alias stays a chat
  alias, because a caller who was promised `/v1/chat/completions` must not get a
  400 naming a model they never asked for. The console offers only targets of
  the kind of the first one.

A model may appear **once** in an alias's route. At most 16 targets are
accepted.

Quota rules that name an **alias** are not supported: rules name a scope and a
metric, and an alias's usage is the target's, so the target is what you write
the rule against.

## In the console

**Routing**, next to Models. It lists every alias — available ones and
unavailable ones, with the reason — and creates, edits and deletes them. The
form takes the name, display name, description and the route: an ordered list
of targets (with a weight each under the weighted rule), the selection rule and
the stickiness window. It reuses the model page's visibility controls rather
than a second set. The list shows each alias's targets in order, with the rule
and window, and marks any target that cannot serve right now.

The **model** screens stay about served models: the Models list does not show
aliases, and a model's own page has an "Aliases pointing here" section, so
deleting or deactivating it shows which names will stop working before you
click.

## Admin API

All of it reads and writes the session cookie like the rest of `/api`.

| Method | Path | What |
|---|---|---|
| `GET` | `/api/admin/aliases` | every alias, with its targets, rule, stickiness window, visibility and status |
| `POST` | `/api/admin/aliases` | create one: `name`, `display_name?`, `description?`, **either** `target_model_id` **or** `targets` (`[{model_id, weight?}]`, in order), `route_rule?`, `route_sticky_ttl_seconds?`, `is_active?`, `is_public?` |
| `PATCH` | `/api/admin/aliases/{id}` | rename, edit the text, replace the route (`targets`, or `target_model_id` for exactly one model), change `route_rule` or `route_sticky_ttl_seconds`, or toggle active/public |
| `DELETE` | `/api/admin/aliases/{id}` | remove it, with its grants and its route |

Grants are **not** set through these routes. An alias row uses the same tables a
model does, so the existing model grant endpoints apply unchanged:

| Method | Path |
|---|---|
| `PUT` / `DELETE` | `/api/admin/groups/{group_id}/models/{model_id}` |
| `PUT` / `DELETE` | `/api/admin/users/{user_id}/models/{model_id}` |

`model_id` there is the alias's id — it is a row of the same table.

Refusals, each with a code an operator can act on:

| Code | Status | When |
|---|---|---|
| `already_exists` | 409 | the name is taken by a model or another alias |
| `unknown_target` | 400 | no model with that id |
| `alias_target_is_alias` | 400 | an alias cannot target an alias |
| `alias_target_inactive` | 400 | the target is deactivated |
| `alias_target_provider_inactive` | 400 | the target's provider is deactivated |
| `alias_kind_mismatch` | 400 | a target of another kind than the alias |
| `alias_duplicate_target` | 400 | the same model listed twice in one route |
| `unknown_route_rule` | 400 | a `route_rule` the gateway does not have |

Malformed input (no targets, a weight outside 1 to 1000, a stickiness window
outside 0 to 2,592,000 seconds, more than 16 targets, both `target_model_id`
and `targets`) is a `400` too. The response's `targets` lists each target with
`position`, `weight`, `available` and, when not available, `unavailable_reason`;
`target` is the first of them, kept for clients written before routing.

The **model** endpoints (`GET/PATCH/DELETE /api/admin/models/{id}`, its prices)
refuse an alias id with `400 model_is_alias`, and the models listing excludes
aliases. `GET /api/admin/models/{id}` for a real model gains
`aliases_pointing_here`, which is empty on the listing and populated on the
detail response.

Every create, retarget and delete is written to the audit trail
(`alias.create`, `alias.update`, `alias.delete`), because a retarget is the one
model-affecting change whose consequences are invisible by design: every client
keeps sending the same name and a different model answers. The route is recorded
as the target names in order (`a, b, c`), and the rule and window as changed
fields.

## Compatibility notes

- **Cerea needs no change.** An alias arrives as an ordinary model card. A
  conversation stores the alias name, so retargeting changes what new turns run
  on — which is the point. A card with `display_name: null` has been handled
  since Cerea 0.15.6.
- A card gained a `description` field, which is new in the model card as well as
  the alias one. OpenAI clients ignore unknown fields.
- The migration (`0063`) makes `models.provider_id` and
  `models.upstream_model` nullable and adds a check constraint holding the two
  shapes apart. Its downgrade **refuses** to run while an alias exists, rather
  than deleting your aliases to make the rollback succeed.
- Routing (`0066`) adds `model_route_targets.weight`, `models.route_rule` and
  `models.route_sticky_ttl_seconds`, the `route_sessions` table and
  `usage_records.route_attempt`. The defaults leave every existing alias
  behaving as it did. Its downgrade **refuses** while any alias has more than
  one target.
- The request log's JSON rows gain `route_attempt` and its CSV gains a final
  `attempt` column.
