"""``/v1/files`` — the only content this gateway stores.

OpenAI's shape, deliberately, down to ``{id, object, bytes, created_at,
filename, purpose}`` and the ``{"deleted": true}`` delete response. Not for
compatibility as an end in itself, but because a knowledge base needs an upload
endpoint and the alternative was inventing one under ``/api`` — which no
`/v1` client can reach at all, since every management route is behind a session
cookie (`deps.get_management_user`). That door problem is the same one
`/v1/billing/groups` exists to solve (ADR 0061); here the answer is better,
because OpenAI already specifies the endpoint we would have had to invent.

**A file belongs to one person and is never shared.** Sharing happens one level
up, on the knowledge base, so there is exactly one thing to reason about when
answering "who can read this document". Deleting the file does not delete what
was indexed from it — see `KnowledgeDocument.file_id`, which is ``SET NULL`` —
because deleting an upload should not silently empty a base somebody else is
retrieving from.

**The size limit is enforced while reading, not after.** `ocr.py` records that
the gateway imposes no request-body limit and neither does Caddy, so a caller
can offer a gigabyte and the only ceiling is the extractor's 25 MiB refusal —
applied *after* the whole payload has been buffered in this process. An upload
endpoint cannot inherit that: this one reads in chunks and abandons the request
the moment the count passes the configured maximum, so memory is bounded by the
limit rather than by the caller's ambition.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import delete, select

from gateway.deps import Principal, PrincipalDep, SessionDep
from gateway.errors import BadRequestError, NotFoundError
from gateway.knowledge.resolver import KnowledgeResolver
from gateway.models import FileBlob, StoredFile

router = APIRouter(prefix="/v1", tags=["pystino"])

#: How much is read from the socket at a time. Large enough that a 20 MiB
#: upload is a few hundred reads, small enough that the overshoot past the
#: limit before it is noticed is negligible.
_READ_CHUNK = 256 * 1024


def require_knowledge(request: Request) -> KnowledgeResolver:
    """The feature switch, as a dependency.

    A deployment that has not enabled knowledge bases answers **404**, not 403
    or 501: the endpoint genuinely does not exist there, and a 403 would tell a
    caller that the feature is present and they are not allowed it, which is a
    different and untrue statement.
    """
    resolver: KnowledgeResolver | None = getattr(request.app.state, "knowledge", None)
    if resolver is None or not resolver.profile.enabled:
        raise NotFoundError("This deployment does not have knowledge bases enabled.")
    return resolver


KnowledgeDep = Annotated[KnowledgeResolver, Depends(require_knowledge)]


class FileObject(BaseModel):
    """One stored file, in OpenAI's file shape."""

    id: uuid.UUID
    object: Literal["file"] = "file"
    bytes: int
    created_at: int
    filename: str
    purpose: str
    #: Ours, not OpenAI's. An operator reconciling "did this upload work" wants
    #: the digest, and a client re-uploading wants to know it already has.
    sha256: str

    @classmethod
    def of(cls, stored: StoredFile) -> FileObject:
        return cls(
            id=stored.id,
            bytes=stored.size_bytes,
            # Seconds since the epoch, as every OpenAI object reports time.
            created_at=int(stored.created_at.timestamp()),
            filename=stored.filename,
            purpose="knowledge",
            sha256=stored.sha256,
        )


class FileList(BaseModel):
    object: Literal["list"] = "list"
    data: list[FileObject]


class FileDeleted(BaseModel):
    id: uuid.UUID
    object: Literal["file"] = "file"
    deleted: bool = True


async def _owned(session: SessionDep, principal: Principal, file_id: uuid.UUID) -> StoredFile:
    """One of this caller's files, or a 404.

    "Not yours" and "does not exist" answer the same 404 on purpose, exactly as
    `_metered.resolve_model` does for models: whose files exist is not this
    caller's business, and a 403 would confirm the id is real.
    """
    result = await session.execute(
        select(StoredFile).where(
            StoredFile.id == file_id,
            StoredFile.owner_user_id == principal.user.id,
        )
    )
    stored = result.scalars().first()
    if stored is None:
        raise NotFoundError(f"No such file: {file_id}")
    return stored


