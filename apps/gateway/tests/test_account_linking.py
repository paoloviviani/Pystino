"""ADR 0093 §3.2: the ``issuer="local"``-only adopter is removed.

Review correction 1 flagged the old matcher (`gw/oidc.py:489, 566-567` at the
time) as unsound on its own: it adopted a local account purely because a
directory login's casefolded address matched the local door's, with no way to
scope that to a directory an administrator actually trusts to say so. §6
replaces it with a cross-issuer rule in stage (c); this stage only removes the
old one, so what is tested here is that removal — a directory login no longer
adopts anything, ever — and that the ``link_by_email`` switch itself still
survives the removal, since stage (c) and the console both still read it.

The full adoption behaviour (verified matches, refusals, what adoption does or
does not change) belonged to `_adopt_local_account`, which no longer exists;
its tests went with it. Stage (c) writes its own for the rule that replaces
it.
"""

from __future__ import annotations

from gateway.config import OIDCSettings
from gateway.identity_registry import record_from_env, record_from_row
from gateway.models import IdentityProvider, LocalCredential, User
from gateway.oidc import provision_user
from gateway.secrets import SecretBox
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

IDP = "https://idp.test"
ADDRESS = "paolo@example.org"
# Shaped like an Argon2 hash and deliberately not one: no test here verifies a
# password, and a real hash would invite someone to try.
NOT_A_HASH = "argon2id-placeholder"


async def make_local_user(session: AsyncSession, *, email: str = ADDRESS) -> User:
    """A local account exactly as ``gateway passwd`` makes one: casefolded subject."""
    user = User(
        issuer="local",
        subject=email.casefold(),
        email=email.casefold(),
        display_name="Local Person",
    )
    session.add(user)
    await session.flush()
    session.add(LocalCredential(user_id=user.id, password_hash=NOT_A_HASH))
    await session.commit()
    return user


class TestTheOldMatcherIsGone:
    async def test_a_verified_matching_login_no_longer_adopts_the_local_account(
        self, session: AsyncSession
    ) -> None:
        """What `_adopt_local_account` used to do here, and no longer does.

        Every argument that used to earn a link — a verified address — is
        passed; the point is that none of it matters any more. The switch
        itself (`allow_local_link`) is not `provision_user`'s to read at all
        now: linking is decided before this function is ever called (§6,
        stage c's `link_by_email`), and this call, on its own, is exactly
        what the plain `/v1` path or a failed link leaves `provision_user`
        to do.
        """
        local = await make_local_user(session)
        user = await provision_user(
            session,
            issuer=IDP,
            subject="idp-subject",
            email=ADDRESS,
            display_name="Directory Person",
            group_names=[],
            settings=OIDCSettings(),
            email_verified=True,
        )
        await session.commit()

        assert user.id != local.id, "no matcher is left to adopt the local account"
        accounts = (await session.execute(select(User))).scalars().all()
        assert len(accounts) == 2


class TestTheSwitchIsCarried:
    """`link_by_email` itself is untouched: stage (c) and the console read it."""

    def test_a_row_carries_it_to_the_record(self, app: object) -> None:
        box: SecretBox = app.state.secrets  # type: ignore[attr-defined]
        row = IdentityProvider(
            name="corp",
            issuer=IDP,
            client_id="c",
            client_secret_encrypted=box.encrypt("s"),
            scopes=["openid"],
            link_by_email=True,
        )
        assert record_from_row(row, box).link_by_email is True

    def test_the_default_is_off_on_a_new_row(self, app: object) -> None:
        box: SecretBox = app.state.secrets  # type: ignore[attr-defined]
        row = IdentityProvider(
            name="corp",
            issuer=IDP,
            client_id="c",
            client_secret_encrypted=box.encrypt("s"),
            scopes=["openid"],
            link_by_email=False,
        )
        assert record_from_row(row, box).link_by_email is False

    def test_the_environment_fallback_defaults_to_off(self, settings: object) -> None:
        """An upgrade may not switch adoption on by itself: no switch, no link."""
        from gateway.config import OIDCSettings as OS

        settings.oidc = OS(  # type: ignore[attr-defined]
            enabled=True, issuer=IDP, client_id="c", client_secret="s"
        )
        record = record_from_env(settings)  # type: ignore[arg-type]
        assert record is not None
        assert record.link_by_email is False

    # The two tests that used to live here — the environment fallback and the
    # seeded row both honouring `GATEWAY_OIDC__LINK_LOCAL_BY_EMAIL=true` — are
    # gone, not just updated: ADR 0093 §1 makes that value a startup error
    # (`test_config_admin_rules.py::TestRemovedVariables`), so `OIDCSettings`
    # can no longer be constructed with it at all. The env-to-row fallback
    # this exercised (`identity_registry.py`'s pre-seed default) is now
    # unreachable rather than wrong; the design leaves it in place for the
    # re-seed to remove.

    # The API-surface tests that used to live here (creating and editing a
    # provider row through POST/PUT `/admin/identity-providers` to prove the
    # switch round-tripped) are gone along with those routes (ADR 0093 §14,
    # closed in this stage): the row is a projection of the environment now,
    # re-seeded at every start, and the tests above already cover
    # `record_from_row`/`record_from_env` carrying the field.
