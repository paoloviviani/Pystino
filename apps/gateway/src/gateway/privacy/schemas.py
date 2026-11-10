"""Request and response models for the privacy pipelines API (ADR 0113, ADR 0114).

Shared by ``/api/admin/privacy`` and ``/api/me/privacy``: a user's pipeline is
the same object as an administrator's, with its scope fixed and its actions
limited to ones that tighten, so one set of models serves both.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from gateway.config import PolicyDefinition
from gateway.errors import BadRequestError
from gateway.response_model import ResponseModel

Kind = Literal["extract", "content"]
ModelType = Literal["chat", "embedding", "rerank"]
WhoMode = Literal["all", "users", "groups", "keys"]
TargetMode = Literal["all", "only", "except"]


class ConditionBody(BaseModel):
    """One condition: a key, values (any of them), and ``negate`` ("is not")."""

    model_config = ConfigDict(extra="forbid")

    #: ``filetype``, ``scan``, ``filename``, ``finds`` or ``classified``.
    key: str
    #: ``classified``: the id of the classifier. ``finds`` names none: it counts what the
    #: rule's own policies find.
    values: list[str] = []
    negate: bool = False
    #: ``finds`` only: at least this many distinct items (1 to 1000).
    count: int = Field(default=1, ge=1, le=1000)


class ActionBody(BaseModel):
    """A reading rule's action: ``default`` (the reader the caller named), ``extract_with`` a
    reader, or ``block``. A content rule has none: its mode says what it does."""

    model_config = ConfigDict(extra="forbid")

    level: str
    #: ``extract_with``: the name of an OCR model (the built-in extractor's engines are
    #: models: ``markitdown``, ``markitdown-ocr``, ``docling``). Default: ``markitdown``.
    reader: str | None = None

    def clean(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, mode="json")


class RuleBody(BaseModel):
    """When <policies> + <conditions> -> <mode> (ADR 0114)."""

    model_config = ConfigDict(extra="forbid")

    #: Kept across saves so the log's rule id stays stable; made if absent.
    id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    #: What the rule looks for, and hides. A content rule needs at least one.
    policies: list[uuid.UUID] = []
    #: ``all`` (default) or ``any`` of the conditions must hold.
    match: Literal["all", "any"] = "all"
    conditions: list[ConditionBody] = []
    #: ``off``, ``detect_only``, ``anonymise_restore``, ``anonymise``, ``redact`` or
    #: ``block``. Required for a content rule; a reading rule's default is ``off``.
    mode: str | None = None
    #: Per entity type modes, over the rule's policies: ``{TYPE: mode}``.
    overrides: dict[str, str] = {}
    #: Reading rules only.
    action: ActionBody | None = None
    #: A warning left by the migration; kept only while the rule is off.
    note: str | None = Field(default=None, max_length=500)

    def clean(self) -> dict[str, Any]:
        return {
            "id": str(self.id) if self.id else None,
            "name": self.name,
            "enabled": self.enabled,
            "policies": [str(p) for p in self.policies],
            "match": self.match,
            "conditions": [c.model_dump() for c in self.conditions],
            "mode": self.mode,
            "overrides": dict(self.overrides),
            "action": self.action.clean() if self.action else None,
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
    #: Content pipelines: the model types covered; empty is any.
    model_types: list[ModelType] = []


class PipelineCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    #: Ignored for a user's pipeline: its who is always the user.
    who: WhoBody = WhoBody()
    target: TargetBody = TargetBody()
    rules: list[RuleBody] = []
    #: Where in the kind's list (0 first). Omitted: last.
    position: int | None = Field(default=None, ge=0)


class PipelineUpdateRequest(BaseModel):
    """Every field optional. The kind is fixed once saved."""

    model_config = ConfigDict(extra="forbid")

    #: Accepted only to be tolerant: it must equal the stored kind, and is ignored.
    kind: Kind | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    who: WhoBody | None = None
    target: TargetBody | None = None
    rules: list[RuleBody] | None = None

    def require_same_kind(self, stored: str) -> None:
        """Refuse an attempt to change the kind; the same kind is a no-op."""
        if self.kind is not None and getattr(self.kind, "value", self.kind) != stored:
            raise BadRequestError(
                "A pipeline's kind cannot be changed; create a new pipeline.",
                code="pipeline_kind_immutable",
            )


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


class RuleView(ResponseModel):
    id: uuid.UUID
    name: str
    enabled: bool
    policies: list[uuid.UUID]
    match: Literal["all", "any"]
    conditions: list[ConditionView]
    mode: str
    overrides: dict[str, str]
    #: Reading rules only.
    action: ActionView | None = None
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
    model_types: list[ModelType]


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
    #: id -> name for every user, group, key, provider, model and policy named.
    labels: dict[str, str]
    #: What it does, in short words ("refuses health data").
    summary: list[str]
    created_at: datetime
    updated_at: datetime


class PipelineCheckResponse(ResponseModel):
    #: ``None`` when the draft cannot be saved; the reason is in ``error``.
    error: str | None = None
    #: Issues, each naming its rule.
    issues: list[IssueView]
    soft_cap: int


class ReorderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    #: Every administrator pipeline id of the kind, in the new order (the order the rules apply).
    order: list[uuid.UUID]


# -- policies --------------------------------------------------------------------


class PolicyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    policy: PolicyDefinition
    is_active: bool = True
    #: Whether users' own pipelines may name it (to apply it, or in "finds at least").
    shared_with_users: bool = False
    reason: str = Field(default="", max_length=500)


class PolicyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    policy: PolicyDefinition | None = None
    is_active: bool | None = None
    shared_with_users: bool | None = None
    reason: str | None = Field(default=None, max_length=500)


class PolicyResponse(ResponseModel):
    id: uuid.UUID
    name: str
    policy: PolicyDefinition
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
    """One entry of a user's own policy: a custom pattern, or an entity type to look for.

    ``kind`` stays ``mode`` for an entity type, as stored (ADR 0114: it carries no mode).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["pattern", "mode"]
    name: str = Field(min_length=1, max_length=64)
    regex: str | None = Field(default=None, max_length=512)


