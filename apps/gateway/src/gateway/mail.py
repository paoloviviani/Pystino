"""Outbound mail: quota notifications, the console's "send test" (ADR 0093 §1).

stdlib ``smtplib`` run in a worker thread, deliberately: the dependency that
would be adopted to send one email is not worth its supply chain, and a send
is off the request path either way.
"""

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from gateway.config import SmtpSettings

logger = logging.getLogger(__name__)


class MailDeliveryError(Exception):
    """The mail server refused or dropped the message. Logged, never shown."""


def _connect(settings: SmtpSettings) -> smtplib.SMTP:
    """One connection, encrypted the way ``security`` says, not the way the
    server happens to offer.

    The previous behaviour — connect plain, then upgrade if the server offers
    STARTTLS — is still here as the ``starttls`` choice, but it is no longer
    the only one: a server that silently stopped advertising STARTTLS used to
    fail open to plaintext, which is the wrong direction to fail for a mail
    that may carry a reset link. ``starttls`` now refuses if the server does
    not offer it, ``tls`` connects already encrypted (port 465 by
    convention), and ``none`` is the explicit opt-out for a sink with no
    certificate on a private network.
    """
    if settings.security == "tls":
        server_ssl = smtplib.SMTP_SSL(settings.host, settings.port, timeout=15)
        server_ssl.ehlo()
        return server_ssl

    server: smtplib.SMTP = smtplib.SMTP(settings.host, settings.port, timeout=15)
    server.ehlo()
    if settings.security == "starttls":
        if not server.has_extn("starttls"):
            server.quit()
            raise MailDeliveryError(
                f"{settings.host}:{settings.port} does not offer STARTTLS "
                "(GATEWAY_SMTP__SECURITY=starttls)"
            )
        server.starttls()
        server.ehlo()
    return server


def send_mail(settings: SmtpSettings, to_address: str, subject: str, body: str) -> None:
    """Blocking send. Call through :func:`send_mail_async` from request code."""
    message = EmailMessage()
    message["From"] = settings.from_address
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    with _connect(settings) as server:
        if settings.username:
            server.login(settings.username, settings.password.get_secret_value())
        server.send_message(message)


async def send_mail_async(settings: SmtpSettings, to_address: str, subject: str, body: str) -> None:
    """The send off the event loop, so a slow mail server cannot stall a request."""
    try:
        await asyncio.to_thread(send_mail, settings, to_address, subject, body)
    except (smtplib.SMTPException, OSError, MailDeliveryError) as exc:
        # The caller already decided what happens to its own state (a stored
        # reset token, a notification's retry) before this runs; raising to it
        # would either leak that an address exists or lie that mail was sent,
        # so the failure stops here and the log says what the email never will.
        logger.error("mail to %s failed: %s", to_address, exc)
        if isinstance(exc, MailDeliveryError):
            raise
        raise MailDeliveryError(str(exc)) from exc
