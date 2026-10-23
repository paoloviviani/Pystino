"""The provider catalogue: known providers described in YAML (backlog P4).

Two layers, merged **field by field by provider id**:

1. the *built-in* catalogue, shipped read-only inside the image
   (``gateway/catalogue_data/providers.d/*.yaml``), and
2. an *operator directory*, mounted into the gateway read-only
   (``GATEWAY_PROVIDERS_DIR``, ``/etc/pystino/providers.d`` when unset), whose
   files override built-in fields and may add providers of their own.

**What it is not.** It holds no credential and creates no provider row. A row
(the encrypted key, whether it is active, its grants) is still made by an
administrator in the console; the catalogue supplies what is true of the *kind*
of provider: where it lives, which behaviour plugin speaks to it, which region
it serves from, what it keeps, and dated price hints. ``providers.catalogue_id``
links a row to its entry, and region and retention are read through that link
at request time, so a restart with a corrected file corrects every row at once
and nothing is copied into the database to go stale.

**Strictness.** A bad file stops the gateway at startup (``CatalogueError``),
and the message names the file and the field. Unknown keys are refused, because
a typo (``regoin: eu``) that is quietly ignored is a residency rule that
quietly does not apply. A key that *looks like a secret* is refused outright:
keys stay encrypted in the database, and a catalogue is the sort of file that
gets committed to a repository.

**Merge semantics.** Files in a directory are read in name order and a later
file overrides an earlier one. Within an entry, every scalar is overridden
individually, ``retention`` is merged per sub-field, ``sources`` is replaced as
a whole, and ``prices`` per model
and ``effective_from`` date. An explicit ``null`` clears a value; omitting the
key leaves the lower layer's value alone. The merged entry is validated again as
a whole (a name and a plugin are required), and the origin of every field
(built-in or override, and which file) is kept for the console.

There is no live reload: the catalogue is read once at startup.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gateway.plugins import registry as plugin_registry

logger = logging.getLogger(__name__)

#: Where the operator directory is looked for when no setting names one. A
#: missing directory here is normal (nobody mounted one); a missing directory
#: that was *named* is a mistake and stops the gateway.
DEFAULT_OVERRIDE_DIR = Path("/etc/pystino/providers.d")

#: The built-in layer, inside the package so it travels with the image.
BUILTIN_DIR = Path(__file__).parent / "catalogue_data" / "providers.d"

_SUFFIXES = (".yaml", ".yml")

BUILTIN: Final = "builtin"
OVERRIDE: Final = "override"

#: Key names refused anywhere in a file. Deliberately broad: a false positive
#: costs a renamed key, a false negative costs a leaked credential.
_SECRET_KEY = re.compile(
    r"(api[_-]?key|apikey|secret|passw|token|credential|authorization|bearer|private[_-]?key|"
    r"access[_-]?key|auth[_-]?header)",
    re.IGNORECASE,
)
#: String values that look like a credential whatever the key is called.
_SECRET_VALUE = re.compile(
    r"^(sk-|sk_|pk_|xox[abprs]-|ghp_|gho_|glpat-|AKIA[0-9A-Z]{12})|^bearer\s+\S+",
    re.IGNORECASE,
)


class CatalogueError(Exception):
    """One or more catalogue files are invalid. The message names each file and field."""


# -- the schema ----------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


Money = Annotated[Decimal, Field(strict=False, ge=0, max_digits=20, decimal_places=8)]
Region = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")]

#: Region tags that say *how much is known*, not *where*. ``various`` is a
#: router (the answer is the routed upstream's); ``not-published`` is a
#: provider that does not say. Both reach routing as *unknown* (``None``), so a
#: residency rule never mistakes either for a place; ``global`` is a real
#: answer ("anywhere") and is passed through, where it matches no specific tag.
REGION_VARIOUS: Final = "various"
REGION_NOT_PUBLISHED: Final = "not-published"
_NOT_A_PLACE: Final = (REGION_VARIOUS, REGION_NOT_PUBLISHED)

#: What the retention fields amount to. ``published``: the provider states a
#: policy (a field may still be ``null`` where it states nothing). ``various``:
#: a router, retention depends on the upstream. ``not_published``: it does not
#: say. Required of every built-in entry, so none ships blank.
RetentionStatus = Literal["published", "various", "not_published"]
ProviderId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]


class RetentionPatch(_Strict):
    """What the provider keeps of a request, as one layer states it."""

    status: RetentionStatus | None = None
    #: ZDR is in force by default for API traffic (true), or is not (false).
    zero_data_retention: bool | None = None
    #: ZDR can be obtained (by request, by contract, by a setting).
    zero_data_retention_available: bool | None = None
    retention_days: Annotated[int, Field(ge=0, le=36500)] | None = None
    trains_on_customer_data: bool | None = None
    note: Annotated[str, Field(max_length=700)] | None = None
    source_url: Annotated[str, Field(max_length=500)] | None = None


class SourcePatch(_Strict):
    """A primary source for a fact, with the day it was read."""

    url: Annotated[str, Field(min_length=8, max_length=500)]
    retrieved: date
    covers: Annotated[str, Field(max_length=100)] | None = None


class PricePatch(_Strict):
    """A dated price hint for one model, per million tokens (as every provider
    publishes them), or per image or page for the models billed that way."""

    effective_from: date
    input_per_mtok: Money | None = None
    output_per_mtok: Money | None = None
    cache_read_per_mtok: Money | None = None
    cache_write_per_mtok: Money | None = None
    per_image: Money | None = None
    per_page: Money | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None


class EntryPatch(_Strict):
    """One provider as one file states it. Only ``id`` is required: an override
    names what it changes and nothing else."""

    id: ProviderId
    name: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    description: Annotated[str, Field(max_length=500)] | None = None
    plugin: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    base_url: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    #: Prepended to the model names this provider contributes (``providers.prefix``).
    prefix: Annotated[str, Field(max_length=32, pattern=r"^[a-zA-Z0-9._-]*$")] | None = None
    #: A lower-case tag for where requests are processed: ``eu``, ``us``,
    #: ``eu-west``. Free-form on purpose; a residency rule compares tags.
    region: Region | None = None
    #: Plain words on the region: selectable? an EU option? what is not covered?
    region_note: Annotated[str, Field(max_length=700)] | None = None
    retention: RetentionPatch | None = None
    #: Where the facts above were read (replaced as a whole by an override).
    sources: list[SourcePatch] | None = None
    #: ``{model id as the provider names it: [dated hints]}``.
    prices: dict[str, list[PricePatch]] | None = None


class _FileDoc(_Strict):
    providers: list[EntryPatch]


# -- the merged result ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Retention:
    status: RetentionStatus | None = None
    zero_data_retention: bool | None = None
    zero_data_retention_available: bool | None = None
    retention_days: int | None = None
    trains_on_customer_data: bool | None = None
    note: str | None = None
    source_url: str | None = None


@dataclass(frozen=True, slots=True)
class Source:
    url: str
    retrieved: date
    covers: str | None = None


@dataclass(frozen=True, slots=True)
class PriceHint:
    model: str
    effective_from: date
    input_per_mtok: Decimal | None = None
    output_per_mtok: Decimal | None = None
    cache_read_per_mtok: Decimal | None = None
    cache_write_per_mtok: Decimal | None = None
    per_image: Decimal | None = None
    per_page: Decimal | None = None
    currency: str = "EUR"


@dataclass(frozen=True, slots=True)
class Origin:
    """Where a field's value came from: the layer, and the file inside it."""

    layer: Literal["builtin", "override"]
    file: str


