"""The API surface for scoped redaction: admin rules, a person's own, preview.

The fold is what makes scoping *safe* (test_redaction_scoping.py owns that), so
what is left for these routes is everything the fold cannot do, and every one of
them is about a rule that would look right and do nothing:

* a rule for a subject that does not exist matches no request, and on the screen
  is indistinguishable from one that matches every request;
* a second rule for the same subject is two policies that disagree, resolved by
  whichever row a query happened to return;
* a personal policy that weakens is *inert*, not dangerous — which is worse to
  discover than a refusal, because "accepted and silently ignored" reads as the
  feature not working. The refusal has to name the entity type or it is no better
  than the silence.

The preview is here too, and two of its properties are load-bearing rather than
cosmetic: a blocked sample is a 200, because a preview that fails looks broken at
the moment it is working; and the sample never reaches a log, because within a
week of shipping it will contain the real prompt that came back wrong.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
import pytest
from conftest import Seeded
from gateway.config import (
    EntityMode,
    EntityPolicy,
    RedactionPolicy,
    RedactionSettings,
    Settings,
)
from gateway.models import RedactionRule, RedactionScope, User
from gateway.redaction.resolver import RedactionResolver
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin
from test_query_counts import counted, summarise
from test_redaction_http import FakeDetector


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The conftest settings, plus a placeholder key.

    Overridden for this module alone because the preview derives placeholders
    with the deployment's HMAC key — the same key ``build_for`` hands every
    engine it constructs — and deriving one from an empty key is refused by the
    placeholder scheme. A deployment running an engine that anonymises always has
    one; only the noop-engined test fixture does not.
    """
    return settings.model_copy(
        update={"redaction": RedactionSettings(engine="noop", placeholder_key=SecretStr("k"))}
    )


def policy(**kwargs: Any) -> dict[str, Any]:
    return RedactionPolicy(**kwargs).model_dump(mode="json")


def install_detector(app: Any, spans_for: dict[str, str]) -> None:
    """Put a detecting engine in force, keeping the app's real resolver."""
    app.state.redactor = FakeDetector(spans_for).redactor()


async def catch_all(
    app: Any, session_factory: async_sessionmaker[AsyncSession], **entities: EntityPolicy
) -> RedactionRule:
    """The rule that decides what every request gets, written directly.

    Since ADR 0039 a deployment filters nothing until this exists, so a test
    about anything downstream of it has to create it — and creating it through
    the API would make every such test a test of the create route as well.
    """
    rule = RedactionRule(
        scope=RedactionScope.ALL,
        scope_id=None,
        policy=policy(entities=entities or {"PERSON": EntityPolicy()}),
    )
    async with session_factory() as session:
        session.add(rule)
        await session.commit()
        await session.refresh(rule)
    await app.state.redaction.refresh()
    return rule


async def admin_client(
    app: Any, session_factory: async_sessionmaker[AsyncSession], seeded: Seeded
) -> None:
    as_user(app, await make_admin(session_factory, seeded))


# --------------------------------------------------------------------------
# admin CRUD
# --------------------------------------------------------------------------


