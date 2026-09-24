"""Cerea without a local gateway: the satellite and generic presets, `cerea init`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gateway.deploy import envfile, registry
from gateway.deploy.cli import cerea_main, main
from gateway.deploy.init import InitError, InitOptions, build_env

RELEASE = {
    "PYSTINO_REGISTRY": "ghcr.io/example",
    "PYSTINO_VERSION": "1.4.0",
    "CEREA_REGISTRY": "ghcr.io/example",
    "CEREA_VERSION": "1.3.2",
}


def _env(**kw) -> dict[str, str]:
    options = InitOptions(
        origin=kw.pop("origin", "https://chat.site-b.example.org"),
        admin_email="ops@example.org",
        deploy_dir=Path("/srv/cerea"),
        **kw,
    )
    result = build_env(options, RELEASE)
    return {k: v for _, items in result.sections for k, v in items}


def test_satellite_uses_central_v1_and_its_idp_with_user_tokens() -> None:
    env = _env(
        preset="satellite",
        central_url="https://llm.central.example.org",
        oidc_chat_client_secret="chat-secret",
    )
    assert env["COMPOSE_PROFILES"] == "chat"
    assert env["CHAT_OPENAI_BASE_URL"] == "https://llm.central.example.org/v1"
    assert env["CHAT_USE_USER_TOKEN"] == "true"
    assert env["CHAT_OPENAI_API_KEY"] == ""
    assert env["OIDC_ISSUER"] == "https://llm.central.example.org/authelia"
    assert env["PROXY_DEFAULT"] == "chat"
    assert "AUTHELIA_STORAGE_KEY" not in env  # one directory: central's


def test_satellite_refuses_a_stored_key_and_a_missing_central() -> None:
    with pytest.raises(InitError, match="central-url"):
        _env(preset="satellite", oidc_chat_client_secret="x")
    with pytest.raises(InitError, match="stores no API key"):
        _env(
            preset="satellite",
            central_url="https://c.example.org",
            upstream_api_key="k",
            oidc_chat_client_secret="x",
        )


def test_generic_forces_shared_key_mode_and_can_bundle_authelia() -> None:
    env = _env(
        preset="generic", upstream_base_url="https://api.openai.com/v1", upstream_api_key="sk-x"
    )
    assert env["COMPOSE_PROFILES"] == "chat,authelia"
    assert env["CHAT_USE_USER_TOKEN"] == "false"
    assert env["CHAT_OPENAI_API_KEY"] == "sk-x"
    # The key is the chat's; the (absent) gateway does not keep a copy.
    assert env["GATEWAY_UPSTREAM__API_KEY"] == ""
    assert env["CHAT_USAGE_ENABLED"] == ""
    with pytest.raises(InitError, match="API key"):
        _env(preset="generic")


def test_cerea_init_picks_the_preset_and_brands_the_shim(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PYSTINO_UPSTREAM_API_KEY", "sk-x")
    assert (
        cerea_main(
            [
                "init",
                "--dir",
                str(tmp_path),
                "--origin",
                "https://chat.example.org",
                "--admin-email",
                "ops@example.org",
            ]
        )
        == 0
    )
    env = envfile.read(tmp_path / ".env")
    assert env["PYSTINO_PRESET"] == "generic"
    shim = (tmp_path / "cerea").read_text()
    assert 'PYSTINO_CLI="${PYSTINO_CLI:-cerea}"' in shim
    assert not (tmp_path / "pystino").exists()

    other = tmp_path / "sat"
    other.mkdir()
    monkeypatch.delenv("PYSTINO_UPSTREAM_API_KEY")
    monkeypatch.setenv("PYSTINO_OIDC_CHAT_CLIENT_SECRET", "chat-secret")
    assert (
        cerea_main(
            [
                "init",
                "--dir",
                str(other),
                "--origin",
                "https://chat.example.org",
                "--admin-email",
                "ops@example.org",
                "--central-url",
                "https://llm.example.org",
            ]
        )
        == 0
    )
    assert envfile.read(other / ".env")["PYSTINO_PRESET"] == "satellite"


def test_cerea_offers_only_the_gateway_less_presets(capsys) -> None:
    with pytest.raises(SystemExit):
        cerea_main(["init", "--preset", "team"])
    assert "invalid choice" in capsys.readouterr().err


def test_standalone_install_passes_doctor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PYSTINO_UPSTREAM_API_KEY", "sk-x")
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "nodocker"))
    assert (
        main(
            [
                "init",
                "--dir",
                str(tmp_path),
                "--origin",
                "https://chat.example.org",
                "--admin-email",
                "ops@example.org",
                "--preset",
                "generic",
            ]
        )
        == 0
    )
    from gateway.deploy import doctor

    report = doctor.check(tmp_path, probe_docker=False)
    assert report.ok, report.errors


@pytest.mark.parametrize(
    ("config", "state"),
    [
        ({"auths": {"ghcr.io": {"auth": "x"}}}, "ok"),
        ({"credHelpers": {"ghcr.io": "gh"}}, "ok"),
        ({"credsStore": "desktop"}, "unknown"),
        ({"auths": {"docker.io": {}}}, "missing"),
    ],
)
def test_registry_login_state(tmp_path: Path, config: dict, state: str) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    assert registry.login_state("ghcr.io/paoloviviani", path) == state
    assert registry.login_state("docker.io/library", path) == "n/a"


def test_dist_init_says_how_to_log_in_to_a_private_registry(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "empty"))
    assert (
        main(
            [
                "init",
                "--dir",
                str(tmp_path),
                "--origin",
                "https://llm.example.org",
                "--admin-email",
                "ops@example.org",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "docker login ghcr.io" in out and "read:packages" in out
