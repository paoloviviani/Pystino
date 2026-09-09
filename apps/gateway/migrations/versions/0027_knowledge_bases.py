"""Knowledge bases: files, documents, chunks and one generic sharing table.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-09

Six tables and one extension (ADR 0018, ADR 0062). This is the first schema here
that stores what a *user wrote* rather than what a request cost, and most of what
follows is a consequence of that.

**The extension is created here, and only on PostgreSQL.** ``CREATE EXTENSION
vector`` needs an image that ships it — ``pgvector/pgvector:pg18`` in
``deploy/compose`` — and a role that may create extensions, which the compose
superuser is. On a managed PostgreSQL where it is not, this migration is where
the deployment finds out, with the extension's own error, rather than at the
first retrieval.

**The vector column has no dimension modifier.** Verified against pgvector 0.8.6,
not assumed: ``vector`` accepts values of differing length in one column, and a
mismatch raises rather than ranking wrongly. That is what makes the embedding
model an administrative setting instead of a schema migration — a knowledge base
pins the model it was indexed with, and two models coexist for exactly as long as
a reindex takes. ADR 0020 recorded the opposite consequence ("switching to a
model with a different output size is a migration, not a configuration change");
it is wrong, and 0062 records the measurement.

**The approximate indexes are partial expression indexes, cast to halfvec.**
Three measurements decided that shape. HNSW refuses a ``vector`` column above
**2000** dimensions and a ``halfvec`` above **4000**, so OpenAI's 3072-dimension
model cannot be indexed as ``vector`` at all. The planner does use a
halfvec-cast index for a halfvec-cast ``ORDER BY`` (``Index Scan using
ix_..._3072``). And a partial predicate on ``dimensions`` is what keeps one
query inside one vector length. Ranking therefore happens in half precision
while storage stays full precision — retrieval order over top-k is unaffected by
the last few bits, and the full vectors remain available if an exact re-rank is
ever wanted.

The pre-created dimensions are the ones real models emit: 384, 512, 768, 1024,
1536, 3072. A base of any other length still works — the query is then an exact
scan, which is correct and slower — and giving it an index is a one-line
migration. Every one of these indexes is on an empty partition, so they cost
nothing until something uses that length.

What is deliberately **not** here:

* **No global embedding or extractor setting.** Those are an append-only config
  row and arrive in 0028, so that this migration can be applied to a deployment
  that never turns the feature on and change nothing about how it behaves.
* **No agent tables.** Agents share this ACL and this vector store but are their
  own decision, and bundling them would make this migration untestable in one
  sitting.
* **No foreign keys on ``resource_shares``.** Both of its addresses are
  polymorphic — the resource is one of three kinds in two different databases,
  and the principal is a user *or* a group. Read the model docstring before
  adding one: the cost is that a deleted resource leaves rows behind, and
  ``sharing.py`` is the single place that cleans up and the single place that
  filters dead grants on read.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Vector lengths that get an approximate index. See the module docstring for
#: why these and not others, and why 3072 must go through halfvec.
INDEXED_DIMENSIONS: tuple[int, ...] = (384, 512, 768, 1024, 1536, 3072)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _embedding_column() -> sa.Column[object]:
    """The vector column, in whichever type the dialect can express.

    A migration here describes SQL and never imports the application, but the
    column type genuinely differs by dialect: PostgreSQL gets pgvector's
    ``VECTOR`` with no modifier, and SQLite — where the unit suite runs — gets
    JSON, because distance is not expressible there at all. Retrieval is
    therefore behind a contract with one real implementation, and is verified by
    a live script rather than by the suite.
    """
    if _is_postgres():
        from pgvector.sqlalchemy import VECTOR

        return sa.Column("embedding", VECTOR(), nullable=True)
    return sa.Column("embedding", sa.JSON(), nullable=True)


def upgrade() -> None:
    if _is_postgres():
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "resource_shares",
        sa.Column("resource_kind", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("principal_kind", sa.String(32), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        # SET NULL rather than CASCADE: erasing an administrator must not erase
        # the record of what they granted.
        sa.Column(
            "granted_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("resource_kind", "resource_id", "principal_kind", "principal_id"),
    )
    op.create_index(
        "ix_resource_shares_principal",
        "resource_shares",
        ["principal_kind", "principal_id"],
    )
    op.create_index(
        "ix_resource_shares_resource",
        "resource_shares",
        ["resource_kind", "resource_id"],
    )

    op.create_table(
        "files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        # CASCADE, unlike every identity foreign key on the ledger: a usage row
        # is financial history and must survive its subject, a file is that
        # person's content and must not.
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "billing_group_id",
            sa.Uuid(),
            sa.ForeignKey("groups.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_files_owner_sha", "files", ["owner_user_id", "sha256"])
    op.create_index("ix_files_created", "files", ["created_at"])

    op.create_table(
        "file_blobs",
        sa.Column(
            "file_id",
            sa.Uuid(),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # A table of its own so that listing files never drags the bytes through
        # the connection, and so that moving them out of PostgreSQL later is a
        # change to one module.
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("file_id"),
    )

    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "billing_group_id",
            sa.Uuid(),
            sa.ForeignKey("groups.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # RESTRICT: without knowing which model made the vectors, a query cannot
        # be embedded compatibly and the base is silently useless. Models
        # deactivate rather than delete, so this never fires — it says that
        # deleting one would be a bug.
        sa.Column(
            "embedding_model_id",
            sa.Uuid(),
            sa.ForeignKey("models.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "extractor_model_id",
            sa.Uuid(),
            sa.ForeignKey("models.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Learned from the length of the first embedding, never typed in.
        sa.Column("dimensions", sa.Integer(), nullable=True),
        sa.Column("chunk_chars", sa.Integer(), nullable=False),
        sa.Column("chunk_overlap", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_bases_owner", "knowledge_bases", ["owner_user_id"])
    op.create_index("ix_knowledge_bases_created", "knowledge_bases", ["created_at"])

    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "knowledge_base_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # SET NULL, not CASCADE: deleting the upload must not silently delete
        # what was learned from it. Only re-extraction becomes impossible.
        sa.Column(
            "file_id",
            sa.Uuid(),
            sa.ForeignKey("files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("source_ref", sa.String(255), nullable=True),
        # The extracted text is kept: re-embedding then costs no extraction at
        # all, where re-extracting a scanned PDF costs money per page.
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("text_sha256", sa.String(64), nullable=True),
        sa.Column("pages", sa.Integer(), nullable=False),
        sa.Column("chars", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_documents_base",
        "knowledge_documents",
        ["knowledge_base_id", "status"],
    )
    # Partial and unique: re-indexing the same external thing replaces it rather
    # than adding a second copy, and a document with no external reference is
    # not constrained at all.
    op.create_index(
        "ix_knowledge_documents_source",
        "knowledge_documents",
        ["knowledge_base_id", "source_ref"],
        unique=True,
        postgresql_where=sa.text("source_ref IS NOT NULL"),
        sqlite_where=sa.text("source_ref IS NOT NULL"),
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Denormalised from the document: every retrieval is "nearest within one
        # base", and a join in front of the vector index buys nothing.
        sa.Column(
            "knowledge_base_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        _embedding_column(),
        # Denormalised from the base because it is the partial-index predicate,
        # which cannot be a join.
        sa.Column("dimensions", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_chunks_base",
        "knowledge_chunks",
        ["knowledge_base_id", "dimensions"],
    )
    op.create_index("ix_knowledge_chunks_document", "knowledge_chunks", ["document_id", "ordinal"])

    if _is_postgres():
        for dim in INDEXED_DIMENSIONS:
            # halfvec for every length rather than vector below 2000 and halfvec
            # above it: one query path is worth more than the last bits of
            # precision in a ranking, and a branch at 2000 is a branch nobody
            # would ever exercise in a test.
            op.execute(
                f"CREATE INDEX ix_knowledge_chunks_hnsw_{dim} ON knowledge_chunks "
                f"USING hnsw ((embedding::halfvec({dim})) halfvec_cosine_ops) "
                f"WHERE dimensions = {dim}"
            )


def downgrade() -> None:
    if _is_postgres():
        for dim in INDEXED_DIMENSIONS:
            op.execute(f"DROP INDEX IF EXISTS ix_knowledge_chunks_hnsw_{dim}")

    op.drop_index("ix_knowledge_chunks_document", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_base", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")

    op.drop_index("ix_knowledge_documents_source", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_base", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")

    op.drop_index("ix_knowledge_bases_created", table_name="knowledge_bases")
    op.drop_index("ix_knowledge_bases_owner", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")

    op.drop_table("file_blobs")
    op.drop_index("ix_files_created", table_name="files")
    op.drop_index("ix_files_owner_sha", table_name="files")
    op.drop_table("files")

    op.drop_index("ix_resource_shares_resource", table_name="resource_shares")
    op.drop_index("ix_resource_shares_principal", table_name="resource_shares")
    op.drop_table("resource_shares")

    # The extension is deliberately left in place. Dropping it would fail while
    # any other table used the type, and a downgrade that succeeds only when
    # nothing else uses pgvector is a downgrade that fails at the worst moment.
