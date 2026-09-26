"""Admin merge (ADR 0093 §7.1): move one person's everything onto another,
irreversibly, in one transaction.

**The registry, `MERGE_RULES`, is the point.** It names every foreign key to
``users.id`` and every user-scoped ``scope_id`` -- the same 19 plus 2 columns
a person's data can live under -- paired with how a merge treats that
column. :func:`assert_merge_rules_are_complete` walks `Base.metadata` itself
and fails if a column exists that the registry does not name, so a table
added later that references a person cannot be silently left out of a
merge: the guard test that calls it is the mechanism, not the registry
alone.

Two categories dispatch generically, straight off the registry, against the
Core table object (`REASSIGN`, `DELETE`); the rest -- unions with a conflict
rule, the user-scoped rows on `limit_rules`/`redaction_rules`, and identities
-- are their own functions below, because each has a real decision to make
that a blanket UPDATE or DELETE cannot express.
"""

from __future__ import annotations

import enum
import logging
import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.directory.authelia_users import UsersFileError
from gateway.directory.service import bundled_users_file
from gateway.identity_events import record_event
from gateway.identity_registry import active_bundled_provider
from gateway.models import (
    Base,
    DirectoryEntry,
    IdentityEventAction,
    IdentityEventActor,
    IdentityProvider,
    MembershipRole,
    MembershipSource,
    User,
    UserIdentity,
    UserMerge,
    UserModelAccess,
)
from gateway.oidc import PENDING_USER_ISSUER
from gateway.secrets import SecretBox
from gateway.types import utcnow

logger = logging.getLogger(__name__)


class MergeRuleKind(enum.StrEnum):
    #: A plain `UPDATE ... SET col = target WHERE col = source`.
    REASSIGN = "reassign"
    #: A plain `DELETE WHERE col = source`. Revocable credentials: the point
    #: of a merge is that the person keeps working, not that a stale token
    #: keeps working under someone else's account.
    DELETE = "delete"
    #: A singleton-per-user row. The target's stands; the source's is
    #: simply deleted, never compared or merged field by field.
    KEEP_TARGET_DELETE_SOURCE = "keep_target_delete_source"
    #: `memberships`: unioned, conflict resolved by the higher role and
    #: `source="manual"` if either side was.
    UNION_MEMBERSHIPS = "union_memberships"
    #: `user_model_access`: unioned, the target's row wins a conflict as-is.
    UNION_MODEL_ACCESS = "union_model_access"
    #: `limit_rules`/`redaction_rules`, only the rows whose `scope="user"`
    #: names this person -- the same column also carries group, provider,
    #: model and api-key scoped rows a merge has no opinion about.
    SCOPE_ID_USER = "scope_id_user"
    #: `user_identities`: see `_classify_identities` and `_apply_identities`.
    #: The primary `(issuer, subject)` on `users` itself moves or drops by
    #: the same rule, which is why this is its own category rather than
    #: "reassign the FK" -- there is no FK for the primary pair to reassign.
    IDENTITIES = "identities"


@dataclass(frozen=True)
class MergeRule:
    table: str
    column: str
    kind: MergeRuleKind


#: ADR 0093 §7.1's own table, verbatim. Order follows the design's, not the
#: column's position in `models.py`.
MERGE_RULES: tuple[MergeRule, ...] = (
    MergeRule("user_identities", "user_id", MergeRuleKind.IDENTITIES),
    MergeRule("memberships", "user_id", MergeRuleKind.UNION_MEMBERSHIPS),
    MergeRule("user_model_access", "user_id", MergeRuleKind.UNION_MODEL_ACCESS),
    MergeRule("api_keys", "user_id", MergeRuleKind.REASSIGN),
    MergeRule("usage_records", "user_id", MergeRuleKind.REASSIGN),
    MergeRule("limit_rules", "scope_id", MergeRuleKind.SCOPE_ID_USER),
    MergeRule("redaction_rules", "scope_id", MergeRuleKind.SCOPE_ID_USER),
    MergeRule("email_settings", "updated_by", MergeRuleKind.REASSIGN),
    MergeRule("identity_providers", "created_by", MergeRuleKind.REASSIGN),
    MergeRule("oidc_config", "created_by", MergeRuleKind.REASSIGN),
    MergeRule("redaction_config", "created_by", MergeRuleKind.REASSIGN),
    MergeRule("redaction_rules", "created_by", MergeRuleKind.REASSIGN),
    MergeRule("directory_sync_runs", "started_by", MergeRuleKind.REASSIGN),
    MergeRule("quota_resets", "created_by", MergeRuleKind.REASSIGN),
    MergeRule("directory_entries", "user_id", MergeRuleKind.REASSIGN),
    MergeRule("refresh_credentials", "user_id", MergeRuleKind.DELETE),
    MergeRule("idp_authorization_codes", "user_id", MergeRuleKind.DELETE),
    MergeRule("password_reset_tokens", "user_id", MergeRuleKind.DELETE),
    MergeRule("local_credentials", "user_id", MergeRuleKind.DELETE),
    MergeRule(
        "quota_notification_settings", "user_id", MergeRuleKind.KEEP_TARGET_DELETE_SOURCE
    ),
    MergeRule("quota_notification_state", "user_id", MergeRuleKind.KEEP_TARGET_DELETE_SOURCE),
)

