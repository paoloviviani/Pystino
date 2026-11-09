"""What the redaction service says it can do (ADR 0113, section 0c).

``GET /healthz`` reports ``pattern_entities`` and ``ner_engines``: for each NER
engine whether it is available, whether it is the default, the entity types it
exposes, whether it takes custom labels at inference (GLiNER) and its speed. The
console offers only engines the service reports available, and a policy that names
one is refused when saved if the service says otherwise or cannot be asked.

A service that predates engines reports none: its one engine is then shown as the
default, so nothing is offered that cannot be chosen.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from gateway.config import NER_ENGINES, Settings
from gateway.privacy.schemas import EnginesResponse, NerEngineView

logger = logging.getLogger(__name__)


def parse_engines(body: dict[str, Any]) -> tuple[list[str], list[NerEngineView]]:
    """The pattern formats and the NER engines of a ``/healthz`` body."""
    patterns = [str(item) for item in body.get("pattern_entities") or []]
    engines: list[NerEngineView] = []
    for raw in body.get("ner_engines") or []:
        if not isinstance(raw, dict) or str(raw.get("name")) not in NER_ENGINES:
            continue
        speed = raw.get("ms_per_token")
        engines.append(
            NerEngineView(
                name=str(raw["name"]),
                available=bool(raw.get("available")),
                default=bool(raw.get("default")),
                entities=[str(e) for e in raw.get("entities") or []],
                custom_labels=bool(raw.get("custom_labels")),
                ms_per_token=float(speed) if isinstance(speed, int | float) else None,
                languages=[str(item) for item in raw.get("languages") or []],
            )
        )
    return patterns, engines


async def fetch_engines(http: httpx.AsyncClient, settings: Settings) -> EnginesResponse:
    """Ask the service now. Never raises: an unreachable service is ``reachable=False``."""
    endpoint = settings.redaction.endpoint
    if settings.redaction.engine != "http" or not endpoint:
        return EnginesResponse(
            reachable=False,
            detail="no redaction service is configured",
            pattern_entities=[],
            engines=[],
        )
    try:
        response = await http.get(
            f"{endpoint.rstrip('/')}/healthz", timeout=settings.redaction.timeout_seconds
        )
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        logger.info("could not ask the redaction service for its engines: %s", exc)
        return EnginesResponse(
            reachable=False,
            detail=f"could not reach {endpoint}",
            pattern_entities=[],
            engines=[],
        )
    patterns, engines = parse_engines(body)
    if not engines and body.get("model_entities") is not None:
        # A service that predates engines: its one NER model is the default.
        engines = [
            NerEngineView(
                name="spacy",
                available=True,
                default=True,
                entities=[str(e) for e in body.get("model_entities") or []],
                custom_labels=False,
                ms_per_token=None,
                languages=[str(item) for item in body.get("languages") or []],
            )
        ]
    return EnginesResponse(reachable=True, detail="ok", pattern_entities=patterns, engines=engines)


def engine_problem(report: EnginesResponse, policy_engine: str | None, labels: bool) -> str | None:
    """Why a policy's engine cannot be chosen against this report, or None when it can."""
    if policy_engine is None:
        if labels:
            default = next((e for e in report.engines if e.default), None)
            if default is None or not default.custom_labels:
                return "Custom entity types need an engine that takes labels (GLiNER): choose it."
        return None
    if not report.reachable:
        return (
            f"The redaction service cannot be reached, so the engine '{policy_engine}' cannot be "
            "checked. Save with the default engine, or try again when it answers."
        )
    found = next((e for e in report.engines if e.name == policy_engine), None)
    if found is None or not found.available:
        return f"The NER engine '{policy_engine}' is not available in the redaction service."
    if labels and not found.custom_labels:
        return f"The engine '{policy_engine}' does not take custom entity types: choose GLiNER."
    return None
