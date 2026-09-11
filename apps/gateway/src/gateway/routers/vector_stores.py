"""``/v1/vector_stores`` — knowledge bases, and retrieving from them.

OpenAI's endpoint names and object shapes where they exist, because a knowledge
base *is* their vector store and reinventing the vocabulary would buy nothing.
Four routes here are ours, and each is here because the OpenAI surface has no
equivalent:

* ``POST .../text`` adds a passage of text with no file behind it. This is what
  "retrieve over previous chats" needs — the chat has a transcript, not an
  upload — and giving that its own tables would mean two indexes, two embedding
  configurations and two reindex buttons.
* ``POST .../reindex`` re-embeds what is already stored. The feature that makes
  a *configurable* embedding model honest: without it, changing the setting
  leaves every existing base on its old model with no way forward.
* ``.../shares`` is the sharing surface, over the ACL in `sharing.py`.
* ``GET .../status`` reports what the deployment is configured to do and
  whether it is ready, so a client can say "no embedding model has been
  chosen" rather than showing an upload button that fails.

**Retrieval is metered.** A search embeds its query, and an embedding costs
tokens; a search nobody is charged for is a search that does not appear in the
report explaining the bill. It is billed to the base's group, not the searcher's
— the corpus is what is being used.

**The query is embedded with the model the base was indexed with**, never the
deployment's current default. Comparing vectors from two models does not degrade
retrieval, it makes it meaningless, and a same-dimension change would mis-rank
silently rather than raising.
"""

from __future__ import annotations

import uuid
from typing import Literal, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from gateway import sharing
from gateway.deps import Principal, PrincipalDep, SessionDep
from gateway.errors import BadRequestError, NotFoundError, PermissionError_
from gateway.knowledge.pipeline import IngestionFailed, Ingestor
from gateway.knowledge.store import store_for
from gateway.models import (
    EVERYONE_PRINCIPAL_ID,
    Group,
    IndexStatus,
    KnowledgeBase,
    KnowledgeDocument,
    ModelDef,
    ResourceKind,
    SharePrincipal,
    ShareRole,
    StoredFile,
    User,
)
from gateway.routers.files import KnowledgeDep

router = APIRouter(prefix="/v1", tags=["pystino"])

KIND = ResourceKind.KNOWLEDGE_BASE


# -- request and response shapes --------------------------------------------


class VectorStoreCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=500)


class VectorStoreUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=500)


class FileCounts(BaseModel):
    """OpenAI's ``file_counts``, which is exactly the progress a client needs."""

    in_progress: int = 0
    completed: int = 0
    failed: int = 0
    total: int = 0


class VectorStoreObject(BaseModel):
    id: uuid.UUID
    object: Literal["vector_store"] = "vector_store"
    created_at: int
    name: str
    description: str
    file_counts: FileCounts
    #: Ours. A base that has never been indexed has no dimensionality and no
    #: model, and a client showing a search box for it would be lying.
    dimensions: int | None
    embedding_model: str | None
    #: Whether this caller owns it, which decides whether the share and delete
    #: controls are shown. Cheaper and more honest than having the client
    #: guess from a 403.
    owned: bool
    role: str


class VectorStoreList(BaseModel):
    object: Literal["list"] = "list"
    data: list[VectorStoreObject]


class DocumentObject(BaseModel):
    id: uuid.UUID
    object: Literal["vector_store.file"] = "vector_store.file"
    created_at: int
    #: OpenAI's vocabulary for the same idea, so a client's progress spinner
    #: needs no translation: `in_progress`, `completed`, `failed`.
    status: str
    title: str
    filename: str | None
    file_id: uuid.UUID | None
    source_ref: str | None
    chunk_count: int
    pages: int
    chars: int
    last_error: str | None
    indexed_at: int | None


class DocumentList(BaseModel):
    object: Literal["list"] = "list"
    data: list[DocumentObject]


class AttachFile(BaseModel):
    file_id: uuid.UUID
    #: What to call it in a citation. Defaults to the uploaded filename, which
    #: is what a reader recognises.
    title: str | None = Field(default=None, max_length=255)


