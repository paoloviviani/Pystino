"""The rule vocabulary of a pipeline, its validation, and the save-time checks (ADR 0114).

A pipeline's rule is a sentence: **When <policies> + <conditions> -> <mode>**.

* ``policies`` are what the rule looks for and what it hides (admin policy ids; for a user
  pipeline, the shared administrator policies and their own).
* ``conditions`` are optional and combine with ``match`` (all or any), each can be
  negated. ``finds`` is the threshold ("at least N distinct items found by the rule's own
  policies, combined"); ``classified`` is administrators' only; an extract rule has
  ``filetype``, ``scan`` and ``filename``. With none, a content rule fires when its
  policies find one item.
* ``mode`` is ``off`` < ``detect_only`` < ``anonymise_restore`` < ``anonymise`` <
  ``redact`` < ``block``, with optional ``overrides`` by entity type. An extract rule has
  an ``action`` (default reader < read with < block) and hiding that is optional.

This module owns what each kind may say, so the admin API, the user API, the test
endpoint and the evaluator agree. **Errors** (:func:`validate_rule`) block saving;
**warnings** (:func:`check`) never block.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from gateway.config import EntityMode, hides
from gateway.redaction import user_rules

KIND_EXTRACT = "extract"
KIND_CONTENT = "content"
KINDS = (KIND_EXTRACT, KIND_CONTENT)

#: What a content pipeline's target may be narrowed to (the model's own type). Empty = any.
MODEL_TYPES = ("chat", "embedding", "rerank")

#: Past this many rules a pipeline stops being readable aloud. A warning, not a limit.
SOFT_CAP = 20
#: A hard bound, because rules are held in memory on every worker.
MAX_RULES = 100
MAX_OVERRIDES = 50

CONDITION_KEYS: dict[str, tuple[str, ...]] = {
    KIND_EXTRACT: ("filetype", "scan", "filename"),
    KIND_CONTENT: ("finds", "classified"),
}
#: Keys that are a yes/no about the request: they carry no values, only ``negate``.
BOOL_KEYS = ("scan",)
#: Keys decided by reading the request's own text.
CONTENT_KEYS = ("finds", "classified")
#: Most items a "finds at least N" may ask for.
MAX_COUNT = 1000
MATCHES = ("all", "any")

FILETYPES = ("pdf", "word", "excel", "powerpoint", "image", "text")
#: The reader a "read with" rule that names none means (``gateway.extraction.DEFAULT_MODEL``).
DEFAULT_READER = "markitdown"

#: The modes a rule may name, weakest first.
MODES = tuple(m.value for m in EntityMode)
#: Reading: what happens to the file, lowest to highest.
EXTRACT_LEVELS = ("default", "extract_with", "block")
#: Where a request ends up, per kind, lowest to highest.
LADDER: dict[str, tuple[str, ...]] = {
    KIND_EXTRACT: EXTRACT_LEVELS,
    KIND_CONTENT: ("pass", "block"),
}
LEVEL_BLOCK = "block"
LEVEL_EXTRACT_WITH = "extract_with"


def base_level(kind: str) -> str:
    """What happens anyway: the default reader, or pass."""
    return LADDER[kind][0]


_MAX_VALUES = 50
_MAX_VALUE_LENGTH = 200
_MAX_NAME = 120


class RuleError(ValueError):
    """A rule that cannot be saved. The message names the field and what is wrong."""


class ContentOnReadError(RuleError):
    """A content condition on an extract rule: answered as a 400, not a field error."""


CONTENT_ON_READ = (
    "Extract rules decide from the file, not its content; content is checked when the text "
    "is embedded or sent to chat."
)


@dataclass(frozen=True, slots=True)
class Condition:
    key: str
    values: tuple[str, ...]
    negate: bool = False
    #: ``finds`` only: the least number of distinct items that makes it true.
    count: int = 1


#: A rule with no conditions in the content kind fires when its policies find one item.
IMPLICIT_FINDS = Condition("finds", (), False, 1)


def _mode(value: str | None, default: EntityMode = EntityMode.OFF) -> EntityMode:
    try:
        return EntityMode(value) if value else default
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Rule:
    """An immutable snapshot of one rule, as the request path holds it.

    ``action`` is the reading rule's ``{level, reader?}`` (empty for content).
    """

    id: uuid.UUID
    kind: str
    name: str
    enabled: bool
    position: int
    policies: tuple[str, ...]
    match: str
    conditions: tuple[Condition, ...]
    mode: str
    overrides: Mapping[str, str]
    action: Mapping[str, Any]
    #: A warning an administrator must read before turning the rule on (the
    #: migration leaves one on a rule it could not convert exactly).
    note: str | None = None

    @property
    def level(self) -> str:
        """Where this rule takes the request: block, read with, or nothing."""
        if self.kind == KIND_EXTRACT:
            return str(self.action.get("level") or "default")
        return LEVEL_BLOCK if self.blocks_all else base_level(self.kind)

    @property
    def mode_enum(self) -> EntityMode:
        return _mode(self.mode)

    @property
    def blocks_all(self) -> bool:
        return self.kind == KIND_CONTENT and self.mode_enum is EntityMode.BLOCK

    @property
    def hides_text(self) -> bool:
        """Whether the rule's policies hide or block anything (mode or an override)."""
        if not self.policies:
            return False
        if hides(self.mode_enum):
            return True
        return any(hides(_mode(m)) for m in self.overrides.values())

    @property
    def records_only(self) -> bool:
        return (
            not self.hides_text
            and bool(self.policies)
            and (
                self.mode_enum is EntityMode.DETECT_ONLY
                or any(_mode(m) is EntityMode.DETECT_ONLY for m in self.overrides.values())
            )
        )

    @property
    def effective_conditions(self) -> tuple[Condition, ...]:
        if self.conditions or self.kind == KIND_EXTRACT:
            return self.conditions
        return (IMPLICIT_FINDS,)

    @property
    def protective(self) -> bool:
        """Whether the rule protects (so it must not be skipped for want of a fact)."""
        if self.kind == KIND_EXTRACT:
            return self.level != "default" or self.hides_text
        return self.hides_text

    @property
    def is_noop(self) -> bool:
        return not self.protective


