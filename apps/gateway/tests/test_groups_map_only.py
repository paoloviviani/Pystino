"""Groups reach a local group only through a mapping — the one rule.

The rule this file pins: a group name from a token resolves through
``oidc_config.group_mappings`` and nothing else. An unmapped name resolves to
nothing even when a local group carries the same name (the IdP's group
namespace is user-writable, so a same-name claim is not a vouch), and it is
what the console lists as a seen name. ``auto`` import maps each new name to
a new local group of the same name — the group and the self-mapping — so the
old "every name becomes a group" convenience survives inside the rule.
"""

from __future__ import annotations

from typing import Any

import pytest
from gateway.config import OIDCSettings, Settings
from gateway.identity_registry import record_from_env
from gateway.models import (
    Group,
    GroupSource,
    OIDCPolicyConfig,
    SeenGroup,
    User,
)
from gateway.oidc import ProvisioningRefused, sign_in
from gateway.oidc_policy import OIDCPolicy
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

ISS = "https://gitlab.example.org"


def _settings(**overrides: Any) -> Settings:
    return Settings(
        oidc=OIDCSettings(enabled=True, issuer=ISS, client_id="c", default_group="", **overrides)
    )


def _record(settings: Settings) -> Any:
    record = record_from_env(settings)
    assert record is not None
    return record


def _policy(mode: str, **overrides: Any) -> OIDCPolicy:
    return OIDCPolicy(join_mode=mode, **overrides)


def _claims(subject: str, groups: list[str]) -> dict[str, Any]:
    return {
        "iss": ISS,
        "sub": subject,
        "email": f"{subject}@example.org",
        "email_verified": True,
        "groups": groups,
    }


async def _sign_in(
    session: AsyncSession,
    subject: str,
    groups: list[str],
    *,
    settings: Settings | None = None,
    policy: OIDCPolicy | None = None,
) -> Any:
    settings = settings or _settings()
    result = await sign_in(
        session,
        _record(settings),
        issuer=ISS,
        subject=subject,
        claims=_claims(subject, groups),
        settings=settings,
        policy=policy,
    )
    await session.commit()
    return result


async def _names(session: AsyncSession, user: User) -> set[str]:
    await session.refresh(user, attribute_names=["memberships"])
    return {m.group.name for m in user.memberships}


async def _row(session: AsyncSession, subject: str) -> User | None:
    return (
        await session.execute(select(User).where(User.issuer == ISS, User.subject == subject))
    ).scalar_one_or_none()


class TestTheMapper:
    """The one mapper, collapsed from two: keep mapped names, drop the rest."""

    def test_an_unmapped_name_is_dropped_even_when_it_names_a_group(self) -> None:
        policy = OIDCPolicy(join_mode="open", group_mappings={})
        assert policy.map_claim_group_names(["research"]) == []

    def test_a_mapped_name_resolves_to_its_target(self) -> None:
        policy = OIDCPolicy(join_mode="open", group_mappings={"gl/research": "research"})
        assert policy.map_claim_group_names(["gl/research", "gl/other"]) == ["research"]

    def test_duplicates_fold_and_order_survives(self) -> None:
        policy = OIDCPolicy(
            join_mode="open",
            group_mappings={"gl/a": "x", "gl/b": "x"},
        )
        assert policy.map_claim_group_names(["gl/a", "gl/b"]) == ["x"]

    def test_a_directory_source_provider_keeps_its_names_vouched(self) -> None:
        """`claim_source=False` is unchanged: the directory's claims never
        reconcile into memberships, and its names arrive vouched by the
        directory connection — mapped names still map, the rest keep their
        own name."""
        policy = OIDCPolicy(join_mode="open", group_mappings={"gl/a": "x"})
        assert policy.map_claim_group_names(["gl/a", "b"], claim_source=False) == ["x", "b"]
        assert policy.map_claim_group_names(["b"], claim_source=False) == ["b"]


