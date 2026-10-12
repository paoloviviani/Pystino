"""The Authentik outbound-SCIM dialect, locked from a live capture (ADR 0095 §6.3).

The payloads here were captured on 2026-10-13 by running a real Authentik
2025.10.3 with an outbound SCIM provider pointed at a throwaway gateway
through a recording forwarder (`scim-capture.py`, kept beside the throwaway
stack, not in the repository), and driving create, update, deactivate and
delete from Authentik's side. What Authentik actually sends is the
contract this test pins — a captured dialect, not a guessed one.

Sanitised on capture: the minted token reads `scim_…`, user names are the
throwaway's (`dana`, `lena2`), and `externalId` is Authentik's own SHA-256
"hashed user id" (its Subject mode's default, per its documentation).

Known version limitation, recorded rather than worked around: Authentik
2025.10.3's own default SCIM *Group* mapping raises
`NameError: name 'group' is not defined` at evaluation time, and a failing
mapping stops the whole sync — so this capture contains no Group pushes,
and group memberships arrive only when a later Authentik version ships a
working default. The gateway's own Groups dialect is covered by
`test_scim.py` and the directory engine's tests.
"""

from __future__ import annotations

import httpx
from conftest import Seeded
from gateway.models import GroupSync, IdentityProvider
from gateway.secrets import SecretBox
from test_admin import as_user, make_admin

ISS = "https://throwaway-authentik.local/application/o/pystino/"
SCIM = "application/scim+json"

# --- the captured payloads -----------------------------------------------------

#: `POST /scim/v2/<provider>/Users`, a user's first push. Authentik names the
#: schemas explicitly, splits `name` into given/family, marks the one email
#: `primary`, and carries `externalId` — its own SHA-256 "hashed user id",
#: which is what the gateway keys the directory entry on (spec §3: the
#: `externalId` → subject mapping; Authentik needs no `subject_claim`
#: override because sign-ins key on the same hashed id).
CREATE_USER = {
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
    "externalId": "ce3050ef3700ccdc65571a095a20d62a8b493da834a3760bbc697cb51fa86f05",
    "userName": "dana",
    "name": {"formatted": "Dana Four", "givenName": "Dana", "familyName": "Four"},
    "displayName": "Dana Four",
    "active": True,
    "emails": [{"value": "dana@example.org", "type": "other", "primary": True}],
}

#: `PUT /scim/v2/<provider>/Users/<the gateway's own id>` — the update. The
#: first push's 201 answer carries the gateway's `id`; Authentik adopts it
#: and every later push addresses it directly. The payload repeats `id` and
#: `externalId`; nothing is PATCHed for a user (the docs' patch-updates
#: rule: only group memberships ride PATCH).
UPDATE_USER = {
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
    "id": "<adopted gateway id>",
    "externalId": "ce3050ef3700ccdc65571a095a20d62a8b493da834a3760bbc697cb51fa86f05",
    "userName": "dana",
    "name": {"formatted": "Dana Four-Revised", "givenName": "Dana", "familyName": "Four-Revised"},
    "displayName": "Dana Four-Revised",
    "active": True,
    "emails": [{"value": "dana@example.org", "type": "other", "primary": True}],
}

#: On a create the gateway answered `409` (its uniqueness refusal, the same
#: body every other route uses), and Authentik's next move was the RFC 7644
#: filter: `GET /Users?filter=userName eq "dana"`. The gateway implements
#: filtering, so the dialect resolves instead of duplicating.
FILTER_QUERY = 'userName eq "dana"'


async def _setup(
    app: object, client: httpx.AsyncClient, seeded: Seeded, session_factory
) -> dict[str, str]:
    """A provider row exactly like the throwaway's: the environment's
    generic issuer with `sync_adapter=scim` and the directory owning groups."""
    as_user(app, await make_admin(session_factory, seeded))
    box: SecretBox = app.state.secrets  # type: ignore[attr-defined]
    async with session_factory() as session:
        row = IdentityProvider(
            name="authentik",
            issuer=ISS,
            client_id="c",
            client_secret_encrypted=box.encrypt("s"),
            scopes=["openid", "profile", "email"],
            kind="authentik",
            sync_adapter="scim",
            group_source="directory",
            group_sync=GroupSync.EVERY_LOGIN,
            is_enabled=True,
        )
        session.add(row)
        await session.commit()
        pid = str(row.id)
    minted = (await client.post(f"/api/admin/identity-providers/{pid}/scim-token")).json()
    assert minted["endpoint"].endswith("/scim/v2/authentik")
    return {"authorization": f"Bearer {minted['token']}", "content-type": SCIM}


