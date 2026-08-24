"""Engine lookup.

Built-in engines are registered here; third-party ones register through the
``llmp.redactors`` entry-point group, so ``pip install`` plus one setting is the
whole integration ([0026](../../../../docs/adr/0026-pluggable-detection.md)):

```toml
[project.entry-points."llmp.redactors"]
my-engine = "my_package:MyRedactor"
```

The factory receives :class:`RedactionSettings` and returns a :class:`Redactor`.

An unknown name is an error naming what *is* available. It is never quietly
downgraded to ``noop``: a gateway that believes redaction is on when it is not is
worse than one that refuses to start.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points

from gateway.config import RedactionSettings
from gateway.redaction.base import Redactor

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "llmp.redactors"

RedactorFactory = Callable[[RedactionSettings], Redactor]

_BUILTIN: dict[str, RedactorFactory] = {}


@dataclass(frozen=True, slots=True)
class EngineInfo:
    """What an operator needs in order to choose an engine.

    Exists because the console lists engines now (ADR 0033) and a bare name is
    not a choice: ``noop`` and ``http`` say nothing about which one leaves
    personal data in a prompt. Three fields, each answering a question asked at
    the moment of enabling one:

    * ``label`` and ``description`` — what it is, and what enabling it means.
    * ``needs_endpoint`` — whether it calls a service, which decides both what
      the API must validate before saving and whether "unreachable" is a
      meaningful state for it.
    * ``redacts`` — whether it removes anything at all. ``noop`` is a real,
      recorded engine rather than an absence (see ``noop.py``), so "is redaction
      on" cannot be derived from the name without hardcoding that name in the
      console. This is that fact, declared.
    """

    name: str
    label: str
    description: str
    needs_endpoint: bool = False
    redacts: bool = True


_INFO: dict[str, EngineInfo] = {}


class UnknownEngineError(ValueError):
    """The configured engine name resolves to nothing."""


def register(
    name: str,
    factory: RedactorFactory,
    *,
    label: str | None = None,
    description: str = "",
    needs_endpoint: bool = False,
    redacts: bool = True,
) -> None:
    """Register a built-in engine. Third parties use the entry point instead.

    The metadata is optional so that adding it did not change the signature
    third-party code depends on. An engine registered without it still lists in
    the console, described by its own name — thin, but honest, and better than
    refusing to show an installed engine because its author did not fill in a
    form.
    """
    _BUILTIN[name] = factory
    _INFO[name] = EngineInfo(
        name=name,
        label=label or name,
        description=description,
        needs_endpoint=needs_endpoint,
        redacts=redacts,
    )


def _discovered() -> dict[str, RedactorFactory]:
    """Factories advertised by installed packages.

    A plugin that fails to import is logged and skipped rather than taking the
    gateway down with it — but only when it is *not* the engine being asked for.
    ``resolve`` raises for that case, because starting without the redaction the
    operator configured is the failure this whole module exists to prevent.
    """
    found: dict[str, RedactorFactory] = {}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            found[entry.name] = entry.load()
        except Exception:
            logger.warning(
                "redaction plugin %r failed to load; ignoring", entry.name, exc_info=True
            )
    return found


def available() -> list[str]:
    return sorted({*_BUILTIN, *_discovered()})


def _info_from(name: str, factory: RedactorFactory) -> EngineInfo:
    """Describe an entry-point engine from attributes on its factory.

    Read with ``getattr`` rather than required, for the same reason the metadata
    on ``register`` is optional: a third-party engine that declares nothing is
    still listed. A class can opt in by carrying ``label``, ``description``,
    ``needs_endpoint`` and ``redacts``, which is how the built-ins would look if
    they were external.
    """
    return EngineInfo(
        name=name,
        label=str(getattr(factory, "label", name)),
        description=str(getattr(factory, "description", "")),
        needs_endpoint=bool(getattr(factory, "needs_endpoint", False)),
        # Defaults to True: an unknown engine is assumed to redact, because
        # assuming the opposite would show a working layer as switched off.
        redacts=bool(getattr(factory, "redacts", True)),
    )


def describe() -> list[EngineInfo]:
    """Every installed engine, for the console's engine list.

    Built from the registry rather than written out in the frontend, so
    installing an engine makes it selectable without a console release — the same
    argument the provider-plugin listing makes (ADR 0032).
    """
    described: dict[str, EngineInfo] = {}
    for name, factory in _discovered().items():
        described[name] = _info_from(name, factory)
    # Built-ins last, so they win a name collision here exactly as they do in
    # `resolve`. An installed package cannot relabel `noop` into something that
    # sounds like it redacts.
    for name in _BUILTIN:
        described[name] = _INFO[name]
    return [described[name] for name in sorted(described)]


def resolve(name: str) -> RedactorFactory:
    """The factory for *name*.

    Built-ins win over plugins of the same name, so an installed package cannot
    silently replace ``noop`` with something that does nothing of the sort.
    """
    if name in _BUILTIN:
        return _BUILTIN[name]

    for entry in entry_points(group=ENTRY_POINT_GROUP):
        if entry.name != name:
            continue
        try:
            return entry.load()  # type: ignore[no-any-return]
        except Exception as exc:
            raise UnknownEngineError(
                f"the redaction engine {name!r} is registered by an installed package "
                f"but failed to load: {exc}"
            ) from exc

    raise UnknownEngineError(
        f"unknown redaction engine {name!r}. Available: {', '.join(available())}. "
        f"Third-party engines register under the {ENTRY_POINT_GROUP!r} entry-point group; "
        "see docs/adr/0026-pluggable-detection.md."
    )
