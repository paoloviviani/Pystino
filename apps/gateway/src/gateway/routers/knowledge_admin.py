"""``/v1/knowledge`` — configuring the pipeline, for an administrator of the *product*.

This lived under ``/api/admin`` and in the gateway console for one commit, and
that was wrong. Which embedding model a deployment uses, and what reads its
documents, is a decision about the **chat product** rather than about the
gateway's operation: the gateway supplies extraction, embedding and a vector
store the way it supplies models and quotas, and choosing among them belongs to
whoever runs the application people actually use. So the screen moved to the
chat's own admin console, and the surface had to follow it here.

**It had to move, not merely be duplicated.** Every ``/api`` route is behind
``get_management_user``, which reads a session cookie and nothing else. The chat
authenticates its users with an OIDC access token, so it could not call
``/api/admin/knowledge`` at all — the same door problem ADR 0061 records for
billing groups, reaching further this time because it covers a whole admin
screen rather than one lookup.

**An API key cannot configure this; only a signed-in administrator can.** That
asymmetry is deliberate and it is the one new rule here. ``is_admin`` is a
property of a *user*, and both a key and a bearer token resolve to one — so
gating on the flag alone would let any key held by an administrator repoint the
extractor at a host of the holder's choosing. A key is a credential a program
holds, frequently in a config file; an access token is evidence a person signed
in minutes ago. Spending money with a key is what keys are for; changing where
documents get sent is not.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.deps import Principal, PrincipalDep, SessionDep
from gateway.errors import BadRequestError, NotFoundError, PermissionError_
from gateway.models import (
    Group,
    IndexStatus,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeConfig,
    KnowledgeDocument,
    ModelDef,
    ModelKind,
    Provider,
    ResourceKind,
    ResourceShare,
    User,
)
from gateway.routers.files import KnowledgeDep

router = APIRouter(prefix="/v1/knowledge", tags=["pystino"])

#: The local extractor's plugin name. A model served by it never sends a
#: document anywhere, which is what makes it the safe default and what makes
#: replacing it the one change that needs a reason.
LOCAL_EXTRACTOR = "extractor"


def require_admin_session(principal: PrincipalDep) -> Principal:
    """A signed-in administrator. Not an API key, however privileged its owner.

    Two refusals, and they are different answers on purpose. A **key** is
    refused even when its owner is an administrator, because the credential is
    the wrong kind — a 403 naming the reason, so whoever wired the client up can
    fix it. A **non-administrator** is also a 403 rather than a 404: they are
    authenticated and the endpoint exists, and pretending otherwise would send
    them looking for a deployment problem.
    """
    if principal.api_key is not None:
        raise PermissionError_(
            "Configuring the knowledge pipeline needs a signed-in administrator. "
            "An API key cannot change where documents are sent, however "
            "privileged its owner."
        )
    if not principal.user.is_admin:
        raise PermissionError_("This needs an administrator.")
    return principal


AdminDep = Annotated[Principal, Depends(require_admin_session)]


# -- shapes ------------------------------------------------------------------


class ConfigRequest(BaseModel):
    """A change. Omitting a field leaves it alone.

    The server's rule rather than a convenience: null in the stored row means
    "this row does not decide", so accepting a whole document would overwrite
    settings nobody touched — and silently re-chunk every base created
    afterwards.
    """

    model_config = ConfigDict(extra="forbid")

    embedding_model: str | None = None
    extractor_model: str | None = None
    #: Explicitly return to the built-in extractor. Needed because null on
    #: ``extractor_model`` means "no change", while "no extractor model" is a
    #: real and different setting.
    clear_extractor: bool = False
    chunk_chars: int | None = Field(default=None, ge=80, le=20_000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=5_000)
    reason: str = Field(default="", max_length=500)


class BaseSummary(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    owner_email: str | None
    group_name: str | None
    embedding_model: str | None
    dimensions: int | None
    document_count: int
    chunk_count: int
    failed_count: int
    #: Indexed with a different embedding model than the one now configured, so
    #: its vectors are not comparable with a new base's. Not a fault — it still
    #: answers searches from its own vectors — but it is what reindex is for.
    stale: bool
    share_count: int
    created_at: datetime


class ConfigEntry(BaseModel):
    """One past decision. The table is append-only, so this is the trail."""

    id: uuid.UUID
    embedding_model: str | None
    extractor_model: str | None
    vector_store: str | None
    chunk_chars: int | None
    chunk_overlap: int | None
    reason: str
    changed_by: str | None
    created_at: datetime


class KnowledgeAdminStatus(BaseModel):
    object: Literal["knowledge.configuration"] = "knowledge.configuration"
    enabled: bool
    ready: bool
    embedding_model: str | None
    extractor_model: str | None
    vector_store: str
    chunk_chars: int
    chunk_overlap: int
    max_upload_bytes: int
    #: ``console`` when a row decides it, ``environment`` when none does.
    #: Reported because the two disagreeing is otherwise invisible.
    source: str
    propagation_seconds: float
    detail: str | None
    available_embedding_models: list[str]
    available_extractor_models: list[str]
    bases: list[BaseSummary]
    stale_base_count: int
    history: list[ConfigEntry]


# -- reading -----------------------------------------------------------------


async def _status(session: AsyncSession, request: Request) -> KnowledgeAdminStatus:
    resolver = request.app.state.knowledge
    profile = resolver.profile
    settings = request.app.state.settings

    # Every model's name in one query: the response names the configured model,
    # each base's model and each history row's, and one lookup apiece would be
    # dozens of round trips to render one screen.
    named = (await session.execute(select(ModelDef.id, ModelDef.name))).all()
    names: dict[uuid.UUID, str] = {row[0]: row[1] for row in named}

    def name_of(model_id: uuid.UUID | None) -> str | None:
        return names.get(model_id) if model_id is not None else None

    available = await session.execute(
        select(ModelDef.name, ModelDef.kind)
        .join(Provider, Provider.id == ModelDef.provider_id)
        .where(ModelDef.is_active.is_(True), Provider.is_active.is_(True))
        .order_by(ModelDef.name)
    )
    embedding_models: list[str] = []
    extractor_models: list[str] = []
    for model_name, kind in available.all():
        if kind is ModelKind.EMBEDDING:
            embedding_models.append(model_name)
        elif kind is ModelKind.OCR:
            extractor_models.append(model_name)

    doc_counts: dict[uuid.UUID, tuple[int, int]] = {
        base_id: (int(total or 0), int(failed or 0))
        for base_id, total, failed in (
            await session.execute(
                select(
                    KnowledgeDocument.knowledge_base_id,
                    func.count(),
                    func.sum(case((KnowledgeDocument.status == IndexStatus.FAILED, 1), else_=0)),
                ).group_by(KnowledgeDocument.knowledge_base_id)
            )
        ).all()
    }
    chunk_counts: dict[uuid.UUID, int] = {
        base_id: int(count)
        for base_id, count in (
            await session.execute(
                select(KnowledgeChunk.knowledge_base_id, func.count()).group_by(
                    KnowledgeChunk.knowledge_base_id
                )
            )
        ).all()
    }
    share_counts: dict[uuid.UUID, int] = {
        resource_id: int(count)
        for resource_id, count in (
            await session.execute(
                select(ResourceShare.resource_id, func.count())
                .where(ResourceShare.resource_kind == ResourceKind.KNOWLEDGE_BASE)
                .group_by(ResourceShare.resource_id)
            )
        ).all()
    }

    rows = await session.execute(
        select(KnowledgeBase, User.email, Group.name)
        .outerjoin(User, User.id == KnowledgeBase.owner_user_id)
        .outerjoin(Group, Group.id == KnowledgeBase.billing_group_id)
        .where(KnowledgeBase.is_active.is_(True))
        .order_by(KnowledgeBase.created_at.desc())
    )
    bases: list[BaseSummary] = []
    stale_count = 0
    for base, owner_email, group_name in rows.all():
        total, failed = doc_counts.get(base.id, (0, 0))
        # Stale means "indexed with a different model than is configured now".
        # A base with nothing indexed is not stale: it has no vectors to be
        # incomparable with.
        stale = (
            base.embedding_model_id is not None
            and profile.embedding_model_id is not None
            and base.embedding_model_id != profile.embedding_model_id
        )
        stale_count += 1 if stale else 0
        bases.append(
            BaseSummary(
                id=base.id,
                name=base.name,
                description=base.description,
                owner_email=owner_email,
                group_name=group_name,
                embedding_model=name_of(base.embedding_model_id),
                dimensions=base.dimensions,
                document_count=total,
                chunk_count=chunk_counts.get(base.id, 0),
                failed_count=failed,
                stale=stale,
                share_count=share_counts.get(base.id, 0),
                created_at=base.created_at,
            )
        )

    history_rows = await session.execute(
        select(KnowledgeConfig, User.email)
        .outerjoin(User, User.id == KnowledgeConfig.created_by)
        .order_by(KnowledgeConfig.created_at.desc())
        .limit(20)
    )
    history = [
        ConfigEntry(
            id=row.id,
            embedding_model=name_of(row.embedding_model_id),
            extractor_model=name_of(row.extractor_model_id),
            vector_store=row.vector_store,
            chunk_chars=row.chunk_chars,
            chunk_overlap=row.chunk_overlap,
            reason=row.reason,
            changed_by=email,
            created_at=row.created_at,
        )
        for row, email in history_rows.all()
    ]

    detail: str | None = None
    if not profile.enabled:
        detail = (
            "Knowledge bases are switched off for this deployment. Set "
            "GATEWAY_KNOWLEDGE__ENABLED=true and restart to offer them."
        )
    elif not profile.ready:
        detail = (
            "No embedding model has been chosen, so nothing can be indexed yet."
            if embedding_models
            else "This deployment has no embedding model at all. One has to be created "
            "in the gateway's own console first, then chosen here."
        )

    return KnowledgeAdminStatus(
        enabled=profile.enabled,
        ready=profile.ready,
        embedding_model=name_of(profile.embedding_model_id),
        extractor_model=name_of(profile.extractor_model_id),
        vector_store=profile.vector_store,
        chunk_chars=profile.chunk_chars,
        chunk_overlap=profile.chunk_overlap,
        max_upload_bytes=settings.knowledge.max_upload_bytes,
        source=profile.source,
        propagation_seconds=resolver.refresh_seconds,
        detail=detail,
        available_embedding_models=embedding_models,
        available_extractor_models=extractor_models,
        bases=bases,
        stale_base_count=stale_count,
        history=history,
    )


async def _model_named(session: AsyncSession, name: str, kind: ModelKind) -> ModelDef:
    result = await session.execute(
        select(ModelDef).where(ModelDef.name == name, ModelDef.is_active.is_(True))
    )
    model = result.scalars().first()
    if model is None:
        raise BadRequestError(f"No active model named {name!r}.", code="unknown_model")
    if model.kind is not kind:
        # Named rather than coerced: accepting a chat model as an embedder would
        # fail at the first upload with a provider error nobody could trace back
        # to this decision.
        raise BadRequestError(
            f"{name!r} is a {model.kind.value} model, not {kind.value}.",
            code="wrong_model_kind",
        )
    return model


@router.get("/config", response_model=KnowledgeAdminStatus)
async def read_config(
    admin: AdminDep, session: SessionDep, knowledge: KnowledgeDep, request: Request
) -> KnowledgeAdminStatus:
    """How documents are extracted and embedded here, and every base built so far."""
    return await _status(session, request)


@router.put("/config", response_model=KnowledgeAdminStatus)
async def write_config(
    payload: ConfigRequest,
    admin: AdminDep,
    session: SessionDep,
    knowledge: KnowledgeDep,
    request: Request,
) -> KnowledgeAdminStatus:
    """Record a new configuration. Append-only: this writes a row, never edits.

    **Changing the embedding model does not touch a single existing base.** Each
    pins the model it was indexed with, so the change applies to the next base
    created and to any base someone reindexes — which is what makes this setting
    safe to change at all rather than a data migration disguised as a form.

    A reason is required for exactly one change: replacing the built-in
    extractor with a model that sends documents to a third party. It is the only
    edit here that alters *where user documents go*, and asking for a sentence on
    every change is how people learn to type "x" (ADR 0033).
    """
    resolver = request.app.state.knowledge
    current = resolver.profile

    embedding_id = current.embedding_model_id
    if payload.embedding_model is not None:
        embedding_id = (
            await _model_named(session, payload.embedding_model, ModelKind.EMBEDDING)
        ).id

    extractor_id = current.extractor_model_id
    if payload.clear_extractor:
        extractor_id = None
    elif payload.extractor_model is not None:
        extractor = await _model_named(session, payload.extractor_model, ModelKind.OCR)
        leaves = extractor.provider is None or extractor.provider.plugin != LOCAL_EXTRACTOR
        if leaves and not payload.reason.strip():
            raise BadRequestError(
                f"{extractor.name!r} sends documents to a provider rather than "
                "extracting them here. Say why, so the decision is on the record.",
                code="reason_required",
            )
        extractor_id = extractor.id

    session.add(
        KnowledgeConfig(
            embedding_model_id=embedding_id,
            extractor_model_id=extractor_id,
            vector_store=current.vector_store,
            chunk_chars=payload.chunk_chars,
            chunk_overlap=payload.chunk_overlap,
            reason=payload.reason.strip(),
            created_by=admin.user.id,
        )
    )
    await session.commit()
    # This worker picks it up now rather than in ten seconds, so the response
    # already reflects the change. The other worker learns from the poll, and
    # `propagation_seconds` is what says so.
    await resolver.refresh()
    return await _status(session, request)


@router.post("/bases/{base_id}/reindex", response_model=KnowledgeAdminStatus)
async def reindex_base(
    base_id: uuid.UUID,
    admin: AdminDep,
    session: SessionDep,
    knowledge: KnowledgeDep,
    request: Request,
) -> KnowledgeAdminStatus:
    """Re-embed one base with the current configuration, whoever owns it.

    An administrator can reindex any base where the owner-facing route allows
    only its owner. The spend still lands on the **base's** group rather than
    the administrator's — `Ingestor` reads the base, not the caller — so
    pressing this does not move somebody else's cost onto whoever pressed it.
    """
    resolver = request.app.state.knowledge
    if not resolver.profile.ready:
        raise BadRequestError(
            "No embedding model is configured, so there is nothing to reindex with.",
            code="embedding_model_not_configured",
        )
    base = await session.get(KnowledgeBase, base_id)
    if base is None or not base.is_active:
        raise NotFoundError(f"No such knowledge base: {base_id}")

    base.embedding_model_id = resolver.profile.embedding_model_id
    base.extractor_model_id = resolver.profile.extractor_model_id
    base.dimensions = None
    rows = await session.execute(
        select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == base.id)
    )
    documents = [
        document
        for document in rows.scalars()
        # Nothing to re-embed from and nothing to re-extract with: left alone
        # rather than failed, so a lost file does not turn a reindex into a list
        # of errors nobody can act on.
        if document.text is not None or document.file_id is not None
    ]
    for document in documents:
        document.status = IndexStatus.PENDING
        document.error = ""
        document.indexed_at = None
    await session.commit()

    ingestor = request.app.state.ingestor
    for document in documents:
        ingestor.spawn(document.id, resolver.profile)
    return await _status(session, request)
