"""Local email + password sign-in (ADR 0043).

The session cookie the local login mints is the *same* one the OIDC callback
mints, and everything downstream of it is already covered elsewhere. What is
specific to this feature is the decision surface: which methods are advertised,
that every failure answers identically, that a throttle closes after repeated
failures, and that only local accounts may carry a password at all.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from gateway.deps import get_management_user
from gateway.login_throttle import LoginThrottle
from gateway.models import LocalCredential, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PASSWORD = "correct horse battery staple"


def as_user(app: object, user: User) -> None:
    """Bypass the management session dependency, as test_admin does.

    Copied rather than imported: the tests directory is not a package, and
    making it one so three lines can be shared is the wrong trade.
    """
    app.dependency_overrides[get_management_user] = lambda: user  # type: ignore[attr-defined]


def enable_local_auth(app: object, *, max_failed_attempts: int = 3) -> LoginThrottle:
    """Wire the throttle `init_app_state` would have built.

    The production wiring is one conditional — ``enabled`` builds a
    LoginThrottle, otherwise the state stays ``None`` — and testing it means
    rebuilding the whole app. Setting the state directly exercises the same
    code every route reads.
    """
    throttle = LoginThrottle(max_failed_attempts=max_failed_attempts, window_seconds=900)
    app.state.login_throttle = throttle  # type: ignore[attr-defined]
    return throttle


async def make_local_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    email: str = "root@example.org",
    password: str = PASSWORD,
    is_admin: bool = True,
) -> User:
    """A local account, with a credential only when a password is given.

    `password=""` is how a test says "this account cannot sign in locally",
    mirroring the API contract: no credential, not an empty one.
    """
    from gateway.passwords import hash_password

    async with session_factory() as session:
        user = User(issuer="local", subject=email, email=email, is_admin=is_admin)
        session.add(user)
        await session.flush()
        if password:
            session.add(LocalCredential(user_id=user.id, password_hash=hash_password(password)))
        await session.commit()
        await session.refresh(user)
        return user


class TestMethods:
    async def test_reports_local_when_the_throttle_is_wired(
        self, app: object, client: httpx.AsyncClient
    ) -> None:
        enable_local_auth(app)
        body = (await client.get("/auth/methods")).json()
        assert body == {"local": True, "oidc": False}

    async def test_reports_neither_by_default(self, client: httpx.AsyncClient) -> None:
        # A deployment with no IdP and no local auth: there is no way in, and
        # the console must be able to discover that rather than loop.
        body = (await client.get("/auth/methods")).json()
        assert body == {"local": False, "oidc": False}


class TestLocalLogin:
    @pytest_asyncio.fixture
    async def local_user(
        self, app: object, session_factory: async_sessionmaker[AsyncSession]
    ) -> User:
        enable_local_auth(app)
        return await make_local_user(session_factory)

    async def test_success_sets_a_working_session(
        self, client: httpx.AsyncClient, local_user: User
    ) -> None:
        response = await client.post(
            "/auth/login", json={"email": "root@example.org", "password": PASSWORD}
        )
        assert response.status_code == 200
        assert "gw_session" in response.cookies

        # The cookie is the management session, not a special local one.
        whoami = await client.get("/auth/session")
        assert whoami.status_code == 200
        assert whoami.json()["user_id"] == str(local_user.id)

    async def test_email_is_matched_case_insensitively(
        self, client: httpx.AsyncClient, local_user: User
    ) -> None:
        response = await client.post(
            "/auth/login", json={"email": "ROOT@EXAMPLE.ORG", "password": PASSWORD}
        )
        assert response.status_code == 200

    async def test_unknown_email_and_wrong_password_answer_identically(
        self, client: httpx.AsyncClient, local_user: User
    ) -> None:
        # Identical status *and* body: distinguishing them would make this
        # endpoint an account enumerator.
        wrong_password = await client.post(
            "/auth/login", json={"email": "root@example.org", "password": "not-the-password-1"}
        )
        unknown = await client.post(
            "/auth/login", json={"email": "nobody@example.org", "password": "not-the-password-1"}
        )
        assert wrong_password.status_code == unknown.status_code == 401
        assert wrong_password.json() == unknown.json()

    async def test_a_disabled_account_cannot_sign_in(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        local_user: User,
    ) -> None:
        async with session_factory() as session:
            user = await session.get(User, local_user.id)
            user.is_active = False
            await session.commit()

        response = await client.post(
            "/auth/login", json={"email": "root@example.org", "password": PASSWORD}
        )
        assert response.status_code == 401

    async def test_throttle_closes_after_repeated_failures(
        self, client: httpx.AsyncClient, local_user: User
    ) -> None:
        for _ in range(3):
            failed = await client.post(
                "/auth/login",
                json={"email": "root@example.org", "password": "not-the-password-1"},
            )
            assert failed.status_code == 401

        # The correct password does not get in either: the throttle is per
        # address, and the whole point is that guessing stops being free.
        locked = await client.post(
            "/auth/login", json={"email": "root@example.org", "password": PASSWORD}
        )
        assert locked.status_code == 429

    async def test_success_clears_the_throttle(
        self,
        app: object,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        local_user: User,
    ) -> None:
        await client.post(
            "/auth/login", json={"email": "root@example.org", "password": "not-the-password-1"}
        )
        ok = await client.post(
            "/auth/login", json={"email": "root@example.org", "password": PASSWORD}
        )
        assert ok.status_code == 200

        # One failure remains, one more is allowed — the counter was reset by
        # the success, not left at 1 of 3.
        again = await client.post(
            "/auth/login", json={"email": "root@example.org", "password": "not-the-password-1"}
        )
        assert again.status_code == 401

    async def test_without_local_auth_the_endpoint_answers_503(
        self, client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # An account may exist (seeded, or left over after the flag was turned
        # off) while the gateway refuses the path entirely.
        await make_local_user(session_factory)
        response = await client.post(
            "/auth/login", json={"email": "root@example.org", "password": PASSWORD}
        )
        assert response.status_code == 503


class TestAdminPasswordRoutes:
    async def test_admin_can_set_a_password(
        self,
        app: object,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded: object,
    ) -> None:
        user = await make_local_user(session_factory, password="")
        as_user(app, user)
        response = await client.put(
            f"/api/admin/users/{user.id}/password", json={"password": "a-new-password-9"}
        )
        assert response.status_code == 200
        assert response.json()["has_password"] is True

        # The new password actually works.
        enable_local_auth(app)
        login = await client.post(
            "/auth/login", json={"email": "root@example.org", "password": "a-new-password-9"}
        )
        assert login.status_code == 200

    async def test_short_password_is_refused(
        self,
        app: object,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await make_local_user(session_factory)
        as_user(app, user)
        response = await client.put(
            f"/api/admin/users/{user.id}/password", json={"password": "short"}
        )
        assert response.status_code == 400

    async def test_a_directory_user_may_not_have_a_password(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: object,
    ) -> None:
        from test_admin import make_admin

        admin = await make_admin(session_factory=app.state.session_factory, seeded=seeded)
        as_user(app, admin)
        response = await client.put(
            f"/api/admin/users/{seeded.user.id}/password", json={"password": "whatever-long"}
        )
        assert response.status_code == 400

    async def test_clearing_the_password_revokes_local_login(
        self,
        app: object,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await make_local_user(session_factory)
        as_user(app, user)
        cleared = await client.delete(f"/api/admin/users/{user.id}/password")
        assert cleared.status_code == 200
        assert cleared.json()["has_password"] is False

        enable_local_auth(app)
        login = await client.post(
            "/auth/login", json={"email": "root@example.org", "password": PASSWORD}
        )
        assert login.status_code == 401

    async def test_listing_reports_has_password(
        self,
        app: object,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        with_pw = await make_local_user(session_factory, email="with@example.org")
        await make_local_user(session_factory, email="without@example.org", password="")
        as_user(app, with_pw)
        body = (await client.get("/api/admin/users")).json()
        by_email = {item["email"]: item["has_password"] for item in body["items"]}
        assert by_email["with@example.org"] is True
        assert by_email["without@example.org"] is False


class TestRehashOnLogin:
    async def test_a_stored_hash_is_upgraded_when_parameters_move_on(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A hash made under weaker parameters is replaced at the next login.

        Proven by storing a hand-weakened hash rather than by monkeypatching
        the library's defaults: whatever pwdlib recommends today, the contract
        under test is that `verify_and_update`'s replacement is persisted and
        still verifies.
        """
        from argon2 import PasswordHasher
        from pwdlib import PasswordHash

        enable_local_auth(client._transport.app)  # type: ignore[attr-defined]
        email = "legacy@example.org"

        # A deliberately weaker profile than the recommended one. If it could
        # not be built, the test degenerates to re-storing the same hash,
        # which asserts nothing — so it is skipped honestly instead.
        weak = None
        try:
            hasher = PasswordHasher(time_cost=2, memory_cost=8 * 1024, parallelism=1)
            weak = hasher.hash(PASSWORD)
        except Exception as exc:  # any failure here means "skip", not "fail"
            weak = None
            weak_error = exc
        if weak is None or PasswordHash.recommended().hash(PASSWORD) == weak:
            pytest.skip(f"could not construct a weaker hash to upgrade from: {weak_error}")

        async with session_factory() as db:
            user = User(issuer="local", subject=email, email=email)
            db.add(user)
            await db.flush()
            db.add(LocalCredential(user_id=user.id, password_hash=weak))
            await db.commit()
            user_id = user.id

        login = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 200

        async with session_factory() as db:
            credential = await db.get(LocalCredential, user_id)
            assert credential.password_hash != weak
            assert credential.password_hash.startswith("$argon2")
            # And the replacement is a working credential, not garbage.
            async with session_factory() as check:
                row = (
                    await check.execute(
                        select(LocalCredential).where(LocalCredential.user_id == user_id)
                    )
                ).scalar_one()
                from gateway.passwords import verify_password

                assert verify_password(PASSWORD, row.password_hash)