@dataclass(frozen=True, slots=True)
class Issue:
    """One save-time warning, in words an administrator can act on."""

    code: str
    message: str
    rule_id: uuid.UUID | None = None
    rule_name: str | None = None


@dataclass(slots=True)
class ContentInfo:
    """What the checks need to know about a policy or a classifier a rule names."""

    name: str
    available: bool = True
    why: str = ""


@dataclass(slots=True)
class CheckContext:
    """Facts about the deployment the checks consult. All optional."""

    content: Mapping[str, ContentInfo] = field(default_factory=dict)
    unavailable_readers: Sequence[str] = ()


def condition_from_json(raw: Mapping[str, Any]) -> Condition:
    return Condition(
        key=str(raw.get("key") or ""),
        values=tuple(str(v) for v in raw.get("values") or ()),
        negate=bool(raw.get("negate", False)),
        count=int(raw.get("count") or 1),
    )


def condition_to_json(condition: Condition) -> dict[str, Any]:
    out: dict[str, Any] = {
        "key": condition.key,
        "values": list(condition.values),
        "negate": condition.negate,
    }
    if condition.key == "finds":
        out["count"] = condition.count
    return out


def rule_to_json(rule: Rule) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(rule.id),
        "name": rule.name,
        "enabled": rule.enabled,
        "policies": list(rule.policies),
        "match": rule.match,
        "conditions": [condition_to_json(c) for c in rule.conditions],
        "mode": rule.mode,
        "overrides": dict(rule.overrides),
    }
    if rule.kind == KIND_EXTRACT:
        out["action"] = dict(rule.action)
    if rule.note:
        out["note"] = rule.note
    return out


