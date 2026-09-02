"""Outbound mail for the self-service password reset (ADR 0049).

stdlib ``smtplib`` run in a worker thread, deliberately: the dependency that
would be adopted to send one email is not worth its supply chain, and the
send is off the request path either way — the reset endpoint answers the
browser before delivery is attempted, because the answer must not depend on
the mail server's mood (and must not leak, by timing or by error, whether the
address exists).
"""

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from gateway.config import PasswordResetSettings

logger = logging.getLogger(__name__)


class MailDeliveryError(Exception):
    """The mail server refused or dropped the message. Logged, never shown."""


def send_mail(
    settings: PasswordResetSettings, to_address: str, subject: str, body: str
) -> None:
    """Blocking send. Call through :func:`send_mail_async` from request code."""
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    # STARTTLS is always attempted: a plain-text hop for a password-reset link
    # is the attack the whole token design exists to make survivable, but a
    # survivable attack is not a reason to hand the link to a wiretap.
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
        server.ehlo()
        if server.has_extn("starttls"):
            server.starttls()
            server.ehlo()
        if settings.smtp_username:
            server.login(
                settings.smtp_username, settings.smtp_password.get_secret_value()
            )
        server.send_message(message)


async def send_mail_async(
    settings: PasswordResetSettings, to_address: str, subject: str, body: str
) -> None:
    """The send off the event loop, so a slow mail server cannot stall a login."""
    try:
        await asyncio.to_thread(send_mail, settings, to_address, subject, body)
    except (smtplib.SMTPException, OSError) as exc:
        # The token is already stored: the person can ask again, and the log
        # says what the email never will. Raising to the endpoint would either
        # leak that the address exists or lie that a mail was sent — so the
        # failure stops here.
        logger.error("password reset mail to %s failed: %s", to_address, exc)
        raise MailDeliveryError(str(exc)) from exc
