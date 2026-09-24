"""The `pystino` command line: init, bootstrap, doctor, set.

Every command takes its answers as flags so CI and scripts never meet a prompt;
`init` asks only for what is missing, and only on a terminal.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

from gateway.deploy import bootstrap, doctor, envfile, presets, stackfiles
from gateway.deploy.init import TLS_MODES, InitError, InitOptions, build_env

SHIM = """#!/bin/sh
# Runs `pystino` for this deployment. Written by `pystino init`; safe to delete
# and re-create. Development installs run it from the checkout with uv;
# distributed installs run it from the gateway image of the version in .env —
# or, for `upgrade <version>`, of the version being upgraded to, so the new
# release's own code writes the new release's files.
set -eu
cd "$(dirname "$0")"
val() { sed -n "s/^$1=//p" .env | tail -n 1 | tr -d "'\\""; }
src=$(val PYSTINO_SRC)
if [ -n "$src" ] && command -v uv >/dev/null 2>&1; then
    exec uv run --project "$src" pystino "$@"
fi
reg=$(val PYSTINO_REGISTRY)
ver=$(val PYSTINO_VERSION)
if [ "${1:-}" = upgrade ] && [ -n "${2:-}" ]; then ver="$2"; fi
tty=""
if [ -t 0 ]; then tty="-it"; fi
exec docker run --rm $tty -u "$(id -u):$(id -g)" -v "$PWD:/deploy" -w /deploy \\
    -e PYSTINO_DEPLOY_DIR="$PWD" "$reg/pystino-gateway:$ver" pystino "$@"
"""

PROXY_README = """# Component hook (report §2, §9). Any *.caddy file here is imported into the
# public site block, above the built-in routes. It is a directory mount, so a
# new or replaced file is seen on the next proxy reload without the stale-inode
# trap a single-file mount has. Reload: docker compose exec proxy caddy reload
# --config /etc/caddy/Caddyfile
"""


def _deploy_dir(args: argparse.Namespace) -> Path:
    raw = getattr(args, "dir", None) or os.environ.get("PYSTINO_DEPLOY_DIR") or os.getcwd()
    return Path(raw).resolve()


def _ask(prompt: str, default: str = "", *, secret: bool = False) -> str:
    if not sys.stdin.isatty():
        return default
    suffix = f" [{default}]" if default and not secret else ""
    reader = getpass.getpass if secret else input
    answer = reader(f"{prompt}{suffix}: ").strip()
    return answer or default


def _git_revision(path: Path) -> str:
    try:
        sha = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{sha}-dirty" if dirty else sha


def cmd_init(args: argparse.Namespace) -> int:
    deploy_dir = _deploy_dir(args)
    env_path = deploy_dir / ".env"
    if env_path.exists() and not args.force:
        print(
            f"{env_path} exists; init never overwrites a deployment (use `pystino set`, or --force)"
        )
        return 1

    origin = args.origin or _ask("Public origin (https://host[:port])")
    admin_email = args.admin_email or _ask(
        "Administrator email (the first OIDC login with it becomes admin)"
    )
    pystino_src = Path(args.pystino_src).resolve() if args.pystino_src else None
    if args.mode == "dev" and pystino_src is None:
        pystino_src = stackfiles.source_checkout()
    cerea_src = Path(args.cerea_src).resolve() if args.cerea_src else None

    options = InitOptions(
        origin=origin,
        admin_email=admin_email,
        preset=args.preset,
        mode=args.mode,
        tls=args.tls,
        idp=args.idp,
        project=args.project,
        deploy_dir=deploy_dir,
        pystino_src=pystino_src,
        cerea_src=cerea_src,
        build_revision=_git_revision(pystino_src) if pystino_src else "unknown",
        http_port=args.http_port,
        https_port=args.https_port,
        gateway_port=args.gateway_port,
        acme_email=args.acme_email or "",
        admin_user=args.admin_user,
        admin_name=args.admin_name or "",
        oidc_issuer=args.oidc_issuer or "",
        oidc_internal_base_url=args.oidc_internal_base_url or "",
        oidc_console_client_secret=os.environ.get("PYSTINO_OIDC_CONSOLE_CLIENT_SECRET", ""),
        oidc_chat_client_secret=os.environ.get("PYSTINO_OIDC_CHAT_CLIENT_SECRET", ""),
        upstream_base_url=args.upstream_base_url,
        # Read from the environment or a prompt, never from argv: argv is
        # visible to every process on the host.
        upstream_api_key=os.environ.get("PYSTINO_UPSTREAM_API_KEY")
        or _ask("Upstream provider API key (empty: add providers in the console)", secret=True),
        cerea_image=args.cerea_image or "",
    )
    try:
        result = build_env(options, stackfiles.release())
    except (InitError, ValueError) as exc:
        print(f"init: {exc}")
        return 2

    envfile.write_atomic(env_path, envfile.render(result.sections))
    (deploy_dir / "proxy.d").mkdir(exist_ok=True)
    readme = deploy_dir / "proxy.d" / "00-readme.caddy"
    if not readme.exists():
        readme.write_text(PROXY_README, encoding="utf-8")
    if args.mode == "dist":
        shutil.copyfile(stackfiles.stack_dir() / "compose.yaml", deploy_dir / "compose.yaml")
    shim = deploy_dir / "pystino"
    shim.write_text(SHIM, encoding="utf-8")
    shim.chmod(0o755)

    print(
        f"wrote {env_path} (mode 0600), proxy.d/, ./pystino"
        + (", compose.yaml" if args.mode == "dist" else "")
    )
    if result.admin_password:
        print("")
        print(f"  First sign-in:  {options.admin_user} / {result.admin_password}")
        print("  Shown once and stored nowhere in plaintext — keep it now.")
    print("")
    print(f"Next:  cd {deploy_dir} && ./pystino doctor && docker compose up -d --wait")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    return bootstrap.run()


def cmd_doctor(args: argparse.Namespace) -> int:
    report = doctor.check(_deploy_dir(args), probe_docker=not args.no_docker)
    for line in report.errors:
        print(f"ERROR   {line}")
    for line in report.warnings:
        print(f"WARN    {line}")
    for line in report.notes:
        print(f"note    {line}")
    print("ok" if report.ok else f"{len(report.errors)} error(s)")
    return 0 if report.ok else 1


def cmd_set(args: argparse.Namespace) -> int:
    env_path = _deploy_dir(args) / ".env"
    changes: dict[str, str] = {}
    for item in args.assignments:
        key, sep, value = item.partition("=")
        if not sep:
            print(f"set: expected KEY=VALUE, got {item!r}")
            return 2
        changes[key.strip()] = value
    try:
        envfile.update(env_path, changes)
    except envfile.EnvFileError as exc:
        print(f"set: {exc}")
        return 2
    print(f"updated {', '.join(changes)}; apply with `docker compose up -d --wait`")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pystino", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write .env for a new deployment")
    init.add_argument("--dir", help="deployment directory (default: current)")
    init.add_argument("--origin", help="public origin, https://host[:port]")
    init.add_argument("--admin-email")
    init.add_argument("--preset", default="team", choices=sorted(presets.PRESETS))
    init.add_argument("--mode", default="dist", choices=("dev", "dist"))
    init.add_argument("--tls", default="acme", choices=TLS_MODES)
    init.add_argument("--idp", default="authelia", choices=("authelia", "external"))
    init.add_argument("--project", default="pystino", help="compose project name")
    init.add_argument("--pystino-src", help="Pystino checkout (dev mode; default: this one)")
    init.add_argument("--cerea-src", help="Cerea checkout to build the chat from (dev mode)")
    init.add_argument("--cerea-image", help="use this chat image instead of the pinned one")
    init.add_argument("--http-port", type=int)
    init.add_argument("--https-port", type=int)
    init.add_argument("--gateway-port", type=int, default=8000)
    init.add_argument("--acme-email")
    init.add_argument("--admin-user", default="admin")
    init.add_argument("--admin-name")
    init.add_argument("--oidc-issuer", help="external IdP issuer")
    init.add_argument(
        "--oidc-internal-base-url", help="external IdP back-channel URL, if not the issuer"
    )
    init.add_argument("--upstream-base-url", default="https://api.cortecs.ai/v1")
    init.add_argument("--force", action="store_true", help="overwrite an existing .env")
    init.set_defaults(func=cmd_init)

    boot = sub.add_parser("bootstrap", help="(inside the stack) converge DB and IdP state")
    boot.set_defaults(func=cmd_bootstrap)

    doc = sub.add_parser("doctor", help="check a deployment directory")
    doc.add_argument("--dir")
    doc.add_argument(
        "--no-docker", action="store_true", help="skip the docker compose version probe"
    )
    doc.set_defaults(func=cmd_doctor)

    setp = sub.add_parser("set", help="change values in .env")
    setp.add_argument("--dir")
    setp.add_argument("assignments", nargs="+", metavar="KEY=VALUE")
    setp.set_defaults(func=cmd_set)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
