"""The redaction layer, as reported to the console.

Read-only, and the reason it exists: the console could not previously answer
"is redaction on, which engine, and is the service answering" at all. See
docs/redaction-scoping-plan.md.

What is pinned here is mostly the *warnings*, because they are the difference
between a configuration dump and a screen that tells an operator something. A
redaction layer's failure mode is detecting nothing, which looks exactly like
finding nothing to detect — so every way it can be silently useless needs to say
so out loud.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import Seeded
from gateway.config import RedactionSettings
from gateway.routers.admin import _redaction_warnings, _sanitised_endpoint
from gateway.schemas import RedactionServiceHealth
from pydantic import SecretStr
from test_admin import as_user, make_admin


def settings(**overrides: object) -> RedactionSettings:
    base: dict[str, object] = {
        "engine": "http",
        "endpoint": "http://redaction:8080",
        "placeholder_key": SecretStr("k"),
        "language": "en",
    }
    base.update(overrides)
    return RedactionSettings(**base)  # type: ignore[arg-type]


def healthy(**overrides: object) -> RedactionServiceHealth:
    base: dict[str, object] = {
        "reachable": True,
        "engine": "presidio",
        "engine_version": "2.2.364",
        "languages": ["en"],
        "models": {"en": "en_core_web_lg"},
        "degraded_languages": [],
        "entities": ["PERSON", "EMAIL_ADDRESS"],
    }
    base.update(overrides)
    return RedactionServiceHealth(**base)  # type: ignore[arg-type]


class TestWarnings:
    def test_a_healthy_deployment_says_nothing(self) -> None:
        assert _redaction_warnings(settings(), "http", healthy()) == []

    def test_noop_is_named_as_not_redacting(self) -> None:
        """The commonest surprise: configured, and doing nothing."""
        notes = _redaction_warnings(settings(engine="noop"), "noop", None)
        assert len(notes) == 1
        assert "not enabled" in notes[0]

    def test_noop_suppresses_the_rest(self) -> None:
        """A warning about `fail_open` on an engine that never fails is noise."""
        notes = _redaction_warnings(settings(engine="noop", fail_open=True), "noop", None)
        assert len(notes) == 1

    def test_fail_open_is_flagged(self) -> None:
        notes = _redaction_warnings(settings(fail_open=True), "http", healthy())
        assert any("fail_open" in note for note in notes)

    def test_an_unreachable_service_says_what_that_means_now(self) -> None:
        """Fail-closed and fail-open are different emergencies."""
        closed = _redaction_warnings(settings(fail_open=False), "http", healthy(reachable=False))
        assert any("being refused" in note for note in closed)

        opened = _redaction_warnings(settings(fail_open=True), "http", healthy(reachable=False))
        assert any("unredacted" in note for note in opened)

    def test_a_language_the_service_does_not_serve(self) -> None:
        """Detection quietly returns nothing, and nothing else reports it."""
        notes = _redaction_warnings(settings(language="it"), "http", healthy())
        assert len(notes) == 1
        assert "'it'" in notes[0] and "does not" in notes[0]
        # And it says what the service *does* offer, so the fix is obvious.
        assert "en" in notes[0]

    def test_an_env_type_the_service_lacks_is_named(self) -> None:
        notes = _redaction_warnings(
            settings(entity_types=["PERSON", "IT_FISCAL_CODE"]),
            "http",
            healthy(),  # detects PERSON, not IT_FISCAL_CODE
        )
        assert any("IT_FISCAL_CODE" in note and "inert" in note for note in notes)
        # PERSON is served; only the missing one is named.
        assert not any("PERSON" in note for note in notes)

    def test_an_admin_rule_type_the_service_lacks_is_named_too(self) -> None:
        """ADR 0037 moved the policy off the env var: a rule set on the console
        must be checked as hard as one set in the environment. The failure this
        guards is the screen blessing a policy the screen itself made inert."""
        notes = _redaction_warnings(
            settings(),
            "http",
            healthy(),  # PERSON and EMAIL_ADDRESS only
            policy_types=["PERSON", "IBAN_CODE"],
        )
        assert any("IBAN_CODE" in note and "inert" in note for note in notes)

    def test_admin_rules_the_service_serves_warn_nothing(self) -> None:
        assert (
            _redaction_warnings(settings(), "http", healthy(), policy_types=["PERSON"]) == []
        )

    def test_a_degraded_language_is_distinguished_from_an_absent_one(self) -> None:
        notes = _redaction_warnings(
            settings(language="it"),
            "http",
            healthy(languages=["en", "it"], degraded_languages=["it"]),
        )
        assert any("without a named-entity model" in note for note in notes)

    def test_entity_types_the_service_cannot_detect(self) -> None:
        """Asked for and inert, which is the worst of both."""
        notes = _redaction_warnings(
            settings(entity_types=["PERSON", "IBAN_CODE"]), "http", healthy()
        )
        assert any("IBAN_CODE" in note and "inert" in note for note in notes)

    def test_no_entity_filter_is_not_a_warning(self) -> None:
        """Null means everything the engine offers, not nothing."""
        assert _redaction_warnings(settings(entity_types=None), "http", healthy()) == []

    def test_placeholders_left_in_the_response(self) -> None:
        notes = _redaction_warnings(settings(restore_in_response=False), "http", healthy())
        assert any("placeholders" in note for note in notes)


class TestEndpointSanitising:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("http://redaction:8080", "http://redaction:8080"),
            # The case this exists for: a URL that grew basic auth.
            ("http://user:secret@redaction:8080", "http://redaction:8080"),
            ("", None),
        ],
    )
    def test_a_credential_in_the_url_is_removed(self, given: str, expected: str | None) -> None:
        assert _sanitised_endpoint(given) == expected


class TestTheRoute:
    async def test_it_reports_the_engine_actually_running(
        self, app: object, client: httpx.AsyncClient, seeded: Seeded, session_factory: object
    ) -> None:
        """From the constructed redactor, not the setting.

        If the two ever disagreed, reporting the setting would describe a
        deployment that does not exist.
        """
        as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
        body = (await client.get("/api/admin/redaction")).json()
        assert body["engine"] == app.state.redactor.name  # type: ignore[attr-defined]
        assert body["enabled"] is (body["engine"] != "noop")

    async def test_the_placeholder_key_is_never_returned(
        self, app: object, client: httpx.AsyncClient, seeded: Seeded, session_factory: object
    ) -> None:
        """It is the HMAC key every placeholder derives from."""
        as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
        response = await client.get("/api/admin/redaction")
        body = response.json()

        # Only the boolean, never the key. Checking the substring would pass
        # trivially against `placeholder_key_set`, so this checks the field is
        # absent and that the configured secret does not appear anywhere.
        assert "placeholder_key" not in body
        assert isinstance(body["placeholder_key_set"], bool)
        secret = app.state.settings.redaction.placeholder_key.get_secret_value()  # type: ignore[attr-defined]
        if secret:
            assert secret not in response.text

    async def test_it_reports_what_redaction_has_done(
        self, app: object, client: httpx.AsyncClient, seeded: Seeded, session_factory: object
    ) -> None:
        """Configuration is not evidence; the entity count is."""
        as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
        activity = (await client.get("/api/admin/redaction")).json()["activity"]
        assert activity["window_seconds"] == 86_400
        assert activity["requests_redacted"] <= activity["requests"]

    async def test_a_silly_window_is_refused(
        self, app: object, client: httpx.AsyncClient, seeded: Seeded, session_factory: object
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
        assert (await client.get("/api/admin/redaction?window_seconds=1")).status_code == 400

    async def test_a_non_admin_cannot_read_it(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        """It names the detection endpoint and the entity types in force."""
        assert (await client.get("/api/admin/redaction")).status_code in (401, 403)
