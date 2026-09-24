"""Directory sync and the bundled Authelia's users, from the console (ADR 0088 draft).

Kept out of `admin.py` (already the gateway's largest module) on purpose.
Every route requires an administrator.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from gateway.deps import AdminUserDep, SessionDep, SettingsDep
from gateway.directory.adapters import AdapterError, build_adapter
from gateway.directory.authelia_users import UsersFile, UsersFileError
from gateway.directory.service import PULL_ADAPTERS, decrypt_config, run_pull
from gateway.errors import BadRequestError, NotFoundError
from gateway.identity_registry import record_from_row
from gateway.models import DirectoryEntry, DirectorySyncRun, IdentityProvider

router = APIRouter(prefix="/api/admin", tags=["directory"])


class SyncRequest(BaseModel):
    dry_run: bool = True
    #: Apply even when the mass-deactivation valve trips (after reviewing it).
    force: bool = False


class SyncConfigRequest(BaseModel):
    """Adapter credentials; write-only (never returned)."""

    config: dict[str, Any] = Field(default_factory=dict)


class PreassignRequest(BaseModel):
    groups: list[str] = Field(default_factory=list, max_length=100)


class AutheliaUserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(default="", max_length=255)
    groups: list[str] = Field(default_factory=list, max_length=100)


class AutheliaUserUpdate(BaseModel):
    email: str | None = Field(default=None, min_length=3, max_length=320)
    display_name: str | None = Field(default=None, max_length=255)
    groups: list[str] | None = Field(default=None, max_length=100)
    disabled: bool | None = None


@router.get("/identity-kinds")
async def identity_kinds(admin: AdminUserDep) -> dict[str, Any]:
    """What each kind of directory can do — the console renders from this."""
    from gateway import identity_policy

    return {kind: identity_policy.capabilities(kind).as_dict() for kind in identity_policy.KINDS}


async def _provider(session: Any, provider_id: uuid.UUID) -> IdentityProvider:
    row = await session.get(IdentityProvider, provider_id)
    if row is None:
        raise NotFoundError(f"No identity provider with id {provider_id}.")
    return row


def _run_dict(run: DirectorySyncRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "trigger": run.trigger,
        "dry_run": run.dry_run,
        "status": run.status,
        "seen": run.seen,
        "created": run.created,
        "linked": run.linked,
        "updated": run.updated,
        "deactivated": run.deactivated,
        "reactivated": run.reactivated,
        "changes": run.changes,
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@router.put("/identity-providers/{provider_id}/sync-config")
async def set_sync_config(
    provider_id: uuid.UUID,
    payload: SyncConfigRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
) -> dict[str, Any]:
    row = await _provider(session, provider_id)
    row.sync_config_encrypted = (
        request.app.state.secrets.encrypt(json.dumps(payload.config)) if payload.config else None
    )
    # New credentials, new first run: it is a dry run again.
    row.sync_confirmed = False
    await session.commit()
    return {"has_config": bool(payload.config)}


@router.post("/identity-providers/{provider_id}/sync/test")
async def test_sync(
    provider_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, request: Request
) -> dict[str, Any]:
    """List a handful of people through the adapter; changes nothing."""
    row = await _provider(session, provider_id)
    record = record_from_row(row, request.app.state.secrets)
    try:
        adapter = build_adapter(
            record, decrypt_config(row, request.app.state.secrets), request.app.state.control_http
        )
        entries = await adapter.list_entries()
    except (AdapterError, OSError, ValueError, KeyError) as exc:
        raise BadRequestError(f"The adapter could not list the directory: {exc}") from exc
    return {
        "total": len(entries),
        "sample": [
            {"username": e.username, "email": e.email, "groups": list(e.groups), "active": e.active}
            for e in entries[:5]
        ],
    }


@router.post("/identity-providers/{provider_id}/sync")
async def run_sync(
    provider_id: uuid.UUID,
    payload: SyncRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
) -> dict[str, Any]:
    row = await _provider(session, provider_id)
    if row.sync_adapter not in PULL_ADAPTERS:
        raise BadRequestError("This provider has no pull adapter to run.")
    _report, run = await run_pull(
        request.app.state.session_factory,
        request.app.state.secrets,
        request.app.state.control_http,
        settings,
        provider_id,
        trigger="manual",
        dry_run=payload.dry_run,
        force=payload.force,
        started_by=admin.id,
    )
    return {"run": _run_dict(run), "confirmed": row.sync_confirmed}


@router.post("/identity-providers/{provider_id}/sync/confirm")
async def confirm_sync(
    provider_id: uuid.UUID, admin: AdminUserDep, session: SessionDep
) -> dict[str, Any]:
    """Accept what the reviewed dry run showed; later runs may change accounts."""
    row = await _provider(session, provider_id)
    last = (
        await session.execute(
            select(DirectorySyncRun)
            .where(DirectorySyncRun.provider_id == provider_id)
            .order_by(DirectorySyncRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is None or not last.dry_run or last.status != "ok":
        raise BadRequestError("Run a dry run that succeeds, review it, then confirm.")
    row.sync_confirmed = True
    await session.commit()
    return {"confirmed": True}


@router.get("/identity-providers/{provider_id}/sync/runs")
async def list_runs(
    provider_id: uuid.UUID, admin: AdminUserDep, session: SessionDep
) -> list[dict[str, Any]]:
    runs = (
        await session.execute(
            select(DirectorySyncRun)
            .where(DirectorySyncRun.provider_id == provider_id)
            .order_by(DirectorySyncRun.started_at.desc())
            .limit(20)
        )
    ).scalars()
    return [_run_dict(run) for run in runs]


@router.get("/identity-providers/{provider_id}/directory")
async def list_directory(
    provider_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, unlinked: bool = False
) -> list[dict[str, Any]]:
    query = select(DirectoryEntry).where(DirectoryEntry.provider_id == provider_id)
    if unlinked:
        query = query.where(DirectoryEntry.user_id.is_(None))
    rows = (await session.execute(query.order_by(DirectoryEntry.username))).scalars()
    return [
        {
            "id": str(r.id),
            "external_id": r.external_id,
            "username": r.username,
            "email": r.email,
            "display_name": r.display_name,
            "groups": r.groups,
            "active": r.active,
            "present": r.present,
            "preassigned_groups": r.preassigned_groups,
            "user_id": str(r.user_id) if r.user_id else None,
        }
        for r in rows
    ]


@router.put("/identity-providers/{provider_id}/directory/{entry_id}/preassigned")
async def preassign(
    provider_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: PreassignRequest,
    admin: AdminUserDep,
    session: SessionDep,
) -> dict[str, Any]:
    """Groups for someone who has not signed in yet; manual memberships at link time."""
    row = await session.get(DirectoryEntry, entry_id)
    if row is None or row.provider_id != provider_id:
        raise NotFoundError("No such directory entry.")
    if row.user_id is not None:
        raise BadRequestError("This person has an account already; edit their groups there.")
    row.preassigned_groups = list(dict.fromkeys(g.strip() for g in payload.groups if g.strip()))
    await session.commit()
    return {"preassigned_groups": row.preassigned_groups}


# --- SCIM push credentials (3c) --------------------------------------------


@router.post("/identity-providers/{provider_id}/scim-token")
async def mint_scim_token(
    provider_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, request: Request
) -> dict[str, Any]:
    """A bearer token for the IdP's SCIM client. Shown once; only its hash is kept."""
    row = await _provider(session, provider_id)
    if row.sync_adapter != "scim":
        raise BadRequestError("Set the provider's sync adapter to scim first.")
    token = "scim_" + secrets.token_urlsafe(32)
    config = json.loads(decrypt_config(row, request.app.state.secrets) or "{}")
    config["token_sha256"] = hashlib.sha256(token.encode()).hexdigest()
    row.sync_config_encrypted = request.app.state.secrets.encrypt(json.dumps(config))
    await session.commit()
    base = str(request.base_url).rstrip("/")
    return {"token": token, "endpoint": f"{base}/scim/v2/{row.name}"}


