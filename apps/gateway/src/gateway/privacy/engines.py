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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
                labels=[str(item).casefold() for item in raw.get("labels") or []],
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


#: What the console and the API say when the redaction service has no NER model at all.
NO_NER_MESSAGE = (
    "No NER engine is installed in the redaction service; entities need one. "
    "Run the NER image with REDACTION_NLP_ENGINE=spacy (with the Cerea kit: "
    './configure --redaction), see the Redaction guide, section "Deploying less of it".'
)


def has_ner(report: EnginesResponse) -> bool | None:
    """Whether any engine of the service can find entities; None when that is not known."""
    if not report.reachable:
        return None
    return any(engine.available and engine.entities for engine in report.engines)


def ner_type_names(report: EnginesResponse) -> set[str]:
    """Every model-backed type any engine reports, plus the first service's four."""
    from gateway.config import LEGACY_NER_TYPES

    names = set(LEGACY_NER_TYPES)
    for engine in report.engines:
        names.update(entity.upper() for entity in engine.entities)
    return names


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


class Switches:
    """What the Detectors tab has switched on: the patterns and each NER engine."""

    __slots__ = ("gliner", "patterns", "spacy")

    def __init__(self, patterns: bool = True, spacy: bool = True, gliner: bool = True) -> None:
        self.patterns = patterns
        self.spacy = spacy
        self.gliner = gliner

    def engine(self, name: str) -> bool:
        return self.gliner if name == "gliner" else self.spacy


def switches_from(row: Any, settings: Settings) -> Switches:
    """The switches in force: the newest stored row over the deployment defaults.

    A per-engine switch left undecided follows the older ``presidio_ner`` switch, so a
    row written before the Detectors tab means what it always did.
    """
    config = settings.redaction

    def pick(stored: bool | None, fallback: bool | None) -> bool:
        if stored is not None:
            return stored
        return True if fallback is None else fallback

    ner = pick(getattr(row, "presidio_ner", None), config.presidio_ner)
    return Switches(
        patterns=pick(
            getattr(row, "presidio_pattern_matching", None), config.presidio_pattern_matching
        ),
        spacy=pick(getattr(row, "spacy_enabled", None), ner),
        gliner=pick(getattr(row, "gliner_enabled", None), ner),
    )


async def current_switches(session: AsyncSession, settings: Settings) -> Switches:
    from gateway.models import RedactionConfig

    row = (
        await session.execute(
            select(RedactionConfig).order_by(RedactionConfig.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    return switches_from(row, settings)


def users_of_detectors(
    policies: list[tuple[str, Any]], default_engine: str | None
) -> dict[str, list[str]]:
    """Which active policies use which detector: ``patterns``, ``spacy``, ``gliner``.

    A policy uses the patterns when it lists a format that is not off, and an engine when
    it lists an entity (or, for GLiNER, a custom entity) that is not off. A policy that
    names no engine reads with the deployment's default.
    """
    from gateway.config import EntityMode, RedactionPolicy

    used: dict[str, list[str]] = {"patterns": [], "spacy": [], "gliner": []}
    for name, raw in policies:
        policy = RedactionPolicy.model_validate(raw or {})
        if any(item.mode is not EntityMode.OFF for item in policy.formats.values()):
            used["patterns"].append(name)
        engine = policy.engine or default_engine
        reads = any(item.mode is not EntityMode.OFF for item in policy.entities.values()) or any(
            c.mode is not EntityMode.OFF for c in policy.custom_entities
        )
        if reads and engine in ("spacy", "gliner"):
            used[engine].append(name)
    return used


async def effective_engines(
    session: AsyncSession, http: httpx.AsyncClient, settings: Settings
) -> EnginesResponse:
    """What the service reports, with the switches and who uses each detector."""
    from gateway.models import RedactionPolicyRecord

    report = await fetch_engines(http, settings)
    switches = await current_switches(session, settings)
    rows = (
        await session.execute(
            select(RedactionPolicyRecord.name, RedactionPolicyRecord.policy).where(
                RedactionPolicyRecord.is_active.is_(True)
            )
        )
    ).all()
    default = next((e.name for e in report.engines if e.default), "spacy")
    used = users_of_detectors([(r[0], r[1]) for r in rows], default)
    report.patterns_enabled = switches.patterns
    report.patterns_used_by = sorted(used["patterns"])
    for engine in report.engines:
        engine.enabled = switches.engine(engine.name)
        engine.used_by = sorted(used.get(engine.name, []))
    return report


def usable(report: EnginesResponse) -> EnginesResponse:
    """The report as a policy may use it: a switched-off detector offers nothing."""
    copy = report.model_copy(deep=True)
    if not copy.patterns_enabled:
        copy.pattern_entities = []
    for engine in copy.engines:
        if not engine.enabled:
            engine.available = False
    return copy