class AddText(BaseModel):
    text: str = Field(min_length=1)
    title: str = Field(default="", max_length=255)
    #: An opaque handle owned by the caller — the chat writes
    #: ``chat:conversation:<id>``. Re-posting the same one **replaces** rather
    #: than duplicating, which is what makes indexing a growing conversation
    #: idempotent instead of accumulating five copies of the same transcript.
    source_ref: str | None = Field(default=None, max_length=255)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    max_num_results: int = Field(default=0, ge=0, le=100)
    #: Cosine similarity floor. Higher is stricter. Zero returns the nearest
    #: whatever they are, which is the right default because a threshold tuned
    #: for one embedding model is wrong for the next.
    min_score: float = Field(default=-1.0, ge=-1.0, le=1.0)


class SearchHit(BaseModel):
    document_id: uuid.UUID
    chunk_id: uuid.UUID
    ordinal: int
    score: float
    text: str
    title: str
    source_ref: str | None


class SearchResults(BaseModel):
    object: Literal["vector_store.search_results.page"] = "vector_store.search_results.page"
    search_query: str
    data: list[SearchHit]


class ShareIn(BaseModel):
    """Who to share with, named in whichever way the caller has.

    Three fields for one thing, because the callers differ. A machine holds the
    id. A person sharing from the chat holds a colleague's address or a group's
    name and has no way to turn either into a uuid — `/api/admin/users` reads a
    session cookie and requires admin, so there is nothing a bearer token can
    ask. `sharing.resolve_principal` decides between them, and records what the
    friendly forms disclose.
    """

    principal_kind: SharePrincipal
    principal_id: uuid.UUID | None = None
    principal_email: str | None = Field(default=None, max_length=320)
    group_name: str | None = Field(default=None, max_length=255)
    role: ShareRole = ShareRole.VIEWER


class ShareObject(BaseModel):
    principal_kind: SharePrincipal
    principal_id: uuid.UUID
    role: ShareRole
    #: The user's email where the principal is a user and still exists. Null for
    #: a group, and null for a grant whose user has been erased — which is
    #: still listed, because nothing else would ever say the dead row is there.
    email: str | None
    created_at: int


class ShareList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ShareObject]


class KnowledgeStatus(BaseModel):
    """What the deployment is configured to do, for a client to read once."""

    enabled: bool
    ready: bool
    embedding_model: str | None
    extractor_model: str | None
    vector_store: str
    chunk_chars: int
    chunk_overlap: int
    max_upload_bytes: int
    source: str
    #: How long a configuration change takes to reach every worker. Reported
    #: because a change that is not instant should say so rather than leave
    #: somebody refreshing a screen (`redaction` reports the same thing).
    propagation_seconds: float
    #: Which decision is missing, when `ready` is false. Prose, because the
    #: answer is always "an administrator has to choose something".
    detail: str | None


# -- helpers ----------------------------------------------------------------


def _ingestor(request: Request) -> Ingestor:
    ingestor: Ingestor | None = getattr(request.app.state, "ingestor", None)
    if ingestor is None:  # pragma: no cover - wired at startup
        raise NotFoundError("This deployment does not have knowledge bases enabled.")
    return ingestor


async def _counts(session: SessionDep, base_id: uuid.UUID) -> FileCounts:
    """One grouped query rather than one per status."""
    rows = await session.execute(
        select(KnowledgeDocument.status, func.count())
        .where(KnowledgeDocument.knowledge_base_id == base_id)
        .group_by(KnowledgeDocument.status)
    )
    counts = FileCounts()
    for status, count in rows.all():
        if status is IndexStatus.READY:
            counts.completed += count
        elif status is IndexStatus.FAILED:
            counts.failed += count
        else:
            counts.in_progress += count
        counts.total += count
    return counts


_OPENAI_STATUS = {
    IndexStatus.PENDING: "in_progress",
    IndexStatus.EXTRACTING: "in_progress",
    IndexStatus.EMBEDDING: "in_progress",
    IndexStatus.READY: "completed",
    IndexStatus.FAILED: "failed",
}


def _document_object(document: KnowledgeDocument, filename: str | None) -> DocumentObject:
    return DocumentObject(
        id=document.id,
        created_at=int(document.created_at.timestamp()),
        status=_OPENAI_STATUS[document.status],
        title=document.title,
        filename=filename,
        file_id=document.file_id,
        source_ref=document.source_ref,
        chunk_count=document.chunk_count,
        pages=document.pages,
        chars=document.chars,
        last_error=document.error or None,
        indexed_at=int(document.indexed_at.timestamp()) if document.indexed_at else None,
    )


