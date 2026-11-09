# ruff: noqa: S608, E501  (fixed SQL built from test constants)
"""Migration 0084: the built-in extractor's engines are OCR models.

Pins what it writes, on SQLite: `markitdown-ocr` and `docling` made beside the seeded
`markitdown` (copying its active and public flags and its grants, unpriced), a name that is
taken left alone, and the read rules rewritten: `builtin` plus an engine to the matching
model, `builtin` with no engine to `markitdown`, other readers untouched, the `engine` key
gone, every change logged. The downgrade writes the old shape back.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from test_migration_0067 import _alembic

T0 = "2026-11-09 12:00:00.000000"


def _old(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0083").returncode == 0
    return path, url


def _rows(path: Path, sql: str) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql)]


def _pipeline(
    conn: sqlite3.Connection,
    name: str,
    rules: list[dict[str, Any]],
    otherwise: dict[str, Any],
    kind: str = "read",
) -> None:
    conn.execute(
        "INSERT INTO privacy_pipelines (id, owner_id, kind, name, enabled, position, who, target, rules, otherwise, created_at, updated_at) "
        "VALUES (?, NULL, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?)",
        (
            uuid.uuid4().hex,
            kind,
            name,
            json.dumps({"mode": "all", "ids": []}),
            json.dumps({"mode": "all", "providers": [], "models": []}),
            json.dumps(rules),
            json.dumps(otherwise),
            T0,
            T0,
        ),
    )


def _rule(name: str, **action: Any) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "enabled": True,
        "observe_only": False,
        "conditions": [],
        "action": action,
    }


def _upgrade(url: str) -> Any:
    up = _alembic(url, "upgrade", "0084")
    assert up.returncode == 0, up.stderr[-2000:]
    return up


def test_the_engine_models_are_made_with_markitdowns_access(tmp_path: Path) -> None:
    path, url = _old(tmp_path)
    group = uuid.uuid4().hex
    with closing(sqlite3.connect(path)) as conn:
        base = conn.execute("SELECT id FROM models WHERE name = 'markitdown'").fetchone()[0]
        # The operator narrowed the seeded model to one group.
        conn.execute("UPDATE models SET is_public = 0 WHERE id = ?", (base,))
        conn.execute(
            "INSERT INTO group_model_access (group_id, model_id, created_at) VALUES (?, ?, ?)",
            (group, base, T0),
        )
        conn.commit()
    up = _upgrade(url)

    models = {
        r["name"]: r
        for r in _rows(
            path,
            "SELECT m.*, p.plugin FROM models m JOIN providers p ON p.id = m.provider_id WHERE p.plugin = 'extractor'",
        )
    }
    assert set(models) == {"markitdown", "markitdown-ocr", "docling"}
    for name in ("markitdown-ocr", "docling"):
        row = models[name]
        assert (row["kind"], row["upstream_model"], row["is_active"], row["is_public"]) == (
            "ocr",
            name,
            1,
            0,
        )
        assert row["provider_id"] == models["markitdown"]["provider_id"]
        grants = _rows(
            path, f"SELECT group_id FROM group_model_access WHERE model_id = '{row['id']}'"
        )
        assert [g["group_id"] for g in grants] == [group]
        assert _rows(path, f"SELECT 1 FROM model_prices WHERE model_id = '{row['id']}'") == []
    assert "created the OCR model 'docling'" in up.stdout + up.stderr


def test_a_deactivated_markitdown_is_mirrored_and_a_taken_name_is_left_alone(
    tmp_path: Path,
) -> None:
    path, url = _old(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE models SET is_active = 0 WHERE name = 'markitdown'")
        other = conn.execute("SELECT id FROM providers WHERE plugin = 'extractor'").fetchone()[0]
        conn.execute(
            "INSERT INTO models (id, name, upstream_model, provider_id, kind, is_active, is_public, input_modalities, output_modalities, supported_features, created_at, updated_at) "
            "VALUES (?, 'docling', 'mine', ?, 'ocr', 1, 1, '[]', '[]', '[]', ?, ?)",
            (uuid.uuid4().hex, other, T0, T0),
        )
        conn.commit()
    up = _upgrade(url)
    rows = {
        r["name"]: r
        for r in _rows(
            path, "SELECT * FROM models WHERE name LIKE 'markitdown%' OR name = 'docling'"
        )
    }
    assert rows["markitdown-ocr"]["is_active"] == 0
    assert rows["docling"]["upstream_model"] == "mine"  # not touched, not duplicated
    assert len(_rows(path, "SELECT 1 FROM models WHERE name = 'docling'")) == 1
    assert "a model named 'docling' exists already" in up.stdout + up.stderr


def _remote_docling(conn: sqlite3.Connection) -> None:
    """A deployment model named `docling` that is a remote service, not the extractor's."""
    provider = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO providers (id, name, base_url, api_key_encrypted, api_key_hint, extra_headers, is_active, prefix, plugin, kind, billing_mode, created_at, updated_at) "
        "VALUES (?, 'cloud-ocr', 'http://ocr.test', '', '', '{}', 1, '', NULL, 'provider', 'own_prices', ?, ?)",
        (provider, T0, T0),
    )
    conn.execute(
        "INSERT INTO models (id, name, upstream_model, provider_id, kind, is_active, is_public, input_modalities, output_modalities, supported_features, created_at, updated_at) "
        "VALUES (?, 'docling', 'docling-serve', ?, 'ocr', 1, 1, '[]', '[]', '[]', ?, ?)",
        (uuid.uuid4().hex, provider, T0, T0),
    )