@dataclass(frozen=True, slots=True)
class CatalogueFacts:
    """What a routing rule may know about the provider behind a target.

    The only thing routing reads from the catalogue. ``None`` for a field means
    *unknown*, never "no": a rule that excludes on ``zero_data_retention`` must
    decide for itself what to do with a provider that says nothing.
    """

    catalogue_id: str
    region: str | None = None
    zero_data_retention: bool | None = None
    retention_days: int | None = None
    trains_on_customer_data: bool | None = None


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    id: str
    name: str
    plugin: str
    base_url: str | None
    description: str | None = None
    prefix: str = ""
    region: str | None = None
    region_note: str | None = None
    retention: Retention = field(default_factory=Retention)
    sources: tuple[Source, ...] = ()
    prices: tuple[PriceHint, ...] = ()
    #: Field path (``name``, ``retention.note``, ``prices.<model>@<date>``) to
    #: where its current value came from.
    origins: Mapping[str, Origin] = field(default_factory=dict)

    @property
    def effective_base_url(self) -> str | None:
        """The entry's URL, else the plugin's own default (Cortecs knows its)."""
        if self.base_url:
            return self.base_url
        try:
            return getattr(plugin_registry.resolve(self.plugin), "default_base_url", None)
        except plugin_registry.UnknownPluginError:  # pragma: no cover - validated at load
            return None

    def facts(self) -> CatalogueFacts:
        return CatalogueFacts(
            catalogue_id=self.id,
            region=None if self.region in _NOT_A_PLACE else self.region,
            zero_data_retention=self.retention.zero_data_retention,
            retention_days=self.retention.retention_days,
            trains_on_customer_data=self.retention.trains_on_customer_data,
        )

    def price_hint_for(self, model: str, today: date) -> PriceHint | None:
        """The latest hint for *model* that is already in force on *today*."""
        dated = [h for h in self.prices if h.model == model and h.effective_from <= today]
        return max(dated, key=lambda h: h.effective_from) if dated else None


