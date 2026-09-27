"""`pystino bootstrap`: the idempotent one-shot every `up` runs.

It replaces the installer's phase 2 and its resume memory. Each step converges
the stack towards `.env` and is safe to repeat:

- the chat's Postgres role and database: created if missing, the role's
  password re-asserted every time (so a rotated `CHAT_PG_PASSWORD` simply
  applies), and the `vector` extension created in the chat database — which
  the chat's own schema needs and, not being a superuser, cannot create;
- Authelia's signing key and first-user file: created **only if absent**. They
  are state (the key signs every token, the users file is the directory), and
  nothing here ever overwrites state.
- Authelia's SMTP notifier password (§8.5): not state, a credential — written
  every run so a rotated `SMTP_PASSWORD` actually rotates it, the same
  reasoning as the Postgres role's password above.

Exit status is the contract: non-zero stops every service that depends on it,
so a bad `.env` fails the stack closed instead of half-starting it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("pystino.bootstrap")

AUTHELIA_DIR = Path(os.environ.get("PYSTINO_AUTHELIA_DIR", "/authelia"))
_ROLE_NAME = "chat"
_DB_NAME = "chat"
_USER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class BootstrapEnv:
    profiles: frozenset[str]
    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str
    chat_pg_password: str
    authelia_admin_user: str
    authelia_admin_email: str
    authelia_admin_name: str
    authelia_admin_password_digest: str
    smtp_password: str

    @classmethod
    def from_environ(cls, environ: dict[str, str] | None = None) -> BootstrapEnv:
        env = dict(os.environ if environ is None else environ)
        profiles = frozenset(
            p.strip() for p in env.get("COMPOSE_PROFILES", "").split(",") if p.strip()
        )
        return cls(
            profiles=profiles,
            postgres_user=env.get("POSTGRES_USER", "gateway"),
            postgres_password=env.get("POSTGRES_PASSWORD", ""),
            postgres_db=env.get("POSTGRES_DB", "gateway"),
            postgres_host=env.get("POSTGRES_HOST", "postgres"),
            chat_pg_password=env.get("CHAT_PG_PASSWORD", ""),
            authelia_admin_user=env.get("AUTHELIA_ADMIN_USER", ""),
            authelia_admin_email=env.get("AUTHELIA_ADMIN_EMAIL", ""),
            authelia_admin_name=env.get("AUTHELIA_ADMIN_NAME", ""),
            authelia_admin_password_digest=env.get("AUTHELIA_ADMIN_PASSWORD_DIGEST", ""),
            smtp_password=env.get("SMTP_PASSWORD", ""),
        )

    def problems(self) -> list[str]:
        """Everything wrong with the environment, all at once."""
        found: list[str] = []
        if not self.postgres_password:
            found.append("POSTGRES_PASSWORD is empty")
        if "chat" in self.profiles and not self.chat_pg_password:
            found.append("the chat profile is on but CHAT_PG_PASSWORD is empty")
        if "authelia" in self.profiles:
            if not _USER.match(self.authelia_admin_user):
                found.append("AUTHELIA_ADMIN_USER must be a lowercase login name")
            if "@" not in self.authelia_admin_email:
                found.append("AUTHELIA_ADMIN_EMAIL must be an email address")
            # argon2id from an installer-era deployment; SHA512-crypt ($6$) and
            # PBKDF2 ($pbkdf2-sha512$, $pbkdf2-sha256$, $pbkdf2$) from
            # ./configure, which has no argon2 dependency (ADR 0091 decision
            # 4). Authelia verifies all of them.
            if not self.authelia_admin_password_digest.startswith(
                ("$argon2", "$6$", "$pbkdf2-sha512$", "$pbkdf2-sha256$", "$pbkdf2$")
            ):
                found.append(
                    "AUTHELIA_ADMIN_PASSWORD_DIGEST must be an argon2, $6$ or $pbkdf2 digest"
                )
        return found


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def ensure_chat_database(env: BootstrapEnv) -> str:
    """Role, database and extension for the chat. Returns what it did."""
    import asyncpg  # the gateway image's own driver; imported late for tests

    async def connect(database: str) -> asyncpg.Connection:
        return await asyncpg.connect(
            host=env.postgres_host,
            user=env.postgres_user,
            password=env.postgres_password,
            database=database,
            timeout=10,
        )

    conn = await connect(env.postgres_db)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", _ROLE_NAME)
        verb = "ALTER" if exists else "CREATE"
        # Role DDL takes no bind parameters; the literal is quoted, not
        # interpolated raw.
        await conn.execute(
            f"{verb} ROLE {_ROLE_NAME} WITH LOGIN PASSWORD {_quote_literal(env.chat_pg_password)}"
        )
        has_db = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", _DB_NAME)
        if not has_db:
            # CREATE DATABASE cannot run in a transaction; asyncpg's execute
            # outside one is exactly that.
            await conn.execute(f"CREATE DATABASE {_DB_NAME} OWNER {_ROLE_NAME}")
    finally:
        await conn.close()

    chat = await connect(_DB_NAME)
    try:
        await chat.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await chat.close()
    role = "updated" if exists else "created"
    database = "present" if has_db else "created"
    return f"chat role {role}, database {database}, vector extension ensured"


def _yaml_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def users_file_text(user: str, name: str, email: str, digest: str) -> str:
    return (
        "# Authelia's user directory (file backend, hot-reloaded).\n"
        "# Created once by `pystino bootstrap` with the first administrator and\n"
        "# never rewritten by it: from here on this file is data, not config.\n"
        "users:\n"
        f"  {user}:\n"
        "    disabled: false\n"
        f"    displayname: {_yaml_str(name or user)}\n"
        f"    password: {_yaml_str(digest)}\n"
        f"    email: {_yaml_str(email)}\n"
        "    groups:\n"
        "      - 'users'\n"
    )


def _write_new(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Create a file that must not exist yet; never replace one that does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def ensure_authelia_state(env: BootstrapEnv, directory: Path = AUTHELIA_DIR) -> list[str]:
    """The signing key and the first-user file, each only if absent."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    done: list[str] = []
    key_path = directory / "keys" / "jwks.pem"
    if key_path.exists():
        done.append("signing key present (kept)")
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        _write_new(key_path, pem)
        done.append("signing key created")

    users_path = directory / "users_database.yml"
    # The same sidecar lock the console's own writes take (ADR 0093 §8.4): a
    # bootstrap racing a console write — two `compose up`s, or a retried
    # one-shot service — is the concurrent-writer case the lock exists for,
    # not a special case exempt from it. The exists-check still runs first,
    # under the lock, so two bootstraps racing each other never both pass it
    # and both call `_write_new` (which would raise on the second `O_EXCL`
    # anyway, but only after the first has already finished writing).
    from gateway.directory.authelia_users import locked_users_file

    with locked_users_file(users_path):
        if users_path.exists():
            done.append("users file present (kept)")
        else:
            text = users_file_text(
                env.authelia_admin_user,
                env.authelia_admin_name,
                env.authelia_admin_email,
                env.authelia_admin_password_digest,
            )
            _write_new(users_path, text.encode("utf-8"))
            done.append(f"users file created with {env.authelia_admin_user}")
    return done


