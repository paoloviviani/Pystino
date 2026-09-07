# 0056 — An identity provider may adopt the local account with the same verified address

- Date: 2026-09-07
- Status: **accepted, built** (per-provider switch, browser login path, console)
- Requested as: "merge IDP accounts with local ones on email basis is something
  we should allow (a switch to be activated when creating the Idp?)"
- **Reverses a refusal recorded in code** — the `LocalCredential` docstring said
  linking "is refused, not merely unimplemented". See "The refusal this
  reverses".
- Builds on [0043](0043-local-password-auth.md) (the local door),
  [0048](0048-provisioning-policy.md) (who may exist here) and
  [0051](0051-settings-identity-and-email.md) (providers are rows, and
  per-directory facts live on them).

## The problem

Users are keyed on `(issuer, subject)`, and `iss` is part of a person's
identity. That is right, and CLAUDE.md has warned about its consequence for a
long time: a local account and a directory account with the same address are two
accounts. The consequence bites in exactly one, extremely common, situation.

A deployment is bootstrapped with a local admin — that is the only way in before
an identity provider is configured (0043), and under the TLS proxy shape it is
the only management credential the deployment seeds (0035). Later the operator
wires up the corporate directory and signs in through it. They arrive as a
stranger: a new row, no admin flag, no groups, no keys, and their own spend
history sitting on an account they can no longer reach except by password. The
honest answer the schema gives — "these are two people" — is, here, wrong.

## The refusal this reverses

The reason not to do this was written down, and it has not stopped being true:

> Linking one person's local credential to their directory identity would let a
> leaked password ride an issuer's trust.

There is a second direction, sharper than the first, that the original note did
not spell out: if an account is adopted on the strength of an `email` claim, then
whoever can make a directory emit that claim can *become* the account it names —
inheriting its admin flag, its groups, its API keys and its billing. In a
directory where people can set their own profile address, an email claim would
be a password for every local account.

What changed is not the risk assessment. It is **who decides**. An operator who
runs the directory their local accounts were named after can make that
statement; the gateway cannot make it for them. So this is a per-provider
switch, off by default, and it is the *only* thing that turns any of the
following on.

## The decision

`identity_providers.link_local_by_email` — a boolean per provider row,
`false` for every existing row and for the environment fallback. When it is on,
a browser login that resolves to no known identity may **adopt** the local
account with the same address, subject to every one of these:

1. **`email_verified` is boolean `true`.** Absent is not consent — a provider
   that does not say has not said yes — and neither is the string `"true"`,
   which OIDC core does not permit. A declined link is logged with the value
   that declined it, so the diagnosis takes seconds. This is the single
   property that separates linking from "an email claim is a password".
2. **The match is on the local door's address**, `(issuer="local",
   subject=<casefolded address>)`, which is unique by construction — and never
   on `users.email`, which is neither unique nor stable, and which the login
   flow itself overwrites from the claims on every sign-in.
3. **Only a local account is adopted.** An account at another directory with
   the same address is never touched: two directories that both know an address
   are not evidence that they mean one person, and the switch is one operator's
   statement about one directory.
4. **One person, one identity per directory.** A second subject arriving from
   the same issuer for the same address is declined, not adopted — that is a
   reassigned address or two accounts in the directory, and adopting would hand
   the newcomer everything the first one has.

## The shape: a row beside the identity, not a rewrite of it

The obvious implementation is to rewrite the adopted row's `(issuer, subject)`
to the provider's. It is wrong, and finding out why is the most useful thing in
this ADR.

`issuer == "local"` is not merely how a row was created. It is read in **eight
places** as *"this account's door is a password"*: local login, the password
reset request, `gateway seed`, `gateway passwd`, changing your own password, the
guard shared by the two administrator password routes (set and clear), creating
a local account from the console, and the rule that refuses an `is_admin` edit
when the directory is authoritative. Rewriting the key would flip all eight
silently: the person would keep a password that opens nothing, and the
deployment would lose the escape hatch that recovers it when the directory is
misconfigured — which, as the next section says, is precisely the failure that
adopting an admin account can cause.

So the identity a row was created with stays put, and the directory identity is
recorded beside it in **`user_identities`**. Resolution reads both, in one
statement:

```sql
users.(issuer, subject) = (?, ?)  OR  users.id IN (
    SELECT user_id FROM user_identities WHERE issuer = ? AND subject = ?)