#: Tables the registry deliberately never names, because their reference to
#: a user is not a foreign key at all: `identity_events` keeps `actor_label`/
#: `target_label` for a person no longer here to look up, and `user_merges`
#: is this feature's own record of who went where. Named here, not just left
#: absent, so the guard's own failure message can say why on purpose rather
#: than someone reading a bare mismatch and wondering if it is a bug.
_DELIBERATELY_UNMANAGED = frozenset({"identity_events", "user_merges"})


def _fk_and_scope_columns() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for table in Base.metadata.tables.values():
        if table.name in _DELIBERATELY_UNMANAGED or table.name == "users":
            continue
        for column in table.columns:
            if column.name == "scope_id":
                found.add((table.name, column.name))
                continue
            for fk in column.foreign_keys:
                if fk.target_fullname == "users.id":
                    found.add((table.name, column.name))
    return found


def assert_merge_rules_are_complete() -> None:
    """Fails if any table references a person and has no merge rule for it.

    Walks `Base.metadata` itself rather than trusting the registry to be an
    honest inventory of itself -- the whole reason this exists is that a
    table added later, with no merge rule written for it, must fail a test
    immediately rather than merge quietly wrong.
    """
    required = _fk_and_scope_columns()
    registered = {(rule.table, rule.column) for rule in MERGE_RULES}
    missing = required - registered
    if missing:
        raise AssertionError(
            f"no merge rule for {sorted(missing)}: add one to gateway.merge.MERGE_RULES "
            "(ADR 0093 §7.1), or to _DELIBERATELY_UNMANAGED with the reason why not"
        )


class MergeNotFound(Exception):
    """The source or the target does not exist."""


class MergeRefused(Exception):
    """A refusal named in ADR 0093 §7.1: source=target, source=caller, either
    pending, or the target inactive. Never a database error -- those raise on
    their own terms."""


@dataclass(frozen=True)
class IdentityRef:
    issuer: str
    subject: str


@dataclass
class MergePreview:
    source_id: uuid.UUID
    target_id: uuid.UUID
    counts: dict[str, int]
    identities_moving: list[IdentityRef]
    identities_dropped: list[IdentityRef]
    resulting_is_admin: bool
    bundled_logins_disabled: list[str]
    chat_note: str = (
        "the chat folds this person's conversations into the target at their next activity"
    )


@dataclass
class MergeSummary:
    target_id: uuid.UUID
    counts: dict[str, int]
    identities_dropped: list[IdentityRef]
    bundled_logins_disabled: list[str]
    #: What the router needs to actually perform the disable, after it
    #: commits this transaction -- `None` when nothing needs disabling.
    #: `bundled_logins_disabled` above is the same fact, shaped for display.
    login_to_disable: tuple[uuid.UUID, str] | None = None


async def _load_pair(
    session: AsyncSession, source_id: uuid.UUID, target_id: uuid.UUID, *, lock: bool
) -> tuple[User | None, User | None]:
    # Locked in id order, not (source, target) order: two concurrent merges
    # that name the same pair of users in opposite roles must take their
    # locks in the same order or one of them deadlocks instead of queuing.
    ids = sorted([source_id, target_id])
    stmt = select(User).where(User.id.in_(ids))
    if lock:
        stmt = stmt.with_for_update()
    rows = {u.id: u for u in (await session.execute(stmt)).scalars().all()}
    return rows.get(source_id), rows.get(target_id)