class TestTheRule:
    async def test_an_unmapped_name_resolves_to_nothing_even_when_a_group_carries_it(
        self, session: AsyncSession
    ) -> None:
        """Today a claimed name equal to a local group lands in it. Under the
        one rule it resolves to nothing — and it is listed as a seen name, so
        the console can map it."""
        session.add(Group(name="research", source=GroupSource.MANUAL))
        await session.commit()

        result = await _sign_in(session, "u1", ["research"], policy=_policy("open"))
        assert await _names(session, result.user) == set()
        seen = (await session.execute(select(SeenGroup))).scalars().all()
        assert [(s.issuer, s.name) for s in seen] == [(ISS, "research")]
        assert result.user.unresolved_group_names == ["research"]

    async def test_a_mapped_name_resolves_through_its_mapping(self, session: AsyncSession) -> None:
        session.add(Group(name="research", source=GroupSource.MANUAL))
        await session.commit()
        policy = _policy("open", group_mappings={"gl/research": "research"})

        result = await _sign_in(session, "u2", ["gl/research"], policy=policy)

        assert await _names(session, result.user) == {"research"}
        assert (await session.execute(select(SeenGroup))).scalars().all() == []

    async def test_a_stranger_refused_by_the_gate_records_nothing(
        self, session: AsyncSession
    ) -> None:
        """The seen bookkeeping belongs to provisioning; a stranger the gate
        refused never reaches it, so a prober cannot fill the seen list with
        names."""
        policy = _policy("group", required_groups=("staff",))
        with pytest.raises(ProvisioningRefused, match="isn't in a group"):
            await _sign_in(session, "stranger", ["staff"], policy=policy)
        assert await _row(session, "stranger") is None
        assert (await session.execute(select(SeenGroup))).scalars().all() == []


class TestAutoImport:
    async def test_auto_creates_the_group_and_the_self_mapping(self, session: AsyncSession) -> None:
        """`auto` keeps its old promise inside the rule: each new claimed name
        becomes a local group of the same name AND a self-mapping, so the
        name keeps resolving after the token that named it is gone. The
        appended row carries the policy's other answers forward — a bare row
        would reset the join mode to the environment on the next poll."""
        session.add(OIDCPolicyConfig(join_mode="group", required_groups=["staff"]))
        await session.commit()

        result = await _sign_in(
            session, "u3", ["eng"], settings=_settings(group_import="auto"), policy=_policy("open")
        )

        assert "eng" in await _names(session, result.user)
        group = (await session.execute(select(Group).where(Group.name == "eng"))).scalar_one()
        assert group.source is GroupSource.OIDC
        rows = (
            (await session.execute(select(OIDCPolicyConfig).order_by(OIDCPolicyConfig.created_at)))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        newest = rows[-1]
        assert newest.group_mappings == [["eng", "eng"]]
        # The carry-forward: the join answers ride along, or the append would
        # silently reset them.
        assert newest.join_mode == "group"
        assert newest.required_groups == ["staff"]

    async def test_the_self_mapping_makes_the_next_login_resolve_without_a_new_row(
        self, session: AsyncSession
    ) -> None:
        settings = _settings(group_import="auto")
        first = await _sign_in(session, "u4", ["eng"], settings=settings, policy=_policy("open"))
        # The next sign-in learns the mapping the way production does: the
        # policy resolver folded the row the first login appended.
        stored = (
            (
                await session.execute(
                    select(OIDCPolicyConfig).order_by(OIDCPolicyConfig.created_at.desc()).limit(1)
                )
            )
            .scalars()
            .one()
        )
        policy = _policy("open", group_mappings=dict(stored.group_mappings or []))
        assert policy.map_claim_group_names(["eng"]) == ["eng"]

        second = await _sign_in(session, "u4", ["eng"], settings=settings, policy=policy)

        assert await _names(session, second.user) == await _names(session, first.user)
        assert len((await session.execute(select(OIDCPolicyConfig))).scalars().all()) == 1

    async def test_auto_self_maps_to_an_existing_group_of_the_same_name(
        self, session: AsyncSession
    ) -> None:
        """A manual group the IdP also names: the self-mapping points at the
        existing group (no same-named twin), which is what keeps the old
        auto behaviour's memberships working under the rule."""
        existing = Group(name="eng", source=GroupSource.MANUAL)
        session.add(existing)
        await session.commit()

        result = await _sign_in(
            session, "u5", ["eng"], settings=_settings(group_import="auto"), policy=_policy("open")
        )

        names = await _names(session, result.user)
        assert "eng" in names
        groups = (await session.execute(select(Group))).scalars().all()
        assert [g.name for g in groups] == ["eng"], "no second group was created"
        newest = (
            (
                await session.execute(
                    select(OIDCPolicyConfig).order_by(OIDCPolicyConfig.created_at.desc()).limit(1)
                )
            )
            .scalars()
            .one()
        )
        assert newest.group_mappings == [["eng", "eng"]]

    async def test_a_refused_stranger_mints_no_self_mapping(self, session: AsyncSession) -> None:
        """Auto creates things at provisioning; a stranger the group gate
        refused never gets there, so their claim mints neither a group nor a
        mapping row."""
        policy = _policy("group", required_groups=("staff",))
        with pytest.raises(ProvisioningRefused, match="isn't in a group"):
            await _sign_in(
                session,
                "stranger",
                ["staff"],
                settings=_settings(group_import="auto"),
                policy=policy,
            )
        assert (await session.execute(select(Group))).scalars().all() == []
        assert (await session.execute(select(OIDCPolicyConfig))).scalars().all() == []
