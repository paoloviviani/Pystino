# 0057 — A directory owns the memberships it granted, and nothing else

- Date: 2026-09-07
- Status: **accepted, built** (membership provenance, per-provider sync mode,
  console)
- Requested as: "IdPs should not overwrite the groups that are already
  existing. I must be able to assign manually a user to a group, even if the
  user comes from an IdP and that IdP does not map to that group. A different
  matter is, the IdP does map to that group and the upstream user is not part
  of it. This should be solved as keycloak does: specify the policy (leave as
  it is, override first login, override every time)"
- **Reverses a rule this codebase stated in a docstring and believed twice** —
  see "The rule this replaces".
- Related: [0048](0048-oidc-policy-configuration.md) (who may exist here),
  [0051](0051-settings-identity-and-email.md) (per-directory facts live on the
  provider row), [0056](0056-account-linking.md) (whose admin hazard this
  softens).

## The rule this replaces

`provision_user` said, in its own docstring:

> Group membership is **replaced**, not merged: the identity provider is
> authoritative, so a group removed there must disappear here, or revoking
> someone's access in the directory would not revoke their ability to bill.
>
> Groups created manually in the gateway are also removed if the IdP does not
> report them, which is a deliberate consequence of that same rule — mixing
> authoritative and local membership silently produces access nobody intended.

The first paragraph is right and is kept. The second is wrong, and the word
that makes it wrong is "that same rule": it is not the same rule. Revoking what
a directory granted and revoking what an administrator granted have nothing in
common except the table they are written in. The consequence in practice was
that an administrator could add a directory user to a group, watch it work, and
have it silently undone the next time that person signed in — a change with no
error, no log line, and a delay measured in however long until their next
login.

## The decision

**Provenance per membership.** `memberships.source` is `oidc` or `manual`.
A login's sync grants and revokes `oidc` memberships. It never touches a
`manual` one.

**A policy per provider for how often it gets to answer**, in Keycloak's
vocabulary because the question is Keycloak's question and operators already
know the words. `identity_providers.group_sync`:

| Mode | The directory's answer is applied |
|---|---|
| `every_login` | on every sign-in — **the default**, and what this gateway has always done |
| `first_login` | when the account first appears here, and never again |
| `never` | not at all; the directory authenticates and nothing more |

`never` is worth spelling out: a new account arrives with **no groups**, no
default billing group, and therefore no ability to bill until an administrator
puts them in one. That is the point of choosing it — authentication from the
directory, authorisation decided here — and it is not the default for exactly
that reason.

The mode is on the provider row and not in the global policy because it is a
statement about one directory. A deployment with a corporate IdP it trusts for
groups and a partner IdP it does not needs both answers at once.

## Why provenance, and not the two things that look simpler

Both alternatives were implemented before this one, and each was killed by a
test rather than by an argument. Worth recording, because they are the ideas
anybody would try first.

**"Whose group is it."** Let the directory own groups it created (`groups.source
= oidc`) plus any group an administrator pointed a mapping at, and let it
replace memberships only in those. This survives about ten minutes. The
bearer-token revocation test failed and was right to: an administrator creates
`engineering` in the console, the directory also names it, and under this rule
the directory may **add** people to that group and may never **remove** them.
Half a revocation is the failure this whole area exists to prevent.

**"Did the token mention it."** Let the directory replace only what it names in
the current token. This is revocation backwards: a directory that stops naming
a group thereby loses the right to remove it, so revoking someone upstream
leaves their access in place forever.

Provenance answers both, because the question was never about groups. It is
about **who granted this particular membership**, and that is a fact about the
membership.

## Consequences that were bugs before

Once membership stops being "whatever the token said", three pieces of derived
state have to be recomputed from what the person actually holds. All three read
the token before this ADR, and all three were wrong for a manually-added user:

- **`is_admin`** was computed from the token's groups, so putting somebody in
  the admin group by hand did not make them an administrator. It does now —
  which is also the escape hatch for the lockout [0056](0056-account-linking.md)
  warns about, where a local admin adopted by a directory loses the flag.
