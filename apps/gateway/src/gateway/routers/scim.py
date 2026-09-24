"""Inbound SCIM 2.0 (RFC 7643/7644) — the push half of directory sync (ADR 0088 draft).

Entra ID, Okta, Authentik, JumpCloud and OneLogin all provision applications
by *pushing* SCIM, so one endpoint here covers them all. Each provider gets its
own base URL, `/scim/v2/<provider name>`, and bearer token (minted in the
console, only its SHA-256 kept). Writes land in the same directory mirror a
pull adapter fills and go through the same engine, so a pushed deactivation
obeys exactly the rules a pulled one does — deactivate, never delete, revoke
only what the directory granted.

The subset implemented is what those IdPs send: Users (list with a userName
filter, get, create, replace, patch, delete) and Groups (list with a
displayName filter, get, create, patch members, delete), plus the discovery
documents. Users are keyed by `externalId` when the IdP sends one — map it to
the OIDC subject (Entra: objectId, with the provider's subject_claim `oid`),
so an account can exist before its first login — else by userName.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.directory.adapters import Entry
from gateway.directory.engine import apply_entries, record_run
from gateway.identity_registry import record_from_row
from gateway.models import DirectoryEntry, IdentityProvider
from gateway.types import utcnow

router = APIRouter(prefix="/scim/v2", tags=["scim"])

SCIM_JSON = "application/scim+json"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
_EQ_FILTER = re.compile(r'^\s*(\w+)\s+eq\s+"([^"]*)"\s*$', re.IGNORECASE)


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        super().__init__(detail)
        self.status, self.detail, self.scim_type = status, detail, scim_type


def _json(body: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=SCIM_JSON)


def _error(exc: ScimError) -> JSONResponse:
    body: dict[str, Any] = {
        "schemas": [ERROR_SCHEMA],
        "status": str(exc.status),
        "detail": exc.detail,
    }
    if exc.scim_type:
        body["scimType"] = exc.scim_type
    return _json(body, exc.status)


async def _authorise(request: Request, session: AsyncSession, provider: str) -> IdentityProvider:
    row = (
        await session.execute(select(IdentityProvider).where(IdentityProvider.name == provider))
    ).scalar_one_or_none()
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    expected = ""
    if row is not None and row.sync_adapter == "scim" and row.sync_config_encrypted:
        config = json.loads(request.app.state.secrets.decrypt(row.sync_config_encrypted))
        expected = config.get("token_sha256", "")
    given = hashlib.sha256(token.encode()).hexdigest()
    # One answer for "no such provider", "not a SCIM provider" and "wrong
    # token", and a constant-time compare for the last.
    if not token or not expected or not hmac.compare_digest(given, expected) or not row.is_enabled:
        raise ScimError(401, "Unauthorized")
    return row


def _user_resource(row: DirectoryEntry, request: Request, provider: str) -> dict[str, Any]:
    base = str(request.base_url).rstrip("/")
    resource: dict[str, Any] = {
        "schemas": [USER_SCHEMA],
        "id": str(row.id),
        "externalId": row.external_id,
        "userName": row.username or row.external_id,
        "displayName": row.display_name,
        "active": bool(row.active and row.present),
        "emails": [{"value": row.email, "primary": True, "type": "work"}] if row.email else [],
        "groups": [{"display": g} for g in row.groups or []],
        "meta": {
            "resourceType": "User",
            "location": f"{base}/scim/v2/{provider}/Users/{row.id}",
            "lastModified": row.last_seen_at.isoformat() if row.last_seen_at else None,
        },
    }
    return resource


def _entry_from_payload(body: dict[str, Any], current: DirectoryEntry | None = None) -> Entry:
    emails = body.get("emails") or []
    primary = next((e for e in emails if e.get("primary")), emails[0] if emails else {})
    name = body.get("name") or {}
    display = (
        body.get("displayName")
        or name.get("formatted")
        or " ".join(p for p in (name.get("givenName"), name.get("familyName")) if p)
    )
    external = body.get("externalId") or (current.external_id if current else None)
    username = body.get("userName") or (current.username if current else None)
    if not (external or username):
        raise ScimError(400, "userName is required", "invalidValue")
    return Entry(
        external_id=str(external or username),
        username=username,
        email=primary.get("value") or (current.email if current else None),
        display_name=display or (current.display_name if current else None),
        groups=tuple(current.groups or ()) if current else (),
        active=bool(body.get("active", True)),
        # Keyed by externalId: that is the OIDC subject the IdP was told to send.
        is_subject=bool(
            body.get("externalId") or (current and current.external_id != current.username)
        ),
    )


async def _apply(
    request: Request, session: AsyncSession, row: IdentityProvider, entries: list[Entry]
) -> None:
    record = record_from_row(row, request.app.state.secrets)
    started = utcnow()
    report = await apply_entries(
        session,
        record,
        entries,
        settings=request.app.state.settings.oidc,
        dry_run=False,
        full=False,
        force=True,
    )
    if report.changes:
        await record_run(session, row.id, report, trigger="push", started_at=started)


async def _entry(session: AsyncSession, provider_id: uuid.UUID, entry_id: str) -> DirectoryEntry:
    try:
        key = uuid.UUID(entry_id)
    except ValueError:
        raise ScimError(404, "Resource not found") from None
    row = await session.get(DirectoryEntry, key)
    if row is None or row.provider_id != provider_id or not row.present:
        raise ScimError(404, "Resource not found")
    return row


async def _entry_by_external(
    session: AsyncSession, provider_id: uuid.UUID, external_id: str
) -> DirectoryEntry | None:
    return (
        await session.execute(
            select(DirectoryEntry).where(
                DirectoryEntry.provider_id == provider_id,
                DirectoryEntry.external_id == external_id,
            )
        )
    ).scalar_one_or_none()


def _as_entry(row: DirectoryEntry, **changes: Any) -> Entry:
    base = {
        "external_id": row.external_id,
        "username": row.username,
        "email": row.email,
        "display_name": row.display_name,
        "groups": tuple(row.groups or ()),
        "active": row.active,
        "is_subject": row.external_id != row.username,
    }
    base.update(changes)
    return Entry(**base)


def _groups_registry(
    row: IdentityProvider, request: Request
) -> tuple[dict[str, Any], dict[str, str]]:
    config = json.loads(request.app.state.secrets.decrypt(row.sync_config_encrypted) or "{}")
    return config, dict(config.get("scim_groups") or {})


def _save_groups(
    row: IdentityProvider, request: Request, config: dict[str, Any], groups: dict[str, str]
) -> None:
    config["scim_groups"] = groups
    row.sync_config_encrypted = request.app.state.secrets.encrypt(json.dumps(config))


def _group_id(provider_id: uuid.UUID, display: str) -> str:
    return str(uuid.uuid5(provider_id, f"group:{display}"))


# --- routes ------------------------------------------------------------------


async def _handle(request: Request, provider: str, work: Any) -> Response:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        try:
            row = await _authorise(request, session, provider)
            return await work(session, row)
        except ScimError as exc:
            await session.rollback()
            return _error(exc)


@router.get("/{provider}/ServiceProviderConfig")
async def service_provider_config(request: Request, provider: str) -> Response:
    async def work(session: AsyncSession, row: IdentityProvider) -> Response:
        return _json(
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
                "patch": {"supported": True},
                "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
                "filter": {"supported": True, "maxResults": 200},
                "changePassword": {"supported": False},
                "sort": {"supported": False},
                "etag": {"supported": False},
                "authenticationSchemes": [
                    {
                        "type": "oauthbearertoken",
                        "name": "Bearer token",
                        "primary": True,
                        "description": "Minted per provider in the Pystino console",
                    }
                ],
            }
        )

    return await _handle(request, provider, work)


@router.get("/{provider}/ResourceTypes")
async def resource_types(request: Request, provider: str) -> Response:
    async def work(session: AsyncSession, row: IdentityProvider) -> Response:
        types = [
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                "id": "User",
                "name": "User",
                "endpoint": "/Users",
                "schema": USER_SCHEMA,
            },
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                "id": "Group",
                "name": "Group",
                "endpoint": "/Groups",
                "schema": GROUP_SCHEMA,
            },
        ]
        return _json({"schemas": [LIST_SCHEMA], "totalResults": 2, "Resources": types})

    return await _handle(request, provider, work)


@router.get("/{provider}/Users")
async def list_users(request: Request, provider: str) -> Response:
    async def work(session: AsyncSession, row: IdentityProvider) -> Response:
        query = select(DirectoryEntry).where(
            DirectoryEntry.provider_id == row.id, DirectoryEntry.present.is_(True)
        )
        if flt := request.query_params.get("filter"):
            match = _EQ_FILTER.match(flt)
            if not match or match.group(1).lower() not in ("username", "externalid"):
                raise ScimError(400, f"unsupported filter {flt!r}", "invalidFilter")
            column = (
                DirectoryEntry.username
                if match.group(1).lower() == "username"
                else DirectoryEntry.external_id
            )
            query = query.where(column == match.group(2))
        rows = list((await session.execute(query.order_by(DirectoryEntry.first_seen_at))).scalars())
        start = max(int(request.query_params.get("startIndex", 1)), 1)
        count = min(int(request.query_params.get("count", 100)), 200)
        page = rows[start - 1 : start - 1 + count]
        return _json(
            {
                "schemas": [LIST_SCHEMA],
                "totalResults": len(rows),
                "startIndex": start,
                "itemsPerPage": len(page),
                "Resources": [_user_resource(r, request, provider) for r in page],
            }
        )

    return await _handle(request, provider, work)


@router.get("/{provider}/Users/{entry_id}")
async def get_user(request: Request, provider: str, entry_id: str) -> Response:
    async def work(session: AsyncSession, row: IdentityProvider) -> Response:
        return _json(_user_resource(await _entry(session, row.id, entry_id), request, provider))

    return await _handle(request, provider, work)


@router.post("/{provider}/Users")
async def create_user(request: Request, provider: str) -> Response:
    async def work(session: AsyncSession, row: IdentityProvider) -> Response:
        body = await request.json()
        entry = _entry_from_payload(body)
        existing = await _entry_by_external(session, row.id, entry.external_id)
        if existing is not None and existing.present:
            raise ScimError(409, "User already exists", "uniqueness")
        await _apply(request, session, row, [entry])
        created = await _entry_by_external(session, row.id, entry.external_id)
        return _json(_user_resource(created, request, provider), 201)

    return await _handle(request, provider, work)


@router.put("/{provider}/Users/{entry_id}")
async def replace_user(request: Request, provider: str, entry_id: str) -> Response:
    async def work(session: AsyncSession, row: IdentityProvider) -> Response:
        current = await _entry(session, row.id, entry_id)
        entry = _entry_from_payload(await request.json(), current)
        entry = Entry(**{**entry.__dict__, "external_id": current.external_id})
        await _apply(request, session, row, [entry])
        return _json(_user_resource(await _entry(session, row.id, entry_id), request, provider))

    return await _handle(request, provider, work)


def _patch_user(current: DirectoryEntry, operations: list[dict[str, Any]]) -> Entry:
    fields: dict[str, Any] = {}
    for op in operations:
        kind = str(op.get("op", "")).lower()
        path = str(op.get("path") or "")
        value = op.get("value")
        if kind not in ("replace", "add", "remove"):
            raise ScimError(400, f"unsupported op {kind!r}", "invalidSyntax")
        items = value.items() if (not path and isinstance(value, dict)) else [(path, value)]
        for key, val in items:
            key = key.lower()
            if key == "active":
                fields["active"] = str(val).lower() == "true" if isinstance(val, str) else bool(val)
            elif key == "displayname":
                fields["display_name"] = val
            elif key == "username":
                fields["username"] = val
            elif key.startswith("emails"):
                email = val[0].get("value") if isinstance(val, list) and val else val
                fields["email"] = email if kind != "remove" else None
            elif key.startswith("name"):
                continue  # displayName carries what we keep
            # Unknown attributes are accepted and ignored, as RFC 7644 allows
            # for attributes the service provider does not store.
    return _as_entry(current, **fields)


@router.patch("/{provider}/Users/{entry_id}")
async def patch_user(request: Request, provider: str, entry_id: str) -> Response:
    async def work(session: AsyncSession, row: IdentityProvider) -> Response:
        current = await _entry(session, row.id, entry_id)
        body = await request.json()
        await _apply(request, session, row, [_patch_user(current, body.get("Operations") or [])])
        return _json(_user_resource(await _entry(session, row.id, entry_id), request, provider))

    return await _handle(request, provider, work)


@router.delete("/{provider}/Users/{entry_id}")
async def delete_user(request: Request, provider: str, entry_id: str) -> Response:
    """Deprovision: the account is deactivated, never deleted (the ledger keeps its rows)."""

    async def work(session: AsyncSession, row: IdentityProvider) -> Response:
        current = await _entry(session, row.id, entry_id)
        await _apply(request, session, row, [_as_entry(current, active=False)])
        current = await session.get(DirectoryEntry, current.id)
        current.present = False
        await session.commit()
        return Response(status_code=204)

    return await _handle(request, provider, work)


def _group_resource(
    gid: str, display: str, members: list[DirectoryEntry], request: Request, provider: str
) -> dict[str, Any]:
    base = str(request.base_url).rstrip("/")
    return {
        "schemas": [GROUP_SCHEMA],
        "id": gid,
        "displayName": display,
        "members": [{"value": str(m.id), "display": m.username} for m in members],
        "meta": {"resourceType": "Group", "location": f"{base}/scim/v2/{provider}/Groups/{gid}"},
    }


async def _members(
    session: AsyncSession, provider_id: uuid.UUID, display: str
) -> list[DirectoryEntry]:
    rows = (
        await session.execute(
            select(DirectoryEntry).where(
                DirectoryEntry.provider_id == provider_id, DirectoryEntry.present.is_(True)
            )
        )
    ).scalars()
    return [r for r in rows if display in (r.groups or [])]


async def _set_membership(
    request: Request,
    session: AsyncSession,
    row: IdentityProvider,
    display: str,
    add: list[str],
    remove: list[str],
) -> None:
    changed: list[Entry] = []
    for entry_id in add:
        member = await _entry(session, row.id, entry_id)
        if display not in (member.groups or []):
            changed.append(_as_entry(member, groups=(*member.groups, display)))
    for entry_id in remove:
        member = await _entry(session, row.id, entry_id)
        if display in (member.groups or []):
            changed.append(
                _as_entry(member, groups=tuple(g for g in member.groups if g != display))
            )
    if changed:
        await _apply(request, session, row, changed)


@router.get("/{provider}/Groups")
async def list_groups(request: Request, provider: str) -> Response:
    async def work(session: AsyncSession, row: IdentityProvider) -> Response:
        _, groups = _groups_registry(row, request)
        items = list(groups.items())
        if flt := request.query_params.get("filter"):
            match = _EQ_FILTER.match(flt)
            if not match or match.group(1).lower() != "displayname":
                raise ScimError(400, f"unsupported filter {flt!r}", "invalidFilter")
            items = [(gid, d) for gid, d in items if d == match.group(2)]
        resources = [
            _group_resource(gid, d, await _members(session, row.id, d), request, provider)
            for gid, d in items
        ]
        return _json(
            {
                "schemas": [LIST_SCHEMA],
                "totalResults": len(resources),
                "startIndex": 1,
                "itemsPerPage": len(resources),
                "Resources": resources,
            }
        )

    return await _handle(request, provider, work)


@router.get("/{provider}/Groups/{group_id}")
async def get_group(request: Request, provider: str, group_id: str) -> Response:
    async def work(session: AsyncSession, row: IdentityProvider) -> Response:
        _, groups = _groups_registry(row, request)
        if group_id not in groups:
            raise ScimError(404, "Resource not found")
        display = groups[group_id]
        return _json(
            _group_resource(
                group_id, display, await _members(session, row.id, display), request, provider
            )
        )

    return await _handle(request, provider, work)


@router.post("/{provider}/Groups")
async def create_group(request: Request, provider: str) -> Response:
    async def work(session: AsyncSession, row: IdentityProvider) -> Response:
        body = await request.json()
        display = str(body.get("displayName") or "").strip()
        if not display:
            raise ScimError(400, "displayName is required", "invalidValue")
        config, groups = _groups_registry(row, request)
        gid = _group_id(row.id, display)
        if gid in groups:
            raise ScimError(409, "Group already exists", "uniqueness")
        groups[gid] = display
        _save_groups(row, request, config, groups)
        await session.commit()
        await _set_membership(
            request, session, row, display, [m["value"] for m in body.get("members") or []], []
        )
        return _json(
            _group_resource(
                gid, display, await _members(session, row.id, display), request, provider
            ),
            201,
        )

    return await _handle(request, provider, work)


_MEMBER_FILTER = re.compile(r'members\[value eq "([^"]+)"\]', re.IGNORECASE)


@router.patch("/{provider}/Groups/{group_id}")
async def patch_group(request: Request, provider: str, group_id: str) -> Response:
    async def work(session: AsyncSession, row: IdentityProvider) -> Response:
        config, groups = _groups_registry(row, request)
        if group_id not in groups:
            raise ScimError(404, "Resource not found")
        display = groups[group_id]
        add: list[str] = []
        remove: list[str] = []
        for op in (await request.json()).get("Operations") or []:
            kind = str(op.get("op", "")).lower()
            path = str(op.get("path") or "")
            value = op.get("value")
            if path.lower() == "members" and kind in ("add", "replace"):
                add += [m["value"] for m in value or []]
                if kind == "replace":
                    current = {str(m.id) for m in await _members(session, row.id, display)}
                    remove += sorted(current - set(add))
            elif path.lower() == "members" and kind == "remove":
                remove += (
                    [m["value"] for m in value or []]
                    if value
                    else [str(m.id) for m in await _members(session, row.id, display)]
                )
            elif (match := _MEMBER_FILTER.fullmatch(path)) and kind == "remove":
                remove.append(match.group(1))
            elif kind == "replace" and (path.lower() == "displayname" or isinstance(value, dict)):
                new = value if isinstance(value, str) else value.get("displayName")
                if new and new != display:
                    for member in await _members(session, row.id, display):
                        await _set_membership(request, session, row, display, [], [str(member.id)])
                        await _set_membership(request, session, row, new, [str(member.id)], [])
                    groups[group_id] = new
                    display = new
                    _save_groups(row, request, config, groups)
                    await session.commit()
        await _set_membership(request, session, row, display, add, remove)
        return _json(
            _group_resource(
                group_id, display, await _members(session, row.id, display), request, provider
            )
        )

    return await _handle(request, provider, work)


@router.delete("/{provider}/Groups/{group_id}")
async def delete_group(request: Request, provider: str, group_id: str) -> Response:
    async def work(session: AsyncSession, row: IdentityProvider) -> Response:
        config, groups = _groups_registry(row, request)
        display = groups.pop(group_id, None)
        if display is None:
            raise ScimError(404, "Resource not found")
        members = [str(m.id) for m in await _members(session, row.id, display)]
        await _set_membership(request, session, row, display, [], members)
        row = await session.get(IdentityProvider, row.id)
        _save_groups(row, request, config, groups)
        await session.commit()
        return Response(status_code=204)

    return await _handle(request, provider, work)
