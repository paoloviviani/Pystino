"""The migration contract (ADR 0113, section 0): the same requests, byte for byte.

``golden/redaction_premigration.json`` records what the code before this change
(Pystino at bed8e70) sent upstream, or answered, for representative redaction
configurations on every surface that redacts: chat completions, responses,
embeddings, rerank, and reading with a remote and with the built-in reader. This
test builds each configuration the way a pre-migration deployment holds it
(``redaction_rules`` and ``user_redaction_rules`` rows in a database at revision
0081), runs ``alembic upgrade head`` on it, loads the resulting policies and
pipelines into the application and sends the same requests. The outputs must be
identical: the redacted text, the placeholders, the refusals, the status codes.

A deployment with no rules (the ``none`` configuration) must behave exactly as
before too.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.config import (
    CustomPattern,
    EntityMode,
    EntityPolicy,
    PlaceholderStyle,
    RedactionPolicy,
)
from gateway.models import PrivacyPipeline, RedactionPolicyRecord, UserPolicy
from helpers import completion_body
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_embeddings import add_embedding_model, embedding_body
from test_migration_0067 import _alembic
from test_ocr_surface import FakeExtractor, add_local_model, add_ocr_model, ocr_response
from test_redaction_http import FakeDetector
from test_rerank import add_rerank_model, cortecs_reply

GOLDEN = Path(__file__).parent / "golden" / "redaction_premigration.json"
T0 = "2026-11-08 12:00:00.000000"

TEXT = (
    "Contact Mario Rossi at mario.rossi@acme.test or alice@evil.test, "
    "IBAN IT60X0542811101000000123456, ticket PRJ-123 and TKT-9."
)
SPANS = {
    "Mario Rossi": "PERSON",
    "mario.rossi@acme.test": "EMAIL_ADDRESS",
    "alice@evil.test": "EMAIL_ADDRESS",
    "IT60X0542811101000000123456": "IBAN_CODE",
}
E = EntityPolicy
M = EntityMode

# (name, [(scope, subject, policy kwargs)], user rules [(kind, name, regex, mode)])
CONFIGS: dict[str, Any] = {
    "none": ([], []),
    "all_only": (
        [
            (
                "all",
                None,
                {
                    "entities": {
                        "EMAIL_ADDRESS": E(mode=M.REDACT),
                        "PERSON": E(mode=M.ANONYMISE_RESTORE),
                    },
                    "allow_list": ["alice@evil.test"],
                },
            )
        ],
        [],
    ),
    "all_provider_model_group_user_key": (
        [
            (
                "all",
                None,
                {
                    "entities": {"PERSON": E(mode=M.ANONYMISE_RESTORE, threshold=0.8)},
                    "allow_list": ["acme.test"],
                },
            ),
            ("provider", "provider", {"entities": {"EMAIL_ADDRESS": E(mode=M.ANONYMISE)}}),
            (
                "model",
                "model",
                {
                    "entities": {
                        "IBAN_CODE": E(mode=M.REDACT, placeholder=PlaceholderStyle.SAME_LENGTH)
                    }
                },
            ),
            ("group", "group", {"entities": {"PERSON": E(mode=M.REDACT, threshold=0.5)}}),
            (
                "user",
                "user",
                {
                    "entities": {"EMAIL_ADDRESS": E(mode=M.REDACT)},
                    "patterns": [CustomPattern(name="PRJ", regex=r"PRJ-\d+", mode=M.REDACT)],
                },
            ),
            (
                "api_key",
                "key",
                {
                    "entities": {"IBAN_CODE": E(mode=M.ANONYMISE_RESTORE)},
                    "patterns": [CustomPattern(name="TKT", regex=r"TKT-\d+", mode=M.ANONYMISE)],
                },
            ),
        ],
        [],
    ),
    "other_subjects_are_inert": (
        [
            ("user", "other", {"entities": {"PERSON": E(mode=M.BLOCK)}}),
            ("group", "other", {"entities": {"EMAIL_ADDRESS": E(mode=M.REDACT)}}),
            ("provider", "other", {"entities": {"IBAN_CODE": E(mode=M.REDACT)}}),
        ],
        [],
    ),
    "block_by_group": (
        [("group", "group", {"entities": {"IBAN_CODE": E(mode=M.BLOCK)}})],
        [],
    ),
    "default_mode_and_provider": (
        [
            ("all", None, {"default_mode": M.OFF, "entities": {"URL": E(mode=M.OFF)}}),
            (
                "provider",
                "provider",
                {"default_mode": M.REDACT, "entities": {"IBAN_CODE": E(mode=M.ANONYMISE_RESTORE)}},
            ),
        ],
        [],
    ),
    "user_rules_over_admin": (
        [
            (
                "all",
                None,
                {"entities": {"EMAIL_ADDRESS": E(mode=M.ANONYMISE), "PERSON": E(mode=M.REDACT)}},
            )
        ],
        [
            ("pattern", "PRJ", r"PRJ-\d+", "redact"),
            ("mode", "EMAIL_ADDRESS", None, "redact"),
            ("mode", "PERSON", None, "anonymise"),
            ("pattern", "TKT", r"TKT-\d+", "anonymise"),
        ],
    ),
    "user_rule_blocks": (
        [("provider", "provider", {"entities": {"PERSON": E(mode=M.REDACT)}})],
        [("mode", "IBAN_CODE", None, "block")],
    ),
    "user_rules_only": (
        [],
        [
            ("mode", "IBAN_CODE", None, "redact"),
            ("pattern", "PRJ", r"PRJ-\d+", "anonymise_restore"),
        ],
    ),
    "env_policy_only": ([], [], {"entities": {"PERSON": E(mode=M.REDACT)}}),
    "env_policy_and_user_rules": (
        [],
        [("mode", "EMAIL_ADDRESS", None, "redact")],
        {"entities": {"PERSON": E(mode=M.REDACT)}, "allow_list": ["acme.test"]},
    ),
    "user_rules_next_to_a_key_scoped_admin_policy": (
        [
            (
                "api_key",
                "key",
                {
                    "entities": {"EMAIL_ADDRESS": E(mode=M.REDACT)},
                    "allow_list": ["mario.rossi@acme.test"],
                },
            )
        ],
        [("mode", "PERSON", None, "redact"), ("pattern", "PRJ", r"PRJ-\d+", "redact")],
    ),
    "inactive_rule": (
        [("all", None, {"entities": {"EMAIL_ADDRESS": E(mode=M.BLOCK)}, "_inactive": True})],
        [],
    ),
}


async def _run(
    app: Any,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session: AsyncSession,
    fake_upstream: FakeUpstream,
) -> dict[str, Any]:
    out: dict[str, Any] = {}

    async def call(surface: str, path: str, body: dict[str, Any], upstream: dict[str, Any]) -> None:
        fake_upstream.set_json(upstream)
        before = len(fake_upstream.bodies)
        r = await client.post(path, json=body, headers=seeded.auth)
        entry: dict[str, Any] = {"status": r.status_code}
        if r.status_code >= 400:
            entry["code"] = r.json().get("error", {}).get("code")
            entry["message"] = r.json().get("error", {}).get("message")
        else:
            entry["response"] = r.json()
        if len(fake_upstream.bodies) > before:
            entry["upstream"] = fake_upstream.bodies[-1]
        out[surface] = entry

    await call(
        "chat",
        "/v1/chat/completions",
        {"model": seeded.model.name, "messages": [{"role": "user", "content": TEXT}]},
        completion_body(),
    )
    await call(
        "responses",
        "/v1/responses",
        {"model": seeded.model.name, "input": TEXT},
        {
            "id": "r1",
            "object": "response",
            "output_text": "ok",
            "usage": {"input_tokens": 7, "output_tokens": 3},
        },
    )
    await call(
        "embeddings",
        "/v1/embeddings",
        {"model": "embed-1", "input": [TEXT, "second " + TEXT]},
        embedding_body(),
    )
    await call(
        "rerank",
        "/v1/rerank",
        {"model": "rank-1", "query": TEXT, "documents": [TEXT, "none"]},
        cortecs_reply(),
    )
    await call(
        "ocr_remote",
        "/v1/ocr",
        {
            "model": "reader",
            "document": {"type": "document_url", "document_url": "https://x.test/a.pdf"},
        },
        ocr_response(pages=1, pages_override=None)
        | {"pages": [{"index": 0, "markdown": TEXT, "images": []}]},
    )
    extractor = FakeExtractor()
    extractor.set(kind="text", text=TEXT, extractor="markitdown", pages=0)
    app.state.control_http = extractor.client()
    r = await client.post(
        "/v1/ocr",
        json={
            "model": "local-reader",
            "document": {
                "type": "document_url",
                "document_url": "data:text/plain;base64," + base64.b64encode(b"x").decode(),
            },
        },
        headers=seeded.auth,
    )
    entry = {"status": r.status_code}
    if r.status_code >= 400:
        entry["code"] = r.json().get("error", {}).get("code")
    else:
        entry["pages"] = [p["markdown"] for p in r.json()["pages"]]
    out["ocr_local"] = entry
    return out


def _old_policy(kwargs: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """The policy as a pre-migration database holds it: one ``entities`` table, no engine.

    (The model now keeps built-in formats and NER types apart; the stored shape of the time
    did not, and that is the shape migration 0083 has to read.)
    """
    kwargs = dict(kwargs)
    inactive = bool(kwargs.pop("_inactive", False))
    new = RedactionPolicy(**kwargs).model_dump(mode="json")
    return {
        "default_mode": new["default_mode"],
        "entities": {**new["formats"], **new["entities"]},
        "patterns": new["patterns"],
        "allow_list": new["allow_list"],
    }, inactive


def _build_old_database(path: Path, name: str, subjects: dict[str, uuid.UUID]) -> None:
    """A database at revision 0081 holding the configuration as it was written then."""
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0081").returncode == 0
    rules, user_rules, *_ = CONFIGS[name]
    with closing(sqlite3.connect(path)) as conn:
        for index, (scope, subject, kwargs) in enumerate(rules):
            policy, inactive = _old_policy(kwargs)
            conn.execute(
                "INSERT INTO redaction_rules (id, name, scope, scope_id, policy, is_active, "
                "reason, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?)",
                (
                    uuid.UUID(int=index + 1).hex,
                    f"rule {scope}",
                    scope,
                    None if subject is None else subjects[subject].hex,
                    json.dumps(policy),
                    0 if inactive else 1,
                    T0,
                    T0,
                ),
            )
        for index, (kind, rname, regex, mode) in enumerate(user_rules):
            conn.execute(
                "INSERT INTO user_redaction_rules (id, user_id, kind, name, regex, mode, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    subjects["user"].hex,
                    kind,
                    rname,
                    regex,
                    "block" if mode == "block_never" else mode,
                    f"2026-11-08 12:00:{index:02d}.000000",
                    T0,
                ),
            )
        conn.commit()
    up = _alembic(url, "upgrade", "head")
    assert up.returncode == 0, up.stderr[-2000:]


def _migrated_rows(path: Path) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        for table in ("redaction_policies", "user_policies", "privacy_pipelines"):
            out[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]  # noqa: S608
    return out


def _uuid(raw: str) -> uuid.UUID:
    return uuid.UUID(raw)


async def _load(
    session_factory: async_sessionmaker[AsyncSession], rows: dict[str, list[dict[str, Any]]]
) -> None:
    async with session_factory() as db:
        for r in rows["redaction_policies"]:
            db.add(
                RedactionPolicyRecord(
                    id=_uuid(r["id"]),
                    name=r["name"],
                    policy=json.loads(r["policy"]),
                    is_active=bool(r["is_active"]),
                    reason=r["reason"],
                )
            )
        await db.flush()
        for r in rows["user_policies"]:
            db.add(
                UserPolicy(
                    id=_uuid(r["id"]),
                    user_id=_uuid(r["user_id"]),
                    name=r["name"],
                    rules=json.loads(r["rules"]),
                )
            )
        for r in rows["privacy_pipelines"]:
            db.add(
                PrivacyPipeline(
                    id=_uuid(r["id"]),
                    owner_id=_uuid(r["owner_id"]) if r["owner_id"] else None,
                    kind=r["kind"],
                    name=r["name"],
                    enabled=bool(r["enabled"]),
                    position=r["position"],
                    who=json.loads(r["who"]),
                    target=json.loads(r["target"]),
                    rules=json.loads(r["rules"]),
                    otherwise=json.loads(r["otherwise"]),
                )
            )
        await db.commit()


async def _check(
    name: str,
    tmp_path: Path,
    app: Any,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    fake_upstream: FakeUpstream,
    *,
    rewrite: Any = None,
) -> tuple[dict[str, Any], dict[str, Any], FakeDetector]:
    await add_embedding_model(session, seeded)
    await add_rerank_model(session, seeded)
    await add_ocr_model(session, seeded)
    await add_local_model(session, seeded)
    subjects = {
        "provider": seeded.provider.id,
        "model": seeded.model.id,
        "group": seeded.group.id,
        "user": seeded.user.id,
        "key": seeded.api_key.id,
        "other": uuid.uuid4(),
    }
    path = tmp_path / "old.db"
    _build_old_database(path, name, subjects)
    rows = _migrated_rows(path)
    if rewrite is not None:
        rewrite(rows)
    await _load(session_factory, rows)
    await app.state.privacy.refresh()
    assert app.state.privacy.bad_rows == []
    if len(CONFIGS[name]) > 2:
        app.state.redaction._policy = RedactionPolicy(**CONFIGS[name][2])
    detector = FakeDetector(SPANS)
    app.state.redactor = detector.redactor()

    got = await _run(app, client, seeded, session, fake_upstream)
    expected = json.loads(GOLDEN.read_text())[name]
    return json.loads(json.dumps(got, default=str)), expected, detector


@pytest.mark.parametrize("name", list(CONFIGS))
async def test_the_same_requests_come_out_byte_for_byte(
    name: str,
    tmp_path: Path,
    app: Any,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    fake_upstream: FakeUpstream,
) -> None:
    got, expected, detector = await _check(
        name, tmp_path, app, client, seeded, session, session_factory, fake_upstream
    )
    assert got == expected
    # The engine choice defaults to today's behaviour: no engine and no labels are asked
    # for, so the redaction service answers with its own default exactly as before.
    assert detector.requests
    assert all(
        r.get("ner_engine") is None and r.get("custom_labels") is None for r in detector.requests
    )


@pytest.mark.parametrize("name", list(CONFIGS))
async def test_a_migrated_policy_chooses_no_engine_and_has_no_new_items(
    name: str, tmp_path: Path
) -> None:
    subjects = {k: uuid.uuid4() for k in ("provider", "model", "group", "user", "key", "other")}
    path = tmp_path / "old.db"
    _build_old_database(path, name, subjects)
    for row in _migrated_rows(path)["redaction_policies"]:
        stored = json.loads(row["policy"])
        assert stored["engine"] is None
        assert stored["keywords"] == [] and stored["custom_entities"] == []
        assert row["shared_with_users"] == 0
        # Nothing is lost in the split: read back, every named type keeps its mode.
        policy = RedactionPolicy.model_validate(stored)
        assert set(policy.service_types()) == set(stored["formats"]) | set(stored["entities"])


def _patterns_as_keywords(rows: dict[str, list[dict[str, Any]]]) -> None:
    """The same patterns as keyword lists: PRJ-123 and TKT-9 are what the regexes find in TEXT."""
    words = {r"PRJ-\d+": "PRJ-123", r"TKT-\d+": "TKT-9"}
    for row in rows["redaction_policies"]:
        policy = json.loads(row["policy"])
        keywords = []
        for pattern in policy["patterns"]:
            keywords.append(
                {
                    "name": pattern["name"],
                    "words": [words[pattern["regex"]]],
                    # A regular expression is case sensitive; a keyword list says so.
                    "case_sensitive": True,
                    "mode": pattern["mode"],
                    "placeholder": pattern["placeholder"],
                }
            )
        policy["patterns"], policy["keywords"] = [], keywords
        row["policy"] = json.dumps(policy)


async def test_a_keyword_list_redacts_exactly_like_the_pattern_it_replaces(
    tmp_path: Path,
    app: Any,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    fake_upstream: FakeUpstream,
) -> None:
    got, expected, _ = await _check(
        "all_provider_model_group_user_key",
        tmp_path,
        app,
        client,
        seeded,
        session,
        session_factory,
        fake_upstream,
        rewrite=_patterns_as_keywords,
    )
    assert got == expected
    policies = [p.policy for p in app.state.privacy.policies.values()]
    assert any(p.keywords for p in policies) and not any(p.patterns for p in policies)
