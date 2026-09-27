"""The 0055 dedup and partial unique index on directory_entries.

Run against a raw SQLite database holding the pre-migration table, with the
migration module loaded by path (its file name starts with digits) and its
`upgrade` driven through Alembic's `Operations` — the same way the suite's
conftest deliberately does *not* touch Alembic, but here the thing under test
is the migration itself: given bound-entry duplicates from the Create
sign-in bug, it must converge them deterministically and then enforce the
shape the ORM now declares.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0055_directory_entries_bound_login_unique.py"
)

DDL = """
CREATE TABLE directory_entries (
    id CHAR(32) PRIMARY KEY,
    provider_id CHAR(32) NOT NULL,
    external_id VARCHAR(255) NOT NULL,
    username VARCHAR(255),
    email VARCHAR(320),
    display_name VARCHAR(255),
    groups JSON,
    active BOOLEAN,
    present BOOLEAN,
    preassigned_groups JSON,
    user_id CHAR(32),
    first_seen_at TIMESTAMP NOT NULL,
    last_seen_at TIMESTAMP NOT NULL
)
"""
UNIQUE_INDEX = (
    "CREATE UNIQUE INDEX uq_directory_entries_provider_ext "
    "ON directory_entries (provider_id, external_id)"
)

PROVIDER = "11111111-1111-1111-1111-111111111111"
OTHER_PROVIDER = "22222222-2222-2222-2222-222222222222"
USER = "33333333-3333-3333-3333-333333333333"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0055", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert(
    conn: sa.Connection,
    *,
    external_id: str,
    user_id: str | None,
    seen: datetime,
    provider_id: str = PROVIDER,
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO directory_entries "
            "(id, provider_id, external_id, username, groups, active, present, "
            "preassigned_groups, user_id, first_seen_at, last_seen_at) "
            "VALUES (:id, :provider_id, :external_id, NULL, '[]', 1, 1, '[]', "
            ":user_id, :seen, :seen)"
        ),
        {
            "id": external_id.replace("-", "") if provider_id == PROVIDER else "elsewhere",
            "provider_id": provider_id,
            "external_id": external_id,
            "user_id": user_id,
            # ISO text, not an adapter-bound datetime: raw sqlite3 has none.
            # ISO 8601 orders lexicographically the same as chronologically,
            # which is all the migration's comparison needs.
            "seen": seen.isoformat(),
        },
    )


def _external_ids(engine: sa.Engine, *, user_id: str | None) -> list[str]:
    # Two fixed queries rather than one built by interpolation: the filter
    # itself is the only thing that differs.
    if user_id is None:
        query = (
            "SELECT external_id FROM directory_entries "
            "WHERE provider_id = :provider_id AND user_id IS NULL ORDER BY external_id"
        )
        params: dict[str, str | None] = {"provider_id": PROVIDER}
    else:
        query = (
            "SELECT external_id FROM directory_entries "
            "WHERE provider_id = :provider_id AND user_id = :user_id ORDER BY external_id"
        )
        params = {"provider_id": PROVIDER, "user_id": user_id}
    with engine.connect() as conn:
        return list(conn.execute(sa.text(query), params).scalars())


def _index_names(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT name FROM pragma_index_list('directory_entries')"))
        return {row for (row,) in rows}


def _insert_bound(engine: sa.Engine, external_id: str) -> None:
    with engine.begin() as conn:
        _insert(conn, external_id=external_id, user_id=USER, seen=datetime.now(UTC))


def _upgrade(module, engine: sa.Engine) -> None:
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        # `Operations.context` installs the proxy that the migration file's
        # module-level `alembic.op` calls route through.
        module.upgrade()


def test_duplicates_converge_to_the_newest_and_the_index_holds(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")

    base = datetime(2026, 1, 1, tzinfo=UTC)
    with engine.begin() as conn:
        conn.execute(sa.text(DDL))
        conn.execute(sa.text(UNIQUE_INDEX))
        # The bug's own shape: two logins bound to one person at one provider,
        # created days apart — the older one first, then the second sign-in.
        _insert(conn, external_id="old-login", user_id=USER, seen=base)
        _insert(conn, external_id="new-login", user_id=USER, seen=base + timedelta(days=120))
        # Unbound rows are exempt: nobody has claimed either login.
        _insert(conn, external_id="stranger-1", user_id=None, seen=base)
        _insert(conn, external_id="stranger-2", user_id=None, seen=base)
        # The same person at a different provider is a different directory's
        # business and must survive untouched.
        _insert(
            conn,
            external_id="old-login",
            user_id=USER,
            seen=base,
            provider_id=OTHER_PROVIDER,
        )

    module = _load_migration()
    _upgrade(module, engine)

    assert _external_ids(engine, user_id=USER) == ["new-login"]
    assert sorted(_external_ids(engine, user_id=None)) == ["stranger-1", "stranger-2"]
    with engine.connect() as conn:
        elsewhere = conn.execute(
            sa.text("SELECT count(*) FROM directory_entries WHERE provider_id = :provider_id"),
            {"provider_id": OTHER_PROVIDER},
        ).scalar_one()
    assert elsewhere == 1

    # The index the ORM now declares is real: a second bound entry for the
    # same person at the same provider is refused, not silently allowed.
    assert "uq_directory_entries_provider_user" in _index_names(engine)
    with pytest.raises(sa.exc.IntegrityError):
        _insert_bound(engine, "newest-login")

    # Downgrade removes the enforcement, not the data.
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        module.downgrade()
    assert "uq_directory_entries_provider_user" not in _index_names(engine)
    _insert_bound(engine, "newest-login")
    assert _external_ids(engine, user_id=USER) == ["new-login", "newest-login"]
    engine.dispose()


def test_the_tie_break_is_the_greater_external_id(tmp_path: Path) -> None:
    """Same first_seen_at on both rows: the ordering must still be total."""
    db_path = tmp_path / "gateway.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    base = datetime(2026, 3, 1, tzinfo=UTC)
    with engine.begin() as conn:
        conn.execute(sa.text(DDL))
        conn.execute(sa.text(UNIQUE_INDEX))
        _insert(conn, external_id="aaa", user_id=USER, seen=base)
        _insert(conn, external_id="zzz", user_id=USER, seen=base)

    module = _load_migration()
    _upgrade(module, engine)

    assert _external_ids(engine, user_id=USER) == ["zzz"]
    engine.dispose()
