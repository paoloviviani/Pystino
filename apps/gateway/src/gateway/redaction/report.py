"""What redaction did to one request, said without saying what it saw.

Two views of one fact, built from the same findings:

* the **log entry** (:func:`build_detail`), written onto the usage row and read
  back by the user and the administrator;
* the **per-response summary** (:func:`build_summary`), which a caller that asked
  for it (``x-pystino-redaction-summary``) receives in the response itself.

The log entry never holds an original or a matched value, nor an offset into the
text. The summary can carry the originals (``values``), but only when the caller
asked for them and authenticates with a user token; that list is built from the
outcome in memory, for the one response, and is not part of anything written
(ADR 0112).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from gateway.config import EffectivePolicy

#: Where in the request a value was found. ``document`` is only ever set by the
#: OCR route, where the extracted document is the screened text; a chat
#: completion cannot tell a pasted document from a message.
SOURCE_MESSAGE = "message"
SOURCE_TOOL_RESULT = "tool_result"
SOURCE_DOCUMENT = "document"

DETAIL_VERSION = 1


@dataclass(slots=True)
class EntityFinding:
    """How many values of one type were hidden, in what mode, and from where."""

    entity_type: str
    count: int = 0
    mode: str = ""
    sources: set[str] = field(default_factory=set)


def record(
    findings: dict[str, EntityFinding],
    entity_type: str,
    *,
    mode: str,
    source: str,
    count: int = 1,
) -> None:
    """Count *count* hidden values of *entity_type* into *findings*."""
    entry = findings.get(entity_type)
    if entry is None:
        entry = findings[entity_type] = EntityFinding(entity_type=entity_type, mode=mode)
    entry.count += count
    entry.sources.add(source)


def _rule_of(policy: EffectivePolicy | None, entity_type: str) -> dict[str, Any] | None:
    decider = policy.decider_for(entity_type) if policy is not None else None
    if decider is None:
        return None
    return {
        "id": str(decider.id) if decider.id is not None else None,
        "name": decider.name,
        "owner": decider.owner,
    }


def entities_of(
    findings: Mapping[str, EntityFinding],
    policy: EffectivePolicy | None,
    *,
    relabel_sources: str | None = None,
) -> list[dict[str, Any]]:
    """One entry per entity type, in a stable order."""
    entries: list[dict[str, Any]] = []
    for name in sorted(findings):
        finding = findings[name]
        entries.append(
            {
                "type": name,
                "count": finding.count,
                "mode": finding.mode,
                "rule": _rule_of(policy, name),
                "sources": [relabel_sources] if relabel_sources else sorted(finding.sources),
            }
        )
    return entries


def build_detail(
    findings: Mapping[str, EntityFinding],
    policy: EffectivePolicy | None,
    *,
    blocked: bool = False,
    relabel_sources: str | None = None,
) -> dict[str, Any] | None:
    """The log entry for a request, or None when there is nothing to log."""
    if not findings and not blocked:
        return None
    return {
        "v": DETAIL_VERSION,
        "blocked": blocked,
        "entities": entities_of(findings, policy, relabel_sources=relabel_sources),
    }


def relabel(detail: dict[str, Any] | None, source: str) -> dict[str, Any] | None:
    """*detail* with every entry's sources replaced by *source*.

    For `/v1/ocr`, where the text screened is the extracted document and a
    "message" would be a false label.
    """
    if detail is None:
        return None
    return {
        **detail,
        "entities": [{**entity, "sources": [source]} for entity in detail["entities"]],
    }


def build_summary(
    *,
    request_id: str,
    detail: Mapping[str, Any] | None,
    blocked: bool = False,
    values: list[dict[str, str]] | None = None,
    routing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``pystino_redaction`` object (the contract in ADR 0112).

    ``routing`` (ADR 0113) is the observation and attribution block. It is
    present whenever the summary is sent for a request of a known kind (chat,
    embedding, reading); the summary itself is sent only when the caller opted in
    and redaction is in force or a routing rule is enabled, so a caller that never
    asked sees the shape it always had.
    """
    entities = list(detail["entities"]) if detail else []
    summary: dict[str, Any] = {
        "applied": bool(entities),
        "request_id": request_id,
        "blocked": blocked,
        "entities": entities,
    }
    if routing is None and detail and "routing" in detail:
        routing = detail["routing"]
    if routing is not None:
        summary["routing"] = dict(routing)
    if values is not None:
        summary["values"] = values
    return summary
