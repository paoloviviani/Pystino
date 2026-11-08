"""Request and response models for the privacy pipelines API (ADR 0113).

Shared by ``/api/admin/privacy`` and ``/api/me/privacy``: a user's pipeline is
the same object as an administrator's, with its scope fixed and its actions
limited to ones that tighten, so one set of models serves both.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gateway.config import RedactionPolicy
from gateway.response_model import ResponseModel

Kind = Literal["read", "embed", "chat"]
WhoMode = Literal["all", "users", "groups", "keys"]
TargetMode = Literal["all", "only", "except"]


class ConditionBody(BaseModel):
    """One condition: a key, values (any of them), and ``negate`` ("is not")."""

    model_config = ConfigDict(extra="forbid")

    key: str
    values: list[str] = []
    negate: bool = False
    #: Content conditions only: ``patterns`` (the quick check) or a detector profile id.
    decide: str | None = None


class ActionBody(BaseModel):
    """Where it is handled, and what is hidden.

    ``level`` is on the ladder of the pipeline's kind (read: ``default``,
    ``read_with``, ``refuse``; embed and chat: ``pass``, ``refuse``).
    """

    model_config = ConfigDict(extra="forbid")

    level: str
    #: ``read_with``: ``builtin`` (the built-in extractor) or the name of a configured OCR model.
    reader: str | None = None
    #: ``read_with`` the built-in extractor only: ``markitdown``, ``ocr`` or ``docling``.
    engine: str | None = None
    #: The policy to apply to what is returned (read), the input (embed) or the
    #: messages and tool results (chat). Never with ``refuse``.
    redact_with: uuid.UUID | None = None

    def clean(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, mode="json")


class RuleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Kept across saves so the log's rule id stays stable; made if absent.
    id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    observe_only: bool = False
    conditions: list[ConditionBody] = []
    action: ActionBody

    def clean(self) -> dict[str, Any]:
        return {
            "id": str(self.id) if self.id else None,
            "name": self.name,
            "enabled": self.enabled,
            "observe_only": self.observe_only,
            "conditions": [c.model_dump() for c in self.conditions],
            "action": self.action.clean(),
        }


class WhoBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: WhoMode = "all"
    ids: list[uuid.UUID] = []


class TargetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: TargetMode = "all"
    providers: list[uuid.UUID] = []
    models: list[uuid.UUID] = []


class PipelineCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    #: Ignored for a user's pipeline: its who is always the user.
    who: WhoBody = WhoBody()
    target: TargetBody = TargetBody()
    rules: list[RuleBody] = []
    otherwise: ActionBody | None = None
    #: Where in the kind's list (0 first). Omitted: last.
    position: int | None = Field(default=None, ge=0)


class PipelineUpdateRequest(BaseModel):
    """Every field optional. The kind is fixed once saved."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    who: WhoBody | None = None
    target: TargetBody | None = None
    rules: list[RuleBody] | None = None
    otherwise: ActionBody | None = None


class PipelineCheckRequest(PipelineCreateRequest):
    """A draft, to be checked as it would be saved."""

    name: str = "Draft"


class IssueView(ResponseModel):
    """A save-time warning. Never blocks saving."""

    code: str
    message: str
    rule_id: uuid.UUID | None = None
    rule_name: str | None = None


class ConditionView(ResponseModel):
    key: str
    values: list[str]
    negate: bool = False
    decide: str | None = None


class ActionView(ResponseModel):
    level: str
    reader: str | None = None
    engine: str | None = None
    redact_with: uuid.UUID | None = None


class RuleView(ResponseModel):
    id: uuid.UUID
    name: str
    enabled: bool
    observe_only: bool
    conditions: list[ConditionView]
    action: ActionView
    issues: list[IssueView]


class WhoView(ResponseModel):
    mode: WhoMode
    ids: list[uuid.UUID]


class TargetView(ResponseModel):
    mode: TargetMode
    providers: list[uuid.UUID]
    models: list[uuid.UUID]


class PipelineResponse(ResponseModel):
    id: uuid.UUID
    #: Null for an administrator's pipeline.
    owner_id: uuid.UUID | None = None
    kind: Kind
    name: str
    enabled: bool
    position: int
    who: WhoView
    target: TargetView
    rules: list[RuleView]
    otherwise: RuleView
    #: id -> name for every user, group, key, provider, model and policy named.
    labels: dict[str, str]
    #: What it does, in short words ("refuses health data").
    summary: list[str]
    created_at: datetime
    updated_at: datetime


