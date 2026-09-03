"""The deployment's mail configuration: a console decision over an env fallback.

See [ADR 0051](../../../../docs/adr/0051-settings-identity-and-email.md).

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
    from gateway.config import PasswordResetSettings

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

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.host) and bool(self.from_address)

    def to_password_reset_settings(self) -> "PasswordResetSettings":
        """The shape the mail sender reads. The import is deferred to dodge a
        config <-> settings-config cycle, and the annotation lives in quotes
        for the same reason."""
        from gateway.config import PasswordResetSettings

        return PasswordResetSettings(
            enabled=self.enabled,
            smtp_host=self.host,
            smtp_port=self.port,
            smtp_username=self.username,
            smtp_password=self.password,
            smtp_from=self.from_address,
        )


async def effective_smtp(
    session: AsyncSession, settings: Settings, secrets: SecretBox
) -> EffectiveSmtp:
    """The row's values when it decides, else the environment's."""
    row = await session.get(EmailSettings, 1)

    env = settings.local_auth.password_reset
    if row is None or not row.smtp_host:
        return EffectiveSmtp(
            host=env.smtp_host,
            port=env.smtp_port,
            username=env.smtp_username,
            password=env.smtp_password.get_secret_value(),
            from_address=env.smtp_from,
            source="environment",
            enabled=env.enabled,
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
