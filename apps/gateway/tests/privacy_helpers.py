"""Writing policies and pipelines straight into the database for tests (ADR 0113).

Creating them through the API would make every test about what comes after a
test of the create routes as well.
"""

from __future__ import annotations

import uuid
from typing import Any

from gateway.config import EntityPolicy, RedactionPolicy
from gateway.models import PrivacyPipeline, RedactionPolicyRecord, UserPolicy
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
        row = RedactionPolicyRecord(
            name=name, policy=policy_json(**policy), is_active=active, shared_with_users=shared
        )
        db.add(row)
        await db.commit()
        policy_id = row.id
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
    async with session_factory() as db:
        row = PrivacyPipeline(
            owner_id=owner,
            kind=kind,
            name=name,
            enabled=enabled,
            position=position,
            who=who or {"mode": "all", "ids": []},
            target=target or {"mode": "all", "providers": [], "models": []},
            rules=rules or [],
            otherwise=otherwise or {"level": BASE[kind]},
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
    async with session_factory() as db:
        row = UserPolicy(user_id=user_id, name=name, rules=entries)
        db.add(row)
        await db.commit()
        policy_id = row.id
    await app.state.privacy.refresh()
    return policy_id
