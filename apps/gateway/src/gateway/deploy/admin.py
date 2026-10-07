"""`pystino admin grant|revoke <email>` and `pystino break-glass`: the two
recovery doors for an env-authoritative single IdP (ADR 0093 §10).

With no local-password door (decision D3), the ways to become an administrator
are the console (another administrator), a provider that decides admin by
claim, the bootstrap address on an empty deployment — and `admin grant`, for
when all three are unavailable but the operator can still reach the
configured identity provider. `admin grant`/`revoke` run inside the gateway
container (`docker compose exec gateway pystino admin grant you@example.org`)
against the database directly, so they need shell access to the host, which
already implies the database. A grant is recorded as `manual`: no directory
can undo it.

`break_glass` is the deeper recovery: it does not need the configured IdP to
be reachable, or even the same one as before, because `./configure
--break-glass` (cerea-deploy) has already rewritten `.env` to the bundled
Authelia before this runs (ADR 0093 §10 host steps 1-2). It replaces
`pystino admin grant` as the *documented* recovery; `admin grant`/`revoke`
stay available for the ordinary case, where whatever IdP is already
configured is still reachable.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.config import Settings
from gateway.deployment_state import mark_bootstrap_consumed
from gateway.directory.authelia_users import LOGIN, UsersFile, UsersFileError, UsersFileLockedError
from gateway.directory.engine import ensure_bundled_default_group
from gateway.directory.service import bundled_users_file
from gateway.email_normalize import is_trusted_email
from gateway.identity_events import record_event
from gateway.identity_registry import active_bundled_provider, reseed_from_env
from gateway.models import DirectoryEntry, IdentityEventAction, IdentityEventActor, User
from gateway.oidc import PENDING_USER_ISSUER, other_active_admin_exists
from gateway.secrets import SecretBox


class AdminCommandError(RuntimeError):
    pass


async def set_admin(session: AsyncSession, email: str, *, grant: bool, issuer: str = "") -> User:
    query = select(User).where(func.lower(User.email) == email.strip().casefold())
    if issuer:
        query = query.where(User.issuer == issuer)
    users = list((await session.execute(query)).scalars())
    if not users:
        raise AdminCommandError(
            f"no account has the email {email}; sign in once through the identity provider first"
        )
    if len(users) > 1:
        issuers = ", ".join(sorted(u.issuer for u in users))
        raise AdminCommandError(
            f"{email} names accounts at several issuers ({issuers}); pass --issuer"
        )
    user = users[0]
    would_revoke = not grant and user.is_admin
    if would_revoke and not await other_active_admin_exists(session, excluding=user.id):
        await record_event(
            session,
            actor_type=IdentityEventActor.CLI,
            actor_label="cli:admin-revoke",
            action=IdentityEventAction.ADMIN_REFUSED_LAST,
            target_user_id=user.id,
            target_label=user.email or email,
            reason="pystino admin revoke would leave no active administrator",
        )
        await session.commit()
        raise AdminCommandError("refusing to revoke the last active administrator")
    user.is_admin = grant
    user.admin_source = "manual"
    if grant and not user.is_active:
        user.is_active = True
        user.deactivated_by = None
    await record_event(
        session,
        actor_type=IdentityEventActor.CLI,
        actor_label="cli:admin-grant" if grant else "cli:admin-revoke",
        action=IdentityEventAction.ADMIN_GRANT if grant else IdentityEventAction.ADMIN_REVOKE,
        target_user_id=user.id,
        target_label=user.email or email,
    )
    if grant:
        await mark_bootstrap_consumed(session)
    await session.commit()
    return user


@dataclass(frozen=True)
class BreakGlassResult:
    user: User
    login: str
    password: str
    login_created: bool


def _several_matches_message(email: str, candidates: list[User]) -> str:
    listing = "\n".join(
        f"  {u.id}  issuer={u.issuer}  "
        f"last_sign_in={u.last_login_at.isoformat() if u.last_login_at else 'never'}"
        for u in candidates
    )
    return f"{email} names several accounts; pass --user-id\n{listing}"


def _derive_login(email: str, users_file: UsersFile) -> str:
    """A free login from an email's local part, validated by `LOGIN` (§10 step 2).

    "Free" means not already a line in the users file — `--login` is the
    escape hatch for an operator who wants a specific name instead.
    """
    local = re.sub(r"[^a-z0-9._-]", "-", (email.split("@", 1)[0] or "user").strip().lower())
    local = local.lstrip("-._") or "user"
    if not local[0].isalnum():
        local = f"u{local}"
    local = local[:64]
    taken = {u.username for u in users_file.users()}
    candidate = local
    n = 2
    while candidate in taken:
        suffix = f"-{n}"
        candidate = f"{local[: 64 - len(suffix)]}{suffix}"
        n += 1
    return candidate


async def break_glass(
    session: AsyncSession,
    settings: Settings,
    secrets: SecretBox,
    *,
    email: str,
    login: str | None,
    user_id: uuid.UUID | None,
    reason: str = "",
) -> BreakGlassResult:
    """`pystino break-glass` (ADR 0093 §10), run inside the gateway container
    against the database and the `authelia-config` volume.

    Called from `docker compose run --rm --no-deps gateway pystino
    break-glass …` — a one-off container, not `up`, so nothing has run
    `main.py`'s startup `reseed_from_env` against the `.env` that
    `./configure --break-glass` just rewrote to the bundled Authelia. This
    command re-seeds itself, first, rather than assume a row already exists:
    the host script's own step 5 (`docker compose up -d --wait`) reseeds
    again afterwards, harmlessly, once the app itself starts.

    Every user, membership, key, ledger row and identity is left untouched:
    only provider enabled flags (via the reseed above) and the target's
    admin/login state change, exactly as §10 promises.
    """
    await reseed_from_env(session, settings, secrets)

    normalized, _ = is_trusted_email(email)
    candidates = list(
        (await session.execute(select(User).where(User.email_normalized == normalized))).scalars()
    )
    if not candidates:
        target = User(
            issuer=PENDING_USER_ISSUER,
            subject=str(uuid.uuid4()),
            email=email,
            email_normalized=normalized,
            admin_edited_fields=["email"],
        )
        session.add(target)
        await session.flush()
    elif len(candidates) == 1:
        target = candidates[0]
    else:
        if user_id is None:
            raise AdminCommandError(_several_matches_message(email, candidates))
        found = next((u for u in candidates if u.id == user_id), None)
        if found is None:
            raise AdminCommandError(f"--user-id {user_id} does not name one of {email}'s accounts")
        target = found

    provider = await active_bundled_provider(session)
    if provider is None:
        raise AdminCommandError(
            "no bundled Authelia provider is configured for this environment; "
            "run ./configure --break-glass again after checking .env"
        )
    users_file = bundled_users_file(provider, secrets)

    entry = (
        await session.execute(
            select(DirectoryEntry).where(
                DirectoryEntry.provider_id == provider.id, DirectoryEntry.user_id == target.id
            )
        )
    ).scalar_one_or_none()
    login_created = entry is None

    try:
        if entry is not None:
            login_name = entry.external_id
            users_file.update(login_name, disabled=False)
            password = users_file.reset_password(login_name)
        else:
            login_name = login or _derive_login(target.email or email, users_file)
            if not LOGIN.match(login_name):
                raise AdminCommandError(
                    "the login must be lowercase letters, digits, '.', '_' or '-'"
                )
            _authelia_user, password = users_file.create(
                login_name, target.email or email, target.display_name or ""
            )
    except UsersFileLockedError as exc:
        raise AdminCommandError(str(exc)) from exc
    except UsersFileError as exc:
        raise AdminCommandError(str(exc)) from exc

    try:
        if login_created:
            session.add(
                DirectoryEntry(
                    provider_id=provider.id,
                    external_id=login_name,
                    username=login_name,
                    email=target.email or email,
                    user_id=target.id,
                )
            )
        target.is_admin = True
        target.admin_source = "manual"
        target.is_active = True
        target.deactivated_by = None
        # A pending user has never had a membership (to-do item 1): being an
        # administrator authenticates through the console's session cookie,
        # never a billing group, but this person may still want to call
        # `/v1` directly, and a fresh pending user has nothing to bill from
        # otherwise.
        await ensure_bundled_default_group(session, target)
        await mark_bootstrap_consumed(session)
        await record_event(
            session,
            actor_type=IdentityEventActor.CLI,
            actor_label="cli:break-glass",
            action=IdentityEventAction.BREAK_GLASS,
            target_user_id=target.id,
            target_label=target.email or email,
            reason=reason,
            detail={"login_new": login_created},
        )
        await session.commit()
    except Exception:
        await session.rollback()
        if login_created:
            with contextlib.suppress(UsersFileError):
                users_file.delete(login_name)
        raise

    return BreakGlassResult(
        user=target, login=login_name, password=password, login_created=login_created
    )
