"""The `pystino` command line: bootstrap, release-pin, admin.

Every command takes its answers as flags so CI and scripts never meet a prompt.

Writing `.env` and standing up a deployment is no longer this CLI's job
(ADR 0091): `deploy/.env.example` in this repository (a Pystino-only
deployment) and the separate `cerea-deploy` repository (the full stack, with
its own `./configure`) are what an operator reads and edits directly. What
stays here runs *inside* a deployment: `bootstrap` (the compose one-shot on
every `up`) and `admin grant|revoke` (break-glass), plus `release-pin`, which
a release of this repository runs against its own checkout.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from gateway.deploy import bootstrap, envfile

#: This file is apps/gateway/src/gateway/deploy/cli.py; the repository root
#: (holding deploy/release.env) is five levels up.
ROOT = Path(__file__).resolve().parents[5]


def cmd_bootstrap(args: argparse.Namespace) -> int:
    return bootstrap.run()


def cmd_admin(args: argparse.Namespace) -> int:
    import asyncio

    from gateway.config import get_settings
    from gateway.db import create_engine, create_session_factory
    from gateway.deploy.admin import AdminCommandError, set_admin

    async def run() -> int:
        engine = create_engine(get_settings())
        try:
            async with create_session_factory(engine)() as session:
                user = await set_admin(
                    session, args.email, grant=args.action == "grant", issuer=args.issuer or ""
                )
        except AdminCommandError as exc:
            print(f"admin: {exc}")
            return 1
        finally:
            await engine.dispose()
        state = "is now" if user.is_admin else "is no longer"
        print(f"{user.email} ({user.issuer}) {state} an administrator")
        return 0

    return asyncio.run(run())


def cmd_release_pin(args: argparse.Namespace) -> int:
    from gateway.deploy import release

    path = Path(args.manifest) if args.manifest else ROOT / "deploy" / "release.env"
    if args.check:
        missing = release.unpinned(envfile.read(path))
        for key in missing:
            print(f"unpinned: {key}")
        return 1 if missing else 0
    try:
        changes = release.pin_file(path)
    except (subprocess.SubprocessError, ValueError, OSError) as exc:
        print(f"release-pin: {exc} (is this host logged in to the registry?)")
        return 1
    for key, value in changes.items():
        print(f"{key}={value}")
    print(f"pinned {len(changes)} image(s) in {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pystino", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    boot = sub.add_parser("bootstrap", help="(inside the stack) converge DB and IdP state")
    boot.set_defaults(func=cmd_bootstrap)

    rp = sub.add_parser("release-pin", help="pin the release manifest's images by digest")
    rp.add_argument("--manifest", help="default: this checkout's deploy/release.env")
    rp.add_argument("--check", action="store_true", help="fail if any image is unpinned")
    rp.set_defaults(func=cmd_release_pin)

    adm = sub.add_parser("admin", help="(inside the gateway) break-glass admin grant/revoke")
    adm.add_argument("action", choices=("grant", "revoke"))
    adm.add_argument("email")
    adm.add_argument("--issuer", help="when the email names accounts at several issuers")
    adm.set_defaults(func=cmd_admin)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
