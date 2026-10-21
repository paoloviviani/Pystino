"""The whole migration chain runs on SQLite.

The unit suite builds its schema with ``create_all`` and a deployment migrates
PostgreSQL, so nothing else ran ``alembic upgrade head`` on SQLite, and
``scripts/smoke_test.sh`` (which does) sat broken for weeks: 0035 spelled
``DROP TABLE … CASCADE`` and 0040 added a foreign key with a bare
``add_column``, and SQLite rejects both. A migration written for PostgreSQL
only is a bug here, because SQLite is the dialect the smoke test, the unit
suite and the quick start run on.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

_INI = Path(__file__).resolve().parents[1] / "alembic.ini"

# Dropped by 0035, when the vector store moved to the chat.
_DROPPED_BY_0035 = {
    "knowledge_chunks",
    "knowledge_documents",
    "knowledge_bases",
    "knowledge_config",
    "file_blobs",
    "files",
    "resource_shares",
}


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (our own interpreter, fixed arguments)
        [sys.executable, "-m", "alembic", "-c", str(_INI), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_every_migration_applies_on_sqlite(tmp_path: Path) -> None:
    database = tmp_path / "migrated.db"
    result = _alembic("-x", f"url=sqlite+aiosqlite:///{database}", "upgrade", "head")
    assert result.returncode == 0, result.stderr[-2000:]

    # `alembic heads` prints "0056 (head)": the revision a finished chain stamps.
    head = _alembic("heads").stdout.split()[0]
    with closing(sqlite3.connect(database)) as connection:
        stamped = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        tables = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert stamped == [(head,)]
    assert not tables & _DROPPED_BY_0035


def test_0056_seen_groups_round_trips_on_sqlite(tmp_path: Path) -> None:
    """0056's tables and columns arrive, and its downgrade (batch mode for the
    two `users` columns, which SQLite cannot drop in place) leaves 0055."""
    database = tmp_path / "seen.db"
    url = f"url=sqlite+aiosqlite:///{database}"
    assert _alembic("-x", url, "upgrade", "0056").returncode == 0

    def schema() -> tuple[set[str], set[str]]:
        with closing(sqlite3.connect(database)) as connection:
            tables = {
                name
                for (name,) in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        return tables, columns

    tables, columns = schema()
    assert {"seen_groups", "seen_group_users"} <= tables
    assert {"unresolved_group_names", "default_group_granted_at"} <= columns

    result = _alembic("-x", url, "downgrade", "0055")
    assert result.returncode == 0, result.stderr[-2000:]
    tables, columns = schema()
    assert not {"seen_groups", "seen_group_users"} & tables
    assert not {"unresolved_group_names", "default_group_granted_at"} & columns


def test_0057_join_modes_and_invites_round_trips_on_sqlite(tmp_path: Path) -> None:
    """0057's columns arrive with their check constraints (including on the
    batch-recreated `oidc_config`, whose 0018 constraint must survive), and
    its downgrade leaves 0056."""
    database = tmp_path / "join.db"
    url = f"url=sqlite+aiosqlite:///{database}"
    assert _alembic("-x", url, "upgrade", "0057").returncode == 0

    with closing(sqlite3.connect(database)) as connection:
        policy_columns = {row[1] for row in connection.execute("PRAGMA table_info(oidc_config)")}
        user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        assert {"join_mode", "required_groups", "group_fallback"} <= policy_columns
        assert {"invited_at", "invited_by"} <= user_columns
        with pytest.raises(sqlite3.IntegrityError, match="ck_oidc_config_join_mode"):
            connection.execute(
                "INSERT INTO oidc_config (id, created_at, join_mode) VALUES "
                "(lower(hex(randomblob(16))), '2026-10-12', 'nonsense')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="ck_oidc_config_unknown_user_policy"):
            connection.execute(
                "INSERT INTO oidc_config (id, created_at, unknown_user_policy) VALUES "
                "(lower(hex(randomblob(16))), '2026-10-12', 'nonsense')"
            )

    result = _alembic("-x", url, "downgrade", "0056")
    assert result.returncode == 0, result.stderr[-2000:]
    with closing(sqlite3.connect(database)) as connection:
        policy_columns = {row[1] for row in connection.execute("PRAGMA table_info(oidc_config)")}
        user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
    assert not {"join_mode", "required_groups", "group_fallback"} & policy_columns
    assert not {"invited_at", "invited_by"} & user_columns


def test_0059_capability_flags_round_trip_on_sqlite(tmp_path: Path) -> None:
    """0059's capability columns arrive — the two group flags defaulting to
    false, the two policy modes with their check constraints — and its
    downgrade leaves 0058."""
    database = tmp_path / "capabilities.db"
    url = f"url=sqlite+aiosqlite:///{database}"
    assert _alembic("-x", url, "upgrade", "0059").returncode == 0

    with closing(sqlite3.connect(database)) as connection:
        group_columns = {row[1] for row in connection.execute("PRAGMA table_info(groups)")}
        policy_columns = {row[1] for row in connection.execute("PRAGMA table_info(oidc_config)")}
        assert {"code_agents_allowed", "api_keys_allowed"} <= group_columns
        assert {"code_agents_mode", "api_keys_mode"} <= policy_columns
        defaults = dict(
            connection.execute(
                "SELECT name, dflt_value FROM pragma_table_info('groups') "
                "WHERE name IN ('code_agents_allowed', 'api_keys_allowed')"
            ).fetchall()
        )
        assert defaults == {"code_agents_allowed": "false", "api_keys_allowed": "false"}
        with pytest.raises(sqlite3.IntegrityError, match="ck_oidc_config_code_agents_mode"):
            connection.execute(
                "INSERT INTO oidc_config (id, created_at, code_agents_mode) VALUES "
                "(lower(hex(randomblob(16))), '2026-10-12', 'nonsense')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="ck_oidc_config_api_keys_mode"):
            connection.execute(
                "INSERT INTO oidc_config (id, created_at, api_keys_mode) VALUES "
                "(lower(hex(randomblob(16))), '2026-10-12', 'nonsense')"
            )

    result = _alembic("-x", url, "downgrade", "0058")
    assert result.returncode == 0, result.stderr[-2000:]
    with closing(sqlite3.connect(database)) as connection:
        group_columns = {row[1] for row in connection.execute("PRAGMA table_info(groups)")}
        policy_columns = {row[1] for row in connection.execute("PRAGMA table_info(oidc_config)")}
    assert not {"code_agents_allowed", "api_keys_allowed"} & group_columns
    assert not {"code_agents_mode", "api_keys_mode"} & policy_columns


def _insert_provider(connection: sqlite3.Connection, provider_id: str) -> None:
    """A minimal served provider, spelled out because `lastrowid` is not it."""
    connection.execute(
        "INSERT INTO providers (id, name, base_url, api_key_encrypted, api_key_hint, "
        "extra_headers, is_active, prefix, plugin, kind, billing_mode, created_at, "
        "updated_at) VALUES (?, 'fake', 'http://example.invalid', '', '', '{}', 1, '', "
        "'generic_openai', 'provider', 'own_prices', '2026-10-18', '2026-10-18')",
        (provider_id,),
    )


def _insert_model(
    connection: sqlite3.Connection,
    model_id: str,
    name: str,
    *,
    upstream: str | None = None,
    provider: str | None = None,
    is_alias: int | None = None,
) -> None:
    """A row of `models`, of either shape, with bound parameters throughout.

    Bound rather than interpolated because a NULL has no literal spelling in SQL:
    an f-string would have to write the string "None", which SQLite reads as an
    identifier — and the test would then be proving that a column named None is
    missing.

    ``is_alias=None`` omits the column, which is how a row written **before**
    0063 is made: naming a column the old schema does not have is the error, not
    the way to write one.
    """
    columns = "id, name, upstream_model, provider_id, kind"
    values = "(?, ?, ?, ?, 'chat'"
    parameters: list[object] = [model_id, name, upstream, provider]
    if is_alias is not None:
        columns += ", is_alias"
        values += ", ?"
        parameters.append(is_alias)
    columns += (
        ", is_active, is_public, input_modalities, output_modalities, "
        "supported_features, created_at, updated_at"
    )
    values += ", 1, 0, '[]', '[]', '[]', '2026-10-18', '2026-10-18')"
    connection.execute(
        f"INSERT INTO models ({columns}) VALUES {values}",  # noqa: S608 - fixed fragments
        tuple(parameters),
    )


def test_0063_model_aliases_keeps_existing_models_intact(tmp_path: Path) -> None:
    """An upgrade over a catalogue that already has models leaves them alone.

    The data half of 0063: `is_alias` defaults to false, so every row written
    before it satisfies the new constraint without a backfill. A migration that
    quietly rewrote rows here would turn "every existing model is a served
    model" into an assumption instead of a fact.
    """
    database = tmp_path / "alias.db"
    url = f"url=sqlite+aiosqlite:///{database}"
    assert _alembic("-x", url, "upgrade", "0062").returncode == 0

    with closing(sqlite3.connect(database)) as connection:
        _insert_provider(connection, "p1")
        _insert_model(connection, "m1", "test-model", upstream="upstream/test-model", provider="p1")
        connection.commit()

    assert _alembic("-x", url, "upgrade", "head").returncode == 0

    with closing(sqlite3.connect(database)) as connection:
        # By name, not "the only row": migration 0041 seeds the local extractor,
        # so a fresh database already has a second model.
        row = connection.execute(
            "SELECT name, upstream_model, provider_id, is_alias FROM models "
            "WHERE name = 'test-model'"
        ).fetchone()
        tables = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        columns = {row[1] for row in connection.execute("PRAGMA table_info(models)")}
        usage_columns = {row[1] for row in connection.execute("PRAGMA table_info(usage_records)")}
    assert row == ("test-model", "upstream/test-model", "p1", 0)
    assert {"is_alias"} <= columns
    assert "model_route_targets" in tables
    assert "ix_model_route_targets_target" in indexes
    # Inside the same `with closing(...)` as the rest, deliberately. A
    # connection left to the garbage collector raises
    # `ResourceWarning: unclosed database` mid-suite, which pytest's unraisable
    # collection turns into an error on *whichever test happens to be running*
    # — the flakiest possible failure, naming a test with nothing to do with it.
    assert "requested_model" in usage_columns


def test_0063_the_check_constraint_rejects_a_half_alias(tmp_path: Path) -> None:
    """Both halves of a half-alias are refused (ADR 0101).

    An alias with a provider, and a served model without one. Each is a row some
    code could otherwise write and some other code would then read as whichever
    of the two it expected — which is the whole failure the constraint exists
    to make loud.
    """
    database = tmp_path / "constraint.db"
    url = f"url=sqlite+aiosqlite:///{database}"
    assert _alembic("-x", url, "upgrade", "head").returncode == 0

    with closing(sqlite3.connect(database)) as connection:
        _insert_provider(connection, "p1")
        connection.commit()

        def insert(name: str, *, is_alias: int, upstream: str | None, provider: str | None):
            _insert_model(
                connection, name, name, upstream=upstream, provider=provider, is_alias=is_alias
            )
            connection.commit()

        # The two shapes that are allowed.
        insert("a-served", is_alias=0, upstream="upstream/a", provider="p1")
        insert("an-alias", is_alias=1, upstream=None, provider=None)

        with pytest.raises(sqlite3.IntegrityError, match="ck_models_alias_or_served"):
            insert("half-a", is_alias=1, upstream=None, provider="p1")
        with pytest.raises(sqlite3.IntegrityError, match="ck_models_alias_or_served"):
            insert("half-b", is_alias=0, upstream="upstream/b", provider=None)


def test_0063_downgrade_refuses_while_an_alias_exists(tmp_path: Path) -> None:
    """The downgrade cannot restore NOT NULL over a row that has no provider,
    and it says so instead of deleting an operator's aliases to get there."""
    database = tmp_path / "downgrade.db"
    url = f"url=sqlite+aiosqlite:///{database}"
    assert _alembic("-x", url, "upgrade", "head").returncode == 0

    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO models (id, name, kind, is_alias, is_active, is_public, "
            "input_modalities, output_modalities, supported_features, created_at, "
            "updated_at) VALUES ('m1', 'fast', 'chat', 1, 1, 0, '[]', '[]', '[]', "
            "'2026-10-18', '2026-10-18')"
        )
        connection.commit()

    refused = _alembic("-x", url, "downgrade", "0062")
    assert refused.returncode != 0
    assert "downgrade refused" in refused.stderr

    # With the alias gone it goes through, and leaves 0062's shape behind.
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DELETE FROM models WHERE name = 'fast'")
        connection.commit()
    assert _alembic("-x", url, "downgrade", "0062").returncode == 0
    with closing(sqlite3.connect(database)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(models)")}
        tables = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "is_alias" not in columns
    assert "model_route_targets" not in tables


def _route_row(
    connection: sqlite3.Connection, row_id: str, alias: str, target: str, pos: int
) -> None:
    connection.execute(
        "INSERT INTO model_route_targets (id, alias_id, target_model_id, position, created_at) "
        "VALUES (?, ?, ?, ?, '2026-10-21')",
        (row_id, alias, target, pos),
    )


def test_0066_routing_policies_keeps_existing_aliases_behaving_as_before(tmp_path: Path) -> None:
    """An alias written before 0066 comes out first-available with one-hour stickiness.

    The defaults are the whole compatibility story: one target means no rule has
    anything to choose, so an upgrade must not change what any existing alias
    does, and the new columns must be filled on every existing row without a
    backfill step that could be skipped.
    """
    database = tmp_path / "routing.db"
    url = f"url=sqlite+aiosqlite:///{database}"
    assert _alembic("-x", url, "upgrade", "0065").returncode == 0

    with closing(sqlite3.connect(database)) as connection:
        _insert_provider(connection, "p1")
        _insert_model(connection, "m1", "served", upstream="up/served", provider="p1", is_alias=0)
        _insert_model(connection, "m2", "an-alias", upstream=None, provider=None, is_alias=1)
        _route_row(connection, "r1", "m2", "m1", 0)
        connection.commit()

    assert _alembic("-x", url, "upgrade", "head").returncode == 0

    with closing(sqlite3.connect(database)) as connection:
        alias = connection.execute(
            "SELECT route_rule, route_sticky_ttl_seconds FROM models WHERE name = 'an-alias'"
        ).fetchone()
        served = connection.execute(
            "SELECT route_rule, route_sticky_ttl_seconds FROM models WHERE name = 'served'"
        ).fetchone()
        weight = connection.execute("SELECT weight FROM model_route_targets").fetchone()
        tables = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        sessions = {row[1] for row in connection.execute("PRAGMA table_info(route_sessions)")}
        usage = {row[1] for row in connection.execute("PRAGMA table_info(usage_records)")}
    assert alias == ("first_available", 3600)
    assert served == ("first_available", 3600)
    assert weight == (1,)
    assert "route_sessions" in tables
    assert sessions == {"alias_id", "key_hash", "target_model_id", "expires_at", "updated_at"}
    assert "route_attempt" in usage


def test_0066_downgrade_refuses_while_an_alias_has_several_targets(tmp_path: Path) -> None:
    """Dropping the columns would silently collapse a routing decision, so it
    refuses until the operator reduces each alias to one target."""
    database = tmp_path / "routing-down.db"
    url = f"url=sqlite+aiosqlite:///{database}"
    assert _alembic("-x", url, "upgrade", "head").returncode == 0

    with closing(sqlite3.connect(database)) as connection:
        _insert_provider(connection, "p1")
        _insert_model(connection, "m1", "a", upstream="up/a", provider="p1", is_alias=0)
        _insert_model(connection, "m2", "b", upstream="up/b", provider="p1", is_alias=0)
        _insert_model(connection, "m3", "routed", upstream=None, provider=None, is_alias=1)
        _route_row(connection, "r1", "m3", "m1", 0)
        _route_row(connection, "r2", "m3", "m2", 1)
        connection.commit()

    refused = _alembic("-x", url, "downgrade", "0065")
    assert refused.returncode != 0
    assert "0066 downgrade refused" in refused.stderr

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DELETE FROM model_route_targets WHERE id = 'r2'")
        connection.commit()
    assert _alembic("-x", url, "downgrade", "0065").returncode == 0
    with closing(sqlite3.connect(database)) as connection:
        models = {row[1] for row in connection.execute("PRAGMA table_info(models)")}
        targets = {row[1] for row in connection.execute("PRAGMA table_info(model_route_targets)")}
        usage = {row[1] for row in connection.execute("PRAGMA table_info(usage_records)")}
        tables = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert not {"route_rule", "route_sticky_ttl_seconds"} & models
    assert "weight" not in targets
    assert "route_attempt" not in usage
    assert "route_sessions" not in tables
    # And it goes up again: the pair is symmetric.
    assert _alembic("-x", url, "upgrade", "head").returncode == 0
