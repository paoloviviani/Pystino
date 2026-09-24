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

from gateway.deploy import (
    adopt,
    bootstrap,
    doctor,
    envfile,
    presets,
    registry,
    stackfiles,
    upgrade,
)
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
image="$reg/pystino-gateway:$ver"
if ! docker image inspect "$image" >/dev/null 2>&1 && ! docker pull -q "$image" >/dev/null; then
    echo "Could not pull $image." >&2
    case "$reg" in ghcr.io/*)
        echo "The images are private while the repositories are: log in once with a token" >&2
        echo "holding read:packages:  docker login ghcr.io -u <github-user>" >&2;;
    esac
    exit 1
fi
tty=""
if [ -t 0 ]; then tty="-it"; fi
exec docker run --rm $tty -u "$(id -u):$(id -g)" -v "$PWD:/deploy" -w /deploy \\
    -e PYSTINO_DEPLOY_DIR="$PWD" "$image" "${PYSTINO_CLI:-pystino}" "$@"
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


def _write_shim(deploy_dir: Path, name: str) -> None:
    """`./pystino` (or `./cerea`, same script, branded CLI) in the deploy dir."""
    text = (
        SHIM
        if name == "pystino"
        else SHIM.replace("set -eu\n", f'set -eu\nPYSTINO_CLI="${{PYSTINO_CLI:-{name}}}"\n', 1)
    )
    shim = deploy_dir / name
    shim.write_text(text, encoding="utf-8")
    shim.chmod(0o755)


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
        central_url=args.central_url or "",
        upstream_base_url=args.upstream_base_url,
        # Read from the environment or a prompt, never from argv: argv is
        # visible to every process on the host.
        upstream_api_key=os.environ.get("PYSTINO_UPSTREAM_API_KEY")
        or _ask("Upstream provider API key (empty: add providers in the console)", secret=True),
        cerea_image=args.cerea_image or "",
        agents=args.agents,
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
    _write_shim(deploy_dir, args.prog_name)

    print(
        f"wrote {env_path} (mode 0600), proxy.d/, ./{args.prog_name}"
        + (", compose.yaml" if args.mode == "dist" else "")
    )
    if result.admin_password:
        print("")
        print(f"  First sign-in:  {options.admin_user} / {result.admin_password}")
        print("  Shown once and stored nowhere in plaintext — keep it now.")
    if args.mode == "dist" and stackfiles.stack_dir() != stackfiles.IMAGE_STACK_DIR:
        written = envfile.read(env_path)
        note = registry.hint(written.get("PYSTINO_REGISTRY", ""))
        if note:
            print("")
            print(note)
    print("")
    print(f"Next:  cd {deploy_dir} && ./{args.prog_name} doctor && docker compose up -d --wait")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    if args.import_only:
        for line in bootstrap.import_authelia_state(Path(args.import_only)):
            print(f"bootstrap: authelia {line}")
        return 0
    return bootstrap.run()


def cmd_adopt(args: argparse.Namespace) -> int:
    new_dir = _deploy_dir(args)
    if (new_dir / ".env").exists() and not args.force:
        print(f"{new_dir / '.env'} exists; adopt never overwrites a deployment (--force)")
        return 1
    options = adopt.AdoptOptions(
        old_deploy_dir=Path(args.old_deploy_dir).resolve(),
        new_deploy_dir=new_dir,
        tls=args.tls,
        mode=args.mode,
        project=args.project,
        pystino_src=Path(args.pystino_src).resolve() if args.pystino_src else None,
        cerea_src=Path(args.cerea_src).resolve() if args.cerea_src else None,
        http_port=args.http_port,
        https_port=args.https_port,
    )
    try:
        result = adopt.build(options, stackfiles.release())
    except (adopt.AdoptError, envfile.EnvFileError) as exc:
        print(f"adopt: {exc}")
        return 2
    envfile.write_atomic(new_dir / ".env", envfile.render(result.sections))
    for relative, data in result.staged.items():
        target = new_dir / "adopt" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        envfile.write_atomic(target, data.decode("utf-8"))
    (new_dir / "proxy.d").mkdir(exist_ok=True)
    readme = new_dir / "proxy.d" / "00-readme.caddy"
    if not readme.exists():
        readme.write_text(PROXY_README, encoding="utf-8")
    if args.mode == "dist":
        shutil.copyfile(stackfiles.stack_dir() / "compose.yaml", new_dir / "compose.yaml")
    _write_shim(new_dir, "pystino")
    print(f"wrote {new_dir / '.env'} from {options.old_deploy_dir} (nothing there was changed)")
    for note in result.notes:
        print(f"note: {note}")
    print("")
    print("Cutover — nothing above has touched the running stack; these do:")
    print(f"  cd {new_dir} && ./pystino doctor")
    print("  # 1. stop the old stack WITHOUT -v (volumes are the data), from its old directory")
    if result.staged:
        print("  # 2. put the carried users file and signing key into the new volume:")
        print(
            '  docker compose run --rm --no-deps -v "$PWD/adopt/authelia-config:/import:ro" '
            "bootstrap pystino bootstrap --import-only /import"
        )
    print("  # 3. docker compose up -d --wait")
    print("  # back: docker compose down (no -v) here, then start the old stack as before")
    return 0


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


def cmd_upgrade(args: argparse.Namespace) -> int:
    deploy_dir = _deploy_dir(args)
    try:
        change = upgrade.plan(
            deploy_dir, args.version, stackfiles.release(), keep_overrides=args.keep_overrides
        )
    except (upgrade.UpgradeError, envfile.EnvFileError, FileNotFoundError) as exc:
        print(f"upgrade: {exc}")
        return 2
    print(f"upgrade {change.old_version} -> {change.new_version}")
    for key, value in sorted(change.changes.items()):
        print(f"  {key}={value}")
    for key, value in sorted(change.kept_overrides.items()):
        print(f"  {key}={value} (your override, kept)")
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    for path in upgrade.apply(deploy_dir, change):
        print(f"wrote {path}")
    print("")
    print("Next:  snapshot the volumes, then  docker compose pull && docker compose up -d --wait")
    print(f"Back:  restore compose.yaml.bak-{change.old_version} and .env.bak-{change.old_version}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pystino", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write .env for a new deployment")
    _add_init_arguments(init, default_preset="team", choices=sorted(presets.PRESETS))
    init.set_defaults(func=cmd_init, prog_name="pystino")

    _add_common_commands(sub, prog_name="pystino")
    return parser


def _add_init_arguments(
    init: argparse.ArgumentParser, *, default_preset: str | None, choices: list[str]
) -> None:
    init.add_argument("--dir", help="deployment directory (default: current)")
    init.add_argument("--origin", help="public origin, https://host[:port]")
    init.add_argument("--admin-email")
    init.add_argument("--preset", default=default_preset, choices=choices)
    init.add_argument("--central-url", help="satellite preset: the central Pystino's origin")
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
    init.add_argument(
        "--agents", action="store_true", help="enable agent machines (the /code panel)"
    )
    init.add_argument("--force", action="store_true", help="overwrite an existing .env")


def _add_common_commands(sub, *, prog_name: str) -> None:
    boot = sub.add_parser("bootstrap", help="(inside the stack) converge DB and IdP state")
    boot.add_argument(
        "--import-only",
        metavar="DIR",
        help="copy an adopted install's Authelia users file and key where absent, then exit",
    )
    boot.set_defaults(func=cmd_bootstrap)

    ad = sub.add_parser("adopt", help="carry an installer-made deployment over (reads only)")
    ad.add_argument("old_deploy_dir", help="the old install's deploy/ directory")
    ad.add_argument("--dir", help="new deployment directory (default: current)")
    ad.add_argument("--tls", required=True, choices=TLS_MODES, help="edge shape = upstream")
    ad.add_argument("--mode", default="dist", choices=("dev", "dist"))
    ad.add_argument("--project", default=adopt.OLD_PROJECT)
    ad.add_argument("--pystino-src")
    ad.add_argument("--cerea-src")
    ad.add_argument("--http-port", type=int)
    ad.add_argument("--https-port", type=int)
    ad.add_argument("--force", action="store_true")
    ad.set_defaults(func=cmd_adopt)

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
    up = sub.add_parser("upgrade", help="move a distributed install to another release")
    up.add_argument("version", help="target release, e.g. 1.5.0")
    up.add_argument("--dir")
    up.add_argument("--dry-run", action="store_true")
    up.add_argument(
        "--keep-overrides",
        action="store_true",
        help="keep a CEREA_VERSION you moved off the manifest on purpose",
    )
    up.set_defaults(func=cmd_upgrade)


def build_cerea_parser() -> argparse.ArgumentParser:
    """`cerea`: the same commands, for a Cerea-only install.

    Cerea is its own product with its own name; a person setting up the chat
    should not have to know it is deployed by a tool named after the gateway.
    `cerea init` offers only the gateway-less presets and picks between them:
    `--central-url` means satellite (against a central Pystino), otherwise
    generic (any OpenAI-compatible endpoint).
    """
    parser = argparse.ArgumentParser(
        prog="cerea", description="Set up and manage a Cerea deployment (the chat, standalone)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="write .env for a new Cerea deployment")
    _add_init_arguments(init, default_preset=None, choices=["satellite", "generic"])
    init.set_defaults(func=cmd_init, prog_name="cerea")
    _add_common_commands(sub, prog_name="cerea")
    return parser


def cerea_main(argv: list[str] | None = None) -> int:
    args = build_cerea_parser().parse_args(argv)
    if args.command == "init" and args.preset is None:
        args.preset = "satellite" if args.central_url else "generic"
    return int(args.func(args))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
