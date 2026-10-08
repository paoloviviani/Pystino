#!/usr/bin/env python3
"""Keep compose.yaml's `pystino.config-rev` labels in step with the repository.

    deploy/pin.py             refresh the pystino.config-rev labels
    deploy/pin.py --check     exit 1 if a label is stale (CI)
    deploy/pin.py --show      print the current labels

compose never notices that a mounted file changed, so the proxy and Authelia
each carry a digest of their configuration directory. A changed Caddyfile
changes the label, the label changes the service definition, and
`docker compose up -d` recreates the container. Run this after editing
deploy/caddy/ or deploy/authelia/; CI runs it with --check.

Same algorithm as the Cerea deploy kit's `kit/tools/pin`, so the two labels mean the same
thing in both repositories: sha256 over sorted relative paths and contents,
first 16 hex characters.

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPOSE = ROOT / "compose.yaml"
#: Service name -> the directory (relative to this file) it mounts as config.
CONFIG_DIRS = {"proxy": "caddy", "authelia": "authelia"}


def config_rev(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return "sha256:" + digest.hexdigest()[:16]


def label_pattern(service: str) -> re.Pattern[str]:
    # The label line inside the service's block: the first `pystino.config-rev`
    # after `  <service>:` at two-space indentation.
    return re.compile(
        rf"(^  {re.escape(service)}:\n(?:(?!^  \S).*\n)*?\s+pystino\.config-rev: \")([^\"]*)(\")",
        re.MULTILINE,
    )


def refreshed(text: str) -> tuple[str, list[str]]:
    changes = []
    for service, directory in CONFIG_DIRS.items():
        rev = config_rev(ROOT / directory)
        pattern = label_pattern(service)
        match = pattern.search(text)
        if match is None:
            raise SystemExit(f"pin: no pystino.config-rev label on service {service!r}")
        if match.group(2) != rev:
            changes.append(f"{service}: config-rev {match.group(2)} -> {rev}")
            text = pattern.sub(lambda m, rev=rev: m.group(1) + rev + m.group(3), text, count=1)
    return text, changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deploy/pin.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="change nothing; exit 1 if stale")
    parser.add_argument("--show", action="store_true", help="print the current labels")
    args = parser.parse_args(argv)

    text = COMPOSE.read_text(encoding="utf-8")
    if args.show:
        for service, directory in CONFIG_DIRS.items():
            print(f"{service}.config-rev={config_rev(ROOT / directory)}")
        return 0

    new, changes = refreshed(text)
    if args.check:
        for line in changes:
            print(f"stale: {line}")
        if changes:
            print("run deploy/pin.py and commit deploy/compose.yaml", file=sys.stderr)
        return 1 if changes else 0
    if changes:
        COMPOSE.write_text(new, encoding="utf-8")
        for line in changes:
            print(line)
    else:
        print("deploy/compose.yaml is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
