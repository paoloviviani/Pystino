"""Wire shapes.

Listings return ``{"data": [...]}`` with the same envelope the gateway's
management API uses, so a reader moving between the two APIs is not learning
two conventions.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from chat_api.models import MessageStatus, Role


class MessageOut(BaseModel):
    id: uuid.UUID
    position: int
    role: Role
    content: str
    reasoning: str | None = None
    status: MessageStatus
    model: str | None = None
    request_id: str | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    model: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = Field(default_factory=list)


class ConversationCreate(BaseModel):
    model: str
    title: str | None = None


class ConversationUpdate(BaseModel):
    title: str | None = None
    model: str | None = None


class SendMessage(BaseModel):
    content: str
    #: Overrides the conversation's model for this turn and every turn after,
    #: because a picker that silently reverts is worse than no picker.
    model: str | None = None


class ModelOut(BaseModel):
    id: str
    owned_by: str | None = None


class Me(BaseModel):
    subject: str
    email: str | None
    display_name: str | None
    groups: list[str]
    is_admin: bool
    #: Where the console lives, so the SPA can offer the link without knowing
    #: the deployment's shape. Empty when nothing is configured.
    console_url: str = ""


class Problem(BaseModel):
    """One error shape, matching the gateway's."""

    error: dict[str, Any]

    @classmethod
    def of(
        cls, message: str, *, code: str | None = None, kind: str = "invalid_request_error"
    ) -> Problem:
        return cls(error={"message": message, "type": kind, "code": code})


EventName = Literal["delta", "done", "error", "message"]
