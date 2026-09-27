"""``pystino idp check`` (ADR 0093 §11) against a fake issuer.

Written and reasoned through carefully while the deploy hold was on (no
`pytest` run yet — see the commit message this landed in): the fake issuer
mirrors the shape `test_oidc_backchannel.py` already pins for discovery and
back-channel calls, extended with a JWKS endpoint (so a real access token
round-trips through real signature verification, the same as production) and
a device-authorization endpoint (RFC 8628), neither of which existed before
this stage.
"""

from __future__ import annotations

import time

import httpx
import pytest
from gateway.config import OIDCSettings, Settings
from gateway.deploy.idp_check import run_check
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

ISSUER = "https://idp.test"
AUDIENCE = "llm-gateway"


@pytest.fixture(scope="module")
def signing_key() -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": "check-key-1"})


def _discovery(issuer: str, *, device: bool = False) -> dict[str, object]:
    doc: dict[str, object] = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/auth",
        "token_endpoint": f"{issuer}/token",
        "jwks_uri": f"{issuer}/jwks",
        "userinfo_endpoint": f"{issuer}/userinfo",
    }
    if device:
        doc["device_authorization_endpoint"] = f"{issuer}/device"
    return doc


def _make_token(key: RSAKey, **overrides: object) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "sub": "subject-1",
        "aud": [AUDIENCE],
        "azp": "chat",
        "exp": now + 300,
        "iat": now,
        "email": "person@example.org",
        "email_verified": True,
        "groups": ["ops"],
    }
    claims.update(overrides)
    return jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key)


class FakeIdp:
    """Answers discovery, JWKS, userinfo and — when built with ``device`` —
    an RFC 8628 device flow that grants on its second poll, so tests do not
    depend on a slow real one succeeding first.
    """

    def __init__(self, key: RSAKey, *, device: bool = False, wrong_issuer: bool = False) -> None:
        self.key = key
        self.device = device
        self.wrong_issuer = wrong_issuer
        self.requests: list[httpx.Request] = []
        self._polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            issuer = f"{ISSUER}-wrong" if self.wrong_issuer else ISSUER
            return httpx.Response(200, json=_discovery(issuer, device=self.device))
        if path.endswith("/jwks"):
            return httpx.Response(200, json=KeySet([self.key]).as_dict())
        if path.endswith("/userinfo"):
            return httpx.Response(200, json={"sub": "subject-1", "groups": ["ops"]})
        if path.endswith("/device"):
            return httpx.Response(
                200,
                json={
                    "device_code": "devcode-1",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": f"{ISSUER}/device/verify",
                    "expires_in": 600,
                    "interval": 0,
                },
            )
        if path.endswith("/token"):
            self._polls += 1
            if self._polls < 2:
                return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(
                200, json={"access_token": _make_token(self.key), "token_type": "Bearer"}
            )
        return httpx.Response(404)


def _settings(**overrides: object) -> Settings:
    oidc = OIDCSettings(
        enabled=True,
        issuer=ISSUER,
        client_id="gateway",
        client_secret="s",
        access_token_audience=AUDIENCE,
        **overrides,  # type: ignore[arg-type]
    )
    return Settings(oidc=oidc)  # type: ignore[arg-type]