def test_a_rule_is_never_pointed_at_a_same_named_model_off_the_extractor(tmp_path: Path) -> None:
    """A local-only rule must not be redirected to a remote service that happens to be called docling."""
    path, url = _old(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        _remote_docling(conn)
        _pipeline(
            conn,
            "Files",
            [
                _rule("Layout", level="read_with", reader="builtin", engine="docling"),
                _rule("Scans", level="read_with", reader="builtin", engine="ocr"),
                _rule("Remote", level="read_with", reader="docling"),
            ],
            otherwise={"level": "read_with", "reader": "builtin", "engine": "docling"},
        )
        conn.commit()
    up = _upgrade(url)
    row = _rows(path, "SELECT * FROM privacy_pipelines WHERE name = 'Files'")[0]
    actions = {r["name"]: r["action"] for r in json.loads(row["rules"])}
    assert actions["Layout"]["reader"] == "markitdown"  # not the remote docling
    assert actions["Scans"]["reader"] == "markitdown-ocr"  # created on the extractor
    assert actions["Remote"]["reader"] == "docling"  # already named the remote one
    assert json.loads(row["otherwise"])["reader"] == "markitdown"
    log = up.stdout + up.stderr
    assert "LOUD: a model named 'docling' exists but is not on the extractor provider" in log
    docling = _rows(path, "SELECT upstream_model FROM models WHERE name = 'docling'")
    assert [d["upstream_model"] for d in docling] == ["docling-serve"]
    # The downgrade leaves the remote reader's name alone.
    down = _alembic(url, "downgrade", "0083")
    assert down.returncode == 0, down.stderr[-2000:]
    row = _rows(path, "SELECT * FROM privacy_pipelines WHERE name = 'Files'")[0]
    actions = {r["name"]: r["action"] for r in json.loads(row["rules"])}
    assert actions["Remote"] == {"level": "read_with", "reader": "docling"}


def test_the_built_in_readers_are_recreated_when_markitdown_is_gone(tmp_path: Path) -> None:
    """Built-in is always there: a deleted markitdown row comes back with 0041's defaults."""
    path, url = _old(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("DELETE FROM models WHERE name = 'markitdown'")
        conn.commit()
    up = _upgrade(url)
    rows = {
        r["name"]: r
        for r in _rows(
            path,
            "SELECT * FROM models WHERE name IN ('markitdown', 'markitdown-ocr', 'docling')",
        )
    }
    assert set(rows) == {"markitdown", "markitdown-ocr", "docling"}
    for row in rows.values():
        assert (row["kind"], row["is_active"], row["is_public"]) == ("ocr", 1, 1)
    assert "'markitdown' model was missing; recreated it" in up.stdout + up.stderr


def test_rules_name_the_model_not_the_engine(tmp_path: Path) -> None:
    path, url = _old(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        _pipeline(
            conn,
            "Files",
            [
                _rule("Plain", level="read_with", reader="markitdown", engine="markitdown"),
                _rule("Scans", level="read_with", reader="builtin", engine="ocr"),
                _rule("Layout", level="read_with", reader="builtin", engine="docling"),
                _rule("Default", level="read_with", reader="builtin"),
                _rule("Cloud", level="read_with", reader="mistral"),
                _rule("Stop", level="refuse"),
            ],
            otherwise={"level": "read_with", "reader": "builtin", "engine": "ocr"},
        )
        _pipeline(conn, "Chat", [], {"level": "pass"}, kind="chat")
        conn.commit()
    up = _upgrade(url)
    row = _rows(path, "SELECT * FROM privacy_pipelines WHERE name = 'Files'")[0]
    actions = {r["name"]: r["action"] for r in json.loads(row["rules"])}
    assert actions == {
        "Plain": {"level": "read_with", "reader": "markitdown"},
        "Scans": {"level": "read_with", "reader": "markitdown-ocr"},
        "Layout": {"level": "read_with", "reader": "docling"},
        "Default": {"level": "read_with", "reader": "markitdown"},
        "Cloud": {"level": "read_with", "reader": "mistral"},
        "Stop": {"level": "refuse"},
    }
    assert json.loads(row["otherwise"]) == {"level": "read_with", "reader": "markitdown-ocr"}
    chat = _rows(path, "SELECT * FROM privacy_pipelines WHERE name = 'Chat'")[0]
    assert json.loads(chat["otherwise"]) == {"level": "pass"}
    log = up.stdout + up.stderr
    assert (
        "rule 'Scans': read with the built-in extractor, engine ocr -> read with 'markitdown-ocr'"
        in log
    )
    assert "rule 'Default': read with the built-in extractor -> read with 'markitdown'" in log
    assert "last rule: read with the built-in extractor, engine ocr" in log


def test_the_downgrade_writes_builtin_and_the_engine_back(tmp_path: Path) -> None:
    path, url = _old(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        _pipeline(
            conn,
            "Files",
            [
                _rule("Scans", level="read_with", reader="builtin", engine="ocr"),
                _rule("Layout", level="read_with", reader="builtin", engine="docling"),
                _rule("Cloud", level="read_with", reader="mistral"),
            ],
            otherwise={"level": "read_with", "reader": "builtin"},
        )
        conn.commit()
    _upgrade(url)
    down = _alembic(url, "downgrade", "0083")
    assert down.returncode == 0, down.stderr[-2000:]
    row = _rows(path, "SELECT * FROM privacy_pipelines WHERE name = 'Files'")[0]
    actions = {r["name"]: r["action"] for r in json.loads(row["rules"])}
    assert actions["Scans"] == {"level": "read_with", "reader": "builtin", "engine": "ocr"}
    assert actions["Layout"] == {"level": "read_with", "reader": "builtin", "engine": "docling"}
    assert actions["Cloud"]["reader"] == "mistral"
    assert json.loads(row["otherwise"]) == {"level": "read_with", "reader": "builtin"}
