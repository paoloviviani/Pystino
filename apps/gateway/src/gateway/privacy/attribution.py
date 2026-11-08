"""What Pystino observed in a request and what handled it (ADR 0113, section 6).

One small JSON object, ``routing``, written into the redaction log entry
(``usage_records.redaction_detail["routing"]``) and returned in the opt-in
per-response summary (``pystino_redaction["routing"]``). It says *what was
seen* (kind, file type, a scan, the target, content verdicts as labels) and
*what handled it* (the extractor that read the file and
why it was not the one asked for, the detector profile, the rule that applied,
the pipelines in scope and what each did).

**No values and no offsets**, as the log has never held them. The one text field
is a file's base name, and it is **never persisted**: file names often carry
personal data. It appears only in the ephemeral per-response summary;
:func:`with_routing` (the one way into the log) strips it and keeps instead which
``filename`` condition matched (rule id and pattern index).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from gateway.privacy.combine import Combined
from gateway.privacy.evaluate import Facts
from gateway.privacy.rules import Rule, glob_matches

if TYPE_CHECKING:
    from gateway.deps import Principal
    from gateway.models import ModelDef

ROUTING_VERSION = 2
MAX_FILENAME = 120
MAX_PIPELINES = 50

#: The detector profile every request is handled by until chat profiles exist.
DEFAULT_PROFILE = "default"

#: Extractor names that mean a page was read as a picture.
_OCR_EXTRACTORS = ("rapidocr",)


def clean_filename(raw: str | None) -> str | None:
    """A base name safe to keep: no directory, no control characters, bounded."""
    if not raw:
        return None
    name = os.path.basename(raw.replace("\\", "/")).strip()
    name = "".join(ch for ch in name if ch.isprintable())
    return name[:MAX_FILENAME] or None


def is_scan_from(extractor: str | None, kind: str | None, has_page_images: bool) -> bool:
    """Whether the extractor reports this document as a scan.

    A document with no text layer, one read by OCR, or one whose pages came back
    as pictures. The extractor is the only thing that can say.
    """
    return (
        kind == "no_text_layer"
        or has_page_images
        or any(name in (extractor or "") for name in _OCR_EXTRACTORS)
    )


def base_facts(
    kind: str,
    principal: Principal,
    model: ModelDef,
    *,
    requested_name: str | None = None,
    texts: tuple[str, ...] = (),
) -> Facts:
    """The facts every kind shares: the caller and the target."""
    provider = model.provider
    group = principal.billing_group
    return Facts(
        kind=kind,
        user_id=str(principal.user.id),
        billing_group_id=str(group.id) if group is not None else None,
        api_key_id=str(principal.api_key.id) if principal.api_key is not None else None,
        target_alias=requested_name
        if requested_name and requested_name != model.api_name
        else None,
        target_model=model.api_name,
        target_provider=provider.name if provider is not None else None,
        provider_id=str(model.provider_id) if model.provider_id is not None else None,
        model_id=str(model.id),
        texts=texts,
    )


def _filename_match(facts: Facts, rules: list[Rule]) -> dict[str, Any] | None:
    """Which ``filename`` pattern of the first rule that used one matched, if any."""
    name = clean_filename(facts.filename)
    if not name:
        return None
    for rule in rules:
        for condition in rule.conditions:
            if condition.key != "filename" or condition.negate:
                continue
            for index, glob in enumerate(condition.values):
                if glob_matches(glob, name):
                    return {"rule_id": str(rule.id), "pattern_index": index}
    return None


def _owner(run: Any) -> str:
    return "user" if run.pipeline.is_user else "admin"


def build(
    facts: Facts,
    *,
    combined: Combined | None = None,
    outcome: str = "default",
    extractor: str | None = None,
    fallback_reason: str = "",
    reader: str | None = None,
    engine: str | None = None,
    profile: str = DEFAULT_PROFILE,
    policies: list[str] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """The ``routing`` object for one request."""
    observed: dict[str, Any] = {
        "target": {
            "alias": facts.target_alias,
            "model": facts.target_model,
            "provider": facts.target_provider,
        },
    }
    runs = combined.runs if combined is not None else []
    seen_rules: list[Rule] = []
    for run in runs:
        if run.evaluation.fired is not None:
            seen_rules.append(run.evaluation.fired)
        seen_rules.extend(run.evaluation.observed)
    if facts.kind == "read":
        observed.update(
            {
                "filename": clean_filename(facts.filename),
                "mime": (facts.mime or "")[:100] or None,
                "filetype": facts.filetype,
                "is_scan": facts.is_scan,
                "filename_match": _filename_match(facts, seen_rules),
            }
        )
    verdicts: dict[str, Any] = {}
    for run in runs:
        for key in ("health", "ids", "lang"):
            if run.evaluation.verdicts.get(key) is not None:
                verdicts.setdefault(key, run.evaluation.verdicts[key])
    if verdicts:
        observed["verdicts"] = {
            "health": verdicts.get("health"),
            "ids": verdicts.get("ids"),
            "lang": verdicts.get("lang"),
            "method": "patterns",
        }
    winner = combined.winner if combined is not None else None
    handled: dict[str, Any] = {
        "extractor": extractor,
        "fallback_reason": fallback_reason or None,
        "reader": reader,
        "engine": engine,
        "profile": profile,
        "routing_rule": (
            {
                "id": str(winner.rule.id),
                "name": winner.rule.name,
                "pipeline_id": str(winner.pipeline.id),
                "pipeline": winner.pipeline.name,
                "owner": _owner(winner),
            }
            if winner is not None
            else None
        ),
        "policies": list(policies or []),
    }
    routing: dict[str, Any] = {
        "v": ROUTING_VERSION,
        "kind": facts.kind,
        "observed": observed,
        "handled": handled,
        "outcome": outcome,
        "pipelines": [
            {
                "id": str(run.pipeline.id),
                "name": run.pipeline.name,
                "owner": _owner(run),
                "hop": run.hop,
                "rule": (
                    None if run.is_otherwise else {"id": str(run.rule.id), "name": run.rule.name}
                ),
                "level": run.level if not run.pending else "pending",
            }
            for run in runs[:MAX_PIPELINES]
        ],
        "observed_rules": [
            {"id": str(r.id), "name": r.name} for run in runs for r in run.evaluation.observed
        ],
    }
    if reason:
        routing["reason"] = reason
    degraded: list[str] = []
    fallbacks: list[str] = []
    for run in runs:
        degraded.extend(m for m in run.evaluation.degraded if m not in degraded)
        fallbacks.extend(m for m in run.evaluation.fallbacks if m not in fallbacks)
    if degraded:
        routing["degraded"] = degraded
    if fallbacks:
        routing["fallbacks"] = fallbacks
    return routing


def with_routing(detail: dict[str, Any] | None, routing: dict[str, Any]) -> dict[str, Any]:
    """*detail* (the redaction log entry) carrying *routing*; one is made if absent."""
    base = dict(detail) if detail is not None else {"v": 1, "blocked": False, "entities": []}
    # The file name is for the response in hand only; the log never keeps it.
    observed = {k: v for k, v in routing["observed"].items() if k != "filename"}
    base["routing"] = {**routing, "observed": observed}
    return base
