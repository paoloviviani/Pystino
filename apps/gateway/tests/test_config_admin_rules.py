"""The env admin-rule settings and startup checks (ADR 0093 §1, §1.1).

`_production_requires_an_admin_rule` and `_production_requires_accepted_clients`
are gated to `environment == "production"`, the same way `_production_requires_secrets`
already is: `OIDCSettings()`'s own defaults (`kind="generic"`, no rule, no
audience) are what hundreds of unrelated unit tests construct, and gating to
production is what keeps this change from breaking every one of them.
"""

from __future__ import annotations

import pytest
from gateway.config import AccessSettings, OIDCSettings, Settings, SmtpSettings, startup_warnings
from pydantic import ValidationError

_PRODUCTION_SECRETS = {"session_secret": "s", "secret_key": "k"}


def _production_settings(**oidc_kwargs: object) -> Settings:
    return Settings(
        environment="production",
        oidc=OIDCSettings(**oidc_kwargs),  # type: ignore[arg-type]
        **_PRODUCTION_SECRETS,
    )


class TestAdminRuleRequired:
    def test_external_idp_with_no_rule_is_refused_in_production(self) -> None:
        with pytest.raises(ValidationError, match="needs an admin rule"):
            _production_settings(kind="generic")

    def test_bundled_authelia_needs_no_rule(self) -> None:
        _production_settings(kind="authelia")

    def test_an_admin_email_satisfies_it(self) -> None:
        _production_settings(kind="generic", admin_emails="ops@example.org")

    def test_a_full_claim_rule_satisfies_it(self) -> None:
        _production_settings(kind="generic", admin_claim="groups", admin_claim_values="admin")

    def test_dev_and_test_environments_are_unaffected(self) -> None:
        # The bare default (kind="generic", no rule) is what most of this
        # suite's fixtures construct; this is the test that would fail loudly
        # if the gate above were ever loosened to run outside production.
        Settings(environment="dev", oidc=OIDCSettings(kind="generic"))


class TestAdminClaimPair:
    def test_half_a_pair_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="set together"):
            Settings(oidc=OIDCSettings(admin_claim="groups"))
        with pytest.raises(ValidationError, match="set together"):
            Settings(oidc=OIDCSettings(admin_claim_values="admin"))

    def test_both_or_neither_is_fine(self) -> None:
        Settings(oidc=OIDCSettings())
        Settings(oidc=OIDCSettings(admin_claim="groups", admin_claim_values="admin"))


class TestAcceptedClients:
    def test_an_audience_with_no_accepted_clients_is_refused_in_production(self) -> None:
        with pytest.raises(ValidationError, match="ACCEPTED_CLIENTS"):
            _production_settings(kind="authelia", access_token_audience="pystino-api")

    def test_accepted_clients_satisfies_it(self) -> None:
        _production_settings(
            kind="authelia",
            access_token_audience="pystino-api",
            accepted_clients="pystino-console,cerea,opencode-enrollment",
        )

    def test_no_audience_needs_no_accepted_clients(self) -> None:
        _production_settings(kind="authelia", access_token_audience="")


class TestMachineClientAcceptance:
    def test_a_named_machine_client_missing_from_accepted_clients_is_refused(self) -> None:
        # ADR 0096: the code_agents gate matches azp/client_id against the
        # machine client, but a token from an unaccepted client is refused
        # before the gate ever runs — the mismatch fails silently as a
        # chat-only person's working machine, so it fails loudly here, naming
        # both values.
        with pytest.raises(ValidationError, match="could ever apply") as caught:
            Settings(
                oidc=OIDCSettings(
                    machine_client_id="gitlab-uid-123",
                    accepted_clients="pystino-console,cerea",
                )
            )
        message = str(caught.value)
        assert "gitlab-uid-123" in message
        assert "pystino-console, cerea" in message

    def test_a_named_machine_client_in_accepted_clients_is_fine(self) -> None:
        Settings(
            oidc=OIDCSettings(
                machine_client_id="gitlab-uid-123",
                accepted_clients="pystino-console,cerea,gitlab-uid-123",
            )
        )

    def test_the_default_machine_client_needs_no_acceptance(self) -> None:
        # The default stands for "no machine client configured": a default
        # deployment (accepted clients without it) starts as before.
        Settings(oidc=OIDCSettings(accepted_clients="pystino-console"))

    def test_an_empty_allowlist_is_still_the_escape_hatch(self) -> None:
        Settings(oidc=OIDCSettings(machine_client_id="gitlab-uid-123", accepted_clients=""))

    def test_an_emptied_machine_client_is_not_a_mismatch(self) -> None:
        Settings(oidc=OIDCSettings(machine_client_id="", accepted_clients="pystino-console"))