@dataclass(frozen=True, slots=True)
class Catalogue:
    entries: Mapping[str, CatalogueEntry]
    #: The directories actually read, for the log line and the console.
    sources: tuple[str, ...] = ()

    def get(self, catalogue_id: str | None) -> CatalogueEntry | None:
        return self.entries.get(catalogue_id) if catalogue_id else None

    def facts_for(self, catalogue_id: str | None) -> CatalogueFacts | None:
        entry = self.get(catalogue_id)
        return entry.facts() if entry else None

    def ordered(self) -> list[CatalogueEntry]:
        return sorted(self.entries.values(), key=lambda e: e.id)


EMPTY = Catalogue(entries={})


# -- reading files -------------------------------------------------------------


def _location(loc: Iterable[Any]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "(file)"


def _scan_secrets(node: Any, path: str, label: str, errors: list[str]) -> None:
    """Refuse anything that looks like a credential, naming where it is."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            # Price fields end in `_per_mtok`, never `token`; the one legitimate
            # near-miss would be a key spelled out in words, which is refused.
            if _SECRET_KEY.search(str(key)):
                errors.append(
                    f"{label}: {here}: a field named like a secret is refused. "
                    "Credentials live encrypted in the database, never in the catalogue"
                )
                continue
            _scan_secrets(value, here, label, errors)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _scan_secrets(value, f"{path}[{index}]", label, errors)
    elif isinstance(node, str) and _SECRET_VALUE.search(node.strip()):
        errors.append(
            f"{label}: {path}: the value looks like a credential and is refused. "
            "Credentials live encrypted in the database, never in the catalogue"
        )


def _read_dir(
    directory: Path, layer: Literal["builtin", "override"]
) -> list[tuple[str, EntryPatch]]:
    """Every entry in a directory's YAML files, in file-name order, validated."""
    errors: list[str] = []
    patches: list[tuple[str, EntryPatch]] = []
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix in _SUFFIXES)
    for path in files:
        label = f"{layer} {path.name}"
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError, OSError) as exc:
            errors.append(f"{label}: cannot be read as YAML: {str(exc).splitlines()[0]}")
            continue
        if raw is None:
            continue  # an empty file is a file with nothing to say
        if not isinstance(raw, dict):
            errors.append(f"{label}: (file): must be a mapping with a 'providers' list")
            continue
        before = len(errors)
        _scan_secrets(raw, "", label, errors)
        if len(errors) > before:
            continue
        try:
            doc = _FileDoc.model_validate(raw)
        except ValidationError as exc:
            errors.extend(
                f"{label}: {_location(e['loc'])}: {e['msg']}"
                + (" (unknown key)" if e["type"] == "extra_forbidden" else "")
                for e in exc.errors()
            )
            continue
        seen: set[str] = set()
        for index, patch in enumerate(doc.providers):
            if patch.id in seen:
                errors.append(
                    f"{label}: providers[{index}].id: {patch.id!r} appears twice in this file"
                )
            seen.add(patch.id)
            _check_url(patch.base_url, f"{label}: providers[{index}].base_url", errors)
            for n, src in enumerate(patch.sources or []):
                _check_url(src.url, f"{label}: providers[{index}].sources[{n}].url", errors)
            if patch.retention is not None:
                _check_url(
                    patch.retention.source_url,
                    f"{label}: providers[{index}].retention.source_url",
                    errors,
                )
            patches.append((path.name, patch))
    if errors:
        raise CatalogueError("\n".join(errors))
    return patches


def _check_url(url: str | None, where: str, errors: list[str]) -> None:
    if url is None:
        return
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        errors.append(f"{where}: must be an http(s) URL with a host")
    elif parts.username or parts.password:
        errors.append(f"{where}: must not carry credentials in the URL")


# -- merging -------------------------------------------------------------------

_SCALARS = ("name", "description", "plugin", "base_url", "prefix", "region", "region_note")


@dataclass(slots=True)
class _Draft:
    scalars: dict[str, Any] = field(default_factory=dict)
    retention: dict[str, Any] = field(default_factory=dict)
    sources: tuple[Source, ...] = ()
    prices: dict[tuple[str, date], dict[str, Any]] = field(default_factory=dict)
    origins: dict[str, Origin] = field(default_factory=dict)
    #: file names that touched this entry, first definition first.
    files: list[str] = field(default_factory=list)