def _check_refusals(
    source: User | None, target: User | None, *, source_id: uuid.UUID, actor_id: uuid.UUID
) -> tuple[User, User]:
    if source is None:
        raise MergeNotFound(f"No user with id {source_id}.")
    if target is None:
        raise MergeNotFound("No such target user.")
    if source.id == target.id:
        raise MergeRefused("A user cannot be merged into themselves.")
    if source.id == actor_id:
        raise MergeRefused(
            "You cannot merge the account you are signed in with. Sign in as the "
            "target, or have another administrator do it."
        )
    if source.issuer == PENDING_USER_ISSUER:
        raise MergeRefused("The source account is pending its first sign-in and cannot merge.")
    if target.issuer == PENDING_USER_ISSUER:
        raise MergeRefused("The target account is pending its first sign-in and cannot merge.")
    if not target.is_active:
        raise MergeRefused("The target account is not active.")
    return source, target


async def _source_identities(session: AsyncSession, source: User) -> list[IdentityRef]:
    linked = (
        await session.execute(select(UserIdentity).where(UserIdentity.user_id == source.id))
    ).scalars().all()
    return [IdentityRef(source.issuer, source.subject)] + [
        IdentityRef(row.issuer, row.subject) for row in linked
    ]


async def _target_issuers(session: AsyncSession, target: User) -> set[str]:
    linked = (
        await session.execute(select(UserIdentity.issuer).where(UserIdentity.user_id == target.id))
    ).scalars().all()
    return {target.issuer, *linked}


def _classify_identities(
    identities: list[IdentityRef], target_issuers: set[str]
) -> tuple[list[IdentityRef], list[IdentityRef]]:
    moving = [i for i in identities if i.issuer not in target_issuers]
    dropped = [i for i in identities if i.issuer in target_issuers]
    return moving, dropped


