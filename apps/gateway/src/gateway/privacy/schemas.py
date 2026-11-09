"""Request and response models for the privacy pipelines API (ADR 0113).

Shared by ``/api/admin/privacy`` and ``/api/me/privacy``: a user's pipeline is
the same object as an administrator's, with its scope fixed and its actions
limited to ones that tighten, so one set of models serves both.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from gateway.config import RedactionPolicy
from gateway.response_model import ResponseModel

Kind = Literal["read", "embed", "chat"]
WhoMode = Literal["all", "users", "groups", "keys"]
TargetMode = Literal["all", "only", "except"]


class ConditionBody(BaseModel):
    """One condition: a key, values (any of them), and ``negate`` ("is not")."""

    model_config = ConfigDict(extra="forbid")

    #: ``filetype``, ``scan``, ``filename``, ``finds`` or ``classified``.
    key: str
    #: ``finds``: the id of the policy. ``classified``: the id of the classifier.
    values: list[str] = []
    negate: bool = False
    #: ``finds`` only: at least this many distinct items (1 to 1000).
    count: int = Field(default=1, ge=1, le=1000)


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
    #: A warning left by the migration; kept only while the rule is off.
    note: str | None = Field(default=None, max_length=500)

    def clean(self) -> dict[str, Any]:
        return {
            "id": str(self.id) if self.id else None,
            "name": self.name,
            "enabled": self.enabled,
            "observe_only": self.observe_only,
            "conditions": [c.model_dump() for c in self.conditions],
            "action": self.action.clean(),
            "note": self.note,
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
    count: int = 1


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
    #: Left by the migration on a rule it had to switch off: read before turning it on.
    note: str | None = None


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
    is_active: bool = True
    #: Whether users' own pipelines may name it (to apply it, or in "finds at least").
    shared_with_users: bool = False
    reason: str = Field(default="", max_length=500)


class PolicyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    policy: RedactionPolicy | None = None
    is_active: bool | None = None
    shared_with_users: bool | None = None
    reason: str | None = Field(default=None, max_length=500)


class PolicyResponse(ResponseModel):
    id: uuid.UUID
    name: str
    policy: RedactionPolicy
    is_active: bool
    shared_with_users: bool
    #: Whether the NER engine the policy chose can run now: ``None`` when the policy
    #: uses none, or the redaction service cannot be asked.
    engine_available: bool | None = None
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


class UserPolicyResponse(ResponseModel):
    id: uuid.UUID
    name: str
    entries: list[UserEntryView]
    used_by: list[str]
    created_at: datetime
    updated_at: datetime


# -- classifiers -----------------------------------------------------------------


class ClassifierResponse(ResponseModel):
    id: uuid.UUID
    name: str
    #: The topic in plain words.
    topic: str
    model_id: uuid.UUID | None = None
    model_name: str | None = None
    provider_name: str | None = None
    #: Whether it can answer now; otherwise ``why_not`` says what is missing.
    available: bool
    why_not: str
    #: Rules that name it ("classified as").
    rules_using: int
    created_at: datetime
    updated_at: datetime


class ClassifierCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    topic: str = Field(default="", max_length=1000)
    #: A chat model of a configured provider. Classifying sends the text to its provider.
    model_id: uuid.UUID | None = None


class ClassifierUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    topic: str | None = Field(default=None, max_length=1000)
    model_id: uuid.UUID | None = None


class ChatModelOption(ResponseModel):
    id: uuid.UUID
    name: str
    provider: str | None = None


class ClassifierListResponse(ResponseModel):
    items: list[ClassifierResponse]
    #: Chat models a classifier may use; empty means no model provider is configured yet.
    models: list[ChatModelOption]


# -- what the redaction service offers --------------------------------------------


class NerEngineView(ResponseModel):
    #: ``spacy`` or ``gliner``.
    name: str
    available: bool
    #: The service's default engine, used by a policy that chooses none.
    default: bool
    #: The entity types the engine exposes.
    entities: list[str]
    #: Whether the engine takes custom entity types (a label per type) at inference.
    custom_labels: bool
    #: Labels the engine always asks of its model, case-folded: a custom label may not equal one.
    labels: list[str] = Field(default_factory=list)
    ms_per_token: float | None = None
    languages: list[str]


class EnginesResponse(ResponseModel):
    #: The service answered. False: nothing below is known and an engine cannot be chosen.
    reachable: bool
    detail: str
    #: The built-in pattern formats the service's recognisers find.
    pattern_entities: list[str]
    engines: list[NerEngineView]


# -- the try-it box ---------------------------------------------------------------


class PolicyTryRequest(BaseModel):
    """A policy as it is being edited, and a text to run it over. Nothing is stored or logged."""

    model_config = ConfigDict(extra="forbid")

    policy: RedactionPolicy
    text: str = Field(min_length=1, max_length=50_000)


class FoundItem(ResponseModel):
    #: ``pattern`` or ``entity``.
    group: str
    type: str
    #: ``pattern``, ``spacy`` or ``gliner``.
    engine: str | None = None
    #: Entities only.
    score: float | None = None
    #: The mode the policy gives it.
    mode: str
    #: The text found: it came from the box, so it is shown to the one who typed it.
    text: str


class PolicyTryResponse(ResponseModel):
    items: list[FoundItem]
    #: The part the redaction service answers could not run, and why.
    unavailable: str | None = None
    #: What the redaction would do to the text (``detect_only`` items are left as they are).
    redacted_text: str | None = None
    blocked: bool = False


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
    #: Policies a pipeline may apply: an administrator's all of them, a user's the
    #: ones marked shared.
    policies: list[NamedId]
    #: The user's own policies (empty in the administrator's options).
    own_policies: list[NamedId]
    #: Classifiers "classified as" may name (administrators only).
    classifiers: list[NamedId]
    #: Entity types a user's own policy may name; null means any.
    entity_types: list[str] | None = None
    #: Whether the redaction service has an NER engine (None: not known). False means
    #: entity types only a model finds are not offered and a policy naming one is refused.
    ner_available: bool | None = None
    ner_note: str | None = None
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


class DryRunItem(ResponseModel):
    """One item a "finds" condition counted: which policy, which group, how sure."""

    policy: str
    #: ``pattern`` or ``entity``.
    group: str
    type: str
    #: ``pattern``, ``spacy`` or ``gliner``.
    engine: str | None = None
    #: Entities only.
    score: float | None = None
    #: The text found. Only in an administrator's test; a user's box leaves it out.
    text: str | None = None


class DryRunStep(ResponseModel):
    rule_id: uuid.UUID
    name: str
    position: int
    #: ``fired``, ``observed``, ``not_matched``, ``skipped_off``, ``not_reached`` or ``pending``.
    result: str
    reasons: list[str]
    degraded: list[str]
    #: Every item the rule's "finds" conditions counted.
    items: list[DryRunItem] = Field(default_factory=list)
    #: Left by the migration on a rule it switched off.
    note: str | None = None
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
    #: What each classifier said, by name.
    verdicts: dict[str, Any]
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
