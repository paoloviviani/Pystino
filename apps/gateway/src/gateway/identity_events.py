"""Writing to `identity_events` (ADR 0093 §3.1).

Every step of the redesign that changes who exists, who administers, which
identities point at which person, or which login someone has, writes one row
here through `record_event` — the brief for every later stage says so, and
this module is what makes that true rather than aspirational: it is the one
place that checks a `detail` dict against its action's allowlist, so a typo'd
or careless key fails the write instead of silently widening what an audit row
may contain. The database refuses the other half — no `UPDATE` or `DELETE`
reaches the table at all, enforced by the triggers `gateway.models` attaches.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models import IdentityEvent, IdentityEventAction, IdentityEventActor

#: These two are irreversible or run with nobody else watching: a merge moves
#: another person's data with no undo but restoring a backup, and break-glass
#: is what runs when normal recovery already failed. Neither leaves any other
#: record of *why* it happened.
_ACTIONS_REQUIRING_REASON = frozenset(
    {IdentityEventAction.USER_MERGE, IdentityEventAction.BREAK_GLASS}
)

#: Per-action allowlist of `detail` keys. An action nothing calls yet keeps an
#: empty set; the stage that first writes it adds the keys its call site
#: needs, here, in the same commit — this dict is deliberately not filled in
#: ahead of the work that needs it.
DETAIL_ALLOWLIST: dict[IdentityEventAction, frozenset[str]] = {
    IdentityEventAction.USER_CREATE: frozenset(),
    IdentityEventAction.USER_UPDATE: frozenset(),
    IdentityEventAction.USER_DISABLE: frozenset(),
    IdentityEventAction.USER_ENABLE: frozenset(),
    IdentityEventAction.USER_DELETE: frozenset(),
    # §5.1: "audit action admin.grant and detail.rule='email'"; the claim rule
    # (§5.2) uses the same column with rule="claim".
    IdentityEventAction.ADMIN_GRANT: frozenset({"rule"}),
    IdentityEventAction.ADMIN_REVOKE: frozenset({"rule"}),
    IdentityEventAction.ADMIN_REFUSED_LAST: frozenset(),
    IdentityEventAction.PASSWORD_RESET: frozenset(),
    IdentityEventAction.LOGIN_CREATE: frozenset(),
    IdentityEventAction.LOGIN_DELETE: frozenset(),
    IdentityEventAction.LOGIN_DISABLE: frozenset(),
    # §6.2: "an identity.link audit row (actor login; detail: the matched
    # address and the new issuer)".
    IdentityEventAction.IDENTITY_LINK: frozenset({"matched_email", "issuer"}),
    IdentityEventAction.IDENTITY_BIND: frozenset(),
    IdentityEventAction.IDENTITY_DROP: frozenset(),
    # §7.1 step 5: "the identity_events user.merge with the summary and the
    # reason" — summary is the counts-per-table dict the preview also shows.
    IdentityEventAction.USER_MERGE: frozenset({"summary"}),
    IdentityEventAction.BREAK_GLASS: frozenset(),
    # §2 step 4: "the change is audited (idp.reseed, with the old and new
    # issuer)".
    IdentityEventAction.IDP_RESEED: frozenset({"old_issuer", "new_issuer"}),
    IdentityEventAction.BOOTSTRAP_ADMIN: frozenset(),
    IdentityEventAction.SESSIONS_REVOKE: frozenset(),
    IdentityEventAction.DEVICES_REVOKE: frozenset(),
}

#: Unconditional, whatever the action: the review's rule is "never a password,
#: digest, token or secret", so it is checked here too rather than trusted to
#: every entry above being written carefully.
_FORBIDDEN_DETAIL_KEY_SUBSTRINGS = ("password", "digest", "token", "secret")


def _validate_allowlist() -> None:
    missing = set(IdentityEventAction) - set(DETAIL_ALLOWLIST)
    if missing:
        raise AssertionError(f"identity_events DETAIL_ALLOWLIST is missing {sorted(missing)}")
    for action, keys in DETAIL_ALLOWLIST.items():
        for key in keys:
            lowered = key.lower()
            if any(bad in lowered for bad in _FORBIDDEN_DETAIL_KEY_SUBSTRINGS):
                raise AssertionError(
                    f"identity_events detail key {key!r} for {action} looks like a secret"
                )


_validate_allowlist()


async def record_event(
    session: AsyncSession,
    *,
    actor_type: IdentityEventActor,
    actor_label: str,
    action: IdentityEventAction,
    actor_user_id: uuid.UUID | None = None,
    target_user_id: uuid.UUID | None = None,
    target_label: str = "",
    issuer: str | None = None,
    subject: str | None = None,
    detail: dict[str, Any] | None = None,
    reason: str | None = None,
) -> IdentityEvent:
    """Append one row to `identity_events`. Call this, never `IdentityEvent(...)`.

    Adds the row to `session` and flushes it, so the caller sees the same
    transaction rules as everything else it just wrote — the caller commits.
    Raises `ValueError` for a `detail` key outside the action's allowlist, or a
    missing `reason` on `user.merge` / `break_glass`, before anything is added
    to the session.
    """
    detail = dict(detail or {})
    allowed = DETAIL_ALLOWLIST[action]
    if extra := set(detail) - allowed:
        raise ValueError(
            f"identity_events detail for {action} may only contain {sorted(allowed)}, "
            f"got unexpected key(s) {sorted(extra)}"
        )
    if action in _ACTIONS_REQUIRING_REASON and not (reason and reason.strip()):
        raise ValueError(f"identity_events {action} requires a reason")

    row = IdentityEvent(
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        action=action,
        target_user_id=target_user_id,
        target_label=target_label,
        issuer=issuer,
        subject=subject,
        detail=detail,
        reason=reason,
    )
    session.add(row)
    await session.flush()
    return row