def _merge(patches: Iterable[tuple[Origin, EntryPatch]]) -> dict[str, _Draft]:
    drafts: dict[str, _Draft] = {}
    for origin, patch in patches:
        draft = drafts.setdefault(patch.id, _Draft())
        draft.files.append(f"{origin.layer} {origin.file}")
        given = patch.model_fields_set
        for name in _SCALARS:
            if name in given:
                draft.scalars[name] = getattr(patch, name)
                draft.origins[name] = origin
        if patch.retention is not None:
            for name in patch.retention.model_fields_set:
                draft.retention[name] = getattr(patch.retention, name)
                draft.origins[f"retention.{name}"] = origin
        if patch.sources is not None:
            draft.sources = tuple(Source(x.url, x.retrieved, x.covers) for x in patch.sources)
            draft.origins["sources"] = origin
        for model, hints in (patch.prices or {}).items():
            for hint in hints:
                key = (model, hint.effective_from)
                slot = draft.prices.setdefault(key, {})
                for name in hint.model_fields_set - {"effective_from"}:
                    slot[name] = getattr(hint, name)
                draft.origins[f"prices.{model}@{hint.effective_from.isoformat()}"] = origin
    return drafts


def _finish(entry_id: str, draft: _Draft, errors: list[str]) -> CatalogueEntry | None:
    """Validate the merged whole; failures name every file that shaped the entry."""
    where = f"provider {entry_id!r} (from {', '.join(dict.fromkeys(draft.files))})"
    problems: list[str] = []
    name = draft.scalars.get("name")
    plugin = draft.scalars.get("plugin")
    if not name:
        problems.append("name: required, and no layer sets it")
    if not plugin:
        problems.append("plugin: required, and no layer sets it")
    elif plugin not in plugin_registry.available():
        problems.append(
            f"plugin: unknown provider plugin {plugin!r}; installed: "
            f"{', '.join(plugin_registry.available())}"
        )
    hints: list[PriceHint] = []
    for (model, effective_from), values in sorted(draft.prices.items()):
        path = f"prices.{model}@{effective_from.isoformat()}"
        has_unit = any(values.get(k) is not None for k in ("per_image", "per_page"))
        if not has_unit and (
            values.get("input_per_mtok") is None or values.get("output_per_mtok") is None
        ):
            problems.append(
                f"{path}: needs input_per_mtok and output_per_mtok (or per_image / per_page)"
            )
            continue
        hints.append(
            PriceHint(
                model=model,
                effective_from=effective_from,
                currency=values.get("currency") or "EUR",
                **{k: v for k, v in values.items() if k != "currency"},
            )
        )
    if problems or not name or not plugin:
        errors.extend(f"{where}: {p}" for p in problems)
        return None
    base_url = draft.scalars.get("base_url")
    return CatalogueEntry(
        id=entry_id,
        name=name,
        plugin=plugin,
        base_url=base_url.rstrip("/") if base_url else None,
        description=draft.scalars.get("description"),
        prefix=draft.scalars.get("prefix") or "",
        region=draft.scalars.get("region"),
        region_note=draft.scalars.get("region_note"),
        retention=Retention(**draft.retention),
        sources=draft.sources,
        prices=tuple(hints),
        origins=dict(draft.origins),
    )


def load_catalogue(
    *, builtin_dir: Path | None = None, override_dir: str | Path | None = None
) -> Catalogue:
    """Read both layers and merge them, or raise :class:`CatalogueError`.

    ``override_dir`` is the *configured* value: empty or ``None`` means "look
    at the default and use it if it is there"; anything else must exist.
    """
    builtin = builtin_dir if builtin_dir is not None else BUILTIN_DIR
    patches: list[tuple[Origin, EntryPatch]] = []
    sources: list[str] = []

    if builtin.is_dir():
        sources.append(str(builtin))
        patches += [(Origin(BUILTIN, f), p) for f, p in _read_dir(builtin, BUILTIN)]

    explicit = bool(override_dir)
    directory = Path(override_dir) if override_dir else DEFAULT_OVERRIDE_DIR
    if directory.is_dir():
        sources.append(str(directory))
        patches += [(Origin(OVERRIDE, f), p) for f, p in _read_dir(directory, OVERRIDE)]
    elif explicit:
        raise CatalogueError(
            f"GATEWAY_PROVIDERS_DIR: {directory} is not a directory. "
            "Mount it, or unset the setting."
        )

    errors: list[str] = []
    entries: dict[str, CatalogueEntry] = {}
    for entry_id, draft in _merge(patches).items():
        entry = _finish(entry_id, draft, errors)
        if entry is not None:
            entries[entry_id] = entry
    if errors:
        raise CatalogueError("\n".join(errors))
    logger.info("provider catalogue: %d entries from %s", len(entries), ", ".join(sources))
    return Catalogue(entries=entries, sources=tuple(sources))
