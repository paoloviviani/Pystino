# Model aliases

An alias is a model name **you** choose that resolves to a model. Callers send
it like any other name and never learn what is behind it.

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

An alias is **unavailable** when its target was deleted, when the target is
inactive, or when the provider behind the target is inactive. A request naming
an unavailable alias is refused with:

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

It is never quietly pointed at something else. An alias that silently fell back
to a second model would bill the target's price for work a different model did.

Deleting a model does **not** delete the aliases pointing at it. Each one keeps
its name and its grants and becomes unavailable with the reason
`target_deleted`; you can then retarget it or remove it.

## One target, for now

An alias points at exactly one model. The storage is already a list —
`model_route_targets`, one row per target with a `position` — because the plan
is several targets with a rule for choosing between them, and a single column
would have to be migrated away when that arrives. Today the save path writes one
row and refuses a second.

Two rules follow from "targets of one alias must be interchangeable":

- **an alias cannot point at another alias** (a chain). It is refused when you
  save it, and again at request time for anything that got in another way;
- **an alias cannot be retargeted across kinds.** A chat alias stays a chat
  alias, because a caller who was promised `/v1/chat/completions` must not get a
  400 naming a model they never asked for. The console shows the kind read-only
  from the target and does not offer a target of another kind.

Quota rules that name an **alias** are not supported: rules name a scope and a
metric, and an alias's usage is the target's, so the target is what you write
the rule against.

## In the console

**Routing**, next to Models. It lists every alias — available ones and
unavailable ones, with the reason — and creates, edits and deletes them. The
form takes the name, display name, description and one target, and reuses the
model page's visibility controls rather than a second set.

The **model** screens stay about served models: the Models list does not show
aliases, and a model's own page has an "Aliases pointing here" section, so
deleting or deactivating it shows which names will stop working before you
click.

## Admin API

All of it reads and writes the session cookie like the rest of `/api`.

| Method | Path | What |
|---|---|---|
| `GET` | `/api/admin/aliases` | every alias, with its target, visibility and status |
| `POST` | `/api/admin/aliases` | create one: `name`, `display_name?`, `description?`, `target_model_id`, `is_active?`, `is_public?` |
| `PATCH` | `/api/admin/aliases/{id}` | rename, edit the text, retarget, or toggle active/public |
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
| `alias_kind_mismatch` | 400 | retargeting to a model of another kind |

The **model** endpoints (`GET/PATCH/DELETE /api/admin/models/{id}`, its prices)
refuse an alias id with `400 model_is_alias`, and the models listing excludes
aliases. `GET /api/admin/models/{id}` for a real model gains
`aliases_pointing_here`, which is empty on the listing and populated on the
detail response.

Every create, retarget and delete is written to the audit trail
(`alias.create`, `alias.update`, `alias.delete`), because a retarget is the one
model-affecting change whose consequences are invisible by design: every client
keeps sending the same name and a different model answers.

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