```

One round trip, not two — `sync_user_from_claims` runs this on the `/v1`
request path, where `test_query_counts.py` bounds authentication at three
selects. A correlated subquery costs none of them; a lookup-then-fallback would
cost one on every request in the deployment.

Everything else follows for free, because everything else is keyed on
`users.id`: API keys, memberships, usage records, quota counters, the default
billing group, and the local credential itself. An adopted account keeps its
spend history because it *is* the same row.

Deleting a `user_identities` row unlinks. That is why the link records
`linked_at` and the address it matched on, and why turning the switch back off
stops new links without undoing existing ones — doing that implicitly would
split one person's account in two, spend and keys on one row and their next
login on another.

## What linking hands to the directory

Adopting an account makes the directory authoritative for it, because
`provision_user` already works that way and this ADR does not carve out an
exception. Two consequences, both pinned by tests so that they are decisions
rather than surprises:

- **Manual group memberships are replaced.** The IdP is authoritative for
  membership (0048), so groups assigned by hand to the local account disappear
  on the login that adopts it.
- **`is_admin` follows the directory when admin groups are configured.** A
  local admin adopted by a directory that does not place them in an admin group
  **loses the flag on that login**. This is the one operational hazard worth
  reading twice before turning the switch on, and the recovery is the local door
  the shape above deliberately keeps working: the row is still `issuer =
  "local"`, so `gateway passwd` still works and the administrator's own
  `is_admin` edit is still permitted.

An inactive local account is adopted and stays inactive: disabling someone must
not be undone by their signing in through SSO.

## Where linking does not happen

**Not on `/v1`.** An access token resolves an existing link but never creates
one. Two reasons, and the second is the real one: the browser callback is the
only door that sees the full claim set — several providers put `email_verified`
on userinfo only, and the access-token path deliberately makes no userinfo
request, because an HTTP round trip to the identity provider per API call is not
a thing a gateway may do. So the claim that would justify a link is the one that
path is least likely to hold. A person who signs into the console once is linked
from then on, on both surfaces.

**Not for the environment fallback.** `record_from_env` reports `false` and
cannot be configured otherwise. An upgrade may not change who can sign in as
whom; the seed turns that fallback into a row on first startup, so turning
linking on is one edit away in the console.

## Bug found while building this, not fixed here

`gateway seed` stores a local user's subject **as typed**, while `gateway
passwd` and the local login both casefold it. A deployment seeded with a
mixed-case address therefore has a local account that cannot sign in with its
password today — the login's casefolded lookup never finds the row — and, with
this ADR, cannot be adopted either, for the same reason and consistently with
it. The fix is not a one-liner: casefolding the `seed` lookup would make a
re-seed create a *second* row for any address already stored mixed-case, so it
needs to match either spelling while writing only the casefolded one. Recorded
rather than done, because it is a different bug from this feature.

## Alternatives rejected

- **Rewriting `users.(issuer, subject)` on link.** Eight silent behaviour
  changes; see above.
- **A global switch.** The trust being extended belongs to one directory. A
  deployment that adds a second provider must not have consented to it in
  advance.
- **Matching on `users.email`.** Not unique, not stable, and overwritten from
  the claims on every login. It is the column that looks right and is wrong.
- **Treating a missing `email_verified` as verified.** This is the whole
  feature's safety property, and several providers omit the claim entirely.
  Being strict costs a support question; being lenient costs the account.
- **A full identity-alias model, with `users.(issuer, subject)` migrated into
  `user_identities` for every row.** The right long-term shape, and what would
  be needed to link one person across *two* directories. Not done: it touches
  every authentication path for a generality nobody has asked for, and
  `user_identities` is already the table that would carry it.
- **Retiring the password when an account is adopted.** Tempting — a local
  password is a second door that bypasses whatever MFA the directory enforces —
  and refused, because it would delete the credential that recovers a
  deployment whose directory has just been misconfigured, at exactly the moment
  that is most likely. The console shows both doors on the user screen instead;
  removing the password stays an explicit administrative act.

## Tested

`apps/gateway/tests/test_account_linking.py` — 29 tests. Off by default;
adoption on a verified address, and the same row id afterwards; declined for
`false`, absent, `"true"` and `1`; the casefolded match; no local account, and
no email; an account at another directory; a second subject from the same
directory; the local password and `issuer == "local"` surviving; membership
replacement and the admin revocation; adoption where auto-provisioning would
refuse a stranger; `/v1` resolving a link and never creating one; the switch
through the management API and on the user listing.

Not covered, and named so nobody assumes otherwise: the browser callback's own
parameter passing. Nothing in this repository drives the redirect flow — the
note at the top of `test_oidc.py` says why — so the seam tested is the function
the callback calls, and the row and record either side of it.
