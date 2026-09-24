"""`doctor --against-running`: what `.env` says, against what `docker inspect` says."""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path

import pytest
from gateway.deploy import doctor, envfile, running, stackfiles
from gateway.deploy.cli import main

yaml = pytest.importorskip("yaml")


def test_interpolation_follows_compose() -> None:
    env = {"SET": "x", "EMPTY": ""}
    assert running.interpolate("${SET}", env) == "x"
    assert running.interpolate("${UNSET:-d}", env) == "d"
    assert running.interpolate("${EMPTY:-d}", env) == "d"
    assert running.interpolate("${EMPTY-d}", env) == ""  # set, so no default
    assert running.interpolate("${UNSET-d}", env) == "d"
    assert running.interpolate("${SET:?needed}", env) == "x"
    assert running.interpolate("$${SET}", env) == "${SET}"
    # compose.yaml's own nesting: an override, else registry + version
    nested = "${IMG:-${REG:-ghcr.io/o}/gw:${V}}"
    assert running.interpolate(nested, {"V": "1"}) == "ghcr.io/o/gw:1"
    assert running.interpolate(nested, {"V": "1", "REG": "local"}) == "local/gw:1"
    assert running.interpolate(nested, {"V": "1", "IMG": "mine:2"}) == "mine:2"


def _deployment(tmp_path: Path) -> tuple[dict, dict[str, str]]:
    argv = ["init", "--dir", str(tmp_path), "--origin", "https://llm.example.org"]
    assert main([*argv, "--admin-email", "ops@example.org", "--preset", "team"]) == 0
    compose = yaml.safe_load((stackfiles.stack_dir() / "compose.yaml").read_text())
    return compose, envfile.read(tmp_path / ".env")


def _inspect_as_compose_would(compose: dict, env: dict[str, str]) -> dict:
    """Containers exactly as `docker compose up` would have made them."""
    project = env["COMPOSE_PROJECT_NAME"]
    containers: list[dict] = []
    ids: dict[str, str] = {}  # one image per reference, shared as compose shares it
    for name, want in running.expected_services(compose, env).items():
        image_id = ids.setdefault(want.image, f"sha256:{len(ids):064x}")
        containers.append(
            {
                "Image": image_id,
                "Config": {
                    "Image": want.image,
                    "Env": [f"{k}={v}" for k, v in want.environment.items()] + ["PATH=/bin"],
                    "Labels": {
                        "com.docker.compose.project": project,
                        "com.docker.compose.service": name,
                    },
                },
                "Mounts": [{"Type": "volume", "Name": v} for v in want.volumes],
                "State": {"Status": "exited", "ExitCode": 0}
                if want.one_shot
                else {"Status": "running", "Health": {"Status": "healthy"}},
            }
        )
    images = [
        {"Id": i, "RepoTags": [ref], "Config": {"Labels": {running.REVISION_LABEL: "abc1234"}}}
        for ref, i in ids.items()
    ]
    return {"containers": containers, "images": images}


def test_a_stack_brought_up_from_this_env_matches(tmp_path: Path) -> None:
    compose, env = _deployment(tmp_path)
    data = _inspect_as_compose_would(compose, env)
    wanted = running.expected_services(compose, env)
    assert {"gateway", "chat", "authelia", "redaction", "proxy"} <= set(wanted)
    assert "playwright" not in wanted  # the fetch profile is off in team
    assert "llm-platform_postgres-data" not in wanted["postgres"].volumes
    assert f"{env['COMPOSE_PROJECT_NAME']}_postgres-data" in wanted["postgres"].volumes
    found = running.compare(wanted, data, env["COMPOSE_PROJECT_NAME"])
    assert not found.errors and not found.warnings, (found.errors, found.warnings)
    assert any("revision abc1234" in note for note in found.notes)


def _by_service(data: dict, name: str) -> dict:
    return next(
        c for c in data["containers"] if c["Config"]["Labels"]["com.docker.compose.service"] == name
    )


def test_drift_is_named_and_secrets_never_printed(tmp_path: Path) -> None:
    compose, env = _deployment(tmp_path)
    data = _inspect_as_compose_would(compose, env)
    wanted = running.expected_services(compose, env)
    project = env["COMPOSE_PROJECT_NAME"]

    changed = copy.deepcopy(data)
    gateway = _by_service(changed, "gateway")
    gateway["Config"]["Env"] = [
        "GATEWAY_SECRET_KEY=the-old-secret" if e.startswith("GATEWAY_SECRET_KEY=") else e
        for e in gateway["Config"]["Env"]
    ]
    _by_service(changed, "chat")["Config"]["Image"] = "local/cerea:old"
    _by_service(changed, "postgres")["Mounts"] = []
    _by_service(changed, "migrate")["State"] = {"Status": "exited", "ExitCode": 1}
    _by_service(changed, "authelia")["State"]["Health"]["Status"] = "unhealthy"
    changed["containers"].remove(_by_service(changed, "valkey"))
    # the proxy's tag now points at a newer image than the one it runs
    proxy = _by_service(changed, "proxy")
    proxy["Image"] = "sha256:" + "f" * 64

    found = running.compare(wanted, changed, project)
    text = "\n".join(found.errors + found.warnings)
    assert "gateway: environment differs from what .env gives now: GATEWAY_SECRET_KEY" in text
    assert "the-old-secret" not in text and env["GATEWAY_SECRET_KEY"] not in text
    assert "chat: runs local/cerea:old" in text
    assert f"postgres: does not mount volume {project}_postgres-data" in text
    assert "migrate: exited with code 1" in text
    assert "authelia: unhealthy" in text
    assert "valkey: no container" in text
    assert "proxy:" in text and "rebuilt or pulled" in text


def test_another_projects_containers_are_not_this_deployment(tmp_path: Path) -> None:
    compose, env = _deployment(tmp_path)
    data = _inspect_as_compose_would(compose, env)
    found = running.compare(running.expected_services(compose, env), data, "someone-else")
    assert any("gateway: no container" in w for w in found.warnings)


def test_doctor_reads_the_shims_json_from_stdin(tmp_path: Path, monkeypatch, capsys) -> None:
    compose, env = _deployment(tmp_path)
    data = _inspect_as_compose_would(compose, env)
    (tmp_path / "compose.yaml").write_text((stackfiles.stack_dir() / "compose.yaml").read_text())
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
    argv = ["doctor", "--dir", str(tmp_path), "--no-docker", "--against-running"]
    assert main([*argv, "--inspect", "-"]) == 0
    assert "the running containers match .env" in capsys.readouterr().out


def test_without_docker_or_data_it_says_how_to_get_it(tmp_path: Path, monkeypatch) -> None:
    _deployment(tmp_path)
    monkeypatch.setattr(running.shutil, "which", lambda _: None)
    report = doctor.check(tmp_path, probe_docker=False)
    doctor.check_running(tmp_path, report)
    assert any("./pystino" in e for e in report.errors)


def test_the_shim_collects_on_the_host_and_never_mounts_the_socket() -> None:
    from gateway.deploy.cli import SHIM

    assert "--against-running" in SHIM and "--inspect -" in SHIM
    assert "docker.sock" not in SHIM
