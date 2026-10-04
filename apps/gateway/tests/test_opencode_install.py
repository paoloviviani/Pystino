"""The opencode setup script, run for real against a fake ``/v1/models``.

The script is what a stranger pipes into bash, so these tests run it the way
they would: as a subprocess, with its own ``HOME``, against a tiny local HTTP
server. Nothing is mocked inside the script, because the failures that matter
are the ones a mock hides: a key in argv, a config merged over the user's own
settings, a commented file rewritten without its comments.

The cards are built with the gateway's own ``ModelCard`` so the fake cannot
drift from what ``GET /v1/models`` really sends.
"""

from __future__ import annotations

import json
import os
import pty
import select
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from gateway.routers.client_scripts import _SCRIPT
from gateway.schemas import ModelCard

KEY = "gwk_SECRETSECRETSECRETSECRET0123456789"
SCRIPT = _SCRIPT

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="runs a bash script")


def card(model_id: str, **fields: Any) -> dict[str, Any]:
    return ModelCard(id=model_id, created=1, **fields).model_dump()


CATALOGUE = [
    card(
        "big-reasoner",
        display_name="Big Reasoner",
        context_window=200000,
        max_output_tokens=32000,
        input_modalities=["text", "image"],
        output_modalities=["text"],
        supported_features=["tools", "Reasoning"],
    ),
    card("plain-chat", context_window=64000),  # no display name, no output limit
    card("embedder", kind="embedding", context_window=8192),
    card("scanner", kind="ocr"),
    card("generator", kind="image"),
]


class FakeGateway:
    """Answers GET /v1/models and remembers who asked, and how."""

    def __init__(self, cards: list[dict[str, Any]], status: int = 200, delay: float = 0.0):
        self.cards = cards
        self.status = status
        self.delay = delay
        self.seen: list[tuple[str, str]] = []  # (path, authorization)
        self.redirect_to: str | None = None
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                gateway.seen.append((self.path, self.headers.get("Authorization", "")))
                if gateway.delay:
                    time.sleep(gateway.delay)
                if gateway.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", gateway.redirect_to)
                    self.end_headers()
                    return
                body = json.dumps({"object": "list", "data": gateway.cards}).encode()
                self.send_response(gateway.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway() -> Iterator[FakeGateway]:
    fake = FakeGateway(CATALOGUE)
    yield fake
    fake.close()


class Run:
    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.xdg = tmp_path / "xdg"
        self.global_config = self.xdg / "opencode" / "opencode.json"

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.xdg),
            "PYSTINO_API_KEY": KEY,
        }
        env.update(extra)
        return env

    def __call__(
        self, *args: str, key: str | None = KEY, stdin: str | None = None, **env: str
    ) -> subprocess.CompletedProcess[str]:
        merged = self.env(**env)
        if key is None:
            merged.pop("PYSTINO_API_KEY")
        # A new session has no controlling terminal, so the script cannot
        # prompt: it is the unattended case, deterministically.
        command = ["bash", str(SCRIPT), *args] if stdin is None else ["bash", "-s", "--", *args]
        return subprocess.run(
            command,
            input=stdin,
            env=merged,
            capture_output=True,
            text=True,
            start_new_session=True,
            timeout=60,
        )


@pytest.fixture
def run(tmp_path: Path) -> Run:
    return Run(tmp_path)


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def pystino_block(path: Path) -> dict[str, Any]:
    return load(path)["provider"]["pystino"]


# --- the model mapping ------------------------------------------------------


def test_models_map_like_galopin(run: Run, gateway: FakeGateway) -> None:
    result = run(gateway.origin)
    assert result.returncode == 0, result.stderr
    block = pystino_block(run.global_config)
    assert block["npm"] == "@ai-sdk/openai-compatible"
    assert block["options"] == {"baseURL": gateway.origin + "/v1", "apiKey": KEY}
    assert block["models"] == {
        "big-reasoner": {
            "name": "Big Reasoner",
            "limit": {"context": 200000, "output": 32000},
            "variants": {
                "low": {"reasoningEffort": "low"},
                "medium": {"reasoningEffort": "medium"},
                "high": {"reasoningEffort": "high"},
            },
            "attachment": True,
            "modalities": {"input": ["text", "image"]},
        },
        # Unknown limits become defaults, never omissions: opencode refuses a
        # model entry without both keys.
        "plain-chat": {"name": "plain-chat", "limit": {"context": 64000, "output": 16384}},
    }
    assert gateway.seen[0][0] == "/v1/models"


