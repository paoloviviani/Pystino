# Billing groups: what pays, what is admitted, what shows

Every request bills exactly one group, and that one group decides what the
request may use, what the model list offers, where the cost lands and which
quotas bind it. This page is the whole rule in one place; the pieces it
links to describe the surrounding machinery (where groups and memberships
come from, where the money is recorded, how agents enroll).

## One group pays every request

A request's billing group is decided in this order:

1. **`x-bill-to: <group name>`** — honoured for callers authenticated with an
   OIDC access token only. An issued API key that sends the header is
   **refused, not ignored**: a key already carries its answer (the group it
   was minted for), and quietly overriding it is how somebody finds out from
   an invoice. A service-to-service caller with a static bearer is likewise
   never asked to choose.
2. **The key's pinned group** — a key is minted against a billing group and
   keeps it (`billing_group_id` at mint time).
3. **The person's default billing group** — what `/v1/me` reports as
   `default_billing_group`, changed with `PUT /api/me/default-billing-group`
   or the console's group picker.

If nothing resolves — a person with no membership and no default, a key with
no group — the request is refused with the message that says so ("No billing
group is set for this key or user. Set a default billing group in the
management API before making requests."). A billing group that has been
disabled is refused by name; a group the caller is **not a member of** is
refused like one that does not exist, so the header cannot enumerate the
deployment.

The resolution re-checks membership on every request rather than trusting
what was true when the key was minted or the token issued: leaving a group
stops you billing it immediately, which is also what makes honouring
`x-bill-to` safe. It removes a round trip through the management API,
nothing more — any group it can name is one the caller could already bill
by changing their default.

## What the billing group decides

- **Admission.** A model is usable when a request for it would be admitted:
  the billing group's grants, the caller's personal grants, and public
  models. Nothing else — in particular not the other groups the person
  belongs to.
- **The model list.** `GET /v1/models` shows *exactly what a request would
  accept, nothing more*, on the same scope: the group named by `x-bill-to`
  (else the default), plus personal grants and public models. It used to
  union every group the person belonged to, which advertised a model every
  request then refused with a 404 to anyone in two groups who billed the
  other one. Anonymous (no credential at all), it is the whole catalogue
  with brochure fields only: the listing is public, a card's detail is not
  (ADR 0081). A key's listing is its pinned group's — a key may not widen
  its scope, because it may not send the header.
- **Attribution.** Every ledger row carries the billing group it was billed
  to, and reports and CSVs group on it. `x-bill-to` is how one person's
  work lands on the team's budget instead of their own.
- **Quotas.** A quota rule scoped to a group binds every request billed to
  it. A per-member rule (`applies_to: each_member`) binds each person's own
  share; an aggregate rule binds the group's total, whoever sent the
  request.
  A global per-user rule (`applies_to: each_user`) is not a group rule: it
  caps each person across every group they bill to; see
  [quotas](accounting-and-quotas.md).

## The default billing group

A person's default is set the first time they can use the gateway — their
first group becomes it — and stays until they or an administrator change it:

- A person in exactly one group after a directory change keeps it as their
  default ("sole group becomes the default"), so a directory edit never
  strands someone without a billing group.
- The bundled default group (`OIDC_DEFAULT_GROUP`, `users` unless set) is
  deliberately *not* a choice anyone made: someone in `users` and one other
  group bills the other group by default, and someone in `users` plus two
  others bills `users` until they choose, rather than being refused. See
  [the default group](identity.md#the-default-group) for how that membership
  is granted.
- The console's group picker sets it (`PUT /api/me/default-billing-group`);
  what the chat shows for it is the billing-group selector on the settings
  usage page.

## Choosing per request

`GET /v1/billing/groups` is what a client reads to offer the choice: the
groups this caller may bill (their current memberships, name and
`is_default`), and which group **this request** would bill
(`billing_group` — the header's answer when one was sent). A client that
wants "bill this to the team" sends `x-bill-to: <name>` on the request
itself; a chat that wants the choice to persist across requests changes the
person's default instead.

## Machines and agents

A coding agent enrolls as the person it runs for, and the credential it
mints carries the same two modes:

- **Pinned** (the CLI default): the group chosen at `galopin enroll`
  (`--group`, or the prompt) is recorded in the machine's credential file and
  sent as `x-bill-to` on every request the machine makes. Its model
  discovery sends the same header, so the models the machine lists are the
  pinned group's.
- **Following the default** (`galopin enroll --default-group`, or
  `galopin auth group --default` on an enrolled machine): the credential
  records the mode, and the machine sends **no** `x-bill-to` at all — the
  gateway bills the person's default and lists what it accepts. One setting
  in the console then moves billing and model visibility for the person and
  every machine that follows them.

`galopin auth group NAME` switches an enrolled machine between the two
modes without re-enrolling (a new machine id would mean a new device to
confirm), and `galopin auth group` with no argument shows the current mode.
A running `galopin run` adopts a new token and a new group without a
restart; the model list is refreshed at start and by `galopin models
refresh`. See [Coding agents](coding-agents.md) for the enrollment and the
credential's lifecycle, and the chat's `/code` panel for pairing.
