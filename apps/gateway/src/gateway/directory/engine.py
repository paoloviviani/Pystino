"""Apply what a directory says to accounts, within provenance.

One code path for every trigger — a scheduled pull, a manual run, a SCIM push
— and for dry runs: the changes are made in the session and, for a dry run (or
when the mass-deactivation valve trips), rolled back, so a dry run reports
exactly what a real run would do rather than a second implementation's guess.

The rules, each the same one a login follows:

- **groups** come from the mirror only when the provider's group_source is
  ``directory``, and are applied as often as its group_sync says (every run;
  only when first linked; never). Only ``oidc`` memberships are touched.
- **admin**, when admin_source is ``claim``, from the entry's groups, with the
  provenance and last-admin guard of `oidc.apply_admin_answer`.
- **deprovisioning** deactivates (``deactivated_by="directory"``) and never
  deletes — the ledger keeps its rows (ADR 0048). A directory reactivates only
  what it deactivated.
- **the valve**: a run that would deactivate more than max(5, 10%) of the
  provider's active linked accounts stops and asks for confirmation. The
  classic failure is a directory answering with an empty list.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.config import OIDCSettings
from gateway.directory.adapters import Entry
from gateway.models import (
    DirectoryEntry,
    DirectorySyncRun,
    GroupSync,
    Membership,
    MembershipRole,
    MembershipSource,
    User,
)
from gateway.oidc import _reconcile_memberships, _resolve_groups, apply_admin_answer
from gateway.types import utcnow

logger = logging.getLogger(__name__)

MAX_CHANGES = 500
VALVE_MINIMUM = 5
VALVE_FRACTION = 0.10


@dataclass
class SyncReport:
    status: str = "ok"
    dry_run: bool = False
    seen: int = 0
    created: int = 0
    linked: int = 0
    updated: int = 0
    deactivated: int = 0
    reactivated: int = 0
    changes: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def note(self, kind: str, who: str, **detail: Any) -> None:
        if len(self.changes) < MAX_CHANGES:
            self.changes.append({"change": kind, "who": who, **detail})

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "dry_run": self.dry_run,
            "seen": self.seen,
            "created": self.created,
            "linked": self.linked,
            "updated": self.updated,
            "deactivated": self.deactivated,
            "reactivated": self.reactivated,
            "changes": self.changes,
            "error": self.error,
        }


def _who(entry: Entry | DirectoryEntry) -> str:
    return entry.username or entry.email or entry.external_id


async def add_manual_memberships(
    session: AsyncSession, user: User, names: Iterable[str]
) -> list[str]:
    """Pre-assigned groups become *manual* memberships: the console's, not the directory's."""
    from gateway.models import Group, GroupSource

    added = []
    have = {m.group_id for m in user.memberships}
    for name in names:
        group = (
            await session.execute(select(Group).where(Group.name == name))
        ).scalar_one_or_none()
        if group is None:
            group = Group(name=name, source=GroupSource.MANUAL)
            session.add(group)
            await session.flush()
        if group.id in have:
            continue
        session.add(
            Membership(
                user_id=user.id,
                group_id=group.id,
                role=MembershipRole.MEMBER,
                source=MembershipSource.MANUAL,
            )
        )
        added.append(name)
    await session.flush()
    return added


async def _oidc_group_names(session: AsyncSession, user: User) -> set[str]:
    await session.refresh(user, attribute_names=["memberships"])
    return {m.group.name for m in user.memberships if m.source == MembershipSource.OIDC}


async def _apply_to_user(
    session: AsyncSession,
    record: Any,
    user: User,
    entry: Entry,
    *,
    newly_linked: bool,
    settings: OIDCSettings,
    report: SyncReport,
) -> None:
    mappings = record.mappings_dict()
    answers = record.group_sync is GroupSync.EVERY_LOGIN or (
        record.group_sync is GroupSync.FIRST_LOGIN and newly_linked
    )
    if answers and record.group_source == "directory":
        before = await _oidc_group_names(session, user)
        wanted = [mappings.get(g, g) for g in entry.groups]
        groups = await _resolve_groups(session, wanted, settings)
        await _reconcile_memberships(session, user, groups)
        after = await _oidc_group_names(session, user)
        if after != before:
            report.updated += 1
            report.note(
                "groups",
                _who(entry),
                added=sorted(after - before),
                removed=sorted(before - after),
            )
    rule = record.admin_rule()
    if answers and rule is not None:
        outcome = await apply_admin_answer(
            session, user, rule.matches({rule.claim: list(entry.groups)}, mappings)
        )
        if outcome != "unchanged":
            report.note("admin", _who(entry), outcome=outcome)
    if record.sync_deprovision == "disable":
        if not entry.active and user.is_active:
            user.is_active = False
            user.deactivated_by = "directory"
            report.deactivated += 1
            report.note("deactivate", _who(entry), reason="disabled in the directory")
        elif entry.active and not user.is_active and user.deactivated_by == "directory":
            user.is_active = True
            user.deactivated_by = None
            report.reactivated += 1
            report.note("reactivate", _who(entry))


async def apply_entries(
    session: AsyncSession,
    record: Any,
    entries: list[Entry],
    *,
    settings: OIDCSettings,
    dry_run: bool,
    full: bool,
    force: bool = False,
) -> SyncReport:
    """Mirror `entries` and apply them. `full` means "this is everyone": absence is departure.

    Leaves the session committed (or rolled back, for a dry run or a tripped
    valve); the caller records the run.
    """
    report = SyncReport(dry_run=dry_run, seen=len(entries))
    provider_id = record.id
    now = utcnow()

    mirror = {
        row.external_id: row
        for row in (
            await session.execute(
                select(DirectoryEntry).where(DirectoryEntry.provider_id == provider_id)
            )
        ).scalars()
    }
    issuer_users = list(
        (await session.execute(select(User).where(User.issuer == record.issuer))).scalars()
    )
    by_subject = {u.subject: u for u in issuer_users}
    by_username = {u.username: u for u in issuer_users if u.username}
    by_id = {u.id: u for u in issuer_users}
    linked_active_before = sum(
        1
        for row in mirror.values()
        if row.user_id and by_id.get(row.user_id) is not None and by_id[row.user_id].is_active
    )

    seen_ids: set[str] = set()
    for entry in entries:
        seen_ids.add(entry.external_id)
        row = mirror.get(entry.external_id)
        if row is None:
            row = DirectoryEntry(
                id=uuid.uuid4(),
                provider_id=provider_id,
                external_id=entry.external_id,
                first_seen_at=now,
                groups=[],
                preassigned_groups=[],
                active=True,
            )
            session.add(row)
            mirror[entry.external_id] = row
        user: User | None = by_id.get(row.user_id) if row.user_id else None
        if user is None and entry.is_subject:
            user = by_subject.get(entry.external_id)
        if user is None and not entry.is_subject and entry.username:
            # Authelia: the login name is what the directory and the token's
            # preferred_username share; the opaque subject is not.
            user = by_username.get(entry.username)
        newly_linked = False
        if user is None and entry.is_subject and entry.active and record.sync_create_users:
            user = User(
                issuer=record.issuer,
                subject=entry.external_id,
                email=entry.email,
                display_name=entry.display_name,
                username=entry.username,
            )
            session.add(user)
            await session.flush()
            by_subject[user.subject] = user
            by_id[user.id] = user
            report.created += 1
            report.note("create", _who(entry))
            newly_linked = True
        if user is not None and row.user_id != user.id:
            row.user_id = user.id
            if not newly_linked:
                report.linked += 1
                report.note("link", _who(entry))
            newly_linked = True
            if row.preassigned_groups:
                await session.refresh(user, attribute_names=["memberships"])
                added = await add_manual_memberships(session, user, row.preassigned_groups)
                if added:
                    report.note("preassigned", _who(entry), groups=added)
        row.username = entry.username
        row.email = entry.email
        row.display_name = entry.display_name
        row.groups = list(entry.groups)
        row.active = entry.active
        row.present = True
        row.last_seen_at = now
        if user is not None:
            await _apply_to_user(
                session,
                record,
                user,
                entry,
                newly_linked=newly_linked,
                settings=settings,
                report=report,
            )

    if full:
        for external_id, row in mirror.items():
            if external_id in seen_ids or not row.present:
                continue
            row.present = False
            user = by_id.get(row.user_id) if row.user_id else None
            if user is not None and user.is_active and record.sync_deprovision == "disable":
                user.is_active = False
                user.deactivated_by = "directory"
                report.deactivated += 1
                report.note("deactivate", _who(row), reason="gone from the directory")
            else:
                report.note("gone", _who(row))

    threshold = max(VALVE_MINIMUM, math.ceil(VALVE_FRACTION * linked_active_before))
    if report.deactivated > threshold and not force and not dry_run:
        report.status = "needs_confirmation"
        report.error = (
            f"this run would deactivate {report.deactivated} accounts (more than {threshold}); "
            "nothing was applied — review the changes and run again with confirmation"
        )
        await session.rollback()
        return report

    if dry_run:
        await session.rollback()
    else:
        await session.commit()
    return report


async def record_run(
    session: AsyncSession,
    provider_id: uuid.UUID,
    report: SyncReport,
    *,
    trigger: str,
    started_at: Any,
    started_by: uuid.UUID | None = None,
) -> DirectorySyncRun:
    run = DirectorySyncRun(
        provider_id=provider_id,
        trigger=trigger,
        dry_run=report.dry_run,
        status=report.status,
        seen=report.seen,
        created=report.created,
        linked=report.linked,
        updated=report.updated,
        deactivated=report.deactivated,
        reactivated=report.reactivated,
        changes=report.changes,
        error=report.error,
        started_at=started_at,
        finished_at=utcnow(),
        started_by=started_by,
    )
    session.add(run)
    await session.commit()
    return run


async def link_at_login(
    session: AsyncSession,
    record: Any,
    user: User,
    claims: dict[str, Any],
    *,
    settings: OIDCSettings,
) -> bool:
    """Link a mirrored entry to the person signing in, for directories whose
    subject is unknown until then (Authelia).

    Matched on the login name *and*, when the entry carries one, a verified
    email — the directory and the token share both, and the second keeps a
    renamed login from inheriting someone else's entry. Pre-assigned groups
    become manual memberships, and with group_source=directory the entry's
    groups apply at once rather than at the next scheduled run.
    """
    username = claims.get("preferred_username")
    if not isinstance(username, str) or not username:
        return False
    row = (
        await session.execute(
            select(DirectoryEntry).where(
                DirectoryEntry.provider_id == record.id,
                DirectoryEntry.username == username,
                DirectoryEntry.user_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    if row.email:
        if claims.get("email_verified") is not True:
            return False
        if str(claims.get("email", "")).casefold() != row.email.casefold():
            return False
    row.user_id = user.id
    await session.refresh(user, attribute_names=["memberships"])
    if row.preassigned_groups:
        await add_manual_memberships(session, user, row.preassigned_groups)
    report = SyncReport()
    entry = Entry(
        external_id=row.external_id,
        username=row.username,
        email=row.email,
        display_name=row.display_name,
        groups=tuple(row.groups or ()),
        active=row.active,
    )
    await _apply_to_user(
        session, record, user, entry, newly_linked=True, settings=settings, report=report
    )
    return True
