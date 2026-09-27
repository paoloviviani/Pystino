"""`mail.py`'s `security` dispatch (ADR 0093 §1): starttls, tls or none.

No real SMTP server here — `smtplib.SMTP` / `SMTP_SSL` are faked, and what is
pinned is which one gets called, with what arguments, for each `security`
value, plus that a server refusing to offer STARTTLS fails the send rather
than silently downgrading to plaintext.
"""

from __future__ import annotations

import smtplib
from typing import Any

import pytest
from gateway.config import SmtpSettings
from gateway.mail import MailDeliveryError, send_mail, send_mail_async


class FakeSMTP:
    kind = "starttls-capable"

    def __init__(self, host: str, port: int, timeout: float = 15) -> None:
        self.host, self.port = host, port
        self.calls: list[str] = []
        self.login_args: tuple[str, str] | None = None
        self.sent: Any = None

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def has_extn(self, name: str) -> bool:
        return self.kind == "starttls-capable"

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)

    def send_message(self, message: object) -> None:
        self.sent = message

    def quit(self) -> None:
        self.calls.append("quit")

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeSMTPNoStarttls(FakeSMTP):
    kind = "plain-only"


class FakeSMTPSSL(FakeSMTP):
    pass


def _settings(**kwargs: object) -> SmtpSettings:
    defaults = {"host": "smtp.example.org", "port": 587, "from_address": "gateway@example.org"}
    defaults.update(kwargs)
    return SmtpSettings(**defaults)  # type: ignore[arg-type]


class TestSecurityDispatch:
    def test_starttls_is_attempted_when_offered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        made: list[FakeSMTP] = []

        def factory(host: str, port: int, timeout: float = 15) -> FakeSMTP:
            server = FakeSMTP(host, port, timeout)
            made.append(server)
            return server

        monkeypatch.setattr(smtplib, "SMTP", factory)
        send_mail(_settings(security="starttls"), "to@example.org", "subject", "body")
        assert made[0].calls == ["ehlo", "starttls", "ehlo"]

    def test_starttls_required_but_not_offered_fails_the_send(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(smtplib, "SMTP", FakeSMTPNoStarttls)
        with pytest.raises(MailDeliveryError, match="does not offer STARTTLS"):
            send_mail(_settings(security="starttls"), "to@example.org", "subject", "body")

    def test_tls_connects_already_encrypted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        made: list[FakeSMTPSSL] = []

        def factory(host: str, port: int, timeout: float = 15) -> FakeSMTPSSL:
            server = FakeSMTPSSL(host, port, timeout)
            made.append(server)
            return server

        monkeypatch.setattr(smtplib, "SMTP_SSL", factory)
        send_mail(_settings(security="tls", port=465), "to@example.org", "subject", "body")
        assert made[0].calls == ["ehlo"]
        assert made[0].port == 465

    def test_none_never_attempts_starttls_even_if_offered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        made: list[FakeSMTP] = []

        def factory(host: str, port: int, timeout: float = 15) -> FakeSMTP:
            server = FakeSMTP(host, port, timeout)
            made.append(server)
            return server

        monkeypatch.setattr(smtplib, "SMTP", factory)
        send_mail(_settings(security="none"), "to@example.org", "subject", "body")
        assert made[0].calls == ["ehlo"]

    def test_login_only_when_a_username_is_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        made: list[FakeSMTP] = []
        monkeypatch.setattr(
            smtplib, "SMTP", lambda *a, **kw: made.append(FakeSMTP(*a, **kw)) or made[-1]
        )
        send_mail(_settings(security="none", username="", password=""), "to@example.org", "s", "b")
        assert made[0].login_args is None

        made.clear()
        send_mail(
            _settings(security="none", username="u", password="p"), "to@example.org", "s", "b"
        )
        assert made[0].login_args == ("u", "p")


class TestSendMailAsync:
    async def test_a_delivery_error_is_not_double_wrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(smtplib, "SMTP", FakeSMTPNoStarttls)
        with pytest.raises(MailDeliveryError, match="does not offer STARTTLS"):
            await send_mail_async(_settings(security="starttls"), "to@example.org", "s", "b")

    async def test_an_smtp_exception_becomes_a_mail_delivery_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Explodes:
            def __init__(self, *a: object, **kw: object) -> None:
                raise smtplib.SMTPConnectError(421, "nope")

        monkeypatch.setattr(smtplib, "SMTP", Explodes)
        with pytest.raises(MailDeliveryError):
            await send_mail_async(_settings(security="starttls"), "to@example.org", "s", "b")
