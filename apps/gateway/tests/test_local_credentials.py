"""ADR 0046: the local door issues `/v1` credentials.

The ADR names the test list; this file is that list:

- the login mints when a client is named, and mints nothing otherwise;
- re-login rotates the refresh credential — the old one stops working;
- the exchange mints a `/v1`-usable access credential, sweeps only dead rows,
  and leaves live ones alone (a concurrent turn may be holding one);
- revoking kills the family, and both are refused afterwards;
- a disabled user is refused at exchange *and* on `/v1`;
- a `/v1` request with a local access credential bills the local user's own
  default billing group — the row the feature exists to reach;
- machine keys never appear in the console's key listing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest_asyncio
from conftest import FakeUpstream
from gateway.login_throttle import LoginThrottle
from gateway.models import (
    ApiKey,
    Group,
    GroupModelAccess,
    LocalCredential,
    Membership,
    ModelDef,
    ModelPrice,
    Provider,
    RefreshCredential,
    UsageRecord,
    User,
)
from gateway.secrets import SecretBox, hint_for
from gateway.security import generate_api_key
from gateway.types import utcnow
from helpers import completion_body
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PASSWORD = "correct horse battery staple"
LOGIN = {"email": "member@example.org", "password": PASSWORD, "client": "chat"}


def enable_local_auth(app: object) -> LoginThrottle:
    """The throttle `init_app_state` would have built (copied from test_local_auth)."""
    throttle = LoginThrottle(max_failed_attempts=10, window_seconds=900)
    app.state.login_throttle = throttle  # type: ignore[attr-defined]
    return throttle


@dataclass
class LocalWorld:
    user: User
    group: Group
    model: ModelDef

    @property
    def login(self) -> dict[str, str]:
        return dict(LOGIN)


@pytest_asyncio.fixture
async def local(
    app: object, session_factory: async_sessionmaker[AsyncSession]
) -> LocalWorld:
    """A local user with a password, a group, and a priced model the group may use."""
    enable_local_auth(app)
    box = SecretBox(["test-encryption-key-not-for-production"])
    from gateway.passwords import hash_password

    async with session_factory() as db:
        group = Group(name="research", description="local test group")
        db.add(group)
        await db.flush()
        user = User(
            issuer="local",
            subject="member@example.org",
            email="member@example.org",
            display_name="Local Member",
            default_billing_group_id=group.id,
        )
        db.add(user)
        await db.flush()
        db.add(Membership(user_id=user.id, group_id=group.id))
        db.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))

        provider = Provider(
            name="fake",
            base_url="http://fake-upstream/v1",
            api_key_encrypted=box.encrypt("upstream-key"),
            api_key_hint=hint_for("upstream-key"),
        )
        db.add(provider)
        await db.flush()
        model = ModelDef(
            name="test-model",
            upstream_model="upstream/test-model",
            provider_id=provider.id,
            context_window=8192,
        )
        db.add(model)
        await db.flush()
        db.add(
            ModelPrice(
                model_id=model.id,
                input_per_mtok=Decimal("1"),
                output_per_mtok=Decimal("2"),
                currency="EUR",
            )
        )
        db.add(GroupModelAccess(group_id=group.id, model_id=model.id))
        await db.commit()
        await db.refresh(user)
        await db.refresh(group)
        await db.refresh(model)
    return LocalWorld(user=user, group=group, model=model)


async def _login(client: httpx.AsyncClient, body: dict[str, object]) -> httpx.Response:
    return await client.post("/auth/login", json=body)


async def _exchange(
    client: httpx.AsyncClient, refresh_token: str
) -> tuple[int, dict[str, object]]:
    response = await client.post("/auth/token", json={"refresh_token": refresh_token})
    try:
        payload: dict[str, object] = response.json()
    except ValueError:
        payload = {}
    return response.status_code, payload


# --------------------------------------------------------------------------
# the login mints
# --------------------------------------------------------------------------


async def test_login_without_client_mints_nothing(
    client: httpx.AsyncClient,
    local: LocalWorld,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The console's login is bit-identical: no client named, no credential."""
    response = await _login(client, {"email": "member@example.org", "password": PASSWORD})
    assert response.status_code == 200
    assert "refresh_token" not in response.json()
    async with session_factory() as db:
        rows = (await db.execute(select(RefreshCredential))).scalars().all()
    assert rows == []