def rule_from_json(kind: str, position: int, raw: Mapping[str, Any]) -> Rule:
    """A stored rule. Raises ``ValueError`` for one that does not read."""
    for condition in raw.get("conditions") or []:
        if condition.get("key") not in CONDITION_KEYS[kind] or not isinstance(
            condition.get("values", []), list
        ):
            raise ValueError("a condition has no known key or values")
    mode = str(raw.get("mode") or "off")
    if mode not in MODES:
        raise ValueError("the rule has no valid mode")
    action: dict[str, Any] = {}
    if kind == KIND_EXTRACT:
        action = dict(raw.get("action") or {"level": "default"})
        if action.get("level") not in EXTRACT_LEVELS:
            raise ValueError("the rule has no action")
    match = str(raw.get("match") or "all")
    if match not in MATCHES:
        raise ValueError("match is all or any")
    overrides = {str(k): str(v) for k, v in dict(raw.get("overrides") or {}).items()}
    if any(v not in MODES for v in overrides.values()):
        raise ValueError("an override has no valid mode")
    return Rule(
        id=uuid.UUID(str(raw["id"])),
        kind=kind,
        name=str(raw.get("name") or ""),
        enabled=bool(raw.get("enabled", True)),
        position=position,
        policies=tuple(str(p) for p in raw.get("policies") or ()),
        match=match,
        conditions=tuple(condition_from_json(c) for c in raw.get("conditions") or []),
        mode=mode,
        overrides=overrides,
        action=action,
        note=(str(raw["note"]) if raw.get("note") else None),
    )


def _clean_values(key: str, raw: Iterable[Any]) -> list[str]:
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise RuleError(f"'{key}' values must be text.")
        value = item.strip()
        if not value:
            raise RuleError(f"'{key}' has an empty value.")
        if len(value) > _MAX_VALUE_LENGTH:
            raise RuleError(f"A '{key}' value is longer than {_MAX_VALUE_LENGTH} characters.")
        if value not in values:
            values.append(value)
    if not values:
        raise RuleError(f"'{key}' needs at least one value.")
    if len(values) > _MAX_VALUES:
        raise RuleError(f"'{key}' has more than {_MAX_VALUES} values.")
    return values


def _validate_value(key: str, value: str) -> str:
    if key == "filetype":
        value = value.lower()
        if value not in FILETYPES:
            raise RuleError(f"File type '{value}' is not one of: {', '.join(FILETYPES)}.")
    elif key == "filename":
        if not value.strip("*?[]"):
            raise RuleError("A file name pattern needs at least one ordinary character.")
    return value


def _parse_glob(glob: str) -> list[tuple[str, Any]]:
    """A glob as tokens: ``("lit", c)``, ``("any", None)``, ``("star", None)`` or
    ``("cls", (negated, ranges))``. ``[`` with no closing ``]`` is a literal."""
    tokens: list[tuple[str, Any]] = []
    i, n = 0, len(glob)
    while i < n:
        ch = glob[i]
        i += 1
        if ch == "*":
            if not tokens or tokens[-1][0] != "star":
                tokens.append(("star", None))
        elif ch == "?":
            tokens.append(("any", None))
        elif ch == "[":
            j = glob.find("]", i + 1)
            if j == -1:
                tokens.append(("lit", ch.lower()))
                continue
            body = glob[i:j]
            negated = body.startswith("!")
            body = body[1:] if negated else body
            ranges: list[tuple[str, str]] = []
            k = 0
            while k < len(body):
                if k + 2 < len(body) and body[k + 1] == "-":
                    ranges.append((body[k].lower(), body[k + 2].lower()))
                    k += 3
                else:
                    ranges.append((body[k].lower(), body[k].lower()))
                    k += 1
            tokens.append(("cls", (negated, ranges)))
            i = j + 1
        else:
            tokens.append(("lit", ch.lower()))
    return tokens


def _token_matches(token: tuple[str, Any], ch: str) -> bool:
    kind, arg = token
    if kind == "lit":
        return bool(arg == ch)
    if kind == "any":
        return True
    negated, ranges = arg
    return bool(any(lo <= ch <= hi for lo, hi in ranges) != negated)


def glob_matches(glob: str, name: str) -> bool:
    """Whether *name* matches *glob* (``*``, ``?``, ``[abc]``, case-insensitive).

    A linear two-pointer matcher that remembers only the last ``*``, not a regular
    expression: a pattern such as ``*a*a*a*a*a*b`` cannot make a backtracking
    engine take exponential time on a hostile name. Worst case is O(len(glob) x
    len(name)), with both bounded (values are at most 200 characters).
    """
    tokens = _parse_glob(glob)
    text = name.lower()
    t = i = 0
    star = -1
    mark = 0
    while i < len(text):
        if t < len(tokens) and tokens[t][0] == "star":
            star, mark = t, i
            t += 1
        elif t < len(tokens) and _token_matches(tokens[t], text[i]):
            t += 1
            i += 1
        elif star != -1:
            mark += 1
            i = mark
            t = star + 1
        else:
            return False
    while t < len(tokens) and tokens[t][0] == "star":
        t += 1
    return t == len(tokens)


