"""Claimed invitations drop their stamp (invite badge showed Invited forever).

Revision ID: 0062
Revises: 0061
Create Date: 2026-10-16

Before this revision claiming an invitation replaced the pending pair with
the real `(issuer, subject)` but left `invited_at`/`invited_by` set. The
Users list keys its "Invited" badge off `invited_at`, so every claimed
account kept the Invited badge (and the expiry marker and re-issue button)
after signing in through the IdP, while Edit — which reads `is_active` and
the issuer — correctly showed Active/IdP. The claim paths now clear the
stamp, and this repair clears it for rows claimed earlier: any account that
is no longer pending but still carries one.

The downgrade is intentionally a no-op: the stamp cannot be reconstructed
for rows this cleared, and migrations only run forward.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None

# The literal value of `gateway.oidc.PENDING_USER_ISSUER`, inlined rather
# than imported: a migration must keep meaning what it meant when written,
# whatever the application code later says.
PENDING_ISSUER = "pystino:pending"


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE users SET invited_at = NULL, invited_by = NULL "
            "WHERE invited_at IS NOT NULL AND issuer != :pending"
        ).bindparams(pending=PENDING_ISSUER)
    )


def downgrade() -> None:
    # No-op by design; see the docstring.
    pass