async def test_login_with_client_returns_refresh_credential_and_identity(
    client: httpx.AsyncClient, local: LocalWorld
) -> None:
    response = await _login(client, local.login)
    assert response.status_code == 200
    body = response.json()
    assert body["refresh_token"].startswith("gwr_")
    assert body["email"] == "member@example.org"
    assert body["display_name"] == "Local Member"
    assert body["groups"] == ["research"]
    assert body["is_admin"] is False


async def test_second_login_rotates_the_credential(
    client: httpx.AsyncClient, local: LocalWorld
) -> None:
    """The old refresh credential dies at re-login; only the newest works."""
    first = (await _login(client, local.login)).json()["refresh_token"]
    second = (await _login(client, local.login)).json()["refresh_token"]
    assert first != second

    assert (await _exchange(client, first))[0] == 401
    assert (await _exchange(client, second))[0] == 200


async def test_login_refused_when_local_auth_is_off(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """No throttle wired means no local door at all — mint or no client."""
    async with session_factory() as db:
        from gateway.passwords import hash_password

        user = User(issuer="local", subject="off@example.org", email="off@example.org")
        db.add(user)
        await db.flush()
        db.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
        await db.commit()

    body = dict(LOGIN) | {"email": "off@example.org"}
    response = await _login(client, body)
    assert response.status_code == 503


# --------------------------------------------------------------------------
# the exchange
# --------------------------------------------------------------------------


async def test_exchange_mints_a_usable_access_credential(
    client: httpx.AsyncClient, local: LocalWorld
) -> None:
    refresh = (await _login(client, local.login)).json()["refresh_token"]
    status, body = await _exchange(client, refresh)
    assert status == 200
    assert isinstance(body["access_token"], str)
    assert body["access_token"].startswith("gwa_")
    assert body["expires_in"] == 900

    # The decisive property: it authenticates on /v1 as the local user.
    listing = await client.get(
        "/v1/models", headers={"authorization": f"Bearer {body['access_token']}"}
    )
    assert listing.status_code == 200


async def test_exchange_leaves_live_keys_alone_and_sweeps_dead_ones(
    client: httpx.AsyncClient,
    local: LocalWorld,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    refresh = (await _login(client, local.login)).json()["refresh_token"]
    await _exchange(client, refresh)
    await _exchange(client, refresh)

    stale = ApiKey(
        user_id=local.user.id,
        prefix="gwa_stale_stale",
        key_hash=generate_api_key(environment_prefix="gwa").key_hash,
        minted_by="chat",
        expires_at=utcnow() - timedelta(seconds=1),
    )
    async with session_factory() as db:
        live = (
            await db.execute(
                select(ApiKey).where(ApiKey.minted_by == "chat", ApiKey.revoked_at.is_(None))
            )
        ).scalars().all()
        assert len(live) == 2
        # An expired row — as if time had passed — is what the next exchange sweeps.
        db.add(stale)
        await db.commit()

    await _exchange(client, refresh)

    async with session_factory() as db:
        assert await db.get(ApiKey, stale.id) is None


async def test_exchange_refuses_wrong_and_malformed_uniformly(
    client: httpx.AsyncClient, local: LocalWorld
) -> None:
    for bad in ["gwr_nope_not_the_secret_at_all", "not-a-credential"]:
        status, body = await _exchange(client, bad)
        assert status == 401
        assert body["error"]["message"] == "The refresh credential is not valid."


async def test_expired_refresh_credential_is_refused(
    client: httpx.AsyncClient,
    local: LocalWorld,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    refresh = (await _login(client, local.login)).json()["refresh_token"]
    async with session_factory() as db:
        row = (await db.execute(select(RefreshCredential))).scalar_one()
        row.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    assert (await _exchange(client, refresh))[0] == 401


# --------------------------------------------------------------------------
# revocation and the disabled user
# --------------------------------------------------------------------------


async def test_revoke_kills_the_family(client: httpx.AsyncClient, local: LocalWorld) -> None:
    refresh = (await _login(client, local.login)).json()["refresh_token"]
    _, access = await _exchange(client, refresh)
    assert isinstance(access["access_token"], str)

    response = await client.post("/auth/revoke", json={"refresh_token": refresh})
    assert response.status_code == 204

    assert (await _exchange(client, refresh))[0] == 401
    refused = await client.get(
        "/v1/models", headers={"authorization": f"Bearer {access['access_token']}"}
    )
    assert refused.status_code == 401


async def test_revoke_is_idempotent_and_uninformative(
    client: httpx.AsyncClient, local: LocalWorld
) -> None:
    """An unknown but well-shaped credential answers 204 like a known one.

    A body too short to be a credential at all is a 400 from field validation —
    that is shape, not knowledge about credentials, and every fielded endpoint
    gives the same answer.
    """
    well_shaped = await client.post(
        "/auth/revoke", json={"refresh_token": "gwr_unknown_unknown_value"}
    )
    assert well_shaped.status_code == 204
    malformed = await client.post("/auth/revoke", json={"refresh_token": "short"})
    assert malformed.status_code == 400


async def test_disabled_user_refused_at_exchange_and_on_v1(
    client: httpx.AsyncClient,
    local: LocalWorld,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    refresh = (await _login(client, local.login)).json()["refresh_token"]
    _, access = await _exchange(client, refresh)
    assert isinstance(access["access_token"], str)

    async with session_factory() as db:
        user = await db.get(User, local.user.id)
        assert user is not None
        user.is_active = False
        await db.commit()

    assert (await _exchange(client, refresh))[0] == 401
    refused = await client.get(
        "/v1/models", headers={"authorization": f"Bearer {access['access_token']}"}
    )
    assert refused.status_code == 401


# --------------------------------------------------------------------------
# attribution and listings
# --------------------------------------------------------------------------


async def test_v1_request_bills_the_local_users_default_group(
    client: httpx.AsyncClient,
    local: LocalWorld,
    session_factory: async_sessionmaker[AsyncSession],
    fake_upstream: FakeUpstream,
) -> None:
    """The access credential is an ordinary key: attribution is the user's row."""
    fake_upstream.set_json(completion_body())
    refresh = (await _login(client, local.login)).json()["refresh_token"]
    _, access = await _exchange(client, refresh)
    assert isinstance(access["access_token"], str)

    served = await client.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {access['access_token']}"},
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert served.status_code == 200, served.text

    async with session_factory() as db:
        record = (await db.execute(select(UsageRecord))).scalar_one()
        assert record.user_id == local.user.id
        assert record.group_id == local.group.id


async def test_machine_keys_are_hidden_from_the_console_listing(
    client: httpx.AsyncClient, local: LocalWorld
) -> None:
    """A minted key never appears in `GET /me/keys`."""
    response = await _login(client, local.login)
    cookie = response.cookies["gw_session"]
    # On the client, not per-request: httpx deprecates per-request cookies.
    # The client fixture is function-scoped, so the mutation stays in this test.
    client.cookies.set("gw_session", cookie)

    _, access = await _exchange(client, response.json()["refresh_token"])
    listing = await client.get("/api/me/keys")
    assert listing.status_code == 200
    # The pagination envelope (ADR 0029) is items/total/limit/offset.
    assert listing.json()["items"] == []
    assert access["access_token"].startswith("gwa_")
