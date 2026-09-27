"""`reseed_from_env` (ADR 0093 §2): the environment wins outright, every start.

`test_idp_logout_and_kind.py::TestTheBundledAutheliaRow` and
`test_oidc_backchannel.py` already cover the plain update-in-place case
(env-owned fields overwritten, console-owned sync fields kept). This file is
the switch: a changed issuer renames the old row aside rather than deleting
it, a switch back reactivates it, every other row is disabled, and both are
audited.
"""

from __future__ import annotations

from gateway.config import OIDCSettings, Settings
from gateway.identity_registry import reseed_from_env
from gateway.models import DirectoryEntry, IdentityEvent, IdentityEventAction, IdentityProvider
from gateway.secrets import SecretBox
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

OLD_ISSUER = "https://old.example.org"
NEW_ISSUER = "https://new.example.org"
BOX = SecretBox(["test-encryption-key-not-for-production"])


def _settings(issuer: str, **kwargs: object) -> Settings:
    client_id = kwargs.pop("client_id", "c")
    return Settings(oidc=OIDCSettings(enabled=True, issuer=issuer, client_id=client_id, **kwargs))  # type: ignore[arg-type]


async def _one_row(session: AsyncSession) -> IdentityProvider:
    return (await session.execute(select(IdentityProvider))).scalar_one()


async def _rows(session: AsyncSession) -> list[IdentityProvider]:
    return list((await session.execute(select(IdentityProvider))).scalars().all())


class TestFirstEverStart:
    async def test_inserts_one_enabled_row_named_default(self, session: AsyncSession) -> None:
        await reseed_from_env(session, _settings(NEW_ISSUER), BOX)
        row = await _one_row(session)
        assert row.name == "default"
        assert row.is_enabled
        assert row.issuer == NEW_ISSUER

    async def test_is_audited(self, session: AsyncSession) -> None:
        await reseed_from_env(session, _settings(NEW_ISSUER), BOX)
        event = (
            await session.execute(
                select(IdentityEvent).where(IdentityEvent.action == IdentityEventAction.IDP_RESEED)
            )
        ).scalar_one()
        assert event.detail == {"old_issuer": "", "new_issuer": NEW_ISSUER}


