"""OIDC claim mapping, provisioning and management sessions.

The full browser redirect flow needs a real identity provider and is not covered
here — see the "not tested" note in the session report. What *is* covered is
everything that runs on our side of the redirect, which is where the configurable
behaviour and the security decisions live.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from gateway.config import OIDCSettings
from gateway.models import Group, GroupSource, Membership, User
from gateway.oidc import (
    OIDCError,
    extract_groups,
    generate_pkce_pair,
    issue_session_token,
    normalise_groups,
    provision_user,
    resolve_claim,
    split_claim_path,
    verify_session_token,
)
from gateway.routers.auth import _safe_next
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

SECRET = "a-test-session-secret"


class TestClaimPaths:
    def test_plain_key(self) -> None:
        assert split_claim_path("groups") == ["groups"]

    def test_nested_path(self) -> None:
        assert split_claim_path("realm_access.roles") == ["realm_access", "roles"]

    def test_escaped_dot_is_literal(self) -> None:
        """Namespaced claim names contain dots that are not nesting."""
        assert split_claim_path(r"https://example\.org/groups") == ["https://example.org/groups"]

    def test_flat_key_wins_over_nesting(self) -> None:
        """A claim literally named 'a.b' must be found before walking into 'a'."""
        claims = {"a.b": ["flat"], "a": {"b": ["nested"]}}
        assert resolve_claim(claims, "a.b") == ["flat"]

    def test_nested_lookup(self) -> None:
        claims = {"realm_access": {"roles": ["admin", "research"]}}
        assert resolve_claim(claims, "realm_access.roles") == ["admin", "research"]

    def test_missing_path_is_none(self) -> None:
        assert resolve_claim({"a": {"b": 1}}, "a.c") is None
        assert resolve_claim({"a": "not-an-object"}, "a.b") is None
        assert resolve_claim({}, "groups") is None


class TestNormaliseGroups:
    def test_list_of_strings(self) -> None:
        assert normalise_groups(["a", "b"]) == ["a", "b"]

    def test_bare_string_is_one_group_not_several(self) -> None:
        """Splitting would be guesswork, and group names contain spaces."""
        assert normalise_groups("Research Group A") == ["Research Group A"]

    def test_list_of_objects_uses_a_recognisable_label(self) -> None:
        assert normalise_groups([{"name": "alpha"}, {"displayName": "beta"}]) == [
            "alpha",
            "beta",
        ]

    def test_empty_and_junk_values(self) -> None:
        assert normalise_groups(None) == []
        assert normalise_groups("") == []
        assert normalise_groups("   ") == []
        assert normalise_groups(42) == []
        assert normalise_groups([{"unrecognised": "x"}]) == []
        assert normalise_groups(["", "  ", "keep"]) == ["keep"]


class TestExtractGroups:
    def test_uses_the_configured_claim(self) -> None:
        settings = OIDCSettings(groups_claim="realm_access.roles")
        claims = {"realm_access": {"roles": ["research"]}, "groups": ["ignored"]}
        assert extract_groups(claims, settings) == ["research"]

    def test_allowlist_filters(self) -> None:
        settings = OIDCSettings(groups_claim="groups", group_allowlist=["kept"])
        assert extract_groups({"groups": ["kept", "dropped"]}, settings) == ["kept"]

    def test_empty_allowlist_imports_everything(self) -> None:
        settings = OIDCSettings(groups_claim="groups")
        assert extract_groups({"groups": ["a", "b"]}, settings) == ["a", "b"]

    def test_duplicates_are_removed_in_order(self) -> None:
        settings = OIDCSettings(groups_claim="groups")
        assert extract_groups({"groups": ["a", "b", "a"]}, settings) == ["a", "b"]


class TestPkce:
    def test_verifier_and_challenge_differ_and_are_url_safe(self) -> None:
        verifier, challenge = generate_pkce_pair()
        assert verifier != challenge
        assert "=" not in challenge
        assert "+" not in challenge and "/" not in challenge
        assert len(verifier) >= 43

    def test_each_pair_is_fresh(self) -> None:
        assert generate_pkce_pair()[0] != generate_pkce_pair()[0]


class TestProvisioning:
    async def test_first_login_creates_user_groups_and_membership(
        self, session: AsyncSession
    ) -> None:
        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="new-subject",
            email="new@example.org",
            display_name="New Person",
            group_names=["research", "admin"],
            settings=OIDCSettings(),
        )
        await session.commit()

        assert user.email == "new@example.org"
        assert user.last_login_at is not None
        groups = {membership.group.name for membership in user.memberships}
        assert groups == {"research", "admin"}
        created = (await session.execute(select(Group))).scalars().all()
        assert all(group.source is GroupSource.OIDC for group in created)

    async def test_second_login_updates_the_profile(self, session: AsyncSession) -> None:
        for email, name in [("old@example.org", "Old"), ("new@example.org", "New")]:
            user = await provision_user(
                session,
                issuer="https://idp.test",
                subject="same-subject",
                email=email,
                display_name=name,
                group_names=["research"],
                settings=OIDCSettings(),
            )
            await session.commit()

        assert user.email == "new@example.org"
        assert user.display_name == "New"
        assert len((await session.execute(select(User))).scalars().all()) == 1

    async def test_second_login_leaves_an_admin_edited_profile_alone(
        self, session: AsyncSession
    ) -> None:
        """The console's correction survives the sign-in that used to revert it.

        ``provision_user`` used to refresh all three profile fields from the
        claims on every login, so an administrator's fix — a misspelt name, an
        address the person no longer reads — silently reverted the next time
        the person signed in. The edit is now recorded on the row (what the
        admin user-update route writes) and a listed field is skipped.

        The row is edited the way the route does it — fields set, the field
        names appended — because the contract under test is between login and
        *whatever the console wrote*, not between login and a fixture spelling.
        """
        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="edited-subject",
            email="old@example.org",
            display_name="Old Name",
            username="old@local",
            group_names=["research"],
            settings=OIDCSettings(),
        )
        await session.commit()
        user.email = "corrected@example.org"
        user.display_name = "Corrected Name"
        user.username = "corrected@local"
        user.admin_edited_fields = ["email", "display_name", "username"]
        await session.commit()

        again = await provision_user(
            session,
            issuer="https://idp.test",
            subject="edited-subject",
            email="directory@example.org",
            display_name="Directory Name",
            username="directory@local",
            group_names=["research"],
            settings=OIDCSettings(),
        )
        await session.commit()

        assert again.email == "corrected@example.org"
        assert again.display_name == "Corrected Name"
        assert again.username == "corrected@local"
        # The account is still one row, still in the group: the override is
        # about the profile, not about the rest of the sync.
        groups = {membership.group.name for membership in again.memberships}
        assert groups == {"research"}

    async def test_only_the_edited_field_is_frozen(
        self, session: AsyncSession
    ) -> None:
        """A field the console never touched keeps following the directory.

        Freezing all three on any edit would be the heavy half of "the console
        is authoritative": a directory rename of a display name nobody here
        corrected would then never arrive, and the console would show a stale
        name with nothing anywhere saying why.
        """
        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="half-edited",
            email="old@example.org",
            display_name="Old Name",
            group_names=["research"],
            settings=OIDCSettings(),
        )
        await session.commit()
        user.email = "corrected@example.org"
        user.admin_edited_fields = ["email"]
        await session.commit()

        again = await provision_user(
            session,
            issuer="https://idp.test",
            subject="half-edited",
            email="directory@example.org",
            display_name="Directory Name",
            group_names=["research"],
            settings=OIDCSettings(),
        )
        await session.commit()

        # The edited field holds; the untouched one moved.
        assert again.email == "corrected@example.org"
        assert again.display_name == "Directory Name"

    async def test_membership_is_replaced_so_revocation_takes_effect(
        self, session: AsyncSession
    ) -> None:
        """The IdP is authoritative: a group removed there must disappear here."""
        await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["research", "finance"],
            settings=OIDCSettings(),
        )
        await session.commit()

        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["research"],
            settings=OIDCSettings(),
        )
        await session.commit()

        assert {membership.group.name for membership in user.memberships} == {"research"}
        remaining = (await session.execute(select(Membership))).scalars().all()
        assert len(remaining) == 1

    async def test_auto_create_can_be_turned_off(self, session: AsyncSession) -> None:
        """Then membership is purely an administrative decision."""
        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["not-created"],
            settings=OIDCSettings(auto_create_groups=False),
        )
        await session.commit()
        assert user.memberships == []
        assert (await session.execute(select(Group))).scalars().all() == []

    async def test_existing_group_is_reused_not_duplicated(self, session: AsyncSession) -> None:
        session.add(Group(name="preexisting", source=GroupSource.MANUAL))
        await session.commit()

        await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["preexisting"],
            settings=OIDCSettings(),
        )
        await session.commit()

        groups = (await session.execute(select(Group))).scalars().all()
        assert len(groups) == 1
        assert groups[0].source is GroupSource.MANUAL

    async def test_inactive_groups_are_not_joined(self, session: AsyncSession) -> None:
        session.add(Group(name="retired", is_active=False))
        await session.commit()

        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["retired"],
            settings=OIDCSettings(),
        )
        await session.commit()
        assert user.memberships == []

    async def test_single_group_becomes_the_default_billing_group(
        self, session: AsyncSession
    ) -> None:
        """A first login should be immediately usable."""
        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["only-group"],
            settings=OIDCSettings(),
        )
        await session.commit()
        assert user.default_billing_group_id is not None

    async def test_several_groups_leave_the_choice_to_the_user(self, session: AsyncSession) -> None:
        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["a", "b"],
            settings=OIDCSettings(),
        )
        await session.commit()
        assert user.default_billing_group_id is None

    async def test_default_is_cleared_when_access_is_lost(self, session: AsyncSession) -> None:
        """Otherwise every later request fails on a group they cannot bill."""
        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["going-away"],
            settings=OIDCSettings(),
        )
        await session.commit()
        assert user.default_billing_group_id is not None

        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["something-else"],
            settings=OIDCSettings(),
        )
        await session.commit()
        # Reassigned to the only remaining group rather than left dangling.
        assert user.default_billing_group_id is not None
        assert {m.group.name for m in user.memberships} == {"something-else"}

    async def test_users_from_different_issuers_are_distinct(self, session: AsyncSession) -> None:
        """Identity is (issuer, subject); the same subject elsewhere is someone else."""
        for issuer in ["https://idp-a.test", "https://idp-b.test"]:
            await provision_user(
                session,
                issuer=issuer,
                subject="shared-subject",
                email=None,
                display_name=None,
                group_names=[],
                settings=OIDCSettings(),
            )
            await session.commit()

        assert len((await session.execute(select(User))).scalars().all()) == 2


class TestSessionTokens:
    def test_round_trip(self) -> None:
        from gateway.types import utcnow

        user_id = uuid.uuid4()
        token = issue_session_token(user_id, secret=SECRET, ttl_seconds=3600)
        claims = verify_session_token(token, secret=SECRET)
        assert claims.user_id == user_id
        assert claims.issued_at <= utcnow()

    def test_wrong_secret_is_rejected(self) -> None:
        token = issue_session_token(uuid.uuid4(), secret=SECRET, ttl_seconds=3600)
        with pytest.raises(OIDCError):
            verify_session_token(token, secret="a-different-secret")

    def test_tampered_token_is_rejected(self) -> None:
        token = issue_session_token(uuid.uuid4(), secret=SECRET, ttl_seconds=3600)
        header, payload, signature = token.split(".")
        with pytest.raises(OIDCError):
            verify_session_token(f"{header}.{payload}.{signature[:-2]}xy", secret=SECRET)

    def test_expired_token_is_rejected(self) -> None:
        token = issue_session_token(
            uuid.uuid4(), secret=SECRET, ttl_seconds=-int(timedelta(hours=1).total_seconds())
        )
        with pytest.raises(OIDCError):
            verify_session_token(token, secret=SECRET)

    def test_garbage_is_rejected(self) -> None:
        with pytest.raises(OIDCError):
            verify_session_token("not-a-jwt", secret=SECRET)

    def test_missing_secret_is_refused_rather_than_defaulted(self) -> None:
        with pytest.raises(OIDCError):
            issue_session_token(uuid.uuid4(), secret="", ttl_seconds=60)
        with pytest.raises(OIDCError):
            verify_session_token("anything", secret="")

    def test_a_login_token_is_not_accepted_as_a_session(self) -> None:
        """Type confusion between our two cookie kinds must not be possible."""
        from gateway.types import utcnow
        from joserfc import jwt
        from joserfc.jwk import OctKey

        now = int(utcnow().timestamp())
        wrong_type = jwt.encode(
            {"alg": "HS256"},
            {"sub": str(uuid.uuid4()), "exp": now + 600, "typ": "gw-login"},
            OctKey.import_key(SECRET),
        )
        with pytest.raises(OIDCError):
            verify_session_token(wrong_type, secret=SECRET)


class TestReturnPath:
    """Where the browser is sent after signing in.

    The callback hands the browser a fresh session cookie and then redirects
    it, which makes this function the difference between a convenience and an
    open redirect. An attacker who can choose the destination sends a victim a
    login link, the victim signs in for real, and lands somewhere hostile with
    every appearance of having arrived from us.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/console",
            "/console/admin/models",
            "/console/admin/users?q=alice&limit=50",
            "/console/admin/quotas#rule-3",
        ],
    )
    def test_a_path_on_this_origin_is_kept(self, path: str) -> None:
        assert _safe_next(path) == path

    @pytest.mark.parametrize(
        "hostile",
        [
            # Protocol-relative: a browser resolves this to another host, the
            # leading slash notwithstanding. The one everybody misses.
            "//evil.test/phish",
            "/\\evil.test/phish",
            "https://evil.test",
            "http://evil.test",
            "javascript:alert(1)",
            "evil.test",
            # Header splitting, on a less careful stack than this one.
            "/console\r\nSet-Cookie: a=b",
            "/console\nLocation: http://evil.test",
        ],
    )
    def test_anything_else_is_refused(self, hostile: str) -> None:
        assert _safe_next(hostile) is None

    def test_absent_is_not_an_error(self) -> None:
        """No `next` is the ordinary case — the caller falls back to a default."""
        assert _safe_next(None) is None
        assert _safe_next("") is None

    def test_refused_rather_than_repaired(self) -> None:
        """A value we had to fix is a value we did not understand.

        Stripping the leading slashes off `//evil.test` yields `evil.test`,
        which is a *relative* path and resolves back to this origin — so the
        sanitising version of this function silently sends the reader to a page
        that does not exist instead of refusing an attack.
        """
        assert _safe_next("//evil.test") is None
