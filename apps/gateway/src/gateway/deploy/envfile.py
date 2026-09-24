"""The deployment's one `.env`: read and written in the dialect Compose reads.

Flat, single-line, ordered. A value that holds anything outside a conservative
set is written single-quoted, which Compose takes literally — that matters for
argon2 digests (`$argon2id$…`), which Compose would otherwise try to
interpolate. A value that cannot be represented that way (a newline, a single
quote) is refused at write time rather than mangled: the old installer's worst
config bugs were values that were silently rewritten on the way into the file.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SAFE = re.compile(r"^[A-Za-z0-9_./:,@+=\-]*$")


class EnvFileError(ValueError):
    pass


def parse(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines. Comments and blank lines are skipped.

    Single quotes are literal (Compose's rule); double quotes are accepted for
    hand-edited files and unwrapped without escape processing.
    """
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.match(key):
            raise EnvFileError(f"line {number}: expected KEY=VALUE, got {raw!r}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def read(path: Path) -> dict[str, str]:
    return parse(path.read_text(encoding="utf-8"))


def format_value(key: str, value: str) -> str:
    if not _KEY.match(key):
        raise EnvFileError(f"{key!r} is not a valid variable name")
    if "\n" in value or "\r" in value:
        raise EnvFileError(f"{key}: values must be single-line")
    if _SAFE.match(value):
        return value
    if "'" in value:
        raise EnvFileError(f"{key}: a value holding a single quote cannot be written literally")
    return f"'{value}'"


Section = tuple[str, Iterable[tuple[str, str]]]


def render(sections: Iterable[Section]) -> str:
    """Sections of (heading, [(key, value)…]) → file text, headings as comments."""
    out: list[str] = []
    for heading, items in sections:
        if out:
            out.append("")
        for line in heading.strip().splitlines():
            out.append(f"# {line}".rstrip())
        for key, value in items:
            out.append(f"{key}={format_value(key, value)}")
    return "\n".join(out) + "\n"


def write_atomic(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Write via a temp file in the same directory, fsync, then rename.

    The rename is what makes a crash mid-write leave the old file intact, and
    the mode is set before any secret is on disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(path: Path, changes: Mapping[str, str]) -> None:
    """Set keys in an existing file, keeping every other line as it was."""
    lines = path.read_text(encoding="utf-8").splitlines()
    pending = dict(changes)
    for index, raw in enumerate(lines):
        key = raw.split("=", 1)[0].strip()
        if key in pending and "=" in raw and not raw.lstrip().startswith("#"):
            lines[index] = f"{key}={format_value(key, pending.pop(key))}"
    for key, value in pending.items():
        lines.append(f"{key}={format_value(key, value)}")
    write_atomic(path, "\n".join(lines) + "\n")