class TestSteadyState:
    async def test_a_second_start_with_the_same_issuer_updates_in_place(
        self, session: AsyncSession
    ) -> None:
        await reseed_from_env(session, _settings(NEW_ISSUER), BOX)
        await reseed_from_env(session, _settings(NEW_ISSUER, client_id="rotated"), BOX)
        assert len(await _rows(session)) == 1
        row = await _one_row(session)
        assert row.client_id == "rotated"

    async def test_the_steady_state_is_not_re_audited(self, session: AsyncSession) -> None:
        await reseed_from_env(session, _settings(NEW_ISSUER), BOX)
        await reseed_from_env(session, _settings(NEW_ISSUER), BOX)
        events = (
            (
                await session.execute(
                    select(IdentityEvent).where(
                        IdentityEvent.action == IdentityEventAction.IDP_RESEED
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1


class TestAnIssuerSwitch:
    async def test_the_old_row_is_renamed_aside_and_disabled_not_deleted(
        self, session: AsyncSession
    ) -> None:
        await reseed_from_env(session, _settings(OLD_ISSUER), BOX)
        old_id = (await _one_row(session)).id

        await reseed_from_env(session, _settings(NEW_ISSUER), BOX)

        rows = {row.id: row for row in await _rows(session)}
        assert len(rows) == 2
        old_row = rows[old_id]
        assert old_row.name.startswith("previous-")
        assert not old_row.is_enabled
        assert old_row.issuer == OLD_ISSUER  # identities at it stay resolvable

        new_row = next(r for r in rows.values() if r.id != old_id)
        assert new_row.name == "default"
        assert new_row.is_enabled
        assert new_row.issuer == NEW_ISSUER

    async def test_is_audited_with_both_issuers(self, session: AsyncSession) -> None:
        await reseed_from_env(session, _settings(OLD_ISSUER), BOX)
        await reseed_from_env(session, _settings(NEW_ISSUER), BOX)
        event = (
            await session.execute(
                select(IdentityEvent)
                .where(IdentityEvent.action == IdentityEventAction.IDP_RESEED)
                .order_by(IdentityEvent.at.desc())
            )
        ).scalars().first()
        assert event is not None
        assert event.detail == {"old_issuer": OLD_ISSUER, "new_issuer": NEW_ISSUER}

    async def test_bundled_directory_entries_move_to_the_new_row(
        self, session: AsyncSession
    ) -> None:
        await reseed_from_env(session, _settings(OLD_ISSUER, kind="authelia"), BOX)
        old_row = await _one_row(session)
        session.add(DirectoryEntry(provider_id=old_row.id, external_id="alice"))
        await session.commit()

        await reseed_from_env(session, _settings(NEW_ISSUER, kind="authelia"), BOX)

        entry = (await session.execute(select(DirectoryEntry))).scalar_one()
        new_row = next(r for r in await _rows(session) if r.id != old_row.id)
        assert entry.provider_id == new_row.id

    async def test_directory_entries_of_a_non_bundled_row_are_left_alone(
        self, session: AsyncSession
    ) -> None:
        await reseed_from_env(session, _settings(OLD_ISSUER, kind="generic"), BOX)
        old_row = await _one_row(session)
        session.add(DirectoryEntry(provider_id=old_row.id, external_id="alice"))
        await session.commit()

        await reseed_from_env(session, _settings(NEW_ISSUER), BOX)

        entry = (await session.execute(select(DirectoryEntry))).scalar_one()
        assert entry.provider_id == old_row.id

    async def test_a_third_provider_row_is_disabled_but_keeps_its_name(
        self, session: AsyncSession
    ) -> None:
        session.add(
            IdentityProvider(
                name="corp",
                issuer="https://corp.example.org",
                client_id="c",
                client_secret_encrypted=BOX.encrypt("s"),
                scopes=["openid"],
                is_enabled=True,
            )
        )
        await session.commit()
        await reseed_from_env(session, _settings(NEW_ISSUER), BOX)

        corp = (
            await session.execute(select(IdentityProvider).where(IdentityProvider.name == "corp"))
        ).scalar_one()
        assert not corp.is_enabled


class TestASwitchBack:
    async def test_the_disabled_row_is_reactivated_and_renamed_default(
        self, session: AsyncSession
    ) -> None:
        await reseed_from_env(session, _settings(OLD_ISSUER), BOX)
        old_id = (await _one_row(session)).id
        await reseed_from_env(session, _settings(NEW_ISSUER), BOX)

        # Switch back: OLD_ISSUER is configured again.
        await reseed_from_env(session, _settings(OLD_ISSUER), BOX)

        rows = {row.id: row for row in await _rows(session)}
        assert len(rows) == 2
        reactivated = rows[old_id]
        assert reactivated.name == "default"
        assert reactivated.is_enabled
        assert reactivated.issuer == OLD_ISSUER

        other = next(r for r in rows.values() if r.id != old_id)
        assert not other.is_enabled
        assert other.name.startswith("previous-")

    async def test_no_unique_name_collision_across_the_round_trip(
        self, session: AsyncSession
    ) -> None:
        # A regression test for the ordering bug this would be: renaming the
        # reactivated row to "default" before the currently-active row has
        # given up that name raises a unique-constraint violation on both
        # dialects, even though the end state is consistent.
        await reseed_from_env(session, _settings(OLD_ISSUER), BOX)
        await reseed_from_env(session, _settings(NEW_ISSUER), BOX)
        await reseed_from_env(session, _settings(OLD_ISSUER), BOX)
        await reseed_from_env(session, _settings(NEW_ISSUER), BOX)
        rows = await _rows(session)
        assert len(rows) == 2
        assert sorted(row.name == "default" for row in rows) == [False, True]
