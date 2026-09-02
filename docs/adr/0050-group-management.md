# 0050 — Groups are created, deleted and given members in the console; manual groups only

- Date: 2026-09-02
- Status: **accepted, built**
- Bounds [0048](0048-oidc-policy-configuration.md), whose reconciliation rule
  this deliberately does not touch.
- Requested as: "group management (create groups, add users, remove users),
  now non existing".

## Context

Groups could only come into being two ways: the identity provider creating
them at a login (source `oidc`), or side effects of other actions — a group
named in a user-creation form (ADR 0048). There was no way to create one
deliberately, delete one, or change who is in it, and the groups card on the
Users screen was a read-only listing with a warning that membership was "not
editable here".

## Decision

**Create, delete, and edit membership — for manual groups only.**

- `POST /api/admin/groups` creates a group with source **`manual`**. That
  source is the load-bearing part: it is the flag that says *this group's
  membership is the platform's to decide*, which is what makes the editing
  below sound. A name that collides is refused (`group_exists`) rather than
  silently merged.
- `POST/DELETE /groups/{id}/members` edits membership. On a manual group that
  is the whole feature. On an **OIDC-sourced group it is refused**
  (`membership_managed_by_idp`), and the refusal is the recorded reasoning
  restated: `provision_user` replaces an OIDC group's members from the token
  at every login (ADR 0048), so a member added by hand would be reconciled
  away at the next sign-in — access granted silently, then revoked the same
  way. A deployment that wants a directory group plus extra members maps the
  IdP group to a manual one (ADR 0048's mapping rules) and edits the manual
  group.
- `DELETE /groups/{id}` removes the group. Model access rows cascade;
  members' default billing group clears when it pointed here; quota and
  redaction rules scoped to the group keep their scope id and go **inert**,
  exactly as user deletion does (ADR 0048). An IdP-owned group is *not*
  refused: refusing cannot stop the IdP from recreating it at the next login,
  so the deletion lasts exactly as long as nobody from that group signs in —
  and the confirmation dialog says so.

## Consequences

- The boundary between the two group kinds is now a fact a screen can point
  at: the Users screen's groups card grows create/members/delete, and the
  members dialog says what a manual group is for. The read-only warning that
  said "membership is not editable here" is gone because it is no longer
  true.
- Removing a membership clears the removed user's default billing group when
  it was this group — the login path's own rule (clear first, adopt after)
  applied by hand, because otherwise the next request bills a group the user
  no longer belongs to.
- Group deletion is a quiet operation on live billing: usage rows keep their
  group id (`ON DELETE SET NULL`), so attribution history is untouched; only
  *future* spend stops being admitted by that group.
