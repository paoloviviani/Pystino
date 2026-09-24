"""`pystino adopt`, against a legacy install made by the old generator itself."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from gateway.deploy import bootstrap, doctor, envfile
from gateway.deploy.cli import main

# The old installer's generator, kept as a fixture: adopt must read exactly
# what it wrote, and the generator itself was deleted with the old deployment.
GENERATOR = Path(__file__).parent / "fixtures" / "legacy" / "generate-authelia-config.sh"
ORIGIN = "https://cerea.example.org"


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture
def legacy(tmp_path: Path) -> Path:
    if not (shutil.which("bash") and shutil.which("openssl") and GENERATOR.is_file()):
        pytest.skip("needs bash, openssl and the legacy generator")
    old = tmp_path / "old-deploy"
    (old / "idp").mkdir(parents=True)
    env = {
        **os.environ,
        "IDP_PUBLIC_ORIGIN": ORIGIN,
        "IDP_SESSION_SECRET": "a" * 64,
        "IDP_HMAC_SECRET": "b" * 64,
        "IDP_STORAGE_KEY": "c" * 64,
        "IDP_CONSOLE_CLIENT_SECRET": "console-plain",
        "IDP_CHAT_CLIENT_SECRET": "chat-plain",
        "IDP_ADMIN_USER": "paolo",
        "IDP_ADMIN_EMAIL": "paolo@example.org",
        "IDP_ADMIN_PASSWORD": "a long password",
        "IDP_OUT_DIR": str(old / "idp"),
    }
    subprocess.run(["bash", str(GENERATOR)], env=env, check=True, capture_output=True)  # noqa: S603, S607
    (old / ".env").write_text(
        "\n".join(
            [
                f"PUBLIC_ORIGIN={ORIGIN}",
                "PUBLIC_HOST=cerea.example.org",
                "TLS_DIRECTIVE=tls internal",
                "POSTGRES_USER=gateway",
                "POSTGRES_PASSWORD=pg-secret",
                "POSTGRES_DB=gateway",
                "GATEWAY_SECRET_KEY=gw-secret-key",
                "GATEWAY_SESSION_SECRET=gw-session",
                "GATEWAY_UPSTREAM__BASE_URL=https://api.example.net/v1",
                "GATEWAY_UPSTREAM__API_KEY=upstream-key",
                "GATEWAY_ACCOUNTING__ENABLED=true",
                "GATEWAY_REDACTION__ENGINE=http",
                "REDACTION_PLACEHOLDER_KEY=old-placeholder-key",
                f"GATEWAY_OIDC__ISSUER={ORIGIN}/authelia",
                "GATEWAY_OIDC__CLIENT_ID=pystino-console",
                "GATEWAY_OIDC__CLIENT_SECRET=console-plain",
                "GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE=pystino-api",
                "GATEWAY_OIDC__LINK_LOCAL_BY_EMAIL=true",
                "CHAT_PG_URL=postgresql://chat:chat-pg-pw@postgres:5432/chat",
                "CHAT_SECRET_KEY=chat-secret",
                "CHAT_OIDC_CLIENT_SECRET=chat-plain",
                "CHAT_REPO=/home/someone/Cerea",
                "IDP_BUNDLED=authelia",
                "IDP_ADMIN_EMAIL=paolo@example.org",
                "CODE_AGENTS_ENABLED=true",
                "",
            ]
        )
    )
    return old


def test_adopt_carries_every_secret_verbatim_and_reads_only(legacy: Path, tmp_path: Path) -> None:
    before = _tree_digest(legacy)
    new = tmp_path / "new"
    new.mkdir()
    assert main(["adopt", str(legacy), "--dir", str(new), "--tls", "upstream"]) == 0
    assert _tree_digest(legacy) == before, "adopt must not touch the old install"

    env = envfile.read(new / ".env")
    # Same project → same volumes → same data.
    assert env["COMPOSE_PROJECT_NAME"] == "llm-platform"
    # The storage key is what keeps every user's opaque subject stable.
    assert env["AUTHELIA_STORAGE_KEY"] == "c" * 64
    assert env["AUTHELIA_SESSION_SECRET"] == "a" * 64
    assert env["AUTHELIA_HMAC_SECRET"] == "b" * 64
    assert env["AUTHELIA_CONSOLE_CLIENT_DIGEST"].startswith("$6$")
    assert env["GATEWAY_SECRET_KEY"] == "gw-secret-key"
    assert env["OIDC_CONSOLE_CLIENT_SECRET"] == "console-plain"
    assert env["OIDC_CHAT_CLIENT_SECRET"] == "chat-plain"
    assert env["CHAT_PG_PASSWORD"] == "chat-pg-pw"
    # The back-channel replaces the hairpin; the edge shape becomes upstream TLS.
    assert env["OIDC_INTERNAL_BASE_URL"] == "http://authelia:9091/authelia"
    assert (env["TLS_MODE"], env["SITE_ADDRESS"], env["HTTP_PORT"]) == (
        "upstream",
        "http://:80",
        "8443",
    )
    assert env["COMPOSE_PROFILES"] == "gateway,chat,redaction,authelia"
    assert env["PYSTINO_BOOTSTRAP_ADMIN_EMAIL"] == "paolo@example.org"
    assert env["CODE_AGENTS_ENABLED"] == "true"
    # Re-minting it would re-label every entity in the old transcripts.
    assert env["REDACTION_PLACEHOLDER_KEY"] == "old-placeholder-key"
    assert not any(key.startswith("GATEWAY_IDP__") for key in env)

    staged = new / "adopt" / "authelia-config"
    assert (staged / "users_database.yml").read_bytes() == (
        legacy / "idp" / "users_database.yml"
    ).read_bytes()
    assert (staged / "keys" / "jwks.pem").read_bytes() == (
        legacy / "idp" / "authelia-jwks-rsa.pem"
    ).read_bytes()
    assert doctor.check(new, probe_docker=False).ok


def test_an_adopted_env_passes_bootstrap_and_imports_once(legacy: Path, tmp_path: Path) -> None:
    new = tmp_path / "new"
    new.mkdir()
    main(["adopt", str(legacy), "--dir", str(new), "--tls", "upstream"])
    env = {**envfile.read(new / ".env"), "COMPOSE_PROFILES": "authelia"}
    # The legacy $6$ admin digest must not fail the stack closed.
    assert bootstrap.BootstrapEnv.from_environ(env).problems() == []

    volume = tmp_path / "volume"
    first = bootstrap.import_authelia_state(new / "adopt" / "authelia-config", volume)
    assert first == ["users_database.yml: imported", "keys/jwks.pem: imported"]
    # bootstrap then finds the state present and keeps it.
    assert bootstrap.run(env, volume) == 0
    assert (volume / "keys" / "jwks.pem").read_bytes() == (
        legacy / "idp" / "authelia-jwks-rsa.pem"
    ).read_bytes()
    again = bootstrap.import_authelia_state(new / "adopt" / "authelia-config", volume)
    assert all("kept" in line for line in again)


def test_adopt_names_what_it_cannot_carry(legacy: Path, tmp_path: Path, capsys) -> None:
    new = tmp_path / "new"
    new.mkdir()
    main(["adopt", str(legacy), "--dir", str(new), "--tls", "upstream"])
    out = capsys.readouterr().out
    assert "CODE_AGENTS_ENABLED is on" in out
    assert "TLS_DIRECTIVE is not carried" in out
    assert "--no-deps" in out  # the import must not start (and so recreate) services


def test_adopt_refuses_bundled_keycloak_and_a_missing_env(tmp_path: Path) -> None:
    old = tmp_path / "old"
    old.mkdir()
    assert main(["adopt", str(old), "--dir", str(tmp_path / "n"), "--tls", "acme"]) == 2
    (old / ".env").write_text(f"PUBLIC_ORIGIN={ORIGIN}\nIDP_BUNDLED=keycloak\n")
    (tmp_path / "n").mkdir()
    assert main(["adopt", str(old), "--dir", str(tmp_path / "n"), "--tls", "acme"]) == 2