async def _bundled_login_to_disable(
    session: AsyncSession, source: User, dropped_issuers: set[str]
) -> tuple[uuid.UUID, str] | None:
    """The source's bundled login, when its identity there is one of the
    dropped ones -- `None` when source has no bundled login, or its identity
    moved cleanly instead of conflicting."""
    provider = await active_bundled_provider(session)
    if provider is None or provider.issuer not in dropped_issuers:
        return None
    entry = (
        await session.execute(
            select(DirectoryEntry).where(
                DirectoryEntry.provider_id == provider.id, DirectoryEntry.user_id == source.id
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        return None
    return provider.id, entry.external_id


async def _count(session: AsyncSession, rule: MergeRule, source_id: uuid.UUID) -> int:
    table = Base.metadata.tables[rule.table]
    stmt = select(func.count()).select_from(table)
    if rule.kind is MergeRuleKind.SCOPE_ID_USER:
        stmt = stmt.where(table.c.scope == "user", table.c.scope_id == source_id)
    else:
        stmt = stmt.where(table.c[rule.column] == source_id)
    return (await session.execute(stmt)).scalar_one()


async def compute_merge_preview(
    session: AsyncSession, *, source_id: uuid.UUID, target_id: uuid.UUID, actor_id: uuid.UUID
) -> MergePreview:
    """Read-only: no row is written, locked or even necessarily lockable --
    a preview is shown before the operator has typed the confirmation, and
    must not hold a transaction open across that.
    """
    source, target = await _load_pair(session, source_id, target_id, lock=False)
    source, target = _check_refusals(source, target, source_id=source_id, actor_id=actor_id)

    identities = await _source_identities(session, source)
    target_issuers = await _target_issuers(session, target)
    moving, dropped = _classify_identities(identities, target_issuers)
    dropped_issuers = {i.issuer for i in dropped}

    counts: dict[str, int] = {}
    for rule in MERGE_RULES:
        if rule.kind is MergeRuleKind.IDENTITIES:
            continue
        counts[rule.table] = counts.get(rule.table, 0) + await _count(session, rule, source.id)
    counts["user_identities"] = len(identities)

    disable = await _bundled_login_to_disable(session, source, dropped_issuers)

    return MergePreview(
        source_id=source.id,
        target_id=target.id,
        counts=counts,
        identities_moving=moving,
        identities_dropped=dropped,
        resulting_is_admin=target.is_admin or source.is_admin,
        bundled_logins_disabled=[disable[1]] if disable else [],
    )


_ROLE_RANK = {MembershipRole.MEMBER: 0, MembershipRole.ADMIN: 1}


async def _merge_memberships(session: AsyncSession, source: User, target: User) -> int:
    """Through the relationship collections, not a raw column assignment.

    `User.memberships` is `lazy="selectin"`, so both users' collections are
    already loaded, in memory, by the time this runs -- loaded once, as part
    of `_load_pair`'s own `select(User)`. Setting `row.user_id = target.id`
    directly would leave `row` sitting in `source.memberships`'s *already
    materialised* Python list regardless, and `cascade="delete-orphan"`
    cascades off exactly that list at `session.delete(source)`, not off a
    fresh query -- so a plain column reassignment here silently loses the
    row the moment the source is deleted. Moving it between the two
    collections keeps the ORM's own bookkeeping (and the FK column, via
    `back_populates`) in the same place at the same time.
    """
    source_rows = list(source.memberships)
    target_by_group = {m.group_id: m for m in target.memberships}
    for row in source_rows:
        existing = target_by_group.get(row.group_id)
        if existing is None:
            source.memberships.remove(row)
            target.memberships.append(row)
        else:
            if _ROLE_RANK[row.role] > _ROLE_RANK[existing.role]:
                existing.role = row.role
            if row.source is MembershipSource.MANUAL:
                existing.source = MembershipSource.MANUAL
            # `row` stays in `source.memberships` and cascade-deletes with it.
    return len(source_rows)


async def _merge_model_access(
    session: AsyncSession, source_id: uuid.UUID, target_id: uuid.UUID
) -> int:
    source_rows = (
        await session.execute(select(UserModelAccess).where(UserModelAccess.user_id == source_id))
    ).scalars().all()
    target_model_ids = {
        r.model_id
        for r in (
            await session.execute(
                select(UserModelAccess).where(UserModelAccess.user_id == target_id)
            )
        ).scalars().all()
    }
    for row in source_rows:
        if row.model_id not in target_model_ids:
            row.user_id = target_id
        # else: the target's row wins; `row` cascade-deletes with the source.
    return len(source_rows)


async def _apply_identities(
    session: AsyncSession,
    source: User,
    target: User,
    moving: list[IdentityRef],
) -> None:
    linked_by_pair = {
        (row.issuer, row.subject): row
        for row in (
            await session.execute(select(UserIdentity).where(UserIdentity.user_id == source.id))
        ).scalars().all()
    }
    for identity in moving:
        row = linked_by_pair.get((identity.issuer, identity.subject))
        if row is not None:
            row.user_id = target.id
        else:
            # The source's own primary pair: there is no `user_identities`
            # row to reassign, because a primary identity lives on `users`
            # itself. It moves by gaining one on the target instead.
            session.add(
                UserIdentity(user_id=target.id, issuer=identity.issuer, subject=identity.subject)
            )


async def merge_users(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    actor_id: uuid.UUID,
    actor_label: str,
    reason: str,
) -> MergeSummary:
    """Runs the whole of ADR 0093 §7.1 in one transaction: identities, the
    per-table rules, the target's own admin flag and profile fallback, the
    `user_merges` row and the `user.merge` audit. The caller commits.

    The one thing this does **not** do is disable a dropped bundled login in
    Authelia -- that is a file write, deliberately made *after* the commit
    (the same reasoning `_sync_bundled_login` gives elsewhere): a login file
    unreachable right now must not roll back a merge that otherwise fully
    succeeded. Returned in the summary so the caller can retry it.
    """
    source, target = await _load_pair(session, source_id, target_id, lock=True)
    source, target = _check_refusals(source, target, source_id=source_id, actor_id=actor_id)

    identities = await _source_identities(session, source)
    target_issuers = await _target_issuers(session, target)
    moving, dropped = _classify_identities(identities, target_issuers)
    dropped_issuers = {i.issuer for i in dropped}
    disable = await _bundled_login_to_disable(session, source, dropped_issuers)

    counts: dict[str, int] = {"user_identities": len(identities)}
    await _apply_identities(session, source, target, moving)

    for rule in MERGE_RULES:
        if rule.kind is MergeRuleKind.IDENTITIES:
            continue
        if rule.kind is MergeRuleKind.UNION_MEMBERSHIPS:
            counts[rule.table] = await _merge_memberships(session, source, target)
        elif rule.kind is MergeRuleKind.UNION_MODEL_ACCESS:
            counts[rule.table] = await _merge_model_access(session, source.id, target.id)
        else:
            counts[rule.table] = counts.get(rule.table, 0) + await _apply_generic(
                session, rule, source.id, target.id
            )

    # The target's own row: admin, then the profile fields a login would
    # otherwise have to fill, only where the target has nothing of its own.
    target.is_admin = target.is_admin or source.is_admin
    if target.email is None and source.email is not None:
        target.email = source.email
        target.email_normalized = source.email_normalized
        target.email_verified = source.email_verified
    if target.display_name is None and source.display_name is not None:
        target.display_name = source.display_name
    if target.username is None and source.username is not None:
        target.username = source.username
    if target.default_billing_group_id is None and source.default_billing_group_id is not None:
        target.default_billing_group_id = source.default_billing_group_id

    target.merged_at = utcnow()
    target.sessions_valid_after = utcnow()

    summary = dict(counts)
    session.add(
        UserMerge(
            source_user_id=source.id,
            target_user_id=target.id,
            actor_user_id=actor_id,
            summary=summary,
            reason=reason,
        )
    )
    await record_event(
        session,
        actor_type=IdentityEventActor.USER,
        actor_user_id=actor_id,
        actor_label=actor_label,
        action=IdentityEventAction.USER_MERGE,
        target_user_id=target.id,
        target_label=target.email or "",
        reason=reason,
        detail={"summary": summary},
    )

    await session.delete(source)
    await session.flush()

    return MergeSummary(
        target_id=target.id,
        counts=counts,
        identities_dropped=dropped,
        bundled_logins_disabled=[disable[1]] if disable else [],
        login_to_disable=disable,
    )


async def _apply_generic(
    session: AsyncSession, rule: MergeRule, source_id: uuid.UUID, target_id: uuid.UUID
) -> int:
    table = Base.metadata.tables[rule.table]
    if rule.kind is MergeRuleKind.SCOPE_ID_USER:
        result = await session.execute(
            update(table)
            .where(table.c.scope == "user", table.c.scope_id == source_id)
            .values(scope_id=target_id)
        )
        return cast(CursorResult[Any], result).rowcount or 0
    if rule.kind is MergeRuleKind.REASSIGN:
        result = await session.execute(
            update(table).where(table.c[rule.column] == source_id).values({rule.column: target_id})
        )
        return cast(CursorResult[Any], result).rowcount or 0
    if rule.kind in (MergeRuleKind.DELETE, MergeRuleKind.KEEP_TARGET_DELETE_SOURCE):
        result = await session.execute(sa_delete(table).where(table.c[rule.column] == source_id))
        return cast(CursorResult[Any], result).rowcount or 0
    raise AssertionError(f"unhandled generic merge rule kind {rule.kind!r}")


async def disable_dropped_bundled_login(
    session: AsyncSession,
    secrets: SecretBox,
    *,
    provider_id: uuid.UUID,
    external_id: str,
    target_id: uuid.UUID,
    actor_id: uuid.UUID,
    actor_label: str,
) -> None:
    """The post-commit half of a merge that dropped a bundled identity
    (`merge_users`' `bundled_logins_disabled`). A file write, so it is never
    inside the merge's own transaction; a login file unreachable right now
    is retried the same way `_sync_bundled_login` is -- by the operator
    re-running the action, here by re-merging, which is idempotent (a
    login already disabled is simply set to disabled again).
    """
    provider = await session.get(IdentityProvider, provider_id)
    if provider is None:
        return
    users_file = bundled_users_file(provider, secrets)
    try:
        users_file.update(external_id, disabled=True)
    except UsersFileError as exc:
        # Soft failure, the same reasoning `_sync_bundled_login` gives: the
        # merge already committed, so the gateway-side state (the dropped
        # identity, the target's data) stands regardless. Only Authelia's
        # own file is out of step until a retry -- re-merging is a no-op by
        # then, so the retry that actually fixes this is a plain admin
        # console action against the users file, not another merge.
        logger.warning(
            "could not disable dropped bundled login %r after merge: %s", external_id, exc
        )
        await record_event(
            session,
            actor_type=IdentityEventActor.USER,
            actor_user_id=actor_id,
            actor_label=actor_label,
            action=IdentityEventAction.LOGIN_DISABLE,
            target_user_id=target_id,
            target_label=external_id,
            detail={"result": "failed"},
        )
        await session.commit()
        return
    await record_event(
        session,
        actor_type=IdentityEventActor.USER,
        actor_user_id=actor_id,
        actor_label=actor_label,
        action=IdentityEventAction.LOGIN_DISABLE,
        target_user_id=target_id,
        target_label=external_id,
    )
    await session.commit()
