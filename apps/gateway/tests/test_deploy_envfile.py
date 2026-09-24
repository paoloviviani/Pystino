"""The `.env` dialect `pystino` writes must be the one Compose reads."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from gateway.deploy import envfile


def test_round_trip_keeps_argon2_digests_literal(tmp_path: Path) -> None:
    digest = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
    text = envfile.render([("head", [("A", "plain"), ("D", digest), ("E", "")])])
    # Single-quoted: Compose takes it literally instead of interpolating $argon2id.
    assert f"D='{digest}'" in text
    assert envfile.parse(text) == {"A": "plain", "D": digest, "E": ""}


def test_values_that_cannot_be_literal_are_refused() -> None:
    with pytest.raises(envfile.EnvFileError, match="single-line"):
        envfile.format_value("K", "two\nlines")
    with pytest.raises(envfile.EnvFileError, match="single quote"):
        envfile.format_value("K", "it's $bad")
    with pytest.raises(envfile.EnvFileError, match="valid variable name"):
        envfile.format_value("lower", "x")


def test_parse_accepts_hand_edits() -> None:
    parsed = envfile.parse('# c\n\nexport A=1\nB="two words"\nC=3 # trailing\n')
    assert parsed == {"A": "1", "B": "two words", "C": "3"}
    with pytest.raises(envfile.EnvFileError, match="line 1"):
        envfile.parse("not a line\n")


def test_write_is_atomic_and_private(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    envfile.write_atomic(path, "A=1\n")
    assert path.read_text() == "A=1\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_update_changes_in_place_and_appends(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# keep me\nA=1\nB=2\n")
    envfile.update(path, {"B": "3", "C": "$x"})
    assert path.read_text() == "# keep me\nA=1\nB=3\nC='$x'\n"
