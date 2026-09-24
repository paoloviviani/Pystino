"""`pystino init`'s rules, as unit tests rather than runs on a live box."""

from __future__ import annotations

from pathlib import Path

import pytest
from argon2 import PasswordHasher
from gateway.deploy.init import InitError, InitOptions, build_env

RELEASE = {
    "PYSTINO_REGISTRY": "ghcr.io/example",
    "PYSTINO_VERSION": "1.4.0",
    "CEREA_REGISTRY": "ghcr.io/example",
    "CEREA_VERSION": "1.3.2",
    "POSTGRES_IMAGE": "pgvector/pgvector:pg18",
}


def _counter(prefix: str):
    n = iter(range(10_000))
    return lambda nbytes=32: f"{prefix}{next(n)}"


def _env(**overrides) -> tuple[dict[str, str], str | None]:
    options = InitOptions(
        origin=overrides.pop("origin", "https://llm.example.org"),
        admin_email=overrides.pop("admin_email", "ops@example.org"),
        deploy_dir=Path("/srv/pystino"),
        **overrides,
    )
    result = build_env(
        options, RELEASE, token=_counter("tok"), hexkey=_counter("hex"), hasher=PasswordHasher()
    )
    flat = {k: v for _, items in result.sections for k, v in items}
    return flat, result.admin_password


def test_distributed_mode_pulls_the_pinned_pair() -> None:
    env, _ = _env()
    assert env["COMPOSE_FILE"] == "compose.yaml"
    assert env["PYSTINO_VERSION"] == "1.4.0"
    assert env["CEREA_VERSION"] == "1.3.2"
    assert "PYSTINO_SRC" not in env


def test_development_mode_differs_only_in_image_origin(tmp_path: Path) -> None:
    dist, _ = _env()
    dev, _ = _env(mode="dev", pystino_src=tmp_path, build_revision="abc1234-dirty")
    assert dev["COMPOSE_FILE"] == (
        f"{tmp_path}/deploy/stack/compose.yaml:{tmp_path}/deploy/stack/compose.build.yaml"
    )
    assert dev["PYSTINO_REGISTRY"] == "local" and dev["PYSTINO_VERSION"] == "dev"
    assert dev["BUILD_REVISION"] == "abc1234-dirty"
    # Without a Cerea checkout the chat is pulled at the pinned version.
    assert dev["CEREA_VERSION"] == "1.3.2"
    image_keys = {
        "COMPOSE_FILE",
        "PYSTINO_SRC",
        "PYSTINO_REGISTRY",
        "PYSTINO_VERSION",
        "BUILD_REVISION",
    }
    assert set(dev) - image_keys == set(dist) - image_keys


def test_development_mode_can_build_the_chat_too(tmp_path: Path) -> None:
    env, _ = _env(mode="dev", pystino_src=tmp_path, cerea_src=tmp_path / "cerea")
    assert env["COMPOSE_FILE"].endswith("compose.build-cerea.yaml")
    assert env["CEREA_VERSION"] == "dev"


def test_tls_modes() -> None:
    acme, _ = _env()
    assert (acme["SITE_ADDRESS"], acme["TLS_DIRECTIVE"], acme["HTTPS_PORT"]) == (
        "https://llm.example.org",
        "",
        "443",
    )
    internal, _ = _env(tls="internal", origin="https://dev.example.test:18443")
    assert internal["TLS_DIRECTIVE"] == "tls internal"
    assert internal["HTTPS_PORT"] == "18443"
    upstream, _ = _env(tls="upstream", http_port=8443)
    assert (upstream["SITE_ADDRESS"], upstream["HTTP_PORT"]) == ("http://:80", "8443")


def test_acme_refuses_an_ip() -> None:
    with pytest.raises(InitError, match="public DNS name"):
        _env(origin="https://10.0.0.5")


def test_bundled_authelia_refuses_an_ip_but_external_idp_does_not() -> None:
    with pytest.raises(InitError, match="dotted host name"):
        _env(tls="internal", origin="https://10.0.0.5")
    env, _ = _env(
        tls="internal",
        origin="https://10.0.0.5",
        idp="external",
        oidc_issuer="https://sso.example.org/realms/x",
        oidc_console_client_secret="c",
        oidc_chat_client_secret="d",
    )
    assert env["OIDC_ISSUER"] == "https://sso.example.org/realms/x"
    assert "authelia" not in env["COMPOSE_PROFILES"]


def test_bundled_authelia_uses_the_back_channel_and_digests() -> None:
    env, password = _env()
    assert env["OIDC_ISSUER"] == "https://llm.example.org/authelia"
    assert env["OIDC_INTERNAL_BASE_URL"] == "http://authelia:9091/authelia"
    assert env["COMPOSE_PROFILES"].split(",")[-1] == "authelia"
    hasher = PasswordHasher()
    assert hasher.verify(env["AUTHELIA_CONSOLE_CLIENT_DIGEST"], env["OIDC_CONSOLE_CLIENT_SECRET"])
    assert hasher.verify(env["AUTHELIA_CHAT_CLIENT_DIGEST"], env["OIDC_CHAT_CLIENT_SECRET"])
    # The minted password is returned for printing once, and only its digest is kept.
    assert password and hasher.verify(env["AUTHELIA_ADMIN_PASSWORD_DIGEST"], password)
    assert password not in env.values()
    assert env["PYSTINO_BOOTSTRAP_ADMIN_EMAIL"] == "ops@example.org"


def test_a_given_password_is_not_echoed_back() -> None:
    _, password = _env(admin_password="correct horse battery")
    assert password is None


def test_origin_and_email_are_validated() -> None:
    with pytest.raises(InitError, match="https"):
        _env(origin="http://llm.example.org")
    with pytest.raises(InitError, match="scheme://host"):
        _env(origin="https://llm.example.org/chat")
    with pytest.raises(InitError, match="real address"):
        _env(admin_email="admin@local")


def test_presets_switch_profiles_not_topology() -> None:
    homelab, _ = _env(preset="homelab")
    team, _ = _env(preset="team")
    assert homelab["COMPOSE_PROFILES"] == "gateway,chat,authelia"
    assert team["COMPOSE_PROFILES"] == "gateway,chat,redaction,authelia"
    assert homelab["GATEWAY_ACCOUNTING__ENABLED"] == "false"
    assert team["GATEWAY_REDACTION__ENGINE"] == "http"


def test_no_house_idp_or_local_passwords_are_configured() -> None:
    env, _ = _env()
    assert not any(key.startswith("GATEWAY_IDP__") for key in env)
    assert "GATEWAY_LOCAL_AUTH__ENABLED" not in env  # compose defaults it to false
