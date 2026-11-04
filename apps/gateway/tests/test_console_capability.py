"""Group capability: console access.

A person whose groups all lack ``console_allowed`` (and who is not an
administrator) may sign in, but the cookie-backed management API refuses them
with 403 ``console_not_allowed``, except ``GET /api/me`` (how the console learns
to show its "managed through the chat" page) and ``/auth/session``. Their
``/v1`` bearer access is untouched. BYOK needs the console: effective ``byok``
is false without it, on ``/v1/me``, ``/api/me`` and at request time.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from conftest import Seeded, bearer_auth, make_token
from gateway.config import AccessSettings
from gateway.models import Group, IdentityEvent, OIDCPolicyConfig
from sqlalchemy import select
from test_admin import make_admin
from test_my_limits import session_cookie
from test_oidc_policy import admin_session as admin_session


async def _set_group(session_factory: Any, group_id: uuid.UUID, **flags: bool) -> None:
    async with session_factory() as session:
        group = await session.get(Group, group_id)
        for name, value in flags.items():
            setattr(group, name, value)
        await session.commit()


#: Every `/api/me/*` route a console page reads, one per router family.
CONSOLE_ROUTES = [
    ("GET", "/api/me/keys"),
    ("GET", "/api/me/usage"),
    ("GET", "/api/me/limits"),
    ("GET", "/api/me/requests"),
    ("GET", "/api/me/usage/groups"),
    ("GET", "/api/me/reports/usage"),
    ("GET", "/api/me/providers"),
    ("GET", "/api/me/models"),
]


class TestNoConsoleAccess:
    async def test_the_me_endpoints_answer_403_console_not_allowed(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
    ) -> None:
        await _set_group(session_factory, seeded.group.id, console_allowed=False)
        cookie = session_cookie(seeded.user.id, app)
        for method, path in CONSOLE_ROUTES:
            response = await client.request(method, path, headers=cookie)
            assert response.status_code == 403, (path, response.text)
            assert response.json()["error"]["code"] == "console_not_allowed", path

    async def test_me_and_session_still_answer_so_the_console_can_say_why(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
    ) -> None:
        app.state.settings.chat.url = "https://chat.example.org"
        await _set_group(session_factory, seeded.group.id, console_allowed=False)
        cookie = session_cookie(seeded.user.id, app)
        me = await client.get("/api/me", headers=cookie)
        assert me.status_code == 200, me.text
        assert me.json()["capabilities"]["console"] is False
        assert me.json()["chat_url"] == "https://chat.example.org"
        assert (await client.get("/auth/session", headers=cookie)).status_code == 200

    async def test_with_the_flag_the_same_routes_answer(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        cookie = session_cookie(seeded.user.id, app)
        for method, path in CONSOLE_ROUTES[:5]:
            response = await client.request(method, path, headers=cookie)
            assert response.status_code == 200, (path, response.text)
        me = (await client.get("/api/me", headers=cookie)).json()
        assert me["capabilities"]["console"] is True

    async def test_bearer_access_is_untouched(
        self,
        bearer_app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: Any,
        session_factory: Any,
    ) -> None:
        await _set_group(session_factory, seeded.group.id, console_allowed=False)
        headers = bearer_auth(make_token(signing_key))
        me = await client.get("/v1/me", headers=headers)
        assert me.status_code == 200, me.text
        assert me.json()["capabilities"]["console"] is False
        assert me.json()["capabilities"]["api_keys"] is True
        models = await client.get("/v1/models", headers=headers)
        assert models.status_code == 200, models.text
        usage = await client.get("/v1/pystino/usage", headers=headers)
        assert usage.status_code == 200, usage.text

    async def test_an_administrator_always_has_the_console(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
    ) -> None:
        await _set_group(session_factory, seeded.group.id, console_allowed=False)
        admin = await make_admin(session_factory=session_factory, seeded=seeded)
        cookie = session_cookie(admin.id, app)
        assert (await client.get("/api/me/keys", headers=cookie)).status_code == 200
        assert (await client.get("/api/admin/groups", headers=cookie)).status_code == 200
        me = (await client.get("/api/me", headers=cookie)).json()
        assert me["capabilities"]["console"] is True


class TestByokNeedsConsole:
    async def test_byok_is_reported_false_without_console(
        self,
        bearer_app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: Any,
        session_factory: Any,
    ) -> None:
        headers = bearer_auth(make_token(signing_key))
        await _set_group(session_factory, seeded.group.id, byok_allowed=True)
        flags = (await client.get("/v1/me", headers=headers)).json()["capabilities"]
        assert (flags["byok"], flags["console"]) == (True, True)

        await _set_group(session_factory, seeded.group.id, console_allowed=False)
        flags = (await client.get("/v1/me", headers=headers)).json()["capabilities"]
        assert (flags["byok"], flags["console"]) == (False, False)

        # and it comes back with the console, like any other loss
        await _set_group(session_factory, seeded.group.id, console_allowed=True)
        flags = (await client.get("/v1/me", headers=headers)).json()["capabilities"]
        assert flags["byok"] is True


class TestManagement:
    async def test_patch_a_group(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        admin_session: dict[str, str],
    ) -> None:
        group = (await client.get("/api/admin/groups", headers=admin_session)).json()["items"][0]
        assert group["console_allowed"] is True
        patched = await client.patch(
            f"/api/admin/groups/{group['id']}",
            json={"console_allowed": False},
            headers=admin_session,
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["console_allowed"] is False

    async def test_bulk_flip_preview_and_audit(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        admin_session: dict[str, str],
        session_factory: Any,
    ) -> None:
        preview = await client.get(
            "/api/admin/oidc/policy/preview?bulk_capability=console&bulk_value=false",
            headers=admin_session,
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["would_lose"] is not None
        response = await client.post(
            "/api/admin/groups/capabilities",
            json={"capability": "console", "value": False},
            headers=admin_session,
        )
        assert response.status_code == 200, response.text
        listing = (await client.get("/api/admin/groups", headers=admin_session)).json()["items"]
        assert listing and not any(item["console_allowed"] for item in listing)
        async with session_factory() as session:
            events = (
                (
                    await session.execute(
                        select(IdentityEvent).where(IdentityEvent.action == "group.capability_bulk")
                    )
                )
                .scalars()
                .all()
            )
        assert [e.detail["capability"] for e in events] == ["console"]

    async def test_an_administrator_does_not_lose_it_by_the_bulk_flip(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        admin_session: dict[str, str],
    ) -> None:
        await client.post(
            "/api/admin/groups/capabilities",
            json={"capability": "console", "value": False},
            headers=admin_session,
        )
        assert (await client.get("/api/me/keys", headers=admin_session)).status_code == 200

    async def test_users_listing_reports_effective_console(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        admin_session: dict[str, str],
    ) -> None:
        users = (await client.get("/api/admin/users", headers=admin_session)).json()["items"]
        assert users and all("console_allowed" in user for user in users)


class TestDefaults:
    async def test_new_groups_default_on(
        self,
        client: httpx.AsyncClient,
        admin_session: dict[str, str],
    ) -> None:
        created = await client.post(
            "/api/admin/groups", json={"name": "fresh"}, headers=admin_session
        )
        assert created.status_code == 201, created.text
        assert created.json()["console_allowed"] is True

    def test_the_environment_default_folds_into_the_new_group_default(self) -> None:
        from gateway.capabilities import default_flags
        from gateway.config import OIDCSettings
        from gateway.oidc_policy import effective_policy

        off = effective_policy(OIDCSettings(), None, AccessSettings(console="off"))
        assert off.console_mode == "off"
        assert default_flags(off).console is False
        assert default_flags(effective_policy(OIDCSettings(), None, AccessSettings())).console

    async def test_the_console_decision_wins_and_saves(
        self,
        app: Any,
        client: httpx.AsyncClient,
        admin_session: dict[str, str],
        session_factory: Any,
    ) -> None:
        saved = await client.put(
            "/api/admin/oidc/policy",
            json={"console_mode": "off", "reason": "chat only"},
            headers=admin_session,
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["console_mode"] == "off"
        assert saved.json()["sources"]["console_mode"] == "console"
        created = await client.post(
            "/api/admin/groups", json={"name": "fresh"}, headers=admin_session
        )
        assert created.json()["console_allowed"] is False
        bad = await client.put(
            "/api/admin/oidc/policy",
            json={"console_mode": "sometimes", "reason": "x"},
            headers=admin_session,
        )
        assert bad.status_code in (400, 422)
        async with session_factory() as session:
            rows = (await session.execute(select(OIDCPolicyConfig))).scalars().all()
        assert any(row.console_mode == "off" for row in rows)


@pytest.mark.parametrize("value", ["on", "off"])
def test_the_access_setting_parses(value: str) -> None:
    assert AccessSettings(console=value).console == value  # type: ignore[arg-type]


class TestRedirectToTheChat:
    """A non-administrator without the capability who opens any console route is
    sent to the chat's usage page; everyone else gets the console as before."""

    @pytest.fixture
    def mounted(self, app: Any, tmp_path: Any) -> Any:
        from gateway.routers.console import mount_console
        from test_console import build_assets

        settings = app.state.settings
        mounted = mount_console(
            app, settings.model_copy(update={"console_dir": str(build_assets(tmp_path / "c"))})
        )
        assert mounted
        return app

    async def test_redirects_any_console_route_to_the_chat_usage_page(
        self, mounted: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        mounted.state.settings.chat.url = "https://example.org/chat/"
        await _set_group(session_factory, seeded.group.id, console_allowed=False)
        cookie = session_cookie(seeded.user.id, mounted)
        for path in ("/console", "/console/", "/console/reports", "/console/me/providers"):
            response = await client.get(path, headers=cookie, follow_redirects=False)
            assert response.status_code == 307, path
            assert response.headers["location"] == "https://example.org/chat/settings/usage"

    async def test_not_for_administrators_members_with_the_flag_or_visitors(
        self, mounted: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        mounted.state.settings.chat.url = "https://example.org/chat"
        # a member who holds it
        cookie = session_cookie(seeded.user.id, mounted)
        assert (await client.get("/console", headers=cookie)).status_code == 200
        # nobody signed in: the console loads (and shows its sign-in)
        assert (await client.get("/console/reports")).status_code == 200
        # a stale / forged cookie is not a redirect either
        bad = {"cookie": "gw_session=nonsense"}
        assert (await client.get("/console", headers=bad)).status_code == 200
        # an administrator in a group without the flag
        await _set_group(session_factory, seeded.group.id, console_allowed=False)
        admin = await make_admin(session_factory=session_factory, seeded=seeded)
        response = await client.get(
            "/console", headers=session_cookie(admin.id, mounted), follow_redirects=False
        )
        assert response.status_code == 200

    async def test_the_sign_in_hop_is_never_redirected(
        self, mounted: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        mounted.state.settings.chat.url = "https://example.org/chat"
        await _set_group(session_factory, seeded.group.id, console_allowed=False)
        cookie = session_cookie(seeded.user.id, mounted)
        response = await client.get("/console/login", headers=cookie, follow_redirects=False)
        assert response.status_code == 200

    async def test_without_a_chat_url_the_console_loads_and_says_why(
        self, mounted: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        assert mounted.state.settings.chat.url == ""
        await _set_group(session_factory, seeded.group.id, console_allowed=False)
        cookie = session_cookie(seeded.user.id, mounted)
        response = await client.get("/console", headers=cookie, follow_redirects=False)
        assert response.status_code == 200