class TestRuleCrud:
    async def test_a_rule_is_created_and_listed_with_its_subjects_name(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await admin_client(app, session_factory, seeded)

        created = await client.post(
            "/api/admin/redaction/rules",
            json={
                "name": "research handles patient data",
                "scope": "group",
                "scope_id": str(seeded.group.id),
                "policy": policy(entities={"PERSON": EntityPolicy(mode=EntityMode.BLOCK)}),
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["scope"] == "group"
        assert body["subject_label"] == "research"
        assert body["policy"]["entities"]["PERSON"]["mode"] == "block"
        assert body["created_by_email"] == seeded.user.email

        listed = await client.get("/api/admin/redaction/rules")
        assert listed.status_code == 200
        page = listed.json()
        assert page["total"] == 1
        assert page["items"][0]["subject_label"] == "research"

    async def test_a_second_rule_for_the_same_subject_is_a_conflict(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Two rules for one subject are two policies that disagree."""
        await admin_client(app, session_factory, seeded)
        payload = {"scope": "group", "scope_id": str(seeded.group.id), "policy": policy()}

        assert (await client.post("/api/admin/redaction/rules", json=payload)).status_code == 201
        again = await client.post("/api/admin/redaction/rules", json=payload)

        assert again.status_code == 409, again.text
        message = again.json()["error"]["message"]
        assert "already exists" in message
        # Named, so the operator knows which row to go and edit.
        assert "research" in message

    async def test_a_rule_for_a_subject_that_does_not_exist_is_refused(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        """The failure is "no such group", not a rule that matches nothing."""
        await admin_client(app, session_factory, seeded)

        response = await client.post(
            "/api/admin/redaction/rules",
            json={"scope": "group", "scope_id": str(uuid.uuid4()), "policy": policy()},
        )

        # 404, like POST /api/admin/limits answering the same question.
        assert response.status_code == 404, response.text
        assert "No group with id" in response.json()["error"]["message"]
        assert (await session.execute(select(RedactionRule))).scalars().all() == []

    @pytest.mark.parametrize("scope", ["provider", "model", "group", "user", "api_key"])
    async def test_every_scope_is_labelled(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        scope: str,
    ) -> None:
        """A ``scope_id`` is a UUID in one of five tables; the console cannot
        turn it into a word without help, and would not have permission to try."""
        await admin_client(app, session_factory, seeded)
        subjects = {
            "provider": (seeded.provider.id, "fake"),
            "model": (seeded.model.id, "test-model"),
            "group": (seeded.group.id, "research"),
            "user": (seeded.user.id, "member@example.org"),
            "api_key": (seeded.api_key.id, seeded.api_key.prefix),
        }
        subject_id, label = subjects[scope]

        created = await client.post(
            "/api/admin/redaction/rules",
            json={"scope": scope, "scope_id": str(subject_id), "policy": policy()},
        )

        assert created.status_code == 201, created.text
        assert created.json()["subject_label"] == label

    async def test_a_subject_that_has_gone_away_labels_as_null(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """``scope_id`` is not a foreign key, so nothing else catches this: a
        rule pointing at a deleted group is inert and looks exactly like one that
        is working."""
        await admin_client(app, session_factory, seeded)
        session.add(
            RedactionRule(scope=RedactionScope.GROUP, scope_id=uuid.uuid4(), policy=policy())
        )
        await session.commit()

        listed = await client.get("/api/admin/redaction/rules")

        assert listed.json()["items"][0]["subject_label"] is None

    async def test_labels_cost_one_query_per_scope_kind_not_one_per_row(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The shape of thing that is invisible until an organisation is large,
        which is the case pagination exists for."""
        await admin_client(app, session_factory, seeded)
        for _ in range(20):
            session.add(
                RedactionRule(scope=RedactionScope.USER, scope_id=uuid.uuid4(), policy=policy())
            )
        session.add(
            RedactionRule(
                scope=RedactionScope.GROUP, scope_id=seeded.group.id, policy=policy()
            )
        )
        await session.commit()

        engine = app.state.engine
        with counted(engine) as statements:
            listed = await client.get("/api/admin/redaction/rules")

        assert listed.json()["total"] == 21
        # Authentication, the count, the page, and one lookup for each of the two
        # scope kinds present. Twenty-one rows do not add twenty-one queries.
        assert len(statements) <= 8, summarise(statements)

    async def test_patch_changes_the_policy_and_delete_removes_the_rule(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await admin_client(app, session_factory, seeded)
        created = await client.post(
            "/api/admin/redaction/rules",
            json={"scope": "model", "scope_id": str(seeded.model.id), "policy": policy()},
        )
        rule_id = created.json()["id"]

        patched = await client.patch(
            f"/api/admin/redaction/rules/{rule_id}",
            json={
                "policy": policy(entities={"URL": EntityPolicy(mode=EntityMode.REDACT)}),
                "is_active": False,
            },
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["policy"]["entities"]["URL"]["mode"] == "redact"
        assert patched.json()["is_active"] is False
        assert patched.json()["subject_label"] == "test-model"

        deleted = await client.delete(f"/api/admin/redaction/rules/{rule_id}")
        assert deleted.status_code == 204
        assert (await session.execute(select(RedactionRule))).scalars().all() == []
        assert (await client.delete(f"/api/admin/redaction/rules/{rule_id}")).status_code == 404

    async def test_a_write_reaches_this_workers_resolver_immediately(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An operator who saves a rule and then previews it must not see the
        answer from before the save and conclude it did not work."""
        await admin_client(app, session_factory, seeded)
        resolver: RedactionResolver = app.state.redaction

        await client.post(
            "/api/admin/redaction/rules",
            json={
                "scope": "user",
                "scope_id": str(seeded.user.id),
                "policy": policy(entities={"URL": EntityPolicy(mode=EntityMode.REDACT)}),
            },
        )

        in_force = resolver.policy_for(user_id=seeded.user.id).policy
        assert in_force.mode_for("URL") is EntityMode.REDACT


class TestAccessControl:
    async def test_a_non_admin_cannot_reach_the_rules(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded, admin=False))
        rule_id = uuid.uuid4()

        assert (await client.get("/api/admin/redaction/rules")).status_code == 403
        assert (
            await client.post(
                "/api/admin/redaction/rules",
                json={"scope": "user", "scope_id": str(seeded.user.id), "policy": policy()},
            )
        ).status_code == 403
        assert (
            await client.patch(f"/api/admin/redaction/rules/{rule_id}", json={"name": "x"})
        ).status_code == 403
        assert (await client.delete(f"/api/admin/redaction/rules/{rule_id}")).status_code == 403
        assert (
            await client.post("/api/admin/redaction/preview", json={"text": "hello"})
        ).status_code == 403

    async def test_no_session_is_401(self, client: httpx.AsyncClient, seeded: Seeded) -> None:
        assert (await client.get("/api/admin/redaction/rules")).status_code == 401
        assert (await client.get("/api/me/redaction")).status_code == 401


# --------------------------------------------------------------------------
# a person's own policy
# --------------------------------------------------------------------------


class TestMyPolicy:
    async def test_it_reports_the_floor_and_what_applies(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded, admin=False))

        empty = (await client.get("/api/me/redaction")).json()
        # No rule of their own, and none anywhere: a deployment filters nothing
        # until somebody writes a rule (ADR 0039).
        assert empty["policy"] is None
        assert empty["effective"]["entities"] == {}
        assert empty["effective"]["default_mode"] == "off"

        await catch_all(app, session_factory, PERSON=EntityPolicy(mode=EntityMode.REDACT))

        body = (await client.get("/api/me/redaction")).json()
        # The catch-all is the floor, and it is visibly the *reason* for the
        # floor rather than an unattributed default.
        assert body["policy"] is None
        assert body["baseline"]["entities"]["PERSON"]["mode"] == "redact"
        assert body["effective"]["entities"]["PERSON"]["mode"] == "redact"

    async def test_a_rule_an_admin_wrote_about_them_cannot_be_relaxed(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The one rule a person could otherwise overrule, which is backwards.

        There is one row per subject, so an administrator's `scope=user` rule and
        that person's own policy are the *same row* — "their own" cannot be told
        by scope, only by who wrote it. Leaving an imposed rule out of the floor
        would let them PUT a weaker policy and relax it to their group's level.
        """
        person = await make_admin(session_factory, seeded, admin=False)
        async with session_factory() as setup:
            # A *different* person wrote it. `make_admin` promotes the seeded
            # user in place, so using it here would make author and subject the
            # same id — and the rule would correctly count as their own.
            author = User(
                issuer="https://idp.test", subject="the-admin", email="admin@example.org"
            )
            setup.add(author)
            await setup.flush()
            setup.add(
                RedactionRule(
                    scope=RedactionScope.USER,
                    scope_id=person.id,
                    policy=policy(entities={"PERSON": EntityPolicy(mode=EntityMode.BLOCK)}),
                    created_by=author.id,
                )
            )
            await setup.commit()
        resolver = app.state.redaction
        await resolver.refresh()

        as_user(app, person)
        # `redact`, not `off`: it is *stronger* than the deployment default, so
        # the baseline check alone would accept it. Only the imposed `block`
        # makes it a weakening, which is what this test is for — asserting on
        # `off` would pass even with the imposed rule ignored, for the wrong
        # reason.
        response = await client.put(
            "/api/me/redaction",
            json={"policy": policy(entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT)})},
        )

        assert response.status_code == 400, response.text
        assert "PERSON" in response.json()["error"]["message"]
        assert "block" in response.json()["error"]["message"]

    async def test_a_policy_that_tightens_is_accepted(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """URL is off deployment-wide; asking for it back is strictly more."""
        as_user(app, await make_admin(session_factory, seeded, admin=False))

        response = await client.put(
            "/api/me/redaction",
            json={
                "policy": policy(entities={"URL": EntityPolicy(mode=EntityMode.REDACT)}),
                "reason": "I paste customer links",
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["effective"]["entities"]["URL"]["mode"] == "redact"

        rule = (await session.execute(select(RedactionRule))).scalars().one()
        assert rule.scope is RedactionScope.USER
        assert rule.scope_id == seeded.user.id
        assert rule.created_by == seeded.user.id
        # And this worker sees it without waiting for the poll.
        resolver: RedactionResolver = app.state.redaction
        in_force = resolver.policy_for(user_id=seeded.user.id).policy
        assert in_force.mode_for("URL") is EntityMode.REDACT

    async def test_a_policy_that_weakens_is_refused_and_names_the_type(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A weakening rule would be inert anyway. It is refused so that "it did
        nothing" is not what the person who wrote it discovers instead."""
        as_user(app, await make_admin(session_factory, seeded, admin=False))
        rule = await catch_all(app, session_factory, PERSON=EntityPolicy())

        response = await client.put(
            "/api/me/redaction",
            json={"policy": policy(entities={"PERSON": EntityPolicy(mode=EntityMode.OFF)})},
        )

        assert response.status_code == 400, response.text
        error = response.json()["error"]
        assert error["code"] == "redaction_policy_weakens"
        assert "PERSON" in error["message"]
        assert "off" in error["message"] and "anonymise_restore" in error["message"]
        # Only the catch-all, so nothing of theirs was written.
        written = (await session.execute(select(RedactionRule))).scalars().all()
        assert [row.id for row in written] == [rule.id]

    async def test_a_raised_threshold_is_a_weakening_too(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Fewer spans caught, with the mode unchanged: the failure dressed as a
        tightening."""
        as_user(app, await make_admin(session_factory, seeded, admin=False))

        response = await client.put(
            "/api/me/redaction",
            json={
                "policy": policy(
                    entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT, threshold=0.95)}
                )
            },
        )

        assert response.status_code == 400, response.text
        assert "PERSON" in response.json()["error"]["message"]
        assert "threshold" in response.json()["error"]["message"]

    async def test_a_group_rule_is_part_of_the_floor(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The invariant is measured against what the administrators set for
        this person's groups, not against the deployment policy alone."""
        session.add(
            RedactionRule(
                scope=RedactionScope.GROUP,
                scope_id=seeded.group.id,
                policy=policy(entities={"PERSON": EntityPolicy(mode=EntityMode.BLOCK)}),
            )
        )
        await session.commit()
        resolver: RedactionResolver = app.state.redaction
        await resolver.refresh()
        as_user(app, await make_admin(session_factory, seeded, admin=False))

        floor = (await client.get("/api/me/redaction")).json()["baseline"]
        assert floor["entities"]["PERSON"]["mode"] == "block"

        response = await client.put(
            "/api/me/redaction",
            json={"policy": policy(entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT)})},
        )

        assert response.status_code == 400, response.text
        assert "PERSON" in response.json()["error"]["message"]

    async def test_an_allow_list_is_refused(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The one field that can only weaken, and neither way of combining it
        across scopes is safe. It stays an administrator's decision."""
        as_user(app, await make_admin(session_factory, seeded, admin=False))

        response = await client.put(
            "/api/me/redaction",
            json={
                "policy": policy(
                    entities={"URL": EntityPolicy(mode=EntityMode.REDACT)},
                    allow_list=["acme.example"],
                )
            },
        )

        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "redaction_allow_list_forbidden"
        assert (await session.execute(select(RedactionRule))).scalars().all() == []

    async def test_a_person_can_only_write_their_own_rule(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """There is no id in the body to forge: the subject comes from the
        session. The only route that can name another person is the admin one,
        and it is closed to them."""
        stranger = uuid.uuid4()
        session.add(
            RedactionRule(
                scope=RedactionScope.USER,
                scope_id=stranger,
                policy=policy(entities={"URL": EntityPolicy(mode=EntityMode.BLOCK)}),
            )
        )
        await session.commit()
        as_user(app, await make_admin(session_factory, seeded, admin=False))

        saved = await client.put(
            "/api/me/redaction",
            json={"policy": policy(entities={"URL": EntityPolicy(mode=EntityMode.REDACT)})},
        )
        assert saved.status_code == 200, saved.text

        rules = {
            rule.scope_id: rule
            for rule in (await session.execute(select(RedactionRule))).scalars().all()
        }
        assert set(rules) == {stranger, seeded.user.id}
        # Untouched: still the stronger mode somebody else was given.
        assert rules[stranger].policy["entities"]["URL"]["mode"] == "block"

        # And the route that could name them refuses this caller outright.
        refused = await client.post(
            "/api/admin/redaction/rules",
            json={"scope": "user", "scope_id": str(stranger), "policy": policy()},
        )
        assert refused.status_code == 403

    async def test_saving_twice_edits_one_rule(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """One rule per subject is a unique index; a second PUT must update it
        rather than trip the constraint."""
        as_user(app, await make_admin(session_factory, seeded, admin=False))
        body = {"policy": policy(entities={"URL": EntityPolicy(mode=EntityMode.REDACT)})}

        assert (await client.put("/api/me/redaction", json=body)).status_code == 200
        second = await client.put(
            "/api/me/redaction",
            json={"policy": policy(entities={"URL": EntityPolicy(mode=EntityMode.BLOCK)})},
        )

        assert second.status_code == 200, second.text
        rule = (await session.execute(select(RedactionRule))).scalars().one()
        assert rule.policy["entities"]["URL"]["mode"] == "block"


# --------------------------------------------------------------------------
# the preview box
# --------------------------------------------------------------------------


class TestPreview:
    async def test_it_shows_the_rewrite_the_model_would_receive(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Not a proxy to /detect: the spans alone say nothing about what the
        provider would see, which is the only question being asked."""
        await admin_client(app, session_factory, seeded)
        install_detector(app, {"Mario Rossi": "PERSON"})
        # The preview runs the policy in force, and nothing is in force until a
        # rule says so — which is the point of previewing at all.
        await catch_all(app, session_factory, PERSON=EntityPolicy())

        response = await client.post(
            "/api/admin/redaction/preview",
            json={"text": "please email Mario Rossi about it"},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["blocked"] is False
        assert body["entity_count"] == 1
        assert "Mario Rossi" not in body["redacted_text"]
        assert "<PERSON_" in body["redacted_text"]
        span = body["spans"][0]
        assert span["entity_type"] == "PERSON"
        assert (span["start"], span["end"]) == (13, 24)
        assert span["score"] == pytest.approx(0.9)
        assert span["mode"] == "anonymise_restore"

    async def test_a_span_the_policy_ignores_is_shown_with_its_mode(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The Italian bug in one screen: the detector finds it, the policy says
        leave it, and until this route existed nothing anywhere showed either."""
        await admin_client(app, session_factory, seeded)
        install_detector(app, {"ilpost.it": "URL"})

        response = await client.post(
            "/api/admin/redaction/preview",
            json={"text": "riassumi le notizie da ilpost.it"},
        )

        body = response.json()
        assert body["entity_count"] == 0
        assert body["redacted_text"] == "riassumi le notizie da ilpost.it"
        assert body["spans"][0] == {
            "entity_type": "URL",
            "start": 23,
            "end": 32,
            "score": pytest.approx(0.9),
            "mode": "off",
            "threshold": pytest.approx(0.5),
            "allow_listed": False,
        }

    async def test_a_blocked_preview_is_a_200_that_says_so(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A preview that refuses looks broken at the moment it is working."""
        await admin_client(app, session_factory, seeded)
        install_detector(app, {"123-45-6789": "US_SSN"})
        created = await client.post(
            "/api/admin/redaction/rules",
            json={
                "scope": "group",
                "scope_id": str(seeded.group.id),
                "policy": policy(entities={"US_SSN": EntityPolicy(mode=EntityMode.BLOCK)}),
            },
        )
        assert created.status_code == 201, created.text

        response = await client.post(
            "/api/admin/redaction/preview",
            json={
                "text": "my ssn is 123-45-6789",
                "scope": "group",
                "scope_id": str(seeded.group.id),
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["blocked"] is True
        assert "US_SSN" in body["blocked_reason"]
        # Never the matched value: a refusal is logged and pasted into tickets.
        assert "123-45-6789" not in body["blocked_reason"]
        # Nothing was rewritten, so there is no text to claim would be sent.
        assert body["redacted_text"] is None
        assert body["scope"] == "group"
        assert body["rule_id"] == created.json()["id"]

    async def test_the_sample_never_reaches_a_log(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Within a week of shipping this box holds the prompt that came back
        wrong — which is real personal data, pasted in by an admin who is
        debugging. A redaction inspector that logs it has defeated itself."""
        await admin_client(app, session_factory, seeded)
        install_detector(app, {"Mario Rossi": "PERSON"})

        with caplog.at_level(logging.DEBUG):
            response = await client.post(
                "/api/admin/redaction/preview",
                json={"text": "Mario Rossi lives at 12 Via Roma"},
            )

        assert response.status_code == 200
        assert "Mario Rossi" not in caplog.text
        assert "Via Roma" not in caplog.text

    async def test_previewing_as_a_subject_that_does_not_exist_is_refused(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Otherwise it silently previews the deployment policy, and reads as a
        rule that is not taking effect."""
        await admin_client(app, session_factory, seeded)

        response = await client.post(
            "/api/admin/redaction/preview",
            json={"text": "hello", "scope": "user", "scope_id": str(uuid.uuid4())},
        )

        assert response.status_code == 404, response.text
        assert "No user with id" in response.json()["error"]["message"]

    async def test_an_engine_that_detects_nothing_says_so(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An empty result under noop must not read as "nothing here to redact"."""
        await admin_client(app, session_factory, seeded)

        response = await client.post(
            "/api/admin/redaction/preview", json={"text": "Mario Rossi"}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["engine"] == "noop"
        assert body["spans"] == []
        assert body["redacted_text"] == "Mario Rossi"
        assert "detects nothing" in body["note"]