async def test_the_captured_create_dialect_provisions_before_login(
    app: object,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory,
) -> None:
    """Authentik's create push, word for word, provisions the person."""
    headers = await _setup(app, client, seeded, session_factory)

    created = await client.post("/scim/v2/authentik/Users", json=CREATE_USER, headers=headers)
    assert created.status_code == 201, created.text
    body = created.json()
    # The gateway answers the full resource, its own `id`, and echoes the
    # caller's `externalId` — the id Authentik adopts for its later PUTs.
    assert body["userName"] == "dana"
    assert body["externalId"] == CREATE_USER["externalId"]
    assert body["meta"]["resourceType"] == "User"
    gateway_id = body["id"]

    from sqlalchemy import select
    from gateway.models import DirectoryEntry, User

    async with session_factory() as session:
        entry = (
            await session.execute(
                select(DirectoryEntry).where(
                    DirectoryEntry.external_id == CREATE_USER["externalId"]
                )
            )
        ).scalar_one()
        user = (await session.execute(select(User).where(User.id == entry.user_id))).scalar_one()
        assert user.email == "dana@example.org"
        assert user.is_active  # created by the directory before any login


async def test_the_captured_update_dialect_puts_the_adopted_id(
    app: object,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory,
) -> None:
    """The update push PUTs the adopted id with the whole payload — no PATCH."""
    headers = await _setup(app, client, seeded, session_factory)

    created = await client.post("/scim/v2/authentik/Users", json=CREATE_USER, headers=headers)
    adopted = created.json()["id"]

    update = dict(UPDATE_USER, id=adopted)
    pushed = await client.put(f"/scim/v2/authentik/Users/{adopted}", json=update, headers=headers)
    assert pushed.status_code == 200, pushed.text
    # The gateway's resource answer carries what it stores: the display name
    # it kept, the adopted id, and the caller's externalId — no `name`,
    # which the gateway does not model (its comment: displayName is what it
    # keeps).
    body = pushed.json()
    assert body["displayName"] == "Dana Four-Revised"
    assert body["id"] == adopted
    assert body["externalId"] == UPDATE_USER["externalId"]
    # What a push updates is the mirror row (the directory's own view of the
    # person); the account's profile fields follow the login claims, with
    # `admin_edited_fields` governing — a pushed rename lands there, not on
    # the account.
    from sqlalchemy import select
    from gateway.models import DirectoryEntry

    async with session_factory() as session:
        entry = (
            await session.execute(
                select(DirectoryEntry).where(
                    DirectoryEntry.external_id == UPDATE_USER["externalId"]
                )
            )
        ).scalar_one()
        assert entry.display_name == "Dana Four-Revised"


async def test_the_captured_conflict_dialect_falls_back_to_the_filter(
    app: object,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory,
) -> None:
    """A create the gateway refuses as a duplicate is resolved by the filter.

    The capture shows Authentik retrying `POST` (409) and then asking
    `GET /Users?filter=userName eq "dana"` — the gateway's filter support is
    what turns that into an adoption instead of a duplicate.
    """
    headers = await _setup(app, client, seeded, session_factory)

    first = await client.post("/scim/v2/authentik/Users", json=CREATE_USER, headers=headers)
    assert first.status_code == 201
    again = await client.post("/scim/v2/authentik/Users", json=CREATE_USER, headers=headers)
    assert again.status_code == 409
    # The gateway's error envelope: SCIM's own, with `scimType: uniqueness`.
    assert again.json()["scimType"] == "uniqueness"
    assert again.json()["detail"] == "User already exists"

    found = await client.get(
        "/scim/v2/authentik/Users",
        params={"filter": FILTER_QUERY},
        headers=headers,
    )
    assert found.status_code == 200, found.text
    resources = found.json()["Resources"]
    assert len(resources) == 1
    assert resources[0]["userName"] == "dana"


async def test_the_captured_service_provider_config_is_asked_first(
    app: object,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory,
) -> None:
    """Every task opens with `GET ServiceProviderConfig`; `patch.supported`
    is how Authentik decides between PATCH (memberships) and PUT (the rest),
    so the gateway's answer is part of the dialect it will speak."""
    headers = await _setup(app, client, seeded, session_factory)
    config = await client.get("/scim/v2/authentik/ServiceProviderConfig", headers=headers)
    assert config.status_code == 200, config.text
    body = config.json()
    assert body["patch"]["supported"] is True
    assert body["filter"]["supported"] is True