def test_non_chat_kinds_are_skipped_and_missing_kind_counts_as_chat(
    run: Run, gateway: FakeGateway
) -> None:
    legacy = card("old-gateway-model")
    del legacy["kind"], legacy["supported_features"], legacy["input_modalities"]
    gateway.cards = [*CATALOGUE, legacy, {"id": ""}, {"no": "id"}]
    run(gateway.origin)
    assert set(pystino_block(run.global_config)["models"]) == {
        "big-reasoner",
        "plain-chat",
        "old-gateway-model",
    }


def test_nonsense_limits_fall_back_to_defaults(run: Run, gateway: FakeGateway) -> None:
    gateway.cards = [
        {**card("m"), "context_window": -5, "max_output_tokens": "lots"},
        {**card("n"), "context_window": None, "max_output_tokens": True},
    ]
    run(gateway.origin)
    models = pystino_block(run.global_config)["models"]
    for name in ("m", "n"):
        assert models[name]["limit"] == {"context": 131072, "output": 16384}


def test_a_key_that_sees_no_chat_models_gets_a_placeholder(run: Run, gateway: FakeGateway) -> None:
    gateway.cards = [card("embedder", kind="embedding")]
    result = run(gateway.origin)
    assert result.returncode == 0
    assert list(pystino_block(run.global_config)["models"]) == ["REPLACE-WITH-MODEL-ID"]


# --- where it writes, and what it keeps -------------------------------------


def test_default_target_is_the_global_config_under_xdg(run: Run, gateway: FakeGateway) -> None:
    result = run(gateway.origin)
    assert result.returncode == 0, result.stderr
    assert run.global_config.is_file()
    assert mode(run.global_config) == 0o600
    assert load(run.global_config)["$schema"] == "https://opencode.ai/config.json"


def test_without_xdg_it_uses_home_dot_config(run: Run, gateway: FakeGateway) -> None:
    env = run.env()
    del env["XDG_CONFIG_HOME"]
    result = subprocess.run(
        ["bash", str(SCRIPT), gateway.origin],
        env=env,
        capture_output=True,
        text=True,
        start_new_session=True,
    )
    assert result.returncode == 0, result.stderr
    assert (run.home / ".config" / "opencode" / "opencode.json").is_file()


