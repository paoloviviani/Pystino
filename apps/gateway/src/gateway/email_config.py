"""The deployment's mail configuration: a console decision over an env fallback.

See ADR 0051.

``email_settings`` is a single row the console edits; when there is no row (or
it names no host) the environment's SMTP values stand, which is what keeps an
upgrade silent — the deployment that configured ADR 0049 through env vars
keeps sending exactly as before, until someone opens the Settings screen.

Sends are rare (a reset link, a test email, a quota crossing), so the row is
read per send rather than cached: a configuration change takes effect on the
next email with no poller, and there is nothing on any request path to worry
about.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from gateway.config import Settings
from gateway.models import EmailSettings
from gateway.secrets import SecretBox

if TYPE_CHECKING:
    from gateway.config import SmtpSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EffectiveSmtp:
    """The SMTP configuration in force, wherever it came from."""

    host: str
    port: int
    username: str
    password: str
    from_address: str
    source: str  # "console" | "environment"
    enabled: bool
    #: "starttls" | "tls" | "none" (ADR 0093 §1). The console's row (`source
    #: == "console"`) has no column for this and always gets "starttls" — the
    #: one thing `mail.py` did before this setting existed, and this row is
    #: on its way out in favour of `GATEWAY_SMTP__*` (§18 Q1), so it is not
    #: getting a new column.
    security: str = "starttls"

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.host) and bool(self.from_address)

    def to_smtp_settings(self) -> "SmtpSettings":
        """The shape the mail sender reads. The import is deferred to dodge a
        config <-> settings-config cycle, and the annotation lives in quotes
        for the same reason."""
        from gateway.config import SmtpSettings

        return SmtpSettings(
            enabled=self.enabled,
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            from_address=self.from_address,
            security=self.security,
        )


async def effective_smtp(
    session: AsyncSession, settings: Settings, secrets: SecretBox
) -> EffectiveSmtp:
    """The row's values when it decides, else the environment's."""
    row = await session.get(EmailSettings, 1)

    env = settings.smtp
    if row is None or not row.smtp_host:
        return EffectiveSmtp(
            host=env.host,
            port=env.port,
            username=env.username,
            password=env.password.get_secret_value(),
            from_address=env.from_address,
            source="environment",
            enabled=env.enabled,
            security=env.security,
        )

    password = ""
    if row.smtp_password_encrypted:
        try:
            password = secrets.decrypt(row.smtp_password_encrypted)
        except Exception:
            # A rotated secret key that no longer opens the password must not
            # make every send crash; it makes sends fail later, in the SMTP
            # layer, where the log names the real problem.
            logger.error("email settings password could not be decrypted")
    return EffectiveSmtp(
        host=row.smtp_host,
        port=row.smtp_port,
        username=row.smtp_username,
        password=password,
        from_address=row.smtp_from,
        source="console",
        enabled=True,
    )
