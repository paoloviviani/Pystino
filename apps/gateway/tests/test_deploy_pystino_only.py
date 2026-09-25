"""The Pystino-only deployment in deploy/ holds its own invariants (ADR 0091).

It is not part of the `gateway` package — deliberately: it is meant to be
read on its own, the way an operator would, with no import of gateway code.
These tests load it by path instead of adding it to sys.path permanently.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "deploy"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _release_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (DEPLOY / "release.env").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def test_compose_pins_the_same_pystino_version_as_release_env() -> None:
    # So a release bump only has to change release.env: this test is the
    # tripwire that says compose.yaml's own default fell behind it.
    compose = (DEPLOY / "compose.yaml").read_text()
    match = re.search(r"\$\{PYSTINO_VERSION:-([^}]*)\}", compose)
    assert match, "deploy/compose.yaml has no PYSTINO_VERSION default to compare"
    release_version = _release_env()["PYSTINO_VERSION"]
    assert match.group(1) == release_version, (
        f"deploy/compose.yaml's PYSTINO_VERSION default ({match.group(1)!r}) does not "
        f"match deploy/release.env's ({release_version!r}); a release bump must change both"
    )


def test_config_rev_labels_match_their_directories() -> None:
    pin = _load_module("deploy_pin", DEPLOY / "pin.py")
    compose = (DEPLOY / "compose.yaml").read_text()
    for service, directory in pin.CONFIG_DIRS.items():
        want = pin.config_rev(DEPLOY / directory)
        match = pin.label_pattern(service).search(compose)
        assert match, f"no pystino.config-rev label on service {service!r}"
        assert match.group(2) == want, (
            f"{service}'s pystino.config-rev label ({match.group(2)}) is stale: "
            f"run deploy/pin.py to set it to {want}"
        )


def test_pin_check_fails_and_names_the_fix(tmp_path: Path, capsys) -> None:
    # A copy of deploy/ with the proxy label deliberately gone stale, so
    # `deploy/pin.py --check` — what CI runs — has something to catch.
    work = tmp_path / "deploy"
    shutil.copytree(DEPLOY, work)
    text = (work / "compose.yaml").read_text()
    stale, count = re.subn(
        r'(  proxy:\n(?:(?!^  \S).*\n)*?\s+pystino\.config-rev: ")[^"]*(")',
        r"\g<1>sha256:0000000000000000\g<2>",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    assert count == 1, "could not find the proxy service's config-rev label to corrupt"
    (work / "compose.yaml").write_text(stale)

    pin = _load_module("deploy_pin_stale", work / "pin.py")
    assert pin.main(["--check"]) == 1
    out, err = capsys.readouterr()
    assert "stale: proxy: config-rev sha256:0000000000000000 ->" in out
    assert "run deploy/pin.py" in err


def test_pin_show_and_refresh_round_trip(tmp_path: Path, capsys) -> None:
    work = tmp_path / "deploy"
    shutil.copytree(DEPLOY, work)
    pin = _load_module("deploy_pin_roundtrip", work / "pin.py")
    assert pin.main([]) == 0
    assert "up to date" in capsys.readouterr().out
    assert pin.main(["--show"]) == 0
    out = capsys.readouterr().out
    assert "proxy.config-rev=sha256:" in out and "authelia.config-rev=sha256:" in out


def test_deploy_has_no_chat() -> None:
    compose = (DEPLOY / "compose.yaml").read_text()
    for gone in ("chat-mongo", "CEREA_IMAGE", "hf-chat", "PROXY_DEFAULT"):
        assert gone not in compose, gone
    caddyfile = (DEPLOY / "caddy" / "Caddyfile").read_text()
    assert "handle /chat" not in caddyfile
    assert "reverse_proxy chat:" not in caddyfile
    authelia = (DEPLOY / "authelia" / "configuration.yml").read_text()
    assert "client_id: 'cerea'" not in authelia
    assert "client_id: 'pystino-console'" in authelia
    assert "client_id: 'opencode-enrollment'" in authelia


def test_authelia_config_comments_hold_no_template_expression() -> None:
    # Authelia's template filter executes the whole file, comments included.
    text = (DEPLOY / "authelia" / "configuration.yml").read_text()
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            assert "{{" not in line, f"line {number}: template syntax in a comment runs"


@pytest.mark.parametrize("path", ["caddy/modes/gateway.caddy", ".gitignore", "proxy.d/README.md"])
def test_expected_files_exist(path: str) -> None:
    assert (DEPLOY / path).is_file(), path
