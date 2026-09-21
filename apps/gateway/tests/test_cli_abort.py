"""The CLI's password prompts abort cleanly on SIGINT.

`gateway passwd` runs inside `docker compose exec` during installs — the
operator's TTY is attached — and Ctrl+C at its getpass prompts must mean
abort, not an asyncio traceback or a process left holding the terminal.
The installer resumes idempotently, so exit 130 (the conventional
SIGINT status) with a one-line message is the honest answer.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from gateway import cli
from gateway.config import Settings


@pytest.fixture
def cli_settings(tmp_path: Path) -> Settings:
    # Only logging setup reads these before the prompt; the abort happens
    # before any database work, so a minimal real Settings is enough.
    return Settings(
        environment="dev",
        log_json=False,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'cli-test.db'}",
        session_secret="test-secret-not-for-production",
        secret_key="test-encryption-key-not-for-production",
    )


def test_passwd_ctrl_c_exits_130(
    cli_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: cli_settings)
    with patch("gateway.cli.getpass.getpass", side_effect=KeyboardInterrupt):
        rc = cli.main(["passwd", "admin@local", "--admin"])
    assert rc == 130


def test_passwd_mismatch_reprompts_then_accepts(
    cli_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Mismatch and policy refusal are retries now, not exit 1: a refusal
    # mid-install aborted the whole phase over a rule nobody stated. A
    # third valid pair proves the loop returns to success; the database
    # write past it is stubbed — the retry contract is what is under test.
    monkeypatch.setattr(cli, "get_settings", lambda: cli_settings)
    monkeypatch.setattr(
        cli, "create_engine", lambda _settings: _FakeEngine(), raising=True
    )
    monkeypatch.setattr(
        cli,
        "create_session_factory",
        lambda _engine: _fake_session_factory(),
    )
    monkeypatch.setattr(cli, "hash_password", lambda _pw: "stubbed-hash", raising=False)
    answers = iter(["one", "two", "short", "short", "good-password-10", "good-password-10"])
    with patch("gateway.cli.getpass.getpass", side_effect=lambda *_: next(answers)):
        rc = cli.main(["passwd", "admin@local"])
    assert rc == 0


def test_seed_prompt_ctrl_c_exits_130(
    cli_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: cli_settings)
    with patch("gateway.cli.getpass.getpass", side_effect=KeyboardInterrupt):
        rc = cli.main(["seed", "--password"])
    assert rc == 130


def _fake_session_factory():
    """A no-op async context manager standing in for the session factory.

    The retry contract is what is under test; the database write that
    follows validation is real code with its own coverage elsewhere.
    """

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, *_: object, **__: object) -> _Result:
            return _Result()

        async def get(self, *_: object, **__: object) -> None:
            return None

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

        def add(self, *_: object) -> None:
            return None

    class _Result:
        def scalar_one_or_none(self) -> None:
            return None

    return _Session


class _FakeEngine:
    async def dispose(self) -> None:
        return None