def clean_action(
    kind: str,
    action: Mapping[str, Any] | None,
    *,
    readers: Collection[str] | None = None,
    user: bool = False,
) -> dict[str, Any]:
    """An extract rule's action, clean, or :class:`RuleError`; empty for content rules."""
    if kind != KIND_EXTRACT:
        if action:
            raise RuleError("Only an extract rule has an action; a content rule has a mode.")
        return {}
    action = dict(action or {"level": "default"})
    level = str(action.get("level") or "default")
    if level not in EXTRACT_LEVELS:
        raise RuleError(f"Choose what happens to the file: {', '.join(EXTRACT_LEVELS)}.")
    unknown = set(action) - {"level", "reader"}
    if unknown:
        raise RuleError(f"The action does not take: {', '.join(sorted(unknown))}.")
    clean: dict[str, Any] = {"level": level}
    reader = action.get("reader")
    if level == LEVEL_EXTRACT_WITH:
        reader = str(reader or DEFAULT_READER)
        if readers is not None and reader not in readers:
            raise RuleError(
                f"'{reader}' is not a reader you can use. Pick one of the OCR models offered."
                if user
                else f"'{reader}' is not a configured OCR model."
            )
        clean["reader"] = reader
    elif reader:
        raise RuleError("A reader goes with 'read with'.")
    return clean


