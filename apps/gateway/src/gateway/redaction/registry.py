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
from importlib.metadata import entry_points

from gateway.config import RedactionSettings
from gateway.redaction.base import Redactor

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "llmp.redactors"

RedactorFactory = Callable[[RedactionSettings], Redactor]

_BUILTIN: dict[str, RedactorFactory] = {}


class UnknownEngineError(ValueError):
    """The configured engine name resolves to nothing."""


def register(name: str, factory: RedactorFactory) -> None:
    """Register a built-in engine. Third parties use the entry point instead."""
    _BUILTIN[name] = factory


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
