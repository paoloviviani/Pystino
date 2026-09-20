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


def test_passwd_mismatch_is_refused(
    cli_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: cli_settings)
    with patch("gateway.cli.getpass.getpass", side_effect=["one", "two"]):
        rc = cli.main(["passwd", "admin@local"])
    assert rc == 1


def test_seed_prompt_ctrl_c_exits_130(
    cli_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: cli_settings)
    with patch("gateway.cli.getpass.getpass", side_effect=KeyboardInterrupt):
        rc = cli.main(["seed", "--password"])
    assert rc == 130