class PipelineCheckResponse(ResponseModel):
    #: ``None`` when the draft cannot be saved; the reason is in ``error``.
    error: str | None = None
    #: Issues by rule id, and for the last rule under its own id (``otherwise``).
    issues: list[IssueView]
    soft_cap: int


class ReorderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    #: Every administrator pipeline id of the kind, in the new order.
    order: list[uuid.UUID]


# -- policies --------------------------------------------------------------------


class PolicyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    policy: RedactionPolicy
    #: The detector profile that finds the types; null is the deployment default.
    profile_id: uuid.UUID | None = None
    is_active: bool = True
    reason: str = Field(default="", max_length=500)


class PolicyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    policy: RedactionPolicy | None = None
    profile_id: uuid.UUID | None = None
    is_active: bool | None = None
    reason: str | None = Field(default=None, max_length=500)


class PolicyResponse(ResponseModel):
    id: uuid.UUID
    name: str
    policy: RedactionPolicy
    profile_id: uuid.UUID | None = None
    profile_name: str | None = None
    is_active: bool
    reason: str
    #: Names of the pipelines that apply it.
    used_by: list[str]
    created_by: uuid.UUID | None = None
    created_by_email: str | None = None
    created_at: datetime
    updated_at: datetime


class UserEntryBody(BaseModel):
    """One entry of a user's own policy: a custom pattern, or a stricter mode for a type."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["pattern", "mode"]
    name: str = Field(min_length=1, max_length=64)
    regex: str | None = Field(default=None, max_length=512)
    mode: str


class UserEntryView(ResponseModel):
    kind: Literal["pattern", "mode"]
    name: str
    regex: str | None = None
    mode: str


class UserPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    entries: list[UserEntryBody] = []
    profile_id: uuid.UUID | None = None


class UserPolicyResponse(ResponseModel):
    id: uuid.UUID
    name: str
    entries: list[UserEntryView]
    profile_id: uuid.UUID | None = None
    used_by: list[str]
    created_at: datetime
    updated_at: datetime


# -- profiles --------------------------------------------------------------------


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
    #: Whether users may pick it for their own policies.
    user_available: bool
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


#: Engines a profile may name. A profile of an engine that is not installed is
#: allowed (it shows "not installed" and follows its fallback); a name nobody
#: could ever install is a typo.
KNOWN_ENGINES = ("presidio", "patterns", "gliner")


class ProfileCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    engine: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_-]+$")
    labels: list[str] = []
    fallback: Literal["refuse", "fallback_to"] = "refuse"
    fallback_profile_id: uuid.UUID | None = None
    user_available: bool = False

    @field_validator("engine")
    @classmethod
    def _known_engine(cls, value: str) -> str:
        if value not in KNOWN_ENGINES:
            raise ValueError(f"Unknown engine '{value}'. Use one of: {', '.join(KNOWN_ENGINES)}.")
        return value


class ProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    labels: list[str] | None = None
    fallback: Literal["refuse", "fallback_to"] | None = None
    fallback_profile_id: uuid.UUID | None = None
    user_available: bool | None = None


class SettingsResponse(ResponseModel):
    #: Entity types users' own policies may name; null means any the detector offers.
    user_entity_types: list[str] | None = None


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_entity_types: list[str] | None = None


# -- options (what the editors may pick) ------------------------------------------


class NamedId(ResponseModel):
    id: uuid.UUID
    name: str


class ModelOption(ResponseModel):
    id: uuid.UUID
    name: str
    provider_id: uuid.UUID | None = None
    provider: str | None = None
    #: ``chat``, ``embedding``, ``rerank``, ``ocr`` and so on.
    kind: str


class ReaderOption(ResponseModel):
    #: ``builtin`` or the model name.
    id: str
    name: str
    builtin: bool


class OptionsResponse(ResponseModel):
    users: list[NamedId]
    groups: list[NamedId]
    keys: list[NamedId]
    providers: list[NamedId]
    models: list[ModelOption]
    readers: list[ReaderOption]
    #: Administrator policies a pipeline may apply.
    policies: list[NamedId]
    #: The user's own policies (empty in the administrator's options).
    own_policies: list[NamedId]
    profiles: list[NamedId]
    #: Entity types a user's own policy may name; null means any.
    entity_types: list[str] | None = None
    engines: list[str]


# -- the test box ------------------------------------------------------------------


class DryRunRequest(BaseModel):
    """Facts to judge, or a file or text to derive them from. Nothing is stored or logged."""

    model_config = ConfigDict(extra="forbid")

    kind: Kind
    #: The caller to pretend to be (an administrator's test). A user's test is always them.
    user_id: uuid.UUID | None = None
    #: The target the request goes to: a model id (any kind), or a provider id alone.
    model_id: uuid.UUID | None = None
    provider_id: uuid.UUID | None = None
    #: Reading: the file's name, MIME type, and whether it is a scan (omit to
    #: let a file decide, or to leave it unknown).
    filename: str | None = None
    mime: str | None = None
    is_scan: bool | None = None
    #: Reading: the file itself, base64, read on the built-in extractor to learn
    #: its type and whether it is a scan. Never kept.
    file_base64: str | None = Field(default=None, max_length=34_000_000)
    #: Embedding and chat: text the content conditions judge. Never logged.
    text: str | None = Field(default=None, max_length=200_000)


class DryRunStep(ResponseModel):
    rule_id: uuid.UUID
    name: str
    position: int
    #: ``fired``, ``observed``, ``not_matched``, ``skipped_off``, ``not_reached`` or ``pending``.
    result: str
    reasons: list[str]
    degraded: list[str]
    is_otherwise: bool = False


class DryRunPipeline(ResponseModel):
    id: uuid.UUID
    name: str
    #: ``admin`` or ``user``.
    owner: str
    hop: int
    #: Short words for the scope ("all users, OpenAI embeddings").
    scope: list[str]
    steps: list[DryRunStep]
    #: The rule that set the pipeline's action; null when pending.
    fired_rule_id: uuid.UUID | None = None
    fired_rule: str | None = None
    fired_otherwise: bool = False
    level: str
    action_text: str
    summary: list[str]


class DryRunSkipped(ResponseModel):
    id: uuid.UUID
    name: str
    owner: str
    why: str


class DryRunHop(ResponseModel):
    reader: str
    reader_name: str
    #: Names of the pipelines that ran for the new reader.
    added: list[str]


class DryRunLevel(ResponseModel):
    level: str
    label: str
    #: Who asked for it, by pipeline name (above the base level only).
    asked_by: list[str]
    winning: bool


class DryRunPolicyType(ResponseModel):
    entity_type: str
    #: The strictest mode, and the policy it came from.
    mode: str
    source: str
    #: Every policy that names it, with its own mode.
    per_policy: list[str]
    #: Whether the text contains it (embed and chat, when a text was given and a
    #: detector ran); null when not checked.
    found: bool | None = None


class DryRunResponse(ResponseModel):
    kind: Kind
    #: ``pass``, ``refuse``, ``read_default``, ``read_with``, ``redact`` or ``pending``.
    outcome: str
    outcome_headline: str
    outcome_text: str
    level: str
    ladder: list[DryRunLevel]
    pipelines: list[DryRunPipeline]
    skipped: list[DryRunSkipped]
    hops: list[DryRunHop]
    #: The rule (and pipeline) that set the winning level; null at the base level.
    winner_rule: str | None = None
    winner_pipeline: str | None = None
    winner_owner: str | None = None
    #: Reading: the reader chosen, who chose it, and the engine and who chose it.
    reader: str | None = None
    reader_by: str | None = None
    reader_overridden: list[str]
    engine: str | None = None
    engine_by: str | None = None
    impossible: str | None = None
    policies: list[str]
    policy_types: list[DryRunPolicyType]
    #: A type set to block was found in the text.
    blocked_by: str | None = None
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
    estimate_ms: int | None = None


# -- the up-front summary for clients ------------------------------------------------


class ModelSummary(ResponseModel):
    """What the pipelines in scope for one model do, in short words."""

    model: str
    kind: str
    #: ``refuses health data``, ``redacts with Default``; empty when nothing applies.
    summary: list[str]
    pipelines: list[str]


class PrivacySummaryResponse(ResponseModel):
    embedding: list[ModelSummary]
    chat: list[ModelSummary]