@router.post("/files", response_model=FileObject)
async def upload_file(
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
    _knowledge: KnowledgeDep,
    file: Annotated[UploadFile, File()],
    purpose: Annotated[str, Form()] = "knowledge",
) -> FileObject:
    """Store one file against this caller.

    ``purpose`` is accepted and recorded as ``knowledge`` whatever is sent.
    OpenAI's vocabulary (``assistants``, ``fine-tune``, ``batch``) describes
    products this gateway does not have, and rejecting the values a client
    sends by habit would break it for no benefit — while pretending to honour
    them would imply behaviour that does not exist.
    """
    settings = request.app.state.settings
    limit = settings.knowledge.max_upload_bytes

    digest = hashlib.sha256()
    parts: list[bytes] = []
    size = 0
    while chunk := await file.read(_READ_CHUNK):
        size += len(chunk)
        if size > limit:
            # Refused mid-read, so the process never holds more than the limit
            # plus one chunk. Reading to the end to measure it would be the bug
            # this endpoint exists to avoid.
            raise BadRequestError(
                f"This file is larger than the {limit} byte upload limit.",
                code="file_too_large",
            )
        digest.update(chunk)
        parts.append(chunk)

    if size == 0:
        raise BadRequestError("The uploaded file is empty.", code="empty_file")

    stored = StoredFile(
        filename=file.filename or "upload",
        # What the caller declared. The extractor sniffs the bytes itself and
        # wins; keeping the claim makes the two comparable when an extraction
        # fails for a reason nobody expected.
        media_type=file.content_type or "application/octet-stream",
        size_bytes=size,
        sha256=digest.hexdigest(),
        owner_user_id=principal.user.id,
        billing_group_id=principal.billing_group.id,
    )
    session.add(stored)
    await session.flush()
    session.add(FileBlob(file_id=stored.id, data=b"".join(parts)))
    await session.commit()
    return FileObject.of(stored)


@router.get("/files", response_model=FileList)
async def list_files(
    session: SessionDep, principal: PrincipalDep, _knowledge: KnowledgeDep
) -> FileList:
    result = await session.execute(
        select(StoredFile)
        .where(StoredFile.owner_user_id == principal.user.id)
        .order_by(StoredFile.created_at.desc())
    )
    return FileList(data=[FileObject.of(stored) for stored in result.scalars()])


@router.get("/files/{file_id}", response_model=FileObject)
async def retrieve_file(
    file_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
    _knowledge: KnowledgeDep,
) -> FileObject:
    return FileObject.of(await _owned(session, principal, file_id))


@router.get("/files/{file_id}/content")
async def download_file(
    file_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
    _knowledge: KnowledgeDep,
) -> Response:
    """The bytes back, as they were uploaded.

    ``Content-Disposition: attachment`` and the stored media type. Attachment
    rather than inline because a stored file is arbitrary caller-supplied
    content served from this origin, and the console shares that origin — an
    HTML file rendered inline here would run with the console's session cookie
    in scope.
    """
    stored = await _owned(session, principal, file_id)
    blob = await session.get(FileBlob, stored.id)
    if blob is None:
        raise NotFoundError(f"The content of file {file_id} is no longer stored.")
    return Response(
        content=blob.data,
        media_type=stored.media_type,
        headers={
            "content-disposition": f'attachment; filename="{stored.filename}"',
            # Belt and braces beside the attachment disposition: this content
            # is not ours and must never be interpreted as though it were.
            "x-content-type-options": "nosniff",
        },
    )


@router.delete("/files/{file_id}", response_model=FileDeleted)
async def delete_file(
    file_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
    _knowledge: KnowledgeDep,
) -> FileDeleted:
    """Forget the bytes. What was indexed from them stays.

    ``knowledge_documents.file_id`` is ``SET NULL``, so a base keeps answering
    from the text it already extracted — only re-extraction becomes impossible.
    Cascading into the index instead would mean tidying up an upload silently
    empties a knowledge base other people are retrieving from.
    """
    stored = await _owned(session, principal, file_id)
    await session.execute(delete(StoredFile).where(StoredFile.id == stored.id))
    await session.commit()
    return FileDeleted(id=file_id)
