"""The deployment's own extractor, seeded by default and out of the catalogue.

Revision ID: 0041
Revises: 0040
Create Date: 2026-09-15

Three facts, one revision, no DDL:

* **Serves by default.** Registering the extractor provider, importing its one
  model and granting it were three console steps every deployment had to walk
  before `/v1/ocr` answered at all. The seed creates the rows the manual path
  would have — same names, same kind, no credential, no price — and the
  manual path stays available beside it for a deployment that wants a second
  reader or a different address.

* **Never resurrects.** The rows are created exactly once, here, by a
  revision that alembic will not run again: an operator who deletes or
  deactivates them afterwards is never overridden on the next boot, because
  there is no next boot that seeds. That is why this is a migration and not
  a startup write — "seed when absent" in a lifespan would re-create a row
  an operator deliberately deleted, and respecting the deletion would take a
  marker whose only job is to remember the operator's decision twice.

* **Hidden from listings, from the same revision.** The provider's kind
  becomes `internal` (`gateway.plugins.base.ProviderKind`), which listings
  read to leave the model out of `/v1/models` and the console's model list
  and to stop warning about its designed-unpriced price. Rows that pre-date
  this revision are re-typed here, so a deployment that created its row by
  hand gets the same presentation as a fresh one. Hiding is presentation,
  not access: the model still resolves on `/v1/ocr` for a caller whose
  grants reach it, and usage meters to the ledger exactly as before.

The seeded model is public (ADR 0045) — any authenticated caller may use it.
That is the access-plane half of "serves by default": without it the rows
would exist and every extraction would still 403 until an administrator knew
which group to grant. Unpriced by design, the widened access charges nothing;
redaction on the extracted text applies as everywhere else.
"""

from __future__ import annotations

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Imported here rather than at module level, as 0003 did with
    # gateway.secrets: a migration must keep loading on a checkout where the
    # application module tree has moved on.
    from gateway.bootstrap import normalize_extractor_kind, seed_extractor

    normalize_extractor_kind(op.get_bind())
    seed_extractor(op.get_bind())


def downgrade() -> None:
    """Deliberately a no-op, and worth saying why.

    The revision carries no DDL, so there is nothing to undo structurally —
    and the rows it seeds are ordinary configuration by the time a downgrade
    runs: an operator may have priced, granted or renamed them, and deleting
    the provider would be refused anyway while a model points at it (ON
    DELETE RESTRICT). Removing the extractor is a console decision, not a
    schema rollback.
    """
    pass