def test_merge_keeps_everything_but_the_pystino_block(run: Run, gateway: FakeGateway) -> None:
    run.global_config.parent.mkdir(parents=True)
    before = {
        "$schema": "https://opencode.ai/config.json",
        "theme": "tokyonight",
        "model": "anthropic/claude-something",
        "mcp": {"docs": {"type": "remote", "url": "https://example.test/mcp"}},
        "provider": {
            "anthropic": {"options": {"apiKey": "{env:ANTHROPIC_API_KEY}"}},
            "pystino": {"name": "stale", "models": {"gone": {"name": "gone"}}},
            "local": {"npm": "@ai-sdk/openai-compatible", "models": {}},
        },
    }
    original = json.dumps(before, indent=4)
    run.global_config.write_text(original)
    run.global_config.chmod(0o644)

    result = run(gateway.origin)
    assert result.returncode == 0, result.stderr

    after = load(run.global_config)
    assert after["theme"] == "tokyonight"
    assert after["model"] == "anthropic/claude-something"  # not asked, not touched
    assert after["mcp"] == before["mcp"]
    assert after["provider"]["anthropic"] == before["provider"]["anthropic"]
    assert after["provider"]["local"] == before["provider"]["local"]
    assert list(after["provider"]) == ["anthropic", "pystino", "local"]  # replaced in place
    assert "gone" not in after["provider"]["pystino"]["models"]
    assert "big-reasoner" in after["provider"]["pystino"]["models"]

    backups = list(run.global_config.parent.glob("opencode.json.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
    assert mode(backups[0]) == 0o600
    # The summary names what was kept and what changed.
    assert (
        "kept as it was: 3 other setting(s) (theme, model, mcp), 2 other provider(s)"
        in result.stderr
    )
    assert "provider.pystino: replaced (1 model(s) before, 2 now; +2, -1)" in result.stderr
    assert "backup:" in result.stderr


def test_running_twice_backs_up_twice_and_changes_nothing_else(
    run: Run, gateway: FakeGateway
) -> None:
    run(gateway.origin)
    first = run.global_config.read_text()
    run(gateway.origin)
    assert run.global_config.read_text() == first
    assert (
        len(list(run.global_config.parent.glob("opencode.json.bak-*"))) == 1
    )  # one existed to save


def test_model_flag_sets_the_default_and_must_exist(run: Run, gateway: FakeGateway) -> None:
    refused = run(gateway.origin, "--model", "nope")
    assert refused.returncode == 2
    assert not run.global_config.exists()  # nothing written on a bad id

    run(gateway.origin, "--model", "plain-chat")
    assert load(run.global_config)["model"] == "pystino/plain-chat"


def test_dry_run_and_print_write_nothing(run: Run, gateway: FakeGateway) -> None:
    dry = run(gateway.origin, "--dry-run")
    assert dry.returncode == 0
    assert "dry run" in dry.stderr
    assert not run.global_config.exists()

    printed = run(gateway.origin, "--print")
    assert printed.returncode == 0
    block = json.loads(printed.stdout)["provider"]["pystino"]
    assert block["options"]["apiKey"] == "{env:PYSTINO_API_KEY}"
    assert KEY not in printed.stdout + printed.stderr
    assert not run.global_config.exists()


def test_output_writes_a_project_file_and_leaves_the_global_one(
    run: Run, gateway: FakeGateway, tmp_path: Path
) -> None:
    project = tmp_path / "proj" / "opencode.json"
    project.parent.mkdir()
    result = run(gateway.origin, "--output", str(project))
    assert result.returncode == 0, result.stderr
    assert "pystino" in load(project)["provider"]
    assert mode(project) == 0o600
    assert not run.global_config.exists()


# --- commented files are refused, untouched ----------------------------------


def test_jsonc_beside_a_missing_json_is_refused(run: Run, gateway: FakeGateway) -> None:
    run.global_config.parent.mkdir(parents=True)
    jsonc = run.global_config.with_suffix(".jsonc")
    jsonc.write_text('{\n  // my notes\n  "theme": "x",\n}\n')
    result = run(gateway.origin)
    assert result.returncode == 7
    assert "comments" in result.stderr
    assert jsonc.read_text() == '{\n  // my notes\n  "theme": "x",\n}\n'
    assert not run.global_config.exists()


@pytest.mark.parametrize(
    "text",
    [
        '{\n  // keep me\n  "theme": "x"\n}\n',
        '{"theme": "x",}\n',
        "[1, 2]\n",
        '{"provider": []}\n',
    ],
)
def test_a_file_that_is_not_strict_json_is_refused_untouched(
    run: Run, gateway: FakeGateway, text: str
) -> None:
    run.global_config.parent.mkdir(parents=True)
    run.global_config.write_text(text)
    result = run(gateway.origin)
    assert result.returncode == 7, result.stderr
    assert run.global_config.read_text() == text
    assert not list(run.global_config.parent.glob("*.bak-*"))


def test_json_next_to_jsonc_is_merged_since_opencode_reads_both(
    run: Run, gateway: FakeGateway
) -> None:
    run.global_config.parent.mkdir(parents=True)
    run.global_config.with_suffix(".jsonc").write_text("{ // hi\n}\n")
    run.global_config.write_text('{"theme": "x"}\n')
    assert run(gateway.origin).returncode == 0
    assert load(run.global_config)["theme"] == "x"


def test_enabled_providers_without_pystino_is_warned_about(run: Run, gateway: FakeGateway) -> None:
    run.global_config.parent.mkdir(parents=True)
    run.global_config.write_text('{"enabled_providers": ["anthropic"]}\n')
    result = run(gateway.origin)
    assert "enabled_providers does not list pystino" in result.stderr


# --- the key ----------------------------------------------------------------


def test_key_in_env_writes_a_reference_not_the_key(run: Run, gateway: FakeGateway) -> None:
    result = run(gateway.origin, "--key-in-env")
    assert result.returncode == 0, result.stderr
    text = run.global_config.read_text()
    assert load(run.global_config)["provider"]["pystino"]["options"]["apiKey"] == (
        "{env:PYSTINO_API_KEY}"
    )
    assert KEY not in text
    assert "NOT written" in result.stderr
    # It still used the key to discover the catalogue.
    assert gateway.seen[0][1] == f"Bearer {KEY}"


def test_default_says_the_key_is_in_the_file(run: Run, gateway: FakeGateway) -> None:
    result = run(gateway.origin)
    assert "written into the file (mode 600)" in result.stderr
    assert load(run.global_config)["provider"]["pystino"]["options"]["apiKey"] == KEY


def test_the_key_is_never_printed(run: Run, gateway: FakeGateway) -> None:
    for args in ([], ["--key-in-env"], ["--dry-run"], ["--print"], ["--model", "plain-chat"]):
        result = run(gateway.origin, *args)
        assert result.returncode == 0, result.stderr
        assert KEY not in result.stdout + result.stderr
        assert KEY[:8] not in result.stdout + result.stderr  # not even a prefix


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs /proc")
def test_the_key_is_never_in_argv(run: Run, gateway: FakeGateway) -> None:
    gateway.delay = 1.5  # hold the request open so the process can be inspected
    process = subprocess.Popen(
        ["bash", str(SCRIPT), gateway.origin],
        env=run.env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.time() + 10
        while not gateway.seen and time.time() < deadline:
            time.sleep(0.05)
        assert gateway.seen, "the script never reached the gateway"
        argv = Path(f"/proc/{process.pid}/cmdline").read_bytes()
        assert KEY.encode() not in argv
        assert b"gwk_" not in argv
    finally:
        process.communicate(timeout=30)


def test_a_refused_key_stops_before_writing(run: Run, gateway: FakeGateway) -> None:
    gateway.status = 401
    result = run(gateway.origin)
    assert result.returncode == 3
    assert KEY not in result.stdout + result.stderr
    assert not run.global_config.exists()


def test_quota_exhaustion_is_not_an_auth_failure(run: Run, gateway: FakeGateway) -> None:
    gateway.status = 429
    result = run(gateway.origin)
    assert result.returncode == 4
    assert "over its cap" in result.stderr


def test_an_unreachable_gateway_writes_nothing_unless_asked(run: Run) -> None:
    result = run("http://127.0.0.1:9")
    assert result.returncode == 6
    assert not run.global_config.exists()
    placeholder = run("http://127.0.0.1:9", "--no-discover")
    assert placeholder.returncode == 0
    assert "REPLACE-WITH-MODEL-ID" in pystino_block(run.global_config)["models"]


def test_a_redirect_never_receives_the_key(run: Run, gateway: FakeGateway) -> None:
    elsewhere = FakeGateway(CATALOGUE)
    try:
        gateway.redirect_to = elsewhere.origin + "/v1/models"
        result = run(gateway.origin)
        assert result.returncode == 6
        assert "redirected" in result.stderr
        assert elsewhere.seen == []
    finally:
        elsewhere.close()


def test_missing_key_without_a_terminal_is_a_clear_error(run: Run, gateway: FakeGateway) -> None:
    result = run(gateway.origin, key=None)
    assert result.returncode == 2
    assert "PYSTINO_API_KEY" in result.stderr


# --- the address ------------------------------------------------------------


def test_address_forms(run: Run, gateway: FakeGateway) -> None:
    run(gateway.origin + "/")
    assert pystino_block(run.global_config)["options"]["baseURL"] == gateway.origin + "/v1"
    run("--base-url", gateway.origin + "/v1")
    assert pystino_block(run.global_config)["options"]["baseURL"] == gateway.origin + "/v1"
    run(PYSTINO_BASE_URL=gateway.origin)
    assert pystino_block(run.global_config)["options"]["baseURL"] == gateway.origin + "/v1"


def test_the_served_origin_is_only_a_default(run: Run, gateway: FakeGateway) -> None:
    text = SCRIPT.read_text()
    assert "served_origin=''" in text
    served = text.replace("served_origin=''", f"served_origin='{gateway.origin}'")
    assert run(stdin=served).returncode == 0
    assert pystino_block(run.global_config)["options"]["baseURL"] == gateway.origin + "/v1"
    # An explicit address wins over it.
    other = FakeGateway(CATALOGUE)
    try:
        run(other.origin, stdin=served.replace(gateway.origin, "http://127.0.0.1:9"))
        assert pystino_block(run.global_config)["options"]["baseURL"] == other.origin + "/v1"
    finally:
        other.close()


@pytest.mark.parametrize("bad", ["ftp://x", "llm.example.org", "https://u:p@host", "https://h?x=1"])
def test_a_bad_address_is_refused(run: Run, bad: str) -> None:
    result = run(bad)
    assert result.returncode == 2
    assert not run.global_config.exists()


# --- piped, the way people run it -------------------------------------------


def test_it_runs_from_a_pipe(run: Run, gateway: FakeGateway) -> None:
    result = run(gateway.origin, stdin=SCRIPT.read_text())
    assert result.returncode == 0, result.stderr
    assert "pystino" in load(run.global_config)["provider"]


def test_a_truncated_download_runs_nothing(run: Run, gateway: FakeGateway) -> None:
    text = SCRIPT.read_text()
    result = run(gateway.origin, stdin=text[: len(text) // 2])
    assert result.returncode != 0
    assert not run.global_config.exists()
    assert gateway.seen == []


@pytest.mark.asyncio
async def test_the_served_script_defaults_to_the_origin_it_came_from(
    run: Run, gateway: FakeGateway, client: httpx.AsyncClient
) -> None:
    host = gateway.origin.removeprefix("http://")
    served = await client.get("/opencode/install.sh", headers={"host": host})
    assert served.status_code == 200
    # No address on the command line: the origin baked in at serve time is used.
    result = run(stdin=served.text, key=KEY)
    assert result.returncode == 0, result.stderr
    assert pystino_block(run.global_config)["options"]["baseURL"] == gateway.origin + "/v1"


# --- the terminal -----------------------------------------------------------


def _drive_tty(command: list[str], env: dict[str, str], script: list[tuple[str, str]]) -> str:
    """Run `command` on a pty; answer each (expected prompt, reply) in turn."""
    pid, fd = pty.fork()
    if pid == 0:  # child: has the pty as its controlling terminal
        os.execvpe(command[0], command, env)
    seen = b""
    try:
        for expected, reply in script:
            deadline = time.time() + 20
            while expected.encode() not in seen:
                assert time.time() < deadline, f"no {expected!r} in {seen!r}"
                if select.select([fd], [], [], 0.2)[0]:
                    seen += os.read(fd, 4096)
            os.write(fd, (reply + "\n").encode())
            seen += b"\x00"  # separate this prompt's output from the next
        deadline = time.time() + 20
        while time.time() < deadline:
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                seen += chunk
    finally:
        os.waitpid(pid, 0)
        os.close(fd)
    return seen.decode(errors="replace")


def test_piped_run_asks_for_the_key_on_the_terminal_and_does_not_echo_it(
    run: Run, gateway: FakeGateway
) -> None:
    env = run.env()
    del env["PYSTINO_API_KEY"]
    # `cat script | bash`: stdin is the script, so only /dev/tty can ask.
    shell = f"cat {SCRIPT} | bash -s -- {gateway.origin}"
    output = _drive_tty(["bash", "-c", shell], env, [("API key", KEY), ("Write it?", "y")])
    assert "wrote" in output
    assert KEY not in output  # hidden prompt: never echoed
    assert pystino_block(run.global_config)["options"]["apiKey"] == KEY


def test_declining_the_confirmation_writes_nothing(run: Run, gateway: FakeGateway) -> None:
    output = _drive_tty(["bash", str(SCRIPT), gateway.origin], run.env(), [("Write it?", "n")])
    assert "aborted" in output
    assert not run.global_config.exists()


# --- installing opencode ----------------------------------------------------


def test_install_opencode_runs_the_pinned_installer_without_the_key(
    run: Run, gateway: FakeGateway, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "installer.log"
    # A curl that serves an "installer" which records its arguments and
    # whether the key reached it. PATH holds nothing else but bash and python3.
    (fake_bin / "curl").write_text(
        "#!/bin/sh\n"
        f'echo "curl $*" >> {log}\n'
        'echo \'echo "installer args: $*" >> ' + str(log) + "; "
        'echo "key in env: ${PYSTINO_API_KEY:-no}" >> ' + str(log) + "'\n"
    )
    (fake_bin / "curl").chmod(0o755)
    for tool in ("bash", "python3", "sh", "echo"):
        found = subprocess.run(["which", tool], capture_output=True, text=True).stdout.strip()
        if found and not (fake_bin / tool).exists():
            (fake_bin / tool).symlink_to(found)
    env = run.env(PATH=str(fake_bin))
    result = subprocess.run(
        ["bash", str(SCRIPT), gateway.origin, "--install-opencode", "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        start_new_session=True,
    )
    assert result.returncode == 0, result.stderr
    text = log.read_text()
    assert "curl -fsSL https://opencode.ai/install" in text
    assert "installer args: --version 1.18.34" in text
    assert "key in env: no" in text


def test_the_pin_is_a_version_and_matches_cerea_when_it_is_checked_out() -> None:
    import re

    pin = re.search(r'opencode_pin="\$\{PYSTINO_OPENCODE_VERSION:-([^}]+)\}"', SCRIPT.read_text())
    assert pin and re.fullmatch(r"\d+\.\d+\.\d+", pin.group(1))
    cerea = Path(__file__).resolve().parents[3].parent / "Cerea/agent/packaging/opencode-version"
    if cerea.is_file():
        assert pin.group(1) == cerea.read_text().strip()