async def _reachable_base(
    session: SessionDep,
    principal: Principal,
    base_id: uuid.UUID,
    *,
    role: ShareRole = ShareRole.VIEWER,
) -> KnowledgeBase:
    """One base this caller may reach, in this role, or a 404/403.

    A base the caller cannot see at all is a **404**: which bases exist is not
    their business. A base they can see but not write is a **403**, because that
    is a fact about their role rather than about the resource, and answering 404
    there would make "shared read-only" indistinguishable from "deleted".
    """
    base = await session.get(KnowledgeBase, base_id)
    if base is None or not base.is_active:
        raise NotFoundError(f"No such vector store: {base_id}")
    if await sharing.may_reach(
        session,
        kind=KIND,
        resource_id=base.id,
        owner_user_id=base.owner_user_id,
        user_id=principal.user.id,
        role=ShareRole.VIEWER,
    ):
        if role is ShareRole.VIEWER:
            return base
        if await sharing.may_reach(
            session,
            kind=KIND,
            resource_id=base.id,
            owner_user_id=base.owner_user_id,
            user_id=principal.user.id,
            role=role,
        ):
            return base
        raise PermissionError_("You have read-only access to this vector store.")
    raise NotFoundError(f"No such vector store: {base_id}")


async def _object(
    session: SessionDep, base: KnowledgeBase, principal: Principal
) -> VectorStoreObject:
    model_name: str | None = None
    if base.embedding_model_id is not None:
        model_name = cast(
            "str | None",
            await session.scalar(
                select(ModelDef.name).where(ModelDef.id == base.embedding_model_id)
            ),
        )
    owned = base.owner_user_id == principal.user.id
    role = "owner"
    if not owned:
        role = (
            "editor"
            if await sharing.may_reach(
                session,
                kind=KIND,
                resource_id=base.id,
                owner_user_id=base.owner_user_id,
                user_id=principal.user.id,
                role=ShareRole.EDITOR,
            )
            else "viewer"
        )
    return VectorStoreObject(
        id=base.id,
        created_at=int(base.created_at.timestamp()),
        name=base.name,
        description=base.description,
        file_counts=await _counts(session, base.id),
        dimensions=base.dimensions,
        embedding_model=model_name,
        owned=owned,
        role=role,
    )


# -- the deployment's configuration -----------------------------------------


@router.get("/vector_stores/status", response_model=KnowledgeStatus)
async def knowledge_status(
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
    request: Request,
) -> KnowledgeStatus:
    profile = knowledge.profile
    settings = request.app.state.settings

    async def name_of(model_id: uuid.UUID | None) -> str | None:
        if model_id is None:
            return None
        return cast(
            "str | None",
            await session.scalar(select(ModelDef.name).where(ModelDef.id == model_id)),
        )

    detail: str | None = None
    if not profile.ready:
        detail = (
            "No embedding model has been chosen. An administrator sets one on the "
            "Knowledge screen in the console."
        )
    return KnowledgeStatus(
        enabled=profile.enabled,
        ready=profile.ready,
        embedding_model=await name_of(profile.embedding_model_id),
        # Null means the built-in extractor, which is the default and the only
        # backend that never sends a document anywhere.
        extractor_model=await name_of(profile.extractor_model_id),
        vector_store=profile.vector_store,
        chunk_chars=profile.chunk_chars,
        chunk_overlap=profile.chunk_overlap,
        max_upload_bytes=settings.knowledge.max_upload_bytes,
        source=profile.source,
        propagation_seconds=knowledge.refresh_seconds,
        detail=detail,
    )


# -- the stores themselves ---------------------------------------------------


