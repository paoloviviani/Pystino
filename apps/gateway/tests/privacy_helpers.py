"""Writing policies and pipelines straight into the database for tests (ADR 0113, 0114).

Creating them through the API would make every test about what comes after a
test of the create routes as well.

Tests are written in the vocabulary the request path had before rules (a policy with a mode
on each item, a rule "conditions -> level (+ redact with P)", a last rule). The helpers store
the v2 shapes by running the same conversion migration 0086 runs on a deployment's data, so a
test of what a *configuration* does reads the same as the configuration did, and the
conversion itself is exercised here as well as in ``test_migration_0086.py``.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from typing import Any

from gateway.config import EntityPolicy, RedactionPolicy
from gateway.models import PrivacyPipeline, RedactionPolicyRecord, UserPolicy
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SPEC = importlib.util.spec_from_file_location(
    "m0086_helpers",
    Path(__file__).parent.parent / "migrations/versions/0086_privacy_rules_model_v2.py",
)
assert _SPEC and _SPEC.loader
_M = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_M)
#: policy id -> {item name: mode}: what the policy did before a rule owned the mode.
PMODES: dict[str, dict[str, str]] = {}

KINDS = ("read", "embed", "chat")
BASE = {"read": "default", "embed": "pass", "chat": "pass"}


def policy_json(**kwargs: Any) -> dict[str, Any]:
    return RedactionPolicy(**kwargs).model_dump(mode="json")


def rule(
    name: str,
    conditions: list[dict[str, Any]] | None = None,
    *,
    enabled: bool = True,
    observe_only: bool = False,
    **action: Any,
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "enabled": enabled,
        "observe_only": observe_only,
        "conditions": conditions or [],
        "action": action or {"level": "refuse"},
    }


async def add_policy(
    app: Any,
    session_factory: async_sessionmaker[AsyncSession],
    name: str = "Policy",
    *,
    active: bool = True,
    shared: bool = False,
    **policy: Any,
) -> uuid.UUID:
    async with session_factory() as db:
        stored, modes = _M.convert_policy(policy_json(**policy), [], name)
        row = RedactionPolicyRecord(
            name=name, policy=stored, is_active=active, shared_with_users=shared
        )
        db.add(row)
        await db.commit()
        policy_id = row.id
        PMODES[str(policy_id)] = modes
    await app.state.privacy.refresh()
    return policy_id


async def add_pipeline(
    app: Any,
    session_factory: async_sessionmaker[AsyncSession],
    kind: str,
    name: str,
    *,
    rules: list[dict[str, Any]] | None = None,
    otherwise: dict[str, Any] | None = None,
    who: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
    owner: uuid.UUID | None = None,
    enabled: bool = True,
    position: int = 0,
    refresh: bool = True,
) -> uuid.UUID:
    old_target = target or {"mode": "all", "providers": [], "models": []}
    if kind == "read":
        new_rules = _M.convert_extract_rules(
            rules or [], otherwise or {"level": "default"}, PMODES, [], name, str(uuid.uuid4())
        )
        new_kind, types = "extract", []
    else:
        new_rules = _M.convert_content_rules(
            rules or [], otherwise or {"level": "pass"}, PMODES, [], name
        )
        new_kind, types = "content", (["chat"] if kind == "chat" else ["embedding", "rerank"])
    async with session_factory() as db:
        row = PrivacyPipeline(
            owner_id=owner,
            kind=new_kind,
            name=name,
            enabled=enabled,
            position=position,
            who=who or {"mode": "all", "ids": []},
            target={**old_target, "model_types": old_target.get("model_types", types)},
            rules=new_rules,
        )
        db.add(row)
        await db.commit()
        pipeline_id = row.id
    if refresh:
        await app.state.privacy.refresh()
    return pipeline_id


async def catch_all(
    app: Any,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    kinds: tuple[str, ...] = KINDS,
    **entities: EntityPolicy,
) -> uuid.UUID:
    """The policy every request gets: one policy and one pipeline per kind on all."""
    policy_id = await add_policy(
        app, session_factory, "All requests", entities=entities or {"PERSON": EntityPolicy()}
    )
    for kind in kinds:
        await add_pipeline(
            app,
            session_factory,
            kind,
            f"All requests: {kind}",
            otherwise={"level": BASE[kind], "redact_with": str(policy_id)},
        )
    return policy_id


async def add_user_policy(
    app: Any,
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    entries: list[dict[str, Any]],
    name: str = "Mine",
) -> uuid.UUID:
    stored, modes = _M.convert_user_entries(entries)
    async with session_factory() as db:
        row = UserPolicy(user_id=user_id, name=name, rules=stored)
        db.add(row)
        await db.commit()
        policy_id = row.id
        PMODES[str(policy_id)] = modes
    await app.state.privacy.refresh()
    return policy_id