class UserEntryView(ResponseModel):
    kind: Literal["pattern", "mode"]
    name: str
    regex: str | None = None


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
    #: The administrator's switch (Detectors tab). An engine that is not enabled cannot be
    #: chosen by a policy, whatever the service reports.
    enabled: bool = True
    #: Names of the active policies that read with this engine.
    used_by: list[str] = Field(default_factory=list)


class EnginesResponse(ResponseModel):
    #: The service answered. False: nothing below is known and an engine cannot be chosen.
    reachable: bool
    detail: str
    #: The built-in pattern formats the service's recognisers find.
    pattern_entities: list[str]
    engines: list[NerEngineView]
    #: The administrator's switch for the pattern recognisers, and the active policies using them.
    patterns_enabled: bool = True
    patterns_used_by: list[str] = Field(default_factory=list)


# -- the try-it box ---------------------------------------------------------------


class PolicyTryRequest(BaseModel):
    """A policy as it is being edited, and a text to run it over. Nothing is stored or logged."""

    model_config = ConfigDict(extra="forbid")

    policy: PolicyDefinition
    text: str = Field(min_length=1, max_length=50_000)


class FoundItem(ResponseModel):
    #: ``pattern`` or ``entity``.
    group: str
    type: str
    #: ``pattern``, ``spacy`` or ``gliner``.
    engine: str | None = None
    #: Entities only.
    score: float | None = None
    #: The text found: it came from the box, so it is shown to the one who typed it.
    text: str


class PolicyTryResponse(ResponseModel):
    items: list[FoundItem]
    #: The part the redaction service answers could not run, and why.
    unavailable: str | None = None
    #: The text with what was found removed (a preview: the mode is a rule's, so it is shown
    #: as ``redact``).
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
    #: The OCR model's name.
    id: str
    name: str
    #: Served by the deployment's own extractor (nothing leaves the stack).
    local: bool


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
    #: Every type the redaction service finds now (pattern formats if switched on, and the
    #: entities of NER engines that are on); null when the service cannot be asked.
    offered_types: list[str] | None = None


# -- the test box ------------------------------------------------------------------


class DryRunRequest(BaseModel):
    """Facts to judge, or a file or text to derive them from. Nothing is stored or logged."""

    model_config = ConfigDict(extra="forbid")

    kind: Kind
    #: The caller to pretend to be (an administrator's test). A user's test is always them.
    user_id: uuid.UUID | None = None
    #: The target the request goes to: a model id (its type is a fact), or a provider id alone.
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
    #: Content: text the rules judge. Never logged.
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
    #: ``matched``, ``not_matched``, ``skipped_off`` or ``pending``.
    result: str
    reasons: list[str]
    degraded: list[str]
    #: What the rule's policies found (what a ``finds`` condition counted).
    items: list[DryRunItem] = Field(default_factory=list)
    #: The rule in words: its policies, its mode, its overrides.
    policies: list[str] = Field(default_factory=list)
    mode: str = "off"
    overrides: dict[str, str] = Field(default_factory=dict)
    #: Left by the migration on a rule it switched off.
    note: str | None = None


class DryRunPipeline(ResponseModel):
    id: uuid.UUID
    name: str
    #: ``admin`` or ``user``.
    owner: str
    hop: int
    #: Short words for the scope ("all users, OpenAI embeddings").
    scope: list[str]
    #: Every rule, matched or not.
    steps: list[DryRunStep]
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


class DryRunClaim(ResponseModel):
    """A piece of text a rule hid (or only recorded), and which rule claimed it."""

    pipeline: str
    rule: str
    rule_id: uuid.UUID | None = None
    #: ``admin`` or ``user``.
    owner: str
    type: str
    #: ``pattern`` or ``entity``.
    group: str
    mode: str
    #: What the claim did: ``hidden`` or ``recorded`` (a ``detect_only`` rule: never hidden).
    action: str
    engine: str | None = None
    score: float | None = None
    #: The text. Only in an administrator's test.
    text: str | None = None


class DryRunResponse(ResponseModel):
    kind: Kind
    #: ``pass``, ``block``, ``read_default``, ``extract_with``, ``hide`` or ``pending``.
    outcome: str
    outcome_headline: str
    outcome_text: str
    #: ``pass``, ``block``, ``default`` or ``extract_with``.
    level: str
    pipelines: list[DryRunPipeline]
    skipped: list[DryRunSkipped]
    hops: list[DryRunHop]
    #: The rule that blocked the request, or chose the reader.
    winner_rule: str | None = None
    winner_pipeline: str | None = None
    winner_owner: str | None = None
    #: Reading: the reader chosen, and who chose it.
    reader: str | None = None
    reader_by: str | None = None
    reader_overridden: list[str]
    impossible: str | None = None
    #: Names of the policies the matching rules apply.
    policies: list[str]
    #: Which rule claimed which span of the text (content, when a text was given).
    claims: list[DryRunClaim] = Field(default_factory=list)
    #: A type an override sets to block was found in the text.
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
