"""Self-service password reset (ADR 0049).

The properties under test are the ones a wrong answer turns into a hole:
one indistinguishable answer for every request (no user enumeration), a token
that is a single-use secret and nothing else, and a confirm that actually
changes the credential — proven by signing in with the new password.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest_asyncio
from gateway.config import Settings
from gateway.models import LocalCredential, PasswordResetToken, User
from gateway.passwords import hash_password
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def reset_app(
    app: Any,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[Any, list[tuple[str, str, str]]]]:
    """The feature armed: enabled, throttled, and the sender captured.

    The mail sender is patched at its import site — the endpoint calls
    ``send_mail_async`` through the router module's namespace, so the capture
    replaces exactly what production would call and records the recipient,
    subject and body of each message.
    """
    import gateway.routers.auth as auth_module

    settings: Settings = app.state.settings
    reset = settings.local_auth.password_reset
    reset.enabled = True
    reset.smtp_host = "smtp.test"
    reset.smtp_from = "Pystino <no-reply@pystino.test>"
    from gateway.routers.auth import ResetRequestThrottle

    app.state.reset_throttle = ResetRequestThrottle(
        reset.request_cooldown_seconds
    )

    sent: list[tuple[str, str, str]] = []

    async def capture(
        _settings: object, to: str, subject: str, body: str
    ) -> None:
        sent.append((to, subject, body))

    original = auth_module.send_mail_async
    auth_module.send_mail_async = capture
    yield app, sent
    auth_module.send_mail_async = original

    async with session_factory() as session:
        await session.execute(PasswordResetToken.__table__.delete())
        await session.commit()


async def make_local_user(
    session_factory: async_sessionmaker[AsyncSession],
    email: str = "local@example.org",
    # Named so the S107 "hardcoded password" rule can see it is a fixture
    # value, not a credential anyone lost sleep over.
    password: str = "the-original-password",  # noqa: S107
) -> User:
    async with session_factory() as session:
        user = User(issuer="local", subject=email, email=email)
        session.add(user)
        await session.flush()
        session.add(
            LocalCredential(user_id=user.id, password_hash=hash_password(password))
        )
        await session.commit()
        return user


def extract_token(body: str) -> str:
    return body.split("token=")[1].split(chr(10))[0].strip()


class TestRequestReset:
    async def test_disabled_is_503(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/auth/password-reset", json={"email": "anyone@example.org"}
        )
        assert response.status_code == 503

    async def test_an_unknown_address_gets_the_success_answer_and_no_mail(
        self,
        client: httpx.AsyncClient,
        reset_app: tuple[Any, list[tuple[str, str, str]]],
    ) -> None:
        response = await client.post(
            "/auth/password-reset", json={"email": "nobody@example.org"}
        )
        assert response.status_code == 200
        assert reset_app[1] == []

    async def test_a_directory_account_gets_no_mail(
        self,
        client: httpx.AsyncClient,
        seeded: Any,
        reset_app: tuple[Any, list[tuple[str, str, str]]],
    ) -> None:
        # The IdP is authoritative for its users' credentials (ADR 0048): a
        # reset link minted here would be overwritten at the next login and
        # mean nothing in between.
        response = await client.post(
            "/auth/password-reset", json={"email": seeded.user.email or ""}
        )
        assert response.status_code == 200
        assert reset_app[1] == []

    async def test_a_local_account_gets_one_link_with_a_token(
        self,
        client: httpx.AsyncClient,
        reset_app: tuple[Any, list[tuple[str, str, str]]],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await make_local_user(session_factory)
        response = await client.post(
            "/auth/password-reset", json={"email": "local@example.org"}
        )
        assert response.status_code == 200
        (to, _subject, body) = reset_app[1][0]
        assert to == "local@example.org"
        assert "one hour" in body
        assert "token=" in body
        # The token itself is in the mail and nowhere else: the row holds the
        # hash, so a database read is not a working link.
        rows = (
            (await session_factory().execute(  # type: ignore[union-attr]
                select(PasswordResetToken)
            ))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].token_hash != extract_token(body)

    async def test_a_new_request_replaces_the_old_link(
        self,
        client: httpx.AsyncClient,
        reset_app: tuple[Any, list[tuple[str, str, str]]],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await make_local_user(session_factory)
        await client.post("/auth/password-reset", json={"email": "local@example.org"})
        app, sent = reset_app
        # Past the cooldown: a fresh ask, not a throttle.
        app.state.reset_throttle._last.clear()
        await client.post("/auth/password-reset", json={"email": "local@example.org"})
        assert len(sent) == 2
        rows = (
            (await session_factory().execute(  # type: ignore[union-attr]
                select(PasswordResetToken)
            ))
            .scalars()
            .all()
        )
        assert len(rows) == 1


class TestConfirmReset:
    async def test_confirm_changes_the_password(
        self,
        client: httpx.AsyncClient,
        reset_app: tuple[Any, list[tuple[str, str, str]]],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await make_local_user(session_factory)
        await client.post("/auth/password-reset", json={"email": "local@example.org"})
        (_to, _subject, body) = reset_app[1][0]
        token = extract_token(body)

        response = await client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "password": "the-brand-new-password"},
        )
        assert response.status_code == 200

        # The proof that matters: the old password is dead, the new one signs in.
        app = reset_app[0]
        from gateway.login_throttle import LoginThrottle

        app.state.login_throttle = LoginThrottle(max_failed_attempts=10, window_seconds=60)
        old = await client.post(
            "/auth/login",
            json={"email": "local@example.org", "password": "the-original-password"},
        )
        assert old.status_code == 401
        new = await client.post(
            "/auth/login",
            json={"email": "local@example.org", "password": "the-brand-new-password"},
        )
        assert new.status_code == 200

        # Spent, and visibly so.
        rows = (
            (await session_factory().execute(  # type: ignore[union-attr]
                select(PasswordResetToken)
            ))
            .scalars()
            .all()
        )
        assert rows[0].used_at is not None

    async def test_a_used_token_is_dead(
        self,
        client: httpx.AsyncClient,
        reset_app: tuple[Any, list[tuple[str, str, str]]],
    ) -> None:
        await make_local_user(reset_app[0].state.session_factory)  # type: ignore[union-attr]
        await client.post("/auth/password-reset", json={"email": "local@example.org"})
        token = extract_token(reset_app[1][0][2])
        await client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "password": "the-brand-new-password"},
        )
        again = await client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "password": "a-different-password"},
        )
        assert again.status_code == 400

    async def test_an_unissued_token_is_the_same_message(
        self, client: httpx.AsyncClient, reset_app: tuple[Any, list[tuple[str, str, str]]]
    ) -> None:
        response = await client.post(
            "/auth/password-reset/confirm",
            json={"token": "x" * 40, "password": "the-brand-new-password"},
        )
        assert response.status_code == 400
        assert "not valid or has expired" in response.json()["error"]["message"]

    async def test_a_short_password_is_refused_and_the_token_survives(
        self,
        client: httpx.AsyncClient,
        reset_app: tuple[Any, list[tuple[str, str, str]]],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await make_local_user(session_factory)
        await client.post("/auth/password-reset", json={"email": "local@example.org"})
        token = extract_token(reset_app[1][0][2])
        short = await client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "password": "short"},
        )
        assert short.status_code == 400
        # The token is not spent by a failed confirm: the person can type a
        # proper password into the same link.
        rows = (
            (await session_factory().execute(  # type: ignore[union-attr]
                select(PasswordResetToken)
            ))
            .scalars()
            .all()
        )
        assert rows[0].used_at is None