- **The default billing group** was cleared when the token did not name it, so
  a user whose only group was granted by an administrator could not bill at
  all.
- **The "sole group becomes the default" rule** read the token's list, so it
  never fired for such a user.

## What the console shows

Two changes, both because the rule is only useful if an administrator can see
it. The provider dialog carries the sync mode as a three-way choice, with the
sentence that matters under it: *"applies only to memberships this directory
granted"*. And the group members list marks each row the directory granted
**from the directory**, because "why is this person still in this group" is
asked on exactly that screen, and now has two possible answers.

One refusal also had to go with the old rule. Adding a member to an
OIDC-sourced group was rejected with `membership_managed_by_idp`, and the
reason it gave was the rule this ADR replaces: "a member added here would
vanish at the next sign-in". It would not any more, and the refusal was
blocking precisely the thing an administrator asks for — putting somebody into
a directory's group when the directory does not name them. Removal was never
restricted and still is not: it takes effect at once, and a membership the
directory granted may of course come back at that person's next sign-in.

## The `/v1` request path

`_claims_diverge` decides whether a bearer token's claims require a write, and
it ran on every request comparing the stored group set to the claimed one for
equality. With provenance, equality is the wrong question: a user with one
administrator-granted group is *permanently* unequal to their token, so every
request would have re-provisioned — a database write on the hot path, and one
whose reconciliation would then have tried to strip that very group.

It now asks the two questions a sync would actually act on:

```python
if claimed - held or granted - claimed:   # something to add, or something the
    return True                           # directory granted and has dropped
```

and skips the group comparison entirely when the provider's mode is not
`every_login`, because then there is nothing a difference could mean.
`test_query_counts.py` still passes at its three selects to authenticate.

## The migration's back-fill

Existing memberships carry no provenance, and `0025` infers it from the only
evidence there is: a membership belonging to a user whose `issuer` is not
`local` was made by a login, because until now the sync replaced every
membership of such a user on every login — an administrator's grant could not
survive long enough to be in the table. A membership of a `local` user was made
by hand, since no sync ever runs for one.

That inference is **exact** for the behaviour being replaced, which is what
makes the upgrade invisible: revocation keeps working for directory users, and
local accounts keep their groups.

## Deliberately not done

- **Promoting an `oidc` membership to `manual` when an administrator re-adds
  it.** It would let an administrator pin a directory-granted group against
  revocation. Keycloak has no such concept, the route currently refuses a
  duplicate membership outright, and "the directory granted this and an
  administrator agrees" is not a state anybody has asked to express. A
  deployment that wants membership pinned deployment-wide chooses
  `first_login` or `never`.
- **Per-mapper sync modes.** Keycloak has them; this has one mode per
  provider. Nobody here has two directories' worth of one directory.
## Bug found while building this, in a test rather than the code

The first version of the new test suite asserted on `user.memberships` and
reported that the `/v1` path was stripping an administrator's grant. It was
not: the rows were correct and the *collection was stale*. A membership added
by a sibling write in the same session does not invalidate a relationship
already loaded on the user object. The suite's helper now reads the groups back
from the database, which is what the assertion was always meant to be about.
Recorded because the same trap is one line away in any test that mixes a route
write with an ORM read.

## Tested

`apps/gateway/tests/test_group_sync.py` — 26 tests. An administrator's grant
surviving one login, many logins, and a group the directory itself created; the
directory still revoking what it granted, including in a group an
administrator created; each of the three modes, including that `never` creates
no groups at all and that switching to it does not reach back over existing
grants; `is_admin` from a manual grant and revoked when the directory drops it;
the sole-manual-group default; the divergence test in all four of its cases;
the policy through the management API, including that an unknown mode is
refused rather than stored; and the members listing reporting who granted each
membership while the user listing declines to guess one.
`test_group_management.py` carries the inverted refusal: adding a member to an
OIDC group now answers 204 and records the grant as the administrator's.