@router.post("/vector_stores", response_model=VectorStoreObject)
async def create_vector_store(
    body: VectorStoreCreate,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> VectorStoreObject:
    """Create a knowledge base, snapshotting how it is to be built.

    The chunk geometry and the embedding model are copied onto the row rather
    than read at ingestion time. That is what makes the deployment's settings
    safe to change: this base keeps being built the way it was created, and a
    later change to the default cannot silently re-chunk half of it.
    """
    profile = knowledge.profile
    base = KnowledgeBase(
        name=body.name,
        description=body.description,
        owner_user_id=principal.user.id,
        billing_group_id=principal.billing_group.id,
        embedding_model_id=profile.embedding_model_id,
        extractor_model_id=profile.extractor_model_id,
        chunk_chars=profile.chunk_chars,
        chunk_overlap=profile.chunk_overlap,
    )
    session.add(base)
    await session.commit()
    return await _object(session, base, principal)


@router.get("/vector_stores", response_model=VectorStoreList)
async def list_vector_stores(
    session: SessionDep, principal: PrincipalDep, knowledge: KnowledgeDep
) -> VectorStoreList:
    """Everything this caller owns or has been shared, newest first."""
    group_ids = await sharing.effective_group_ids(session, principal.user.id)
    rows = await session.execute(
        select(KnowledgeBase)
        .where(
            KnowledgeBase.is_active.is_(True),
            sharing.reachable(
                kind=KIND,
                owner_column=KnowledgeBase.owner_user_id,
                resource_id_column=KnowledgeBase.id,
                user_id=principal.user.id,
                group_ids=group_ids,
            ),
        )
        .order_by(KnowledgeBase.created_at.desc())
    )
    bases = list(rows.scalars())
    return VectorStoreList(data=[await _object(session, base, principal) for base in bases])


@router.get("/vector_stores/{base_id}", response_model=VectorStoreObject)
async def retrieve_vector_store(
    base_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> VectorStoreObject:
    base = await _reachable_base(session, principal, base_id)
    return await _object(session, base, principal)


@router.post("/vector_stores/{base_id}", response_model=VectorStoreObject)
async def update_vector_store(
    base_id: uuid.UUID,
    body: VectorStoreUpdate,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> VectorStoreObject:
    base = await _reachable_base(session, principal, base_id, role=ShareRole.EDITOR)
    if body.name is not None:
        base.name = body.name
    if body.description is not None:
        base.description = body.description
    await session.commit()
    return await _object(session, base, principal)


@router.delete("/vector_stores/{base_id}")
async def delete_vector_store(
    base_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> dict[str, object]:
    """Only the owner, and it takes the grants with it.

    Deleting is deliberately **not** an editor's right: an editor was given the
    ability to add and remove documents, and reading "you may edit this" as
    "you may destroy it" is the kind of inference an ACL should never make.
    Every share is removed explicitly, because `resource_shares.resource_id` is
    not a foreign key and nothing in the schema will do it.
    """
    base = await session.get(KnowledgeBase, base_id)
    if base is None or not base.is_active:
        raise NotFoundError(f"No such vector store: {base_id}")
    if base.owner_user_id != principal.user.id:
        # A viewer gets the same 404 they would get for a base that does not
        # exist; an editor is told why, because they can see it.
        if await sharing.may_reach(
            session,
            kind=KIND,
            resource_id=base.id,
            owner_user_id=base.owner_user_id,
            user_id=principal.user.id,
        ):
            raise PermissionError_("Only the owner can delete a vector store.")
        raise NotFoundError(f"No such vector store: {base_id}")

    await sharing.delete_shares(session, kind=KIND, resource_id=base.id)
    await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == base.id))
    await session.commit()
    return {"id": str(base_id), "object": "vector_store.deleted", "deleted": True}


# -- documents in a store ----------------------------------------------------


@router.get("/vector_stores/{base_id}/files", response_model=DocumentList)
async def list_documents(
    base_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> DocumentList:
    await _reachable_base(session, principal, base_id)
    rows = await session.execute(
        select(KnowledgeDocument, StoredFile.filename)
        .outerjoin(StoredFile, StoredFile.id == KnowledgeDocument.file_id)
        .where(KnowledgeDocument.knowledge_base_id == base_id)
        .order_by(KnowledgeDocument.created_at.desc())
    )
    return DocumentList(
        data=[_document_object(document, filename) for document, filename in rows.all()]
    )


@router.post("/vector_stores/{base_id}/files", response_model=DocumentObject)
async def attach_file(
    base_id: uuid.UUID,
    body: AttachFile,
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> DocumentObject:
    """Index one uploaded file into this base.

    Returns immediately with ``status: in_progress``. Extraction and embedding
    happen in a detached task (ADR 0019 — OCR belongs nowhere near a request
    path), and the document's status is the progress bar.
    """
    base = await _reachable_base(session, principal, base_id, role=ShareRole.EDITOR)
    if not knowledge.profile.ready:
        raise BadRequestError(
            "No embedding model has been configured for this deployment, so "
            "documents cannot be indexed yet.",
            code="embedding_model_not_configured",
        )
    stored = await session.get(StoredFile, body.file_id)
    # A file belongs to one person: indexing somebody else's upload into a base
    # you happen to be able to edit would be a way to read their document back
    # out through search.
    if stored is None or stored.owner_user_id != principal.user.id:
        raise NotFoundError(f"No such file: {body.file_id}")

    document = KnowledgeDocument(
        knowledge_base_id=base.id,
        file_id=stored.id,
        title=body.title or stored.filename,
        status=IndexStatus.PENDING,
    )
    session.add(document)
    await session.commit()

    _ingestor(request).spawn(document.id, knowledge.profile)
    return _document_object(document, stored.filename)


@router.post("/vector_stores/{base_id}/text", response_model=DocumentObject)
async def add_text(
    base_id: uuid.UUID,
    body: AddText,
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> DocumentObject:
    """Index a passage of text with no file behind it.

    Ours, not OpenAI's, and the reason is "retrieve over previous chats": the
    chat has a transcript rather than an upload. Reusing this table means one
    index, one embedding configuration and one reindex button instead of two of
    each.

    ``source_ref`` makes it idempotent. Re-posting the same handle **replaces**
    the document and its chunks, which is what indexing a conversation that is
    still growing needs — otherwise a base accumulates five overlapping copies
    of the same thread and every search returns all of them.
    """
    base = await _reachable_base(session, principal, base_id, role=ShareRole.EDITOR)
    if not knowledge.profile.ready:
        raise BadRequestError(
            "No embedding model has been configured for this deployment, so "
            "text cannot be indexed yet.",
            code="embedding_model_not_configured",
        )

    document: KnowledgeDocument | None = None
    if body.source_ref is not None:
        result = await session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_id == base.id,
                KnowledgeDocument.source_ref == body.source_ref,
            )
        )
        document = result.scalars().first()

    if document is None:
        document = KnowledgeDocument(
            knowledge_base_id=base.id,
            source_ref=body.source_ref,
            title=body.title or (body.source_ref or "text"),
        )
        session.add(document)
    else:
        # Replacing: the old chunks go now rather than at the end of ingestion,
        # so a search landing mid-reindex cannot return passages from the
        # previous version alongside the new ones.
        store = store_for(session.bind.dialect.name if session.bind else "sqlite")
        await store.delete_document(session, document.id)
        document.title = body.title or document.title

    document.text = body.text
    document.chars = len(body.text)
    document.chunk_count = 0
    document.status = IndexStatus.PENDING
    document.error = ""
    document.indexed_at = None
    await session.commit()

    _ingestor(request).spawn(document.id, knowledge.profile)
    return _document_object(document, None)


@router.delete("/vector_stores/{base_id}/files/{document_id}")
async def delete_document(
    base_id: uuid.UUID,
    document_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> dict[str, object]:
    await _reachable_base(session, principal, base_id, role=ShareRole.EDITOR)
    document = await session.get(KnowledgeDocument, document_id)
    if document is None or document.knowledge_base_id != base_id:
        raise NotFoundError(f"No such document: {document_id}")
    # The chunks go with it by CASCADE; the uploaded file does not, because it
    # is the caller's own and they may want to index it elsewhere.
    await session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == document_id))
    await session.commit()
    return {"id": str(document_id), "object": "vector_store.file.deleted", "deleted": True}


@router.post("/vector_stores/{base_id}/reindex", response_model=DocumentList)
async def reindex(
    base_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> DocumentList:
    """Re-embed everything in this base with the current configuration.

    The feature that makes a configurable embedding model honest. Without it,
    changing the setting leaves every existing base on its old model with no
    way forward, and "configurable" would mean "configurable once".

    **Re-embedding, not re-extraction.** The extracted text is kept on the
    document precisely so this costs one embedding call per batch and no OCR at
    all — re-extracting a scanned PDF costs real money per page. A document
    whose text was lost with its file cannot be rebuilt and is left alone rather
    than failed.

    Owner only. A reindex spends the owning group's budget, and an editor
    spending somebody else's money is not a right "may add documents" implies.
    """
    base = await session.get(KnowledgeBase, base_id)
    if base is None or not base.is_active:
        raise NotFoundError(f"No such vector store: {base_id}")
    if base.owner_user_id != principal.user.id:
        raise PermissionError_("Only the owner can reindex a vector store.")
    if not knowledge.profile.ready:
        raise BadRequestError(
            "No embedding model has been configured for this deployment.",
            code="embedding_model_not_configured",
        )

    # The base moves to the deployment's current model. This is the one place
    # that changes it, so a base's model only ever changes as a deliberate act.
    base.embedding_model_id = knowledge.profile.embedding_model_id
    base.extractor_model_id = knowledge.profile.extractor_model_id
    base.dimensions = None

    rows = await session.execute(
        select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == base.id)
    )
    documents = [
        document
        for document in rows.scalars()
        # Nothing to re-embed from, and nothing to re-extract with: leave it as
        # it is rather than marking it failed, so a lost file does not turn a
        # reindex into a list of errors nobody can act on.
        if document.text is not None or document.file_id is not None
    ]
    for document in documents:
        document.status = IndexStatus.PENDING
        document.error = ""
        document.indexed_at = None
    await session.commit()

    ingestor = _ingestor(request)
    for document in documents:
        ingestor.spawn(document.id, knowledge.profile)
    return DocumentList(data=[_document_object(document, None) for document in documents])


# -- retrieval ---------------------------------------------------------------


@router.post("/vector_stores/{base_id}/search", response_model=SearchResults)
async def search(
    base_id: uuid.UUID,
    body: SearchRequest,
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> SearchResults:
    """The nearest passages in one base, best first.

    Costs one embedding call, billed to the base's group — the corpus is what
    is being used, and a search charged to the searcher would put a shared
    base's retrieval cost on whoever happened to ask.
    """
    base = await _reachable_base(session, principal, base_id)
    if base.dimensions is None or base.embedding_model_id is None:
        # Not an error: a base with nothing indexed yet has no vectors to
        # compare against, and an empty result is the honest answer.
        return SearchResults(search_query=body.query, data=[])

    settings = request.app.state.settings
    limit = body.max_num_results or settings.knowledge.search_limit
    # -1.0 is the sentinel for "not specified" rather than a real floor: a
    # cosine similarity of -1 is a legitimate value (opposite vectors), so
    # `min_score: 0` from a caller has to be distinguishable from silence.
    floor = settings.knowledge.min_score if body.min_score == -1.0 else body.min_score

    ingestor = _ingestor(request)
    owner, group = await ingestor.billing_for(session, base)
    try:
        vector = await ingestor.embed_query(
            session, base=base, owner=owner, group=group, text=body.query
        )
    except IngestionFailed as exc:
        raise BadRequestError(str(exc), code="search_unavailable") from exc

    store = store_for(session.bind.dialect.name if session.bind else "sqlite")
    hits = await store.search(
        session,
        knowledge_base_id=base.id,
        dimensions=base.dimensions,
        query=vector,
        limit=limit,
        min_score=floor,
    )
    await session.commit()
    return SearchResults(
        search_query=body.query,
        data=[
            SearchHit(
                document_id=hit.document_id,
                chunk_id=hit.chunk_id,
                ordinal=hit.ordinal,
                score=hit.score,
                text=hit.text,
                title=hit.title,
                source_ref=hit.source_ref,
            )
            for hit in hits
        ],
    )


# -- sharing -----------------------------------------------------------------


@router.get("/vector_stores/{base_id}/shares", response_model=ShareList)
async def list_base_shares(
    base_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> ShareList:
    """Who this base is shared with. Owner only.

    A viewer seeing the full recipient list would learn who else has the
    document, which is not implied by being given it.
    """
    base = await session.get(KnowledgeBase, base_id)
    if base is None or not base.is_active or base.owner_user_id != principal.user.id:
        raise NotFoundError(f"No such vector store: {base_id}")
    listed = await sharing.list_shares(session, kind=KIND, resource_id=base.id)
    return ShareList(
        data=[
            ShareObject(
                principal_kind=share.principal_kind,
                principal_id=share.principal_id,
                role=share.role,
                email=email,
                created_at=int(share.created_at.timestamp()),
            )
            for share, email in listed
        ]
    )


@router.post("/vector_stores/{base_id}/shares", response_model=ShareObject)
async def share_base(
    base_id: uuid.UUID,
    body: ShareIn,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> ShareObject:
    """Share it, or change an existing share's role. Owner only.

    Re-sharing is an upsert of the role — the composite primary key forbids one
    principal holding two — and it keeps ``granted_by`` and ``created_at``,
    because a role change is not a new grant.

    **The principal must exist**, and a group must be active. Silently storing
    a grant to a nonexistent id would look like it worked and never take
    effect.
    """
    base = await session.get(KnowledgeBase, base_id)
    # `administers` rather than an owner comparison, so a base the deployment
    # owns can be published by an administrator — and so a colleague's cannot
    # be, by anybody (ADR 0066). 404 rather than 403 for the same reason as
    # everywhere else here: telling the two apart confirms the base exists.
    if (
        base is None
        or not base.is_active
        or not sharing.administers(
            owner_user_id=base.owner_user_id,
            user_id=principal.user.id,
            is_admin=principal.user.is_admin,
        )
    ):
        raise NotFoundError(f"No such vector store: {base_id}")

    if body.principal_kind is SharePrincipal.EVERYONE:
        # Nothing to resolve: the id is a constant placeholder rather than an
        # address, and there is no row to check exists.
        principal_id = EVERYONE_PRINCIPAL_ID
    else:
        principal_id = await sharing.resolve_principal(
            session,
            kind=body.principal_kind,
            principal_id=body.principal_id,
            principal_email=body.principal_email,
            group_name=body.group_name,
            caller_id=principal.user.id,
        )

    if body.principal_kind is SharePrincipal.EVERYONE:
        pass
    elif body.principal_kind is SharePrincipal.USER:
        target = await session.get(User, principal_id)
        if target is None or not target.is_active:
            raise BadRequestError("No such user.", code="unknown_principal")
        if target.id == base.owner_user_id:
            raise BadRequestError(
                "The owner already has full access; sharing with them would do nothing.",
                code="redundant_share",
            )
    else:
        group = await session.get(Group, principal_id)
        if group is None or not group.is_active:
            raise BadRequestError("No such group.", code="unknown_principal")

    share = await sharing.grant(
        session,
        kind=KIND,
        resource_id=base.id,
        principal_kind=body.principal_kind,
        principal_id=principal_id,
        role=body.role,
        granted_by=principal.user.id,
    )
    await session.commit()
    email: str | None = None
    if body.principal_kind is SharePrincipal.USER:
        email = await session.scalar(select(User.email).where(User.id == principal_id))
    return ShareObject(
        principal_kind=share.principal_kind,
        principal_id=share.principal_id,
        role=share.role,
        email=email,
        created_at=int(share.created_at.timestamp()),
    )


@router.delete("/vector_stores/{base_id}/shares/{principal_kind}/{principal_id}")
async def unshare_base(
    base_id: uuid.UUID,
    principal_kind: SharePrincipal,
    principal_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> dict[str, object]:
    """Withdraw one grant. Idempotent.

    The owner may withdraw any of them. An **administrator may withdraw a
    publication and nothing else** (ADR 0066) — removing reach is not granting
    it, and an operator must be able to pull something from the whole
    deployment without deleting somebody's work to do it. Every other grant on
    a base they do not own stays out of reach, including the personal share
    sitting beside the publication they just withdrew.
    """
    base = await session.get(KnowledgeBase, base_id)
    permitted = base is not None and (
        sharing.may_unpublish(
            owner_user_id=base.owner_user_id,
            user_id=principal.user.id,
            is_admin=principal.user.is_admin,
        )
        if principal_kind is SharePrincipal.EVERYONE
        else sharing.administers(
            owner_user_id=base.owner_user_id,
            user_id=principal.user.id,
            is_admin=principal.user.is_admin,
        )
    )
    if base is None or not base.is_active or not permitted:
        raise NotFoundError(f"No such vector store: {base_id}")
    removed = await sharing.revoke(
        session,
        kind=KIND,
        resource_id=base.id,
        principal_kind=principal_kind,
        principal_id=principal_id,
    )
    await session.commit()
    # Not a 404 when there was nothing to remove: an administrator clicking
    # twice has not made a mistake, and the end state is what they asked for.
    return {"object": "vector_store.share.deleted", "deleted": removed}
