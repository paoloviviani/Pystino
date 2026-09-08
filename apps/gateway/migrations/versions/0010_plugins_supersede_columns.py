"""three reactive columns removed, and what they said translated

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-24

See ADR 0032. ``providers.plugin`` replaces three
columns that were each added for one counterparty's habit:

* ``auth_scheme`` — added when Anthropic's own API turned out to reject a bearer
  token on ``/v1/messages``. **Translated, not dropped:** a row with
  ``x_api_key`` becomes ``plugin = 'anthropic'``, which presents the credential
  the same way and adds the ``anthropic-version`` header it also needs. Nothing
  about how such a row authenticates changes.
* ``forward_stream_options`` — added when a counterparty documented that unknown
  parameters can narrow its routing pool. **Not translatable**, because the
  answer is now the plugin's: the generic plugin asks for usage on a streamed
  chat completion, the Cortecs plugin does not. Divergent rows are named below
  before being dropped.
* ``upstream_cost_unit`` — added when one counterparty turned out to report cost
  in micro-EUR. **Not translatable** for the same reason and with a sharper
  consequence: a generic provider stops recording the counterparty's figure at
  upgrade. Divergent rows are named below too.

The two untranslatable ones print a warning naming the affected providers rather
than failing. A migration that refuses leaves a deployment unable to upgrade over
a configuration question, and a migration that is silent lets a reconciliation
column quietly stop being populated. Naming the rows is the only answer that is
neither.

Downgrade restores the columns with their original defaults. It cannot restore
the *values* of the two untranslatable ones — that information is gone once this
runs, which is what the warning is for.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: What each plugin's ``prepare_payload`` does about ``stream_options``, so the
#: warning below can tell a row that keeps its behaviour from one that loses it.
#: Kept as a literal rather than imported from ``gateway.plugins``: a migration
#: must describe the schema as it was at this revision, and importing today's
#: registry would make an old upgrade path change meaning when a plugin does.
_ASKS_FOR_STREAM_USAGE = {None: True, "generic": True, "anthropic": False, "cortecs": False}


def _warn_about_untranslatable_values(bind: sa.engine.Connection) -> None:
    """Say out loud what this upgrade changes, per provider row.

    Printed rather than logged: alembic runs from a command line and from an
    entrypoint, and ``print`` reaches both. It is the only record an operator
    gets, because the values are gone immediately afterwards.
    """
    rows = bind.execute(
        sa.text(
            "SELECT name, plugin, forward_stream_options, upstream_cost_unit "
            "FROM providers ORDER BY name"
        )
    ).fetchall()

    for name, plugin, forwards, unit in rows:
        asks = _ASKS_FOR_STREAM_USAGE.get(plugin, True)
        if bool(forwards) != asks:
            became = "will now ask for" if asks else "will now stop asking for"
            print(
                f"  migration 0010: provider {name!r} had forward_stream_options="
                f"{bool(forwards)!r} and its plugin ({plugin or 'generic'}) {became} "
                "stream usage. Behaviour changes at this upgrade. If that is wrong, "
                "the fix is a plugin, not a column — see ADR 0032."
            )
        # A plugin that reads its own counterparty's cost does not need the
        # declaration; only the generic one relied on it, and it now reports
        # nothing at all.
        if unit and plugin in (None, "generic"):
            print(
                f"  migration 0010: provider {name!r} declared upstream_cost_unit={unit!r} "
                "and has no plugin that knows that unit, so its reported cost will no "
                "longer be recorded. Past rows keep the figure they were recorded with. "
                "To keep recording it, register a plugin under the 'llmp.providers' "
                "entry point — see ADR 0032."
            )


def upgrade() -> None:
    bind = op.get_bind()
    _warn_about_untranslatable_values(bind)

    # Translated first, while the column still exists. Only rows that have not
    # already been given a plugin by hand: an explicit choice outranks an
    # inference drawn from an auth scheme.
    bind.execute(
        sa.text(
            "UPDATE providers SET plugin = 'anthropic', kind = 'provider' "
            "WHERE auth_scheme = 'x_api_key' AND (plugin IS NULL OR plugin = 'generic')"
        )
    )

    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.drop_column("forward_stream_options")
        batch_op.drop_column("auth_scheme")
        batch_op.drop_column("upstream_cost_unit")


def downgrade() -> None:
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "forward_stream_options",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "auth_scheme",
                sa.Enum("bearer", "x_api_key", name="auth_scheme", native_enum=False, length=32),
                nullable=False,
                server_default="bearer",
            )
        )
        batch_op.add_column(sa.Column("upstream_cost_unit", sa.String(length=32), nullable=True))

    # The one value this can put back, because the plugin name carries it.
    op.get_bind().execute(
        sa.text("UPDATE providers SET auth_scheme = 'x_api_key' WHERE plugin = 'anthropic'")
    )
