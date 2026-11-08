"""Request and response models for ``/api/admin/privacy`` (ADR 0113)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from gateway.response_model import ResponseModel

Kind = Literal["read", "embed", "chat"]


class ConditionBody(BaseModel):
    """One condition: a key, values (any of them), and ``negate`` ("is not")."""

    model_config = ConfigDict(extra="forbid")

    key: str
    values: list[str]
    negate: bool = False
    #: Content conditions only: ``patterns`` (the quick check) or a detector profile id.
    decide: str | None = None


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Reading: ``reader`` or ``refuse``. Embedding: ``local`` or ``refuse``.
    type: str
    reader: Literal["markitdown", "ocr", "docling"] | None = None
    #: ``local`` only: a model name to prefer; the first local embedding model otherwise.
    model: str | None = None

    def clean(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class ConditionView(ResponseModel):
    key: str
    values: list[str]
    negate: bool = False
    decide: str | None = None


class ActionView(ResponseModel):
    type: str
    reader: str | None = None
    model: str | None = None


class IssueView(ResponseModel):
    """A save-time warning. Never blocks saving."""

    code: str
    message: str
    rule_id: uuid.UUID | None = None
    rule_name: str | None = None


class RuleResponse(ResponseModel):
    id: uuid.UUID
    kind: Kind
    name: str
    enabled: bool
    observe_only: bool
    position: int
    conditions: list[ConditionView]
    action: ActionView
    issues: list[IssueView]
    created_at: datetime
    updated_at: datetime


class RuleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    observe_only: bool = False
    conditions: list[ConditionBody] = []
    action: ActionBody
    #: Where in the kind's list to put it (0 first). Omitted: last.
    position: int | None = Field(default=None, ge=0)


class RuleUpdateRequest(BaseModel):
    """Every field optional. The kind is fixed once saved."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    observe_only: bool | None = None
    conditions: list[ConditionBody] | None = None
    action: ActionBody | None = None


class RuleCheckRequest(BaseModel):
    """A draft, to be checked as it would sit in the list if saved."""

    model_config = ConfigDict(extra="forbid")

    kind: Kind
    name: str = "Draft"
    enabled: bool = True
    observe_only: bool = False
    conditions: list[ConditionBody] = []
    action: ActionBody
    #: The rule being edited, if any: it is replaced by the draft rather than added.
    rule_id: uuid.UUID | None = None


class RuleCheckResponse(ResponseModel):
    #: ``None`` when the draft cannot be saved; the reason is in ``error``.
    error: str | None = None
    issues: list[IssueView]
    soft_cap: int


class ReorderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    #: Every rule id of the kind, in the new order.
    order: list[uuid.UUID]


class ProfileResponse(ResponseModel):
    id: uuid.UUID
    name: str
    description: str
    engine: str
    labels: list[str]
    fallback: Literal["refuse", "fallback_to"]
    fallback_profile_id: uuid.UUID | None = None
    fallback_profile_name: str | None = None
    is_default: bool
    #: ``available``, ``down`` or ``not_installed``, read from health.
    status: Literal["available", "down", "not_installed"]
    status_detail: str
    #: Whether it can answer a content condition in this release.
    decides: bool
    #: Estimated cost of a verdict, in words ("instant").
    cost_hint: str
    rules_using: int
    created_at: datetime
    updated_at: datetime


class ProfileCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    engine: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_-]+$")
    labels: list[str] = []
    fallback: Literal["refuse", "fallback_to"] = "refuse"
    fallback_profile_id: uuid.UUID | None = None


class ProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    labels: list[str] | None = None
    fallback: Literal["refuse", "fallback_to"] | None = None
    fallback_profile_id: uuid.UUID | None = None


class RuleTestRequest(BaseModel):
    """Facts to judge, or a file or text to derive them from. Nothing is stored or logged."""

    model_config = ConfigDict(extra="forbid")

    kind: Kind
    #: The caller to pretend to be: an email and/or group names.
    user: str | None = None
    groups: list[str] = []
    #: Reading: the file's name, MIME type, and whether it is a scan (omit to
    #: let a file decide, or to leave it unknown).
    filename: str | None = None
    mime: str | None = None
    is_scan: bool | None = None
    #: Reading: the file itself, base64, read on the local extractor to learn
    #: its type and whether it is a scan. Never kept.
    file_base64: str | None = Field(default=None, max_length=34_000_000)
    #: Embedding: the target model or provider, and whether it is local.
    target_model: str | None = None
    target_provider: str | None = None
    target_local: bool | None = None
    #: Embedding: text the content conditions judge. Never logged.
    text: str | None = Field(default=None, max_length=200_000)


class FiredRule(ResponseModel):
    id: uuid.UUID
    name: str
    action: ActionView


class RuleTestStep(ResponseModel):
    rule_id: uuid.UUID
    name: str
    position: int
    #: ``fired``, ``observed``, ``not_matched``, ``skipped_off``, ``not_reached`` or ``pending``.
    result: str
    reasons: list[str]
    degraded: list[str]


class RuleTestResponse(ResponseModel):
    kind: Kind
    #: ``default``, ``refuse``, ``reader``, ``local`` or ``pending``.
    outcome: str
    outcome_text: str
    fired: FiredRule | None = None
    observed: list[FiredRule]
    steps: list[RuleTestStep]
    #: What was judged: labels and names, never the text.
    facts: dict[str, Any]
    verdicts: dict[str, Any]
    fallbacks: list[str]
    degraded: list[str]
    #: ``scan`` when a rule is waiting for the file to be read.
    needs: str | None = None
    #: What the extractor reported, when a file was read.
    extractor: str | None = None
    fallback_reason: str | None = None
