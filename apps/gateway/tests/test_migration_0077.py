# ruff: noqa: S608  (fixed SQL built from test constants)
"""Migration 0077: bring your own key (ownership columns, name uniqueness, ledger flag).

The properties worth pinning on SQLite (PostgreSQL is covered by
``test_migration_0077_postgres.py``, which runs when ``TEST_DATABASE_URL`` is
set):

* every existing row is a deployment row, nothing is backfilled and no group
  gains the capability;
* the globally unique name index became two partial ones: a deployment name is
  still unique among deployment rows, a private name is unique per owner, and a
  private name may equal a deployment one;
* the check constraints keep the owner pair consistent and a private model never
  an alias;
* the downgrade refuses while private rows exist (restoring a global unique name
  could collide them) and otherwise restores the old indexes.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest
from test_migration_0067 import _alembic

T0 = "2026-11-03 12:00:00.000000"


def _id() -> str:
    return uuid.uuid4().hex


def _user(conn: sqlite3.Connection, subject: str) -> str:
    uid = _id()
    conn.execute(
        "INSERT INTO users (id, issuer, subject, created_at, updated_at) "
        f"VALUES ('{uid}', 'https://idp.test', '{subject}', '{T0}', '{T0}')"
    )
    return uid


def _provider(conn: sqlite3.Connection, name: str, owner: str | None = None) -> str:
    pid = _id()
    extra = ", owner_kind, owner_id" if owner else ""
    values = f", 'user', '{owner}'" if owner else ""
    conn.execute(
        "INSERT INTO providers (id, name, base_url, api_key_encrypted, api_key_hint, "
        f"extra_headers, created_at, updated_at{extra}) "
        f"VALUES ('{pid}', '{name}', 'https://x.example/v1', '', '', '{{}}', "
        f"'{T0}', '{T0}'{values})"
    )
    return pid


def _model(conn: sqlite3.Connection, name: str, provider: str, owner: str | None = None) -> str:
    mid = _id()
    extra = ", owner_kind, owner_id" if owner else ""
    values = f", 'user', '{owner}'" if owner else ""
    conn.execute(
        "INSERT INTO models (id, name, provider_id, upstream_model, created_at, updated_at"
        f"{extra}) VALUES ('{mid}', '{name}', '{provider}', 'up', '{T0}', '{T0}'{values})"
    )
    return mid


def _upgrade(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0076").returncode == 0
    return path, url


def test_existing_rows_stay_deployment_rows_and_no_group_gains_the_capability(
    tmp_path: Path,
) -> None:
    path, url = _upgrade(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        provider = _provider(conn, "p")
        _model(conn, "m", provider)
        conn.execute(
            "INSERT INTO groups (id, name, source, is_active, created_at, updated_at, "
            f"code_agents_allowed, api_keys_allowed) VALUES ('{_id()}', 'g', 'manual', 1, "
            f"'{T0}', '{T0}', 1, 1)"
        )
        conn.execute(
            "INSERT INTO usage_records (id, request_id, status, model_name, streamed, "
            "prompt_tokens, completion_tokens, total_tokens, cached_prompt_tokens, "
            "reasoning_tokens, usage_source, cost, currency, redacted_entity_count, "
            f"created_at, updated_at) VALUES ('{_id()}', 'r', 'completed', 'm', 0, 0, 0, 0, 0, 0, "
            f"'unavailable', 0, 'EUR', 0, '{T0}', '{T0}')"
        )
        conn.commit()

    up = _alembic(url, "upgrade", "0077")
    assert up.returncode == 0, up.stderr[-2000:]

    with closing(sqlite3.connect(path)) as conn:
        for table in ("providers", "models"):
            # (the extractor's rows, seeded by 0041, are among them)
            rows = conn.execute(
                f"SELECT owner_kind, owner_id, admin_disabled FROM {table}"
            ).fetchall()
            assert rows and set(rows) == {(None, None, 0)}
        assert conn.execute("SELECT byok_allowed FROM groups").fetchall() == [(0,)]
        assert conn.execute("SELECT byok FROM usage_records").fetchall() == [(0,)]
        oidc = {r[1] for r in conn.execute("PRAGMA table_info(oidc_config)")}
        assert {"byok_mode", "byok_private_networks"} <= oidc
        indexes = {
            r[1]: r[2] for r in conn.execute("PRAGMA index_list(models)")
        }  # name -> unique flag
        assert indexes["ix_models_name"] == 0
        assert indexes["uq_models_name_deployment"] == 1
        assert indexes["uq_models_owner_name"] == 1
        assert "ix_models_owner" in indexes


def test_names_are_unique_among_deployment_rows_and_per_owner(tmp_path: Path) -> None:
    path, url = _upgrade(tmp_path)
    assert _alembic(url, "upgrade", "0077").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        alice, bob = _user(conn, "alice"), _user(conn, "bob")
        deployment = _provider(conn, "shared")
        # A private row may carry a deployment row's name, and two owners may share one.
        a_provider = _provider(conn, "shared", alice)
        b_provider = _provider(conn, "shared", bob)
        _model(conn, "m", deployment)
        _model(conn, "m", a_provider, alice)
        _model(conn, "m", b_provider, bob)
        conn.commit()
        # But a deployment name stays globally unique among deployment rows...
        with pytest.raises(sqlite3.IntegrityError):
            _provider(conn, "shared")
        with pytest.raises(sqlite3.IntegrityError):
            _model(conn, "m", deployment)
        # ...and an owner cannot repeat one.
        with pytest.raises(sqlite3.IntegrityError):
            _provider(conn, "shared", alice)
        with pytest.raises(sqlite3.IntegrityError):
            _model(conn, "m", a_provider, alice)
        conn.rollback()


def test_check_constraints_keep_the_owner_pair_consistent_and_private_models_unaliased(
    tmp_path: Path,
) -> None:
    path, url = _upgrade(tmp_path)
    assert _alembic(url, "upgrade", "0077").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        owner = _user(conn, "alice")
        provider = _provider(conn, "p")
        for statement in (
            # a kind without an owner, an owner without a kind, and an unknown kind
            f"INSERT INTO providers (id, name, base_url, api_key_encrypted, api_key_hint, "
            f"extra_headers, created_at, updated_at, owner_kind) VALUES ('{_id()}', 'a', 'u', "
            f"'', '', '{{}}', '{T0}', '{T0}', 'user')",
            f"INSERT INTO providers (id, name, base_url, api_key_encrypted, api_key_hint, "
            f"extra_headers, created_at, updated_at, owner_id) VALUES ('{_id()}', 'b', 'u', "
            f"'', '', '{{}}', '{T0}', '{T0}', '{owner}')",
            f"INSERT INTO providers (id, name, base_url, api_key_encrypted, api_key_hint, "
            f"extra_headers, created_at, updated_at, owner_kind, owner_id) VALUES ('{_id()}', "
            f"'c', 'u', '', '', '{{}}', '{T0}', '{T0}', 'team', '{owner}')",
            # a private alias
            f"INSERT INTO models (id, name, is_alias, created_at, updated_at, owner_kind, "
            f"owner_id) VALUES ('{_id()}', 'al', 1, '{T0}', '{T0}', 'user', '{owner}')",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)
        # An ordinary alias is unaffected.
        conn.execute(
            f"INSERT INTO models (id, name, is_alias, created_at, updated_at) "
            f"VALUES ('{_id()}', 'fast', 1, '{T0}', '{T0}')"
        )
        assert provider


def test_deleting_the_owner_cascades_to_their_rows(tmp_path: Path) -> None:
    """The ON DELETE CASCADE is a backstop (the erasure path deletes explicitly,
    models first), but where the database can honour it, it does."""
    path, url = _upgrade(tmp_path)
    assert _alembic(url, "upgrade", "0077").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        owner = _user(conn, "alice")
        provider = _provider(conn, "mine", owner)
        _model(conn, "m", provider, owner)
        conn.commit()
        # models.provider_id is RESTRICT, so the cascade needs the models to go first.
        conn.execute(f"DELETE FROM models WHERE owner_id = '{owner}'")
        conn.execute(f"DELETE FROM users WHERE id = '{owner}'")
        assert conn.execute(
            "SELECT count(*) FROM providers WHERE owner_id IS NOT NULL"
        ).fetchone() == (0,)


def test_downgrade_refuses_while_private_rows_exist_then_restores_the_old_indexes(
    tmp_path: Path,
) -> None:
    path, url = _upgrade(tmp_path)
    assert _alembic(url, "upgrade", "0077").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        owner = _user(conn, "alice")
        provider = _provider(conn, "mine", owner)
        conn.commit()

    refused = _alembic(url, "downgrade", "0076")
    assert refused.returncode != 0
    assert "private" in refused.stderr.lower()

    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"DELETE FROM providers WHERE id = '{provider}'")
        conn.commit()
    down = _alembic(url, "downgrade", "0076")
    assert down.returncode == 0, down.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        for table in ("providers", "models"):
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert not columns & {"owner_kind", "owner_id", "admin_disabled"}
            indexes = {r[1]: r[2] for r in conn.execute(f"PRAGMA index_list({table})")}
            assert indexes[f"ix_{table}_name"] == 1  # unique again
            assert f"uq_{table}_owner_name" not in indexes
        assert "byok" not in {r[1] for r in conn.execute("PRAGMA table_info(usage_records)")}
    assert _alembic(url, "upgrade", "head").returncode == 0


# -- PostgreSQL -----------------------------------------------------------------
#
# SQLite is more forgiving than PostgreSQL in the ways that matter here: partial
# unique indexes, CHECK-with-NULL and the two ON DELETE actions on one delete.
# Runs only with TEST_DATABASE_URL (a throwaway pgvector/pgvector:pg18).

_PG = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def scratch_postgres() -> Iterator[Callable[[], str]]:
    import asyncpg
    from conftest import _parse_admin_url

    parts = _parse_admin_url()
    name = f"t_mig0077_{uuid.uuid4().hex[:8]}"

    async def run(sql: str) -> None:
        conn = await asyncpg.connect(
            host=parts["host"], port=parts["port"], user=parts["user"], password=parts["password"]
        )
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(run(f"CREATE DATABASE {name}"))
    try:
        yield (
            lambda: (
                f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
                f"@{parts['host']}:{parts['port']}/{name}"
            )
        )
    finally:
        asyncio.run(run(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


@pytest.mark.skipif(not _PG, reason="TEST_DATABASE_URL is not set")
def test_postgres_constraints_cascades_and_downgrade(
    scratch_postgres: Callable[[], str],
) -> None:
    import asyncpg
    from conftest import _parse_admin_url

    url = scratch_postgres()
    parts = _parse_admin_url()
    database = url.rsplit("/", 1)[1]
    assert _alembic(url, "upgrade", "0076").returncode == 0
    up = _alembic(url, "upgrade", "0077")
    assert up.returncode == 0, up.stderr[-2000:]

    async def run(statements: list[str], expect_error: str | None = None) -> list[object]:
        conn = await asyncpg.connect(
            host=parts["host"],
            port=parts["port"],
            user=parts["user"],
            password=parts["password"],
            database=database,
        )
        try:
            out: list[object] = []
            for statement in statements:
                try:
                    out.append(await conn.fetch(statement))
                except asyncpg.PostgresError as exc:
                    if expect_error is None:
                        raise
                    assert expect_error in type(exc).__name__, (statement, exc)
                    return [type(exc).__name__]
            return out
        finally:
            await conn.close()

    alice, bob = str(uuid.uuid4()), str(uuid.uuid4())
    now = "now()"

    def user(uid: str, subject: str) -> str:
        return (
            "INSERT INTO users (id, issuer, subject, created_at, updated_at, is_admin, is_active) "
            f"VALUES ('{uid}', 'https://idp.test', '{subject}', {now}, {now}, false, true)"
        )

    def provider(name: str, owner: str | None = None, pid: str | None = None) -> str:
        kind = ", owner_kind, owner_id" if owner else ""
        values = f", 'user', '{owner}'" if owner else ""
        return (
            "INSERT INTO providers (id, name, base_url, api_key_encrypted, api_key_hint, "
            f"extra_headers, created_at, updated_at{kind}) VALUES "
            f"('{pid or uuid.uuid4()}', '{name}', 'https://x.example', '', '', '{{}}', "
            f"{now}, {now}{values})"
        )

    def model(name: str, provider_id: str, owner: str | None = None, mid: str | None = None) -> str:
        kind = ", owner_kind, owner_id" if owner else ""
        values = f", 'user', '{owner}'" if owner else ""
        return (
            "INSERT INTO models (id, name, provider_id, upstream_model, created_at, updated_at"
            f"{kind}) VALUES ('{mid or uuid.uuid4()}', '{name}', '{provider_id}', 'up', "
            f"{now}, {now}{values})"
        )

    deployment, a_provider, b_provider = (str(uuid.uuid4()) for _ in range(3))
    asyncio.run(
        run(
            [
                user(alice, "alice"),
                user(bob, "bob"),
                provider("shared", pid=deployment),
                provider("shared", alice, a_provider),
                provider("shared", bob, b_provider),
                model("m", deployment),
                model("m", a_provider, alice),
                model("m", b_provider, bob),
            ]
        )
    )
    # Uniqueness: deployment names among deployment rows, private names per owner.
    for statement in (
        provider("shared"),
        model("m", deployment),
        provider("shared", alice),
        model("m", a_provider, alice),
    ):
        asyncio.run(run([statement], expect_error="UniqueViolation"))
    # CHECK: an owner without a kind (NULL passes a naive check), a kind without an owner.
    bare = (
        "INSERT INTO providers (id, name, base_url, api_key_encrypted, api_key_hint, "
        "extra_headers, created_at, updated_at, {cols}) VALUES ('{id}', '{name}', 'u', '', '', "
        "'{{}}', now(), now(), {vals})"
    )
    for cols, vals in (("owner_id", f"'{alice}'"), ("owner_kind", "'user'")):
        asyncio.run(
            run(
                [bare.format(cols=cols, vals=vals, id=uuid.uuid4(), name=f"x-{cols}")],
                expect_error="CheckViolation",
            )
        )
    asyncio.run(
        run(
            [
                "INSERT INTO models (id, name, is_alias, created_at, updated_at, owner_kind, "
                f"owner_id) VALUES ('{uuid.uuid4()}', 'al', true, now(), now(), 'user', '{alice}')"
            ],
            expect_error="CheckViolation",
        )
    )
    # Deleting a user: the two ON DELETE CASCADEs and models.provider_id RESTRICT.
    # PostgreSQL gives no order between them, which is why the erasure path
    # deletes the models first; what it does here is recorded, not assumed.
    asyncio.run(
        run(
            [
                f"DELETE FROM models WHERE owner_id = '{bob}'",
                f"DELETE FROM users WHERE id = '{bob}'",
            ]
        )
    )
    remaining = asyncio.run(run(["SELECT count(*) FROM providers WHERE owner_id IS NOT NULL"]))
    assert [dict(r) for r in remaining[0]] == [{"count": 1}]  # type: ignore[union-attr]

    # Downgrade refuses while a private row exists, and leaves the schema alone.
    refused = _alembic(url, "downgrade", "0076")
    assert refused.returncode != 0 and "private" in refused.stderr.lower()
    still = asyncio.run(
        run(["SELECT count(*) FROM information_schema.columns WHERE column_name = 'byok_allowed'"])
    )
    assert [dict(r) for r in still[0]] == [{"count": 1}]  # type: ignore[union-attr]
    asyncio.run(
        run(
            [
                f"DELETE FROM models WHERE owner_id = '{alice}'",
                f"DELETE FROM providers WHERE owner_id = '{alice}'",
            ]
        )
    )
    down = _alembic(url, "downgrade", "0076")
    assert down.returncode == 0, down.stderr[-2000:]
    assert _alembic(url, "upgrade", "head").returncode == 0