class TestDiscovery:
    async def test_reports_issuer_endpoints_and_key_count(self, signing_key: RSAKey) -> None:
        idp = FakeIdp(signing_key, device=True)
        settings = _settings()
        report = await run_check(
            settings,
            discovery_only=True,
            http=httpx.AsyncClient(transport=httpx.MockTransport(idp)),
        )
        assert report.ok
        joined = "\n".join(report.lines)
        assert "matches OIDC_ISSUER" in joined
        assert f"{ISSUER}/device" in joined
        assert "1 key(s)" in joined

    async def test_a_mismatched_issuer_fails_the_check(self, signing_key: RSAKey) -> None:
        idp = FakeIdp(signing_key, wrong_issuer=True)
        settings = _settings()
        report = await run_check(
            settings,
            discovery_only=True,
            http=httpx.AsyncClient(transport=httpx.MockTransport(idp)),
        )
        assert not report.ok
        assert any("does NOT match" in line for line in report.lines)

    async def test_discovery_failure_stops_the_check(self, signing_key: RSAKey) -> None:
        def unreachable(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        settings = _settings()
        report = await run_check(
            settings,
            discovery_only=True,
            http=httpx.AsyncClient(transport=httpx.MockTransport(unreachable)),
        )
        assert not report.ok
        assert any(line.startswith("discovery: FAILED") for line in report.lines)


class TestToken:
    async def test_a_valid_token_reports_sub_email_and_groups(self, signing_key: RSAKey) -> None:
        idp = FakeIdp(signing_key)
        settings = _settings()
        token = _make_token(signing_key)
        report = await run_check(
            settings, token=token, http=httpx.AsyncClient(transport=httpx.MockTransport(idp))
        )
        joined = "\n".join(report.lines)
        assert "sub='subject-1'" in joined
        assert "person@example.org" in joined
        assert "'ops'" in joined

    async def test_wrong_audience_fails_and_still_reports_what_it_saw(
        self, signing_key: RSAKey
    ) -> None:
        idp = FakeIdp(signing_key)
        settings = _settings()
        token = _make_token(signing_key, aud=["someone-else"])
        report = await run_check(
            settings, token=token, http=httpx.AsyncClient(transport=httpx.MockTransport(idp))
        )
        assert not report.ok
        assert any("FAILED" in line and "audience" in line for line in report.lines)

    async def test_email_verified_as_a_string_is_reported_not_accepted(
        self, signing_key: RSAKey
    ) -> None:
        idp = FakeIdp(signing_key)
        settings = _settings(admin_emails="person@example.org")
        token = _make_token(signing_key, email_verified="true")
        report = await run_check(
            settings, token=token, http=httpx.AsyncClient(transport=httpx.MockTransport(idp))
        )
        assert not report.ok
        assert any("not the JSON boolean true" in line for line in report.lines)

    async def test_an_email_rule_that_matches_reports_yes(self, signing_key: RSAKey) -> None:
        idp = FakeIdp(signing_key)
        settings = _settings(admin_emails="person@example.org")
        token = _make_token(signing_key)
        report = await run_check(
            settings, token=token, http=httpx.AsyncClient(transport=httpx.MockTransport(idp))
        )
        assert report.ok
        assert any(line.strip().startswith("admin (email rule): yes") for line in report.lines)

    async def test_a_claim_rule_that_matches_reports_yes(self, signing_key: RSAKey) -> None:
        idp = FakeIdp(signing_key)
        settings = _settings(admin_claim="groups", admin_claim_values="ops")
        token = _make_token(signing_key)
        report = await run_check(
            settings, token=token, http=httpx.AsyncClient(transport=httpx.MockTransport(idp))
        )
        assert report.ok
        assert any(line.strip().startswith("admin (claim rule): yes") for line in report.lines)

    async def test_no_matching_rule_fails_with_a_hint(self, signing_key: RSAKey) -> None:
        idp = FakeIdp(signing_key)
        settings = _settings(admin_emails="nobody@example.org")
        token = _make_token(signing_key)
        report = await run_check(
            settings, token=token, http=httpx.AsyncClient(transport=httpx.MockTransport(idp))
        )
        assert not report.ok
        assert any("no admin rule would grant" in line for line in report.lines)

    async def test_link_by_email_reports_the_candidate_count(self, signing_key: RSAKey) -> None:
        idp = FakeIdp(signing_key)
        settings = _settings(link_by_email=True)
        token = _make_token(signing_key)

        async def count_candidates(email: str) -> int:
            assert email == "person@example.org"
            return 2

        report = await run_check(
            settings,
            token=token,
            http=httpx.AsyncClient(transport=httpx.MockTransport(idp)),
            count_link_candidates=count_candidates,
        )
        assert any("would link to 2 existing candidate(s)" in line for line in report.lines)

    async def test_link_by_email_without_a_database_says_so(self, signing_key: RSAKey) -> None:
        idp = FakeIdp(signing_key)
        settings = _settings(link_by_email=True)
        token = _make_token(signing_key)
        report = await run_check(
            settings, token=token, http=httpx.AsyncClient(transport=httpx.MockTransport(idp))
        )
        assert any("no database was reached" in line for line in report.lines)


class TestDeviceFlow:
    async def test_polls_until_granted_then_reports_the_token(self, signing_key: RSAKey) -> None:
        idp = FakeIdp(signing_key, device=True)
        # An admin rule is configured so this test's assertion is about the
        # device flow reaching a token at all, not about the separate "no
        # admin rule configured" failure `check_token` also reports.
        settings = _settings(admin_emails="person@example.org")
        printed: list[str] = []
        report = await run_check(
            settings,
            device=True,
            client_id="machine",
            http=httpx.AsyncClient(transport=httpx.MockTransport(idp)),
            echo=printed.append,
        )
        assert report.ok
        assert any("Open" in line for line in printed)
        assert idp._polls >= 2

    async def test_no_client_id_fails_before_starting(self, signing_key: RSAKey) -> None:
        idp = FakeIdp(signing_key, device=True)
        settings = _settings()
        report = await run_check(
            settings, device=True, http=httpx.AsyncClient(transport=httpx.MockTransport(idp))
        )
        assert not report.ok
        assert any("needs a client id" in line for line in report.lines)

    async def test_no_device_endpoint_published_fails_clearly(self, signing_key: RSAKey) -> None:
        idp = FakeIdp(signing_key, device=False)
        settings = _settings()
        report = await run_check(
            settings,
            device=True,
            client_id="machine",
            http=httpx.AsyncClient(transport=httpx.MockTransport(idp)),
        )
        assert not report.ok
        assert any(
            "does not publish a device_authorization_endpoint" in line for line in report.lines
        )


async def test_discovery_only_stops_before_any_token_work(signing_key: RSAKey) -> None:
    idp = FakeIdp(signing_key)
    settings = _settings()
    report = await run_check(
        settings, discovery_only=True, http=httpx.AsyncClient(transport=httpx.MockTransport(idp))
    )
    assert report.ok
    assert not any(r.url.path.endswith("/userinfo") for r in idp.requests)


async def test_no_mode_given_is_discovery_only_by_default(signing_key: RSAKey) -> None:
    idp = FakeIdp(signing_key)
    settings = _settings()
    report = await run_check(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(idp)))
    assert report.ok
    assert any("discovery only" in line for line in report.lines)