class TestGroupsBaselineMachineClient:
    def test_groups_baseline_with_unaccepted_machine_client_is_refused(self) -> None:
        # ADR 0096: the env baseline itself says "groups" while the machine
        # client — defaulted here, as in a hand-written .env with only
        # ACCEPTED_CLIENTS set — is not among a non-empty allowlist. The
        # restriction is in use and could never fire.
        with pytest.raises(ValidationError, match="could never fire") as caught:
            Settings(
                access=AccessSettings(code_agents="groups"),
                oidc=OIDCSettings(accepted_clients="pystino-console,cerea"),
            )
        message = str(caught.value)
        assert "GATEWAY_ACCESS__CODE_AGENTS" in message
        assert "opencode-enrollment" in message
        assert "pystino-console, cerea" in message

    def test_groups_baseline_with_accepted_machine_client_is_fine(self) -> None:
        Settings(
            access=AccessSettings(code_agents="groups"),
            oidc=OIDCSettings(machine_client_id="m", accepted_clients="pystino-console,m"),
        )

    def test_groups_baseline_with_empty_allowlist_is_fine(self) -> None:
        Settings(access=AccessSettings(code_agents="groups"), oidc=OIDCSettings())

    def test_everyone_baseline_needs_no_machine_client(self) -> None:
        Settings(oidc=OIDCSettings(accepted_clients="pystino-console"))


class TestCsvFields:
    def test_admin_email_list_strips_and_drops_empties(self) -> None:
        settings = OIDCSettings(admin_emails=" a@example.org, b@example.org ,, ")
        assert settings.admin_email_list() == ["a@example.org", "b@example.org"]

    def test_accepted_client_list(self) -> None:
        settings = OIDCSettings(accepted_clients="one, two")
        assert settings.accepted_client_list() == ["one", "two"]

    def test_empty_string_is_an_empty_list(self) -> None:
        assert OIDCSettings().admin_email_list() == []


class TestRemovedVariables:
    def test_link_local_by_email_true_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="LINK_BY_EMAIL"):
            OIDCSettings(link_local_by_email=True)

    def test_link_local_by_email_false_is_ignored(self) -> None:
        OIDCSettings(link_local_by_email=False)

    def test_the_old_smtp_nesting_is_refused(self) -> None:
        from gateway.config import LocalAuthSettings

        with pytest.raises(ValidationError, match="GATEWAY_SMTP__"):
            LocalAuthSettings(password_reset={"smtp_host": "old.example.org"})

    def test_the_old_smtp_nesting_left_untouched_is_fine(self) -> None:
        from gateway.config import LocalAuthSettings

        LocalAuthSettings()


class TestStartupWarnings:
    def test_link_by_email_warns_every_time(self) -> None:
        settings = Settings(oidc=OIDCSettings(link_by_email=True))
        assert any("LINK_BY_EMAIL is on" in w for w in startup_warnings(settings))

    def test_no_warning_when_off(self) -> None:
        settings = Settings(oidc=OIDCSettings(link_by_email=False))
        assert not any("LINK_BY_EMAIL" in w for w in startup_warnings(settings))

    def test_bootstrap_email_with_an_external_idp_warns(self) -> None:
        settings = Settings(
            bootstrap_admin_email="ops@example.org", oidc=OIDCSettings(kind="generic")
        )
        assert any("ignored" in w for w in startup_warnings(settings))

    def test_bootstrap_email_with_authelia_does_not_warn(self) -> None:
        settings = Settings(
            bootstrap_admin_email="ops@example.org", oidc=OIDCSettings(kind="authelia")
        )
        assert not any("ignored" in w for w in startup_warnings(settings))

    def test_an_audience_with_no_chat_client_id_warns(self) -> None:
        """Announce refuses every token silently otherwise (ADR 0093 §4.1)."""
        settings = Settings(oidc=OIDCSettings(access_token_audience="pystino-api"))
        assert any("CHAT_CLIENT_ID" in w for w in startup_warnings(settings))

    def test_no_warning_once_the_chat_client_id_is_set(self) -> None:
        settings = Settings(
            oidc=OIDCSettings(access_token_audience="pystino-api", chat_client_id="cerea")
        )
        assert not any("CHAT_CLIENT_ID" in w for w in startup_warnings(settings))

    def test_no_warning_with_no_audience_at_all(self) -> None:
        """No audience means /v1 takes no OIDC tokens, announce included."""
        settings = Settings(oidc=OIDCSettings())
        assert not any("CHAT_CLIENT_ID" in w for w in startup_warnings(settings))

    def test_default_machine_client_missing_from_accepted_clients_warns(self) -> None:
        # ADR 0096: a hand-written .env with the IdP's client in
        # ACCEPTED_CLIENTS and no MACHINE_CLIENT_ID keeps the default, which
        # matches nothing — the code_agents restriction would not apply to
        # machine tokens until the setting names the right client.
        settings = Settings(oidc=OIDCSettings(accepted_clients="pystino-console,gitlab-uid"))
        matching = [w for w in startup_warnings(settings) if "MACHINE_CLIENT_ID" in w]
        assert len(matching) == 1
        assert "opencode-enrollment" in matching[0]
        assert "pystino-console, gitlab-uid" in matching[0]
        assert "would not apply" in matching[0]

    def test_no_warning_when_the_default_machine_client_is_accepted(self) -> None:
        settings = Settings(
            oidc=OIDCSettings(accepted_clients="pystino-console,opencode-enrollment")
        )
        assert not any("MACHINE_CLIENT_ID" in w for w in startup_warnings(settings))

    def test_no_warning_with_no_accepted_clients(self) -> None:
        assert not any("MACHINE_CLIENT_ID" in w for w in startup_warnings(Settings()))


class TestSmtpSettings:
    def test_default_security_is_starttls(self) -> None:
        assert SmtpSettings().security == "starttls"

    def test_an_unknown_security_value_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            SmtpSettings(security="ssl")  # type: ignore[arg-type]
