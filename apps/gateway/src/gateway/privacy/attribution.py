"""What Pystino observed in a request and what handled it (ADR 0113, section 6).

One small JSON object, ``routing``, written into the redaction log entry
(``usage_records.redaction_detail["routing"]``) and returned in the opt-in
per-response summary (``pystino_redaction["routing"]``). It says *what was
seen* (kind, file type, a scan, the caller's groups, the target, content
verdicts as labels) and *what handled it* (the extractor that read the file and
why it was not the one asked for, the detector profile, the rule that applied).

**No values and no offsets**, as the log has never held them. The one text field
is a file's base name, which the user chose and the administrator's rules match
on; it is cut to :data:`MAX_FILENAME` characters and never a path.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from gateway.privacy.evaluate import Evaluation, Facts

if TYPE_CHECKING:
    from gateway.deps import Principal
    from gateway.models import ModelDef

ROUTING_VERSION = 1
MAX_FILENAME = 120
MAX_GROUPS = 20

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
    kind: str, principal: Principal, model: ModelDef, *, requested_name: str | None = None
) -> Facts:
    """The facts every kind shares: the caller and the target."""
    groups = tuple(
        sorted({m.group.name for m in principal.user.memberships if m.group is not None})
    )
    provider = model.provider
    return Facts(
        kind=kind,
        user_email=principal.user.email,
        groups=groups,
        target_alias=requested_name
        if requested_name and requested_name != model.api_name
        else None,
        target_model=model.api_name,
        target_provider=provider.name if provider is not None else None,
        target_local=bool(provider.is_local) if provider is not None else None,
    )


def build(
    facts: Facts,
    *,
    evaluation: Evaluation | None = None,
    outcome: str = "default",
    extractor: str | None = None,
    fallback_reason: str = "",
    reader: str | None = None,
    profile: str = DEFAULT_PROFILE,
) -> dict[str, Any]:
    """The ``routing`` object for one request."""
    observed: dict[str, Any] = {
        "groups": list(facts.groups[:MAX_GROUPS]),
        "target": {
            "alias": facts.target_alias,
            "model": facts.target_model,
            "provider": facts.target_provider,
            "local": facts.target_local,
        },
    }
    if facts.kind == "read":
        observed.update(
            {
                "filename": clean_filename(facts.filename),
                "mime": (facts.mime or "")[:100] or None,
                "filetype": facts.filetype,
                "is_scan": facts.is_scan,
            }
        )
    fired = evaluation.fired if evaluation is not None else None
    if evaluation is not None and any(k in evaluation.verdicts for k in ("health", "ids", "lang")):
        observed["verdicts"] = {
            "health": evaluation.verdicts.get("health"),
            "ids": evaluation.verdicts.get("ids"),
            "lang": evaluation.verdicts.get("lang"),
            "method": "patterns",
        }
    handled: dict[str, Any] = {
        "extractor": extractor,
        "fallback_reason": fallback_reason or None,
        "reader": reader,
        "profile": profile,
        "routing_rule": ({"id": str(fired.id), "name": fired.name} if fired is not None else None),
    }
    routing: dict[str, Any] = {
        "v": ROUTING_VERSION,
        "kind": facts.kind,
        "observed": observed,
        "handled": handled,
        "outcome": outcome,
        "observed_rules": [
            {"id": str(r.id), "name": r.name} for r in (evaluation.observed if evaluation else [])
        ],
    }
    if evaluation is not None:
        if evaluation.degraded:
            routing["degraded"] = list(evaluation.degraded)
        if evaluation.fallbacks:
            routing["fallbacks"] = list(evaluation.fallbacks)
    return routing


def with_routing(detail: dict[str, Any] | None, routing: dict[str, Any]) -> dict[str, Any]:
    """*detail* (the redaction log entry) carrying *routing*; one is made if absent."""
    base = dict(detail) if detail is not None else {"v": 1, "blocked": False, "entities": []}
    base["routing"] = routing
    return base
