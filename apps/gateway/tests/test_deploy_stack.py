"""The stack files hold the invariants the design rests on (ADR 0086)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from gateway.deploy import doctor, envfile, stackfiles
from gateway.deploy.cli import main

STACK = stackfiles.stack_dir()


def _compose() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((STACK / "compose.yaml").read_text())


def test_compose_yaml_builds_nothing() -> None:
    for name, service in _compose()["services"].items():
        assert "build" not in service, f"{name}: builds belong in compose.build.yaml"
        assert "image" in service, name


def test_build_file_adds_build_stanzas_and_nothing_else() -> None:
    yaml = pytest.importorskip("yaml")
    for fname in ("compose.build.yaml", "compose.build-cerea.yaml"):
        services = yaml.safe_load((STACK / fname).read_text())["services"]
        base = _compose()["services"]
        for name, service in services.items():
            assert name in base, f"{fname}: {name} is not a service in compose.yaml"
            assert set(service) == {"build"}, f"{fname}: {name} may only add build:"


def test_cross_profile_dependencies_are_optional() -> None:
    services = _compose()["services"]
    for name, service in services.items():
        mine = set(service.get("profiles", []))
        for dep, spec in (service.get("depends_on") or {}).items():
            theirs = set(services[dep].get("profiles", []))
            if theirs and not theirs <= mine:
                assert spec.get("required") is False, f"{name} → {dep} must be required: false"


def test_no_single_file_bind_mounts() -> None:
    # The inode-staleness class: a bind-mounted single file pins the old inode.
    for name, service in _compose()["services"].items():
        for volume in service.get("volumes", []):
            source = re.sub(r"\$\{[^}]*\}", "X", str(volume)).split(":")[0]
            if "/" in source:
                assert source.endswith(".d") or not Path(source).suffix, f"{name}: {volume}"


def test_nothing_mentions_the_house_idp_or_a_ca_bundle() -> None:
    text = (STACK / "compose.yaml").read_text()
    for gone in ("GATEWAY_IDP__", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "ca-bundle", "CHAT_REPO"):
        assert gone not in re.sub(r"(?m)^\s*#.*$", "", text), gone


def test_every_compose_variable_is_written_by_init_or_defaulted(tmp_path: Path) -> None:
    text = re.sub(r"(?m)^\s*#.*$", "", (STACK / "compose.yaml").read_text())
    required = set(re.findall(r"\$\{([A-Z0-9_]+):\?", text))
    code = main(
        [
            "init",
            "--dir",
            str(tmp_path),
            "--origin",
            "https://llm.example.org",
            "--admin-email",
            "ops@example.org",
            "--preset",
            "team",
        ]
    )
    assert code == 0
    written = envfile.read(tmp_path / ".env")
    missing = sorted(required - set(written))
    assert not missing, f"compose requires but init never writes: {missing}"


def test_release_manifest_pins_both_products() -> None:
    release = stackfiles.release()
    for key in ("PYSTINO_VERSION", "CEREA_VERSION", "PYSTINO_REGISTRY"):
        assert release.get(key), key


def test_init_then_doctor_is_clean(tmp_path: Path, capsys) -> None:
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
    assert "First sign-in:  admin / " in out
    assert (tmp_path / "compose.yaml").is_file()
    assert (tmp_path / "proxy.d").is_dir()
    assert (tmp_path / "pystino").stat().st_mode & 0o111
    report = doctor.check(tmp_path, probe_docker=False)
    assert report.ok, report.errors
    # A second init refuses to clobber the deployment.
    assert (
        main(
            [
                "init",
                "--dir",
                str(tmp_path),
                "--origin",
                "https://x.example.org",
                "--admin-email",
                "a@example.org",
            ]
        )
        == 1
    )


def test_doctor_names_what_is_missing(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("COMPOSE_PROFILES=gateway,chat\nGATEWAY_IDP__ENABLED=true\n")
    report = doctor.check(tmp_path, probe_docker=False)
    assert not report.ok
    joined = "\n".join(report.errors + report.warnings)
    for fragment in ("POSTGRES_PASSWORD", "GATEWAY_SECRET_KEY", "CHAT_PG_PASSWORD", "house IdP"):
        assert fragment in joined


def test_caddyfile_has_one_site_and_the_component_hook() -> None:
    text = (STACK / "proxy" / "Caddyfile").read_text()
    assert text.count("{$SITE_ADDRESS}") == 1
    assert "import /etc/caddy/extra/*.caddy" in text
    assert "cerea.pviviani.eu" not in text


def test_authelia_config_holds_no_secret_and_every_client() -> None:
    text = (STACK / "authelia" / "configuration.yml").read_text()
    body = re.sub(r"(?m)^\s*#.*$", "", text)
    assert "BEGIN RSA" not in body and "$argon2" not in body and "$plaintext$" not in body
    for client in ("pystino-console", "cerea", "opencode-enrollment"):
        assert f"client_id: '{client}'" in body
    # Every template variable it reads is one compose.yaml passes in.
    compose = (STACK / "compose.yaml").read_text()
    for var in set(re.findall(r'env "(X_PYSTINO_[A-Z_]+)"', body)):
        assert f"{var}:" in compose, var
