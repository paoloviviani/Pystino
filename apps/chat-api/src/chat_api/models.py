"""The chat database.

Its own schema in its own database, sharing only the PostgreSQL server with the
gateway. Users are *not* a table here: a person is identified by the
``(issuer, subject)`` pair their token carries, which is the same key the
gateway uses, so the two agree about who someone is without either owning the
other's rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from chat_api.types import TZDateTime, utcnow


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


#: PostgreSQL in production, SQLite in the unit suite. `JSONB` is worth having
#: for the columns queried by content; the variant keeps the tests runnable.
_JSON = JSON().with_variant(JSONB(), "postgresql")


def _enum(enum: type[StrEnum], name: str) -> SAEnum:
    """A CHECK-constrained VARCHAR storing the enum's *values*.

    `mapped_column(String(16))` looks equivalent and is not: it stores the value
    but reads back a bare `str`, so `status is MessageStatus.FAILED` is quietly
    False forever. That was found by the tests here — and the visible symptom
    was not a type error but a conversation whose history stopped being sent to
    the model, because the filter that selects completed messages matched
    nothing.

    `native_enum=False` keeps it a VARCHAR: a PostgreSQL ENUM type needs a
    migration to add a member, and these will gain members.
    """
    return SAEnum(
        enum,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda members: [member.value for member in members],
    )


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageStatus(StrEnum):
    """Why a message stopped.

    An assistant message exists *before* the model has finished, so the terminal
    states have to be distinguishable from "still going" — a stream that dies
    with the browser must not leave a row that reads as a complete answer.
    """

    STREAMING = "streaming"
    COMPLETE = "complete"
    #: The client went away. The text so far is kept: it was really generated,
    #: really billed, and throwing it out would make the ledger disagree with
    #: the transcript.
    INTERRUPTED = "interrupted"
    #: The gateway refused — quota, access, or blocked content. `error` says
    #: which, in the gateway's own words.
    FAILED = "failed"


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_owner", "issuer", "subject", "updated_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()

    # The owner, by the same key the gateway uses. Not a foreign key to a users
    # table here: this service does not own identity and must not invent a
    # second row for a person who already exists in the realm.
    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(255))

    title: Mapped[str] = mapped_column(String(255), default="New chat")
    #: The model this conversation is set to. A *name*, not an id: the gateway's
    #: catalogue is the gateway's, and a stored id here would be a foreign key
    #: across a service boundary that nothing can enforce.
    model: Mapped[str] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, default=utcnow, onupdate=utcnow
    )
    archived_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.position",
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # Position, not created_at, decides order. Two messages written in the
        # same millisecond are possible and a timestamp tie has no answer.
        UniqueConstraint("conversation_id", "position", name="uq_message_position"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)

    role: Mapped[Role] = mapped_column(_enum(Role, "message_role"))
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[MessageStatus] = mapped_column(
        _enum(MessageStatus, "message_status"), default=MessageStatus.COMPLETE
    )

    #: The model's own thinking, when it emits any, kept apart from the answer.
    #: A separate column rather than a marked-up region of `content`, because
    #: the two are different things to a reader and to anything that later
    #: re-sends this message as history — reasoning is **not** sent back to the
    #: model, and a scheme that had to parse it out of the prose would send it
    #: back the first time the parsing was wrong.
    reasoning: Mapped[str | None] = mapped_column(Text, default=None)

    #: The model that produced an assistant message, as the gateway reported it
    #: serving — which is not always the model that was asked for, and the
    #: difference is worth keeping.
    model: Mapped[str | None] = mapped_column(String(255), default=None)
    #: The gateway's request id. The one string that ties this row to a row in
    #: the ledger, and therefore to what it cost; without it a transcript and an
    #: invoice cannot be reconciled at all.
    request_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    usage: Mapped[dict[str, object] | None] = mapped_column(_JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Session(Base):
    """A signed-in browser, and the OIDC tokens it stands for.

    Tokens live here rather than in the cookie for two reasons. A refresh token
    in a cookie is a long-lived credential in the one place cross-site attacks
    reach, and a server-side row can be deleted — sign-out that actually ends
    the session, rather than asking the browser to forget.

    The refresh token is encrypted at rest. The access token is not stored at
    all: it expires in minutes and is fetched fresh from the refresh token when
    a request needs one, so a database dump yields nothing that works.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(255), index=True)

    email: Mapped[str | None] = mapped_column(String(320), default=None)
    display_name: Mapped[str | None] = mapped_column(String(255), default=None)
    groups: Mapped[list[str] | None] = mapped_column(_JSON, default=None)
    is_admin: Mapped[bool] = mapped_column(default=False)

    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
