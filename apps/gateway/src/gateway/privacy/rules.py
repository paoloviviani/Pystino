"""The rule vocabulary of a pipeline, its validation, and the save-time checks (ADR 0113).

A pipeline's rule is a *sentence*: conditions that must all hold, one action.
This module owns what each kind may say, so the admin API, the user API, the
test endpoint and the evaluator agree.

Conditions are request facts (file type, file name, scan) and two verdicts: "a
policy finds at least N items" and "classified as a classifier" (ADR 0113, section
0c). There is no quick check and no built-in topic.

Validation is split on purpose. **Errors** (:func:`validate_rule`) block saving:
an unknown key, a value of the wrong shape, an action the kind does not have, a
user's rule that would loosen something. **Warnings** (:func:`check`) never
block: a rule that can never fire, one a renamed file defeats, a policy whose
engine is down. A warning is a statement about the pipeline as it is now, so it can go
stale when another rule changes; the console asks for them again each time.

Scope (who, which target) is a pipeline's, not a rule's: see ``pipelines``.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

KIND_READ = "read"
KIND_EMBED = "embed"
KIND_CHAT = "chat"
KINDS = (KIND_READ, KIND_EMBED, KIND_CHAT)

#: Past this many rules a pipeline stops being readable aloud. A warning, not a limit.
SOFT_CAP = 20
#: A hard bound, because rules are held in memory on every worker.
MAX_RULES = 100

#: Which condition keys each kind offers. Reading decides from the request's facts about
#: the file only (its type, whether it is a scan, its name); what the text says is checked
#: when it is embedded or sent to chat.
CONDITION_KEYS: dict[str, tuple[str, ...]] = {
    KIND_READ: ("filetype", "scan", "filename"),
    KIND_EMBED: ("finds", "classified"),
    KIND_CHAT: ("finds", "classified"),
}
#: Keys that are a yes/no about the request: they carry no values, only ``negate``.
BOOL_KEYS = ("scan",)
#: Keys decided by reading the request's own text. Their one value is the id of the
#: policy (``finds``, with ``count``) or the classifier (``classified``).
CONTENT_KEYS = ("finds", "classified")
#: Most items a "finds at least N" may ask for.
MAX_COUNT = 1000

FILETYPES = ("pdf", "word", "excel", "powerpoint", "image", "text")
#: The reader a "read with" rule that names none means: the built-in extractor's
#: default-engine model (``gateway.extraction.DEFAULT_MODEL``). The built-in
#: extractor's other engines are models of their own (``markitdown-ocr``,
#: ``docling``), picked like any reader.
DEFAULT_READER = "markitdown"

#: The ladder, lowest to highest, per kind. The highest level across the
#: pipelines in scope wins.
LADDER: dict[str, tuple[str, ...]] = {
    KIND_READ: ("default", "read_with", "refuse"),
    KIND_EMBED: ("pass", "refuse"),
    KIND_CHAT: ("pass", "refuse"),
}
LEVEL_REFUSE = "refuse"
LEVEL_READ_WITH = "read_with"


def base_level(kind: str) -> str:
    """What happens anyway: the default reader, or pass."""
    return LADDER[kind][0]


_MAX_VALUES = 50
_MAX_VALUE_LENGTH = 200
_MAX_NAME = 120


class RuleError(ValueError):
    """A rule that cannot be saved. The message names the field and what is wrong."""


class ContentOnReadError(RuleError):
    """A content condition on a reading rule: answered as a 400, not a field error."""


CONTENT_ON_READ = (
    "Reading rules decide from the file, not its content; content is checked when the text "
    "is embedded or sent to chat."
)


@dataclass(frozen=True, slots=True)
class Condition:
    key: str
    values: tuple[str, ...]
    negate: bool = False
    #: ``finds`` only: the least number of distinct items that makes it true.
    count: int = 1


@dataclass(frozen=True, slots=True)
class Rule:
    """An immutable snapshot of one rule, as the request path holds it.

    ``action`` is ``{level, reader?, redact_with?}``. The pipeline's
    fixed last rule ("everything else") is a ``Rule`` with no conditions.
    """

    id: uuid.UUID
    kind: str
    name: str
    enabled: bool
    observe_only: bool
    position: int
    conditions: tuple[Condition, ...]
    action: Mapping[str, Any]
    #: A warning an administrator must read before turning the rule on (the
    #: migration leaves one on a rule whose condition no longer exists).
    note: str | None = None

    @property
    def level(self) -> str:
        return str(self.action.get("level") or base_level(self.kind))

    @property
    def is_noop(self) -> bool:
        """Adds nothing to what happens anyway."""
        return self.level == base_level(self.kind) and not self.action.get("redact_with")

    @property
    def protective(self) -> bool:
        """Whether the action protects (so it must not be skipped for want of a fact)."""
        return not self.is_noop


@dataclass(frozen=True, slots=True)
class Issue:
    """One save-time warning, in words an administrator can act on."""

    code: str
    message: str
    #: The other rule involved, for shadowing.
    rule_id: uuid.UUID | None = None
    rule_name: str | None = None


@dataclass(slots=True)
class ContentInfo:
    """What the checks need to know about a policy or a classifier a condition names."""

    name: str
    #: Whether what it needs is there now (the NER engine; the model).
    available: bool = True
    #: Why not, in words an administrator can act on.
    why: str = ""


@dataclass(slots=True)
class CheckContext:
    """Facts about the deployment the checks consult. All optional."""

    #: Policies and classifiers by id.
    content: Mapping[str, ContentInfo] = field(default_factory=dict)
    #: Names of readers (OCR models) this deployment cannot run now, for example the
    #: ``docling`` model when the extractor image has no Docling.
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
    out = {
        "id": str(rule.id),
        "name": rule.name,
        "enabled": rule.enabled,
        "observe_only": rule.observe_only,
        "conditions": [condition_to_json(c) for c in rule.conditions],
        "action": dict(rule.action),
    }
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
    action = dict(raw.get("action") or {})
    if action.get("level") not in LADDER[kind]:
        raise ValueError("the rule has no action")
    return Rule(
        id=uuid.UUID(str(raw["id"])),
        kind=kind,
        name=str(raw.get("name") or ""),
        enabled=bool(raw.get("enabled", True)),
        observe_only=bool(raw.get("observe_only", False)),
        position=position,
        conditions=tuple(condition_from_json(c) for c in raw.get("conditions") or []),
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
    action: Mapping[str, Any],
    *,
    policy_ids: Collection[str] | None = None,
    readers: Collection[str] | None = None,
    user: bool = False,
    otherwise: bool = False,
) -> dict[str, Any]:
    """The clean action of a rule, or :class:`RuleError`.

    ``policy_ids`` and ``readers`` are what may be named (``None``: not checked).
    For a user (``user=True``) only actions that tighten are accepted: read with a
    reader, refuse, redact with a policy. A user's rule that adds
    nothing is refused, except the fixed last rule (``otherwise``), which may.
    """
    ladder = LADDER[kind]
    level = str(action.get("level") or "")
    if level not in ladder:
        raise RuleError(f"Choose where it is handled: {', '.join(ladder)}.")
    unknown = set(action) - {"level", "reader", "redact_with"}
    if unknown:
        raise RuleError(f"The action does not take: {', '.join(sorted(unknown))}.")
    clean: dict[str, Any] = {"level": level}
    reader = action.get("reader")
    if level == LEVEL_READ_WITH:
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
    policy = action.get("redact_with")
    if policy:
        if level == LEVEL_REFUSE:
            raise RuleError("A refused request is not handled further, so no policy applies.")
        if policy_ids is not None and str(policy) not in policy_ids:
            raise RuleError("The policy chosen for 'redact with' does not exist.")
        clean["redact_with"] = str(policy)
    if user and not otherwise and level == base_level(kind) and not clean.get("redact_with"):
        raise RuleError(
            "A rule of yours has to add protection: read with a reader, refuse, or redact with a "
            "policy."
        )
    return clean


def _content_value(
    key: str,
    raw: Iterable[Any],
    *,
    finds_policy_ids: Collection[str] | None,
    classifier_ids: Collection[str] | None,
    user: bool,
) -> list[str]:
    """The one id a ``finds`` or ``classified`` condition names, if it may be named."""
    items = [str(v) for v in raw]
    if len(items) != 1:
        raise RuleError(f"'{key}' names one {'policy' if key == 'finds' else 'classifier'}.")
    value = items[0]
    if key == "classified":
        if user:
            raise RuleError(
                "'classified as' sends the text to a model provider, so only administrators use it."
            )
        if classifier_ids is not None and value not in classifier_ids:
            raise RuleError("The classifier chosen for 'classified as' does not exist.")
    elif finds_policy_ids is not None and value not in finds_policy_ids:
        raise RuleError(
            "The policy chosen for 'finds at least' is not one you can use."
            if user
            else "The policy chosen for 'finds at least' does not exist."
        )
    return [value]


def validate_rule(
    kind: str,
    raw: Mapping[str, Any],
    *,
    finds_policy_ids: Collection[str] | None = None,
    classifier_ids: Collection[str] | None = None,
    policy_ids: Collection[str] | None = None,
    readers: Collection[str] | None = None,
    user: bool = False,
) -> dict[str, Any]:
    """A rule as clean JSON (with an id), or :class:`RuleError`.

    ``finds_policy_ids`` are the policies a "finds" condition may name (for a user:
    the administrators' shared ones and their own), ``classifier_ids`` the
    classifiers. ``None`` means not checked.
    """
    if kind not in KINDS:
        raise RuleError("Choose what the pipeline is for: reading, embedding or chat.")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise RuleError("Give every rule a name.")
    if len(name) > _MAX_NAME:
        raise RuleError(f"A rule name is longer than {_MAX_NAME} characters.")
    seen: set[tuple[str, str]] = set()
    conditions: list[dict[str, Any]] = []
    for item in raw.get("conditions") or []:
        key = str(item.get("key") or "")
        if kind == KIND_READ and key in CONTENT_KEYS:
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
                key,
                item.get("values") or (),
                finds_policy_ids=finds_policy_ids,
                classifier_ids=classifier_ids,
                user=user,
            )
        else:
            values = [_validate_value(key, v) for v in _clean_values(key, item.get("values") or ())]
        seen_key = (key, values[0] if key in CONTENT_KEYS else "")
        if seen_key in seen:
            raise RuleError(
                f"'{key}' is used twice; put the values in one condition."
                if key not in CONTENT_KEYS
                else f"The same {'policy' if key == 'finds' else 'classifier'} is used twice."
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
        "observe_only": bool(raw.get("observe_only", False)),
        "conditions": conditions,
        "action": clean_action(
            kind, raw.get("action") or {}, policy_ids=policy_ids, readers=readers, user=user
        ),
    }
    # A warning is read, not kept: once the rule is turned on, the note is gone.
    note = str(raw.get("note") or "").strip()[:500]
    if note and not enabled:
        out["note"] = note
    return out


# -- save-time checks --------------------------------------------------------------


def _implied_by(rule_c: Condition, earlier_c: Condition) -> bool:
    """Whether every request satisfying *rule_c* also satisfies *earlier_c*.

    Conservative: ``False`` whenever unsure, because the result is shown as
    "never fires", and a false claim there would send an administrator to delete
    a rule that works.
    """
    if rule_c.key != earlier_c.key or rule_c.negate != earlier_c.negate:
        return False
    if rule_c.key in BOOL_KEYS:
        return True
    if rule_c.key == "finds":
        # At least N of the same policy: more required implies fewer; for "not at
        # least N", fewer required implies more.
        if rule_c.values != earlier_c.values:
            return False
        return rule_c.count <= earlier_c.count if rule_c.negate else rule_c.count >= earlier_c.count
    mine, theirs = set(rule_c.values), set(earlier_c.values)
    if rule_c.key == "filename":
        if "*" in theirs:
            return not rule_c.negate
        mine_l, theirs_l = {v.lower() for v in mine}, {v.lower() for v in theirs}
        subset = mine_l <= theirs_l
    else:
        subset = mine <= theirs
    # not-in-V: a smaller excluded set is the wider condition.
    return (theirs <= mine) if rule_c.negate else subset


def _overlaps(rule_c: Condition, earlier_c: Condition) -> bool:
    return (
        rule_c.key == earlier_c.key
        and rule_c.key != "finds"
        and not rule_c.negate
        and not earlier_c.negate
        and bool(set(rule_c.values) & set(earlier_c.values))
    )


def covers(earlier: Rule, rule: Rule) -> bool:
    """Whether *earlier* catches everything *rule* would (so *rule* never fires)."""
    # No conditions on *earlier* means it matches every request: all([]) is True.
    return all(any(_implied_by(rc, ec) for rc in rule.conditions) for ec in earlier.conditions)


def partly_covers(earlier: Rule, rule: Rule) -> bool:
    """Whether *earlier* certainly catches some, but not all, of what *rule* would."""
    if covers(earlier, rule):
        return False
    overlapping = False
    for ec in earlier.conditions:
        if any(_implied_by(rc, ec) for rc in rule.conditions):
            continue
        if any(_overlaps(rc, ec) for rc in rule.conditions):
            overlapping = True
            continue
        return False
    return overlapping


def shadowing(rules: Sequence[Rule]) -> dict[uuid.UUID, tuple[str, Rule]]:
    """For each rule in order: ``("never"|"partly", the earlier rule)``.

    Only enabled, non-observe-only rules shadow: an observe-only rule takes no
    action and lets the search continue, so it never hides a later one.
    """
    found: dict[uuid.UUID, tuple[str, Rule]] = {}
    consuming: list[Rule] = []
    for rule in rules:
        if rule.enabled:
            for earlier in consuming:
                if covers(earlier, rule):
                    found[rule.id] = ("never", earlier)
                    break
                if rule.id not in found and partly_covers(earlier, rule):
                    found[rule.id] = ("partly", earlier)
            if not rule.observe_only:
                consuming.append(rule)
    return found


def check(
    rules: Sequence[Rule], otherwise: Rule | None = None, context: CheckContext | None = None
) -> dict[uuid.UUID, list[Issue]]:
    """Warnings for each rule of ONE pipeline, by id (the last rule included)."""
    ctx = context or CheckContext()
    ordered = sorted(rules, key=lambda r: r.position)
    everything = [*ordered, *([otherwise] if otherwise is not None else [])]
    issues: dict[uuid.UUID, list[Issue]] = {r.id: [] for r in everything}
    for rule_id, (how, earlier) in shadowing(ordered).items():
        issues[rule_id].append(
            Issue(
                "never_fires" if how == "never" else "partly_shadowed",
                (
                    f"'{earlier.name}' above already catches everything this rule does."
                    if how == "never"
                    else f"'{earlier.name}' above catches some of what this rule does."
                ),
                rule_id=earlier.id,
                rule_name=earlier.name,
            )
        )
    for rule in everything:
        out = issues[rule.id]
        is_otherwise = rule is otherwise
        if not rule.conditions and not is_otherwise:
            out.append(
                Issue("catches_all", "This rule has no conditions, so it matches every request.")
            )
        keys = {c.key for c in rule.conditions}
        live = rule.enabled and not rule.observe_only
        if (
            rule.kind == KIND_READ
            and live
            and rule.protective
            and rule.level in (LEVEL_REFUSE, LEVEL_READ_WITH)
            and not is_otherwise
            and keys
            and keys <= {"filename", "filetype"}
        ):
            out.append(
                Issue(
                    "convenience_only",
                    "A renamed file will not be caught. To protect content wherever it comes "
                    "from, add embed and chat pipelines with a 'finds at least' condition.",
                )
            )
        if (
            rule.level == LEVEL_READ_WITH
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
        if rule.kind == KIND_READ and live and keys & {"filetype", "filename"}:
            out.append(
                Issue(
                    "unnamed_matches",
                    "A file whose name or type cannot be told (a link with no extension, "
                    "no name sent) is treated as matching, so this rule applies to it.",
                )
            )
        for condition in rule.conditions:
            if condition.key not in CONTENT_KEYS:
                continue
            info = ctx.content.get(condition.values[0]) if condition.values else None
            if info is None:
                out.append(
                    Issue(
                        "unavailable_now",
                        "The policy or classifier this condition names no longer exists; "
                        "the request is refused when the rule cannot be decided.",
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
BADGE_CODES = ("never_fires", "partly_shadowed")


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