def clean_overrides(raw: Any) -> dict[str, str]:
    """Per entity type modes: ``{TYPE: mode}``, types in the one alphabet placeholders use."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, Mapping):
        raise RuleError("Overrides are a mapping of entity type to mode.")
    if len(raw) > MAX_OVERRIDES:
        raise RuleError(f"A rule has at most {MAX_OVERRIDES} overrides.")
    out: dict[str, str] = {}
    for key, value in raw.items():
        try:
            name = user_rules.clean_label(str(key))
        except ValueError as exc:
            raise RuleError(f"An override names no entity type: {exc}") from exc
        mode = str(value)
        if mode not in MODES:
            raise RuleError(f"'{mode}' is not a mode. Use: {', '.join(MODES)}.")
        out[name] = mode
    return out


def _content_value(
    key: str, raw: Iterable[Any], *, classifier_ids: Collection[str] | None, user: bool
) -> list[str]:
    items = [str(v) for v in raw]
    if key == "finds":
        if items:
            raise RuleError(
                "'finds at least' counts what the rule's own policies find: it names none."
            )
        return []
    if len(items) != 1:
        raise RuleError("'classified' names one classifier.")
    if user:
        raise RuleError(
            "'classified as' sends the text to a model provider, so only administrators use it."
        )
    if classifier_ids is not None and items[0] not in classifier_ids:
        raise RuleError("The classifier chosen for 'classified as' does not exist.")
    return items


def validate_rule(
    kind: str,
    raw: Mapping[str, Any],
    *,
    classifier_ids: Collection[str] | None = None,
    policy_ids: Collection[str] | None = None,
    readers: Collection[str] | None = None,
    user: bool = False,
) -> dict[str, Any]:
    """A rule as clean JSON (with an id), or :class:`RuleError`.

    ``policy_ids`` are the policies the rule may name (for a user: the administrators'
    shared ones and their own), ``classifier_ids`` the classifiers. ``None``: not checked.
    """
    if kind not in KINDS:
        raise RuleError("Choose what the pipeline is for: extract or content.")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise RuleError("Give every rule a name.")
    if len(name) > _MAX_NAME:
        raise RuleError(f"A rule name is longer than {_MAX_NAME} characters.")
    policies: list[str] = []
    for item in raw.get("policies") or ():
        value = str(item)
        if policy_ids is not None and value not in policy_ids:
            raise RuleError(
                "A policy of this rule is not one you can use."
                if user
                else "A policy of this rule does not exist."
            )
        if value not in policies:
            policies.append(value)
    if kind == KIND_CONTENT and not policies:
        raise RuleError("A rule needs at least one policy: it is what the rule looks for.")
    mode = str(raw.get("mode") or ("off" if kind == KIND_EXTRACT else ""))
    if kind == KIND_CONTENT and not mode:
        raise RuleError(f"Choose what the rule does: {', '.join(MODES)}.")
    if mode not in MODES:
        raise RuleError(f"'{mode}' is not a mode. Use: {', '.join(MODES)}.")
    if kind == KIND_EXTRACT and mode == "block":
        raise RuleError("An extract rule blocks with its action; its mode is how it hides.")
    overrides = clean_overrides(raw.get("overrides"))
    if kind == KIND_EXTRACT and "block" in overrides.values():
        raise RuleError("An extract rule blocks with its action; an override cannot block.")
    if (overrides or mode != "off") and not policies:
        raise RuleError("A mode needs a policy to hide with.")
    match = str(raw.get("match") or "all")
    if match not in MATCHES:
        raise RuleError("'match' is all or any.")
    seen: set[tuple[str, str]] = set()
    conditions: list[dict[str, Any]] = []
    for item in raw.get("conditions") or []:
        key = str(item.get("key") or "")
        if kind == KIND_EXTRACT and key in CONTENT_KEYS:
            raise ContentOnReadError(CONTENT_ON_READ)
        if key not in CONDITION_KEYS[kind]:
            allowed = ", ".join(CONDITION_KEYS[kind])
            raise RuleError(
                f"'{key or '(none)'}' is not a condition for this kind. Use: {allowed}."
            )
        if key in BOOL_KEYS:
            if item.get("values"):
                raise RuleError(f"'{key}' is a yes or no: it takes no values.")
            values: list[str] = []
        elif key in CONTENT_KEYS:
            values = _content_value(
                key, item.get("values") or (), classifier_ids=classifier_ids, user=user
            )
        else:
            values = [_validate_value(key, v) for v in _clean_values(key, item.get("values") or ())]
        seen_key = (key, values[0] if key == "classified" else "")
        if seen_key in seen:
            raise RuleError(
                f"'{key}' is used twice; put the values in one condition."
                if key not in CONTENT_KEYS
                else "The same condition is used twice."
            )
        seen.add(seen_key)
        condition: dict[str, Any] = {
            "key": key,
            "values": values,
            "negate": bool(item.get("negate")),
        }
        if key == "finds":
            try:
                count = int(1 if item.get("count") is None else item["count"])
            except (TypeError, ValueError) as exc:
                raise RuleError("'finds at least' needs a number.") from exc
            if not 1 <= count <= MAX_COUNT:
                raise RuleError(f"'finds at least' takes a number from 1 to {MAX_COUNT}.")
            condition["count"] = count
        elif item.get("count") not in (None, 1):
            raise RuleError(f"'{key}' takes no count.")
        conditions.append(condition)
    if any(c["key"] == "finds" for c in conditions) and not policies:
        raise RuleError("'finds at least' needs the rule to have a policy.")
    rule_id = raw.get("id")
    try:
        ident = str(uuid.UUID(str(rule_id))) if rule_id else str(uuid.uuid4())
    except ValueError:
        ident = str(uuid.uuid4())
    enabled = bool(raw.get("enabled", True))
    out: dict[str, Any] = {
        "id": ident,
        "name": name,
        "enabled": enabled,
        "policies": policies,
        "match": match,
        "conditions": conditions,
        "mode": mode,
        "overrides": overrides,
    }
    if kind == KIND_EXTRACT:
        out["action"] = clean_action(kind, raw.get("action"), readers=readers, user=user)
    elif raw.get("action"):
        raise RuleError("Only an extract rule has an action; a content rule has a mode.")
    # A warning is read, not kept: once the rule is turned on, the note is gone.
    note = str(raw.get("note") or "").strip()[:500]
    if note and not enabled:
        out["note"] = note
    return out


# -- save-time checks --------------------------------------------------------------


def check(
    rules: Sequence[Rule], context: CheckContext | None = None
) -> dict[uuid.UUID, list[Issue]]:
    """Warnings for each rule of ONE pipeline, by id."""
    ctx = context or CheckContext()
    ordered = sorted(rules, key=lambda r: r.position)
    issues: dict[uuid.UUID, list[Issue]] = {r.id: [] for r in ordered}
    for rule in ordered:
        out = issues[rule.id]
        keys = {c.key for c in rule.conditions}
        if (
            rule.kind == KIND_EXTRACT
            and rule.enabled
            and rule.level in (LEVEL_BLOCK, LEVEL_EXTRACT_WITH)
            and keys
            and keys <= {"filename", "filetype"}
        ):
            out.append(
                Issue(
                    "convenience_only",
                    "A renamed file will not be caught. To protect content wherever it comes "
                    "from, add content pipelines with a 'finds at least' condition.",
                )
            )
        if (
            rule.level == LEVEL_EXTRACT_WITH
            and str(rule.action.get("reader") or DEFAULT_READER) in ctx.unavailable_readers
        ):
            out.append(
                Issue(
                    "unavailable_now",
                    f"The reader '{rule.action.get('reader') or DEFAULT_READER}' cannot be used "
                    "now (this deployment's extractor does not offer it); a file this rule "
                    "sends there is refused.",
                )
            )
        if rule.kind == KIND_EXTRACT and rule.enabled and keys & {"filetype", "filename"}:
            out.append(
                Issue(
                    "unnamed_matches",
                    "A file whose name or type cannot be told (a link with no extension, "
                    "no name sent) is treated as matching, so this rule applies to it.",
                )
            )
        for pid in rule.policies:
            info = ctx.content.get(pid)
            if info is None:
                out.append(
                    Issue(
                        "unavailable_now",
                        "A policy this rule names no longer exists; the request is refused "
                        "when the rule cannot be applied.",
                    )
                )
            elif not info.available:
                out.append(
                    Issue(
                        "unavailable_now",
                        f"{info.name} cannot be used now ({info.why or 'unavailable'}); "
                        "a protective rule then acts as if it matched, and any other is refused "
                        "(503).",
                    )
                )
        for condition in rule.conditions:
            if condition.key == "classified":
                info = ctx.content.get(condition.values[0]) if condition.values else None
                if info is None:
                    out.append(
                        Issue(
                            "unavailable_now",
                            "The classifier this condition names no longer exists; the request "
                            "is refused when the rule cannot be decided.",
                        )
                    )
                elif not info.available:
                    out.append(
                        Issue(
                            "unavailable_now",
                            f"{info.name} cannot be used now ({info.why or 'unavailable'}).",
                        )
                    )
        if rule.note:
            out.append(Issue("migrated", rule.note))
    if len(ordered) > SOFT_CAP:
        for rule in ordered:
            issues[rule.id].append(
                Issue(
                    "soft_cap",
                    f"More than {SOFT_CAP} rules in one pipeline is hard to read aloud; "
                    "consider merging some.",
                )
            )
    return issues


#: Warning codes that mean "look at this" without being a status of their own.
BADGE_CODES: tuple[str, ...] = ()


def filetype_of(mime: str | None, filename: str | None) -> str | None:
    """The coarse file type a ``filetype`` condition names, from MIME or extension."""
    mime = (mime or "").split(";")[0].strip().lower()
    ext = (filename or "").rsplit(".", 1)[-1].lower() if filename and "." in filename else ""
    if mime == "application/pdf" or ext == "pdf":
        return "pdf"
    if mime.startswith("image/") or ext in (
        "png",
        "jpg",
        "jpeg",
        "tif",
        "tiff",
        "bmp",
        "gif",
        "webp",
    ):
        return "image"
    if (
        "wordprocessingml" in mime
        or mime == "application/msword"
        or ext in ("doc", "docx", "odt", "rtf")
    ):
        return "word"
    if "spreadsheetml" in mime or "ms-excel" in mime or ext in ("xls", "xlsx", "ods", "csv"):
        return "excel"
    if "presentationml" in mime or "ms-powerpoint" in mime or ext in ("ppt", "pptx", "odp"):
        return "powerpoint"
    if mime.startswith("text/") or ext in ("txt", "md", "html", "htm", "json", "xml"):
        return "text"
    return None
