"""The first-start migration for bundled Authelia users (ADR 0093 §13.4).

Three things a deployment that had the bundled Authelia *before* this stage
needs, none of them a schema change and all of them safe to run again:

1. **Memberships stop following a login.** `memberships.source='oidc'` for a
   bundled user meant "the directory granted this, and a sync may revoke
   it" — but the bundled row's own `group_source` is now `none` (stage a),
   so nothing will ever grant or revoke one of these again. Converting the
   provenance to `manual` is what makes today's groups *console* groups,
   matching what `add_manual_memberships`/the console now writes for a new
   grant, rather than leaving old rows in a provenance the code no longer
   produces.
2. **The users file stops carrying group assignments** (§8.3): every entry's
   `groups` becomes exactly `["users"]`, under the same lock every other
   write to the file takes.
3. **A `directory_entries` row exists for every file login**, so the
   bundled-only admin routes (reset, disable, enable) — which all resolve a
   login through `directory_entries.external_id`, never `users.username`
   (the stable-login rule, stage b) — have a row to find for a person who
   signed in long before this stage existed. Bound where a user with a
   matching `(issuer, username)` already exists (that pairing is exactly
   what a real sign-in through this same bundled issuer already
   established); left unbound otherwise, since there is no user to point it
   at yet.

Idempotent: a second run finds every membership already `manual`, every
group already `["users"]` and every login already entered, and does nothing
beyond the reads that establish that.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.directory.authelia_users import UsersFile, UsersFileError
from gateway.directory.service import decrypt_config
from gateway.identity_events import record_event
from gateway.models import (
    DirectoryEntry,
    IdentityEventAction,
    IdentityEventActor,
    IdentityProvider,
    Membership,
    MembershipSource,
    User,
)
from gateway.secrets import SecretBox

logger = logging.getLogger(__name__)


def _users_file(provider: IdentityProvider, secrets: SecretBox) -> UsersFile:
    config = json.loads(decrypt_config(provider, secrets) or "{}")
    return UsersFile(Path(config.get("path") or "/authelia/users_database.yml"))


async def migrate_bundled_directory(
    session: AsyncSession, provider: IdentityProvider, secrets: SecretBox
) -> None:
    """Run all three steps for the given (already-confirmed-bundled) provider.

    Callers decide whether a failure here is fatal, the same posture as
    `reseed_from_env` and the admin-email sweep in `main.py`: this only
    reads and writes, and never raises anything but a genuine database or
    filesystem error.
    """
    await session.execute(
        update(Membership)
        .where(
            Membership.source == MembershipSource.OIDC,
            Membership.user_id.in_(select(User.id).where(User.issuer == provider.issuer)),
        )
        .values(source=MembershipSource.MANUAL)
    )

    users_file = _users_file(provider, secrets)
    try:
        removed = users_file.normalize_groups()
    except UsersFileError:
        # No file yet (a fresh deployment with the profile off, or between
        # bootstrap and the volume being mounted) — nothing to normalise,
        # and not this function's job to create the file.
        removed = {}
        logger.info("no bundled users file to normalise groups on yet")
    if removed:
        distinct_names = sorted({name for names in removed.values() for name in names})
        await record_event(
            session,
            actor_type=IdentityEventActor.SYSTEM,
            actor_label="system",
            action=IdentityEventAction.IDP_RESEED,
            detail={"removed_groups": distinct_names},
        )

    try:
        file_logins = [u.username for u in users_file.users()]
    except UsersFileError:
        file_logins = []

    if file_logins:
        existing = {
            row.external_id
            for row in (
                await session.execute(
                    select(DirectoryEntry).where(DirectoryEntry.provider_id == provider.id)
                )
            )
            .scalars()
            .all()
        }
        for login in file_logins:
            if login in existing:
                continue
            # link_at_login's own matcher, applied once here rather than
            # left to run at a login this deployment's users may never
            # trigger again: the pairing a real sign-in through this issuer
            # already established.
            matched = (
                await session.execute(
                    select(User).where(User.issuer == provider.issuer, User.username == login)
                )
            ).scalar_one_or_none()
            session.add(
                DirectoryEntry(
                    provider_id=provider.id,
                    external_id=login,
                    username=login,
                    user_id=matched.id if matched is not None else None,
                )
            )
    await session.flush()
