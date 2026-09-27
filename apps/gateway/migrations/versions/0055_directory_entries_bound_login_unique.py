"""directory_entries: one bound login per person (ADR 0093 §8.2).

Revision ID: 0055
Revises: 0054
Create Date: 2026-09-27

`create_user` binds a directory entry to the new (pending) user at creation
time, and `create_sign_in` used to accept the console's offer for that same
person — a pending user has no issuer of its own and no linked identity, so
the console's `hasBundledLogin` read "no", offered Create sign-in instead of
Reset password, and accepting it created a second entry bound to the same
user at the same provider. Nothing prevented that pair: uniqueness lived on
`(provider_id, external_id)` — two logins — never on the person. The result
was one person with two working passwords, and the admin routes that read
one entry with `scalar_one_or_none` (reset password, the disable sync) now
finding two rows and failing with MultipleResultsFound.

The fix is a partial unique index on `(provider_id, user_id)` where the user
is bound, plus this migration's dedup step, because the index alone would
refuse to build over data that already violates it.

The dedup keeps the **newest** row per `(provider_id, user_id)` and deletes
the older extras. "Newest" is decided by `first_seen_at`, ties broken by the
greater `external_id` — external_id is already unique per provider, so that
ordering is total and the outcome is deterministic no matter how many rows
tie on the timestamp. (Content is not merged across the kept and dropped
rows; pre-existing duplicate bindings are admin-action debris from the bug
above, not data anyone reconciled, so the newest row is taken as the truth.)
Unbound rows (user_id NULL) are untouched — a directory may list many people
who have never signed in, all sharing NULL, and the index exempts them.

The partial index is dialect-partial too: the `WHERE user_id IS NOT NULL`
predicate is carried for both SQLite (the test suite) and PostgreSQL (the
deployment), each in its own dialect kwarg.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

_KEEP_ONLY_NEWEST = """
DELETE FROM directory_entries
WHERE user_id IS NOT NULL
  AND EXISTS (
    SELECT 1
    FROM directory_entries AS newer
    WHERE newer.provider_id = directory_entries.provider_id
      AND newer.user_id = directory_entries.user_id
      AND (newer.first_seen_at > directory_entries.first_seen_at
           OR (newer.first_seen_at = directory_entries.first_seen_at
               AND newer.external_id > directory_entries.external_id))
  )
"""


def upgrade() -> None:
    # Before the index, not after: an existing deployment that hit the bug
    # holds duplicate bindings, and creating the unique index over them would
    # fail rather than clean them up.
    op.execute(_KEEP_ONLY_NEWEST)
    op.create_index(
        "uq_directory_entries_provider_user",
        "directory_entries",
        ["provider_id", "user_id"],
        unique=True,
        sqlite_where=sa.text("user_id IS NOT NULL"),
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_directory_entries_provider_user", table_name="directory_entries")