def ensure_smtp_password_file(env: BootstrapEnv, directory: Path = AUTHELIA_DIR) -> str | None:
    """The SMTP password Authelia's notifier reads through its own
    ``AUTHELIA_NOTIFIER_SMTP_PASSWORD_FILE`` secret convention (§8.5) —
    written here, never interpolated into ``configuration.yml``.

    Unlike the signing key and the users file, this is a credential, not
    identity-bearing state: rewritten every run so rotating ``SMTP_PASSWORD``
    in ``.env`` and redeploying actually rotates it, rather than the first
    value silently sticking forever. An empty ``SMTP_PASSWORD`` (SMTP off,
    or never configured) leaves whatever is there alone — nothing reads an
    unused file, and there is nothing safe to infer about deleting it from
    the environment simply lacking a value this run.
    """
    if not env.smtp_password:
        return None
    path = directory / "keys" / "smtp_password"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(env.smtp_password)
        handle.flush()
        os.fsync(handle.fileno())
    # `O_CREAT`'s mode is filtered by umask; a pre-existing file could also
    # already carry different permissions from an older bootstrap. Setting it
    # explicitly is what actually guarantees 0600 either way.
    os.chmod(path, 0o600)
    return "SMTP password file written"


AUTHELIA_DATA_DIR = Path(os.environ.get("PYSTINO_AUTHELIA_DATA_DIR", "/authelia-data"))
#: The gateway's uid/gid in its image, which Authelia also runs as (compose PUID).
SHARED_UID = 1001


def share_authelia_volumes(*directories: Path, uid: int = SHARED_UID) -> list[str]:
    """Hand the Authelia volumes to the uid the gateway and Authelia share.

    The console writes the users file (D10), so the gateway (uid 1001) must own
    it; Authelia runs as the same uid. Volumes an older install created as
    root (an adopted authelia-data, say) are re-owned here, every `up` —
    idempotent, and only when bootstrap runs as root.
    """
    if os.geteuid() != 0:
        return []
    done = []
    for directory in directories:
        if not directory.exists():
            continue
        for path in [directory, *directory.rglob("*")]:
            os.chown(path, uid, uid, follow_symlinks=False)
        done.append(f"{directory} owned by {uid}")
    return done


def run(environ: dict[str, str] | None = None, authelia_dir: Path = AUTHELIA_DIR) -> int:
    env = BootstrapEnv.from_environ(environ)
    problems = env.problems()
    if problems:
        for problem in problems:
            print(f"bootstrap: {problem}")
        print("bootstrap: refusing to continue — fix .env and run `docker compose up -d` again")
        return 2
    try:
        if "chat" in env.profiles:
            print(f"bootstrap: {asyncio.run(ensure_chat_database(env))}")
        if "authelia" in env.profiles:
            for line in ensure_authelia_state(env, authelia_dir):
                print(f"bootstrap: authelia {line}")
            if (smtp_line := ensure_smtp_password_file(env, authelia_dir)) is not None:
                print(f"bootstrap: authelia {smtp_line}")
            for line in share_authelia_volumes(authelia_dir, AUTHELIA_DATA_DIR):
                print(f"bootstrap: {line}")
    except Exception as exc:  # the exit code is the contract; say why first
        print(f"bootstrap: failed: {exc}")
        return 1
    print("bootstrap: done")
    return 0