# --- the bundled Authelia's users (D10) -------------------------------------


def _users_file(row: IdentityProvider, request: Request) -> UsersFile:
    if row.kind != "authelia":
        raise BadRequestError("Only a bundled Authelia provider has a users file to manage.")
    config = json.loads(decrypt_config(row, request.app.state.secrets) or "{}")
    return UsersFile(Path(config.get("path") or "/authelia/users_database.yml"))


def _users_error(exc: UsersFileError) -> BadRequestError:
    return BadRequestError(str(exc), code="authelia_users")


@router.get("/identity-providers/{provider_id}/authelia-users")
async def list_authelia_users(
    provider_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, request: Request
) -> list[dict[str, Any]]:
    try:
        return [
            u.as_dict() for u in _users_file(await _provider(session, provider_id), request).list()
        ]
    except UsersFileError as exc:
        raise _users_error(exc) from exc


@router.post(
    "/identity-providers/{provider_id}/authelia-users", status_code=status.HTTP_201_CREATED
)
async def create_authelia_user(
    provider_id: uuid.UUID,
    payload: AutheliaUserCreate,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
) -> dict[str, Any]:
    """Create a person in the bundled directory. The password is minted and shown once."""
    users = _users_file(await _provider(session, provider_id), request)
    try:
        user, password = users.create(
            payload.username, payload.email, payload.display_name, payload.groups
        )
    except UsersFileError as exc:
        raise _users_error(exc) from exc
    return {"user": user.as_dict(), "password": password}


@router.patch("/identity-providers/{provider_id}/authelia-users/{username}")
async def update_authelia_user(
    provider_id: uuid.UUID,
    username: str,
    payload: AutheliaUserUpdate,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
) -> dict[str, Any]:
    users = _users_file(await _provider(session, provider_id), request)
    try:
        return users.update(username, **payload.model_dump(exclude_unset=True)).as_dict()
    except UsersFileError as exc:
        raise _users_error(exc) from exc


@router.post("/identity-providers/{provider_id}/authelia-users/{username}/reset-password")
async def reset_authelia_password(
    provider_id: uuid.UUID,
    username: str,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
) -> dict[str, Any]:
    users = _users_file(await _provider(session, provider_id), request)
    try:
        return {"password": users.reset_password(username)}
    except UsersFileError as exc:
        raise _users_error(exc) from exc


@router.delete(
    "/identity-providers/{provider_id}/authelia-users/{username}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_authelia_user(
    provider_id: uuid.UUID,
    username: str,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
) -> None:
    users = _users_file(await _provider(session, provider_id), request)
    try:
        users.delete(username)
    except UsersFileError as exc:
        raise _users_error(exc) from exc
