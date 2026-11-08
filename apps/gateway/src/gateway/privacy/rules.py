"""The routing rule vocabulary, its validation, and the save-time checks (ADR 0113).

A rule is a *sentence*: a kind of request, conditions that must all hold, one
action. This module owns what each kind may say, so the admin API, the test
endpoint and the evaluator agree, and so a later kind (chat, phase 2) widens a
table here and nothing else.

Validation is split on purpose. **Errors** (:func:`validate`) block saving: an
unknown key, a value of the wrong shape, an action the kind does not have.
**Warnings** (:func:`check`) never block: a rule that can never fire, one a
renamed file defeats, a profile that is down. A warning is a statement about the
list as it is now, so it can go stale when another rule changes; the console
asks for them again each time.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

KIND_READ = "read"
KIND_EMBED = "embed"
KIND_CHAT = "chat"
KINDS = (KIND_READ, KIND_EMBED, KIND_CHAT)
#: Kinds an administrator can create rules for today. Chat arrives in phase 2;
#: the column and the evaluator already carry it.
CREATABLE_KINDS = (KIND_READ, KIND_EMBED)

#: Past this many rules a list stops being readable aloud. A warning, not a limit.
SOFT_CAP = 20

PATTERNS = "patterns"

#: Which condition keys each kind offers.
CONDITION_KEYS: dict[str, tuple[str, ...]] = {
    KIND_READ: ("filetype", "filename", "who"),
    KIND_EMBED: ("who", "target", "verdict", "lang"),
    KIND_CHAT: ("who", "target", "verdict", "lang"),
}
#: Keys decided by reading the request's own text, which carry a ``decide``.
CONTENT_KEYS = ("verdict", "lang")

FILETYPES = ("pdf", "word", "excel", "powerpoint", "image", "text", "scan")
VERDICTS = ("health", "ids")
READERS = ("markitdown", "ocr", "docling")

#: Actions per kind: type -> allowed extra fields.
ACTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    KIND_READ: {"reader": ("reader",), "refuse": ()},
    KIND_EMBED: {"refuse": (), "local": ("model",)},
    KIND_CHAT: {"refuse": ()},
}
#: Actions that protect (refuse, keep on this server) as opposed to merely
#: choosing a reader: the ones that fail closed when something they need is down.
PROTECTIVE = {"refuse", "local"}

_LANG = re.compile(r"^[a-z]{2,3}$")
_MAX_VALUES = 50
_MAX_VALUE_LENGTH = 200
_MAX_NAME = 120


class RuleError(ValueError):
    """A rule that cannot be saved. The message names the field and what is wrong."""


@dataclass(frozen=True, slots=True)
class Condition:
    key: str
    values: tuple[str, ...]
    negate: bool = False
    #: ``patterns`` or a detector profile id; only on content conditions.
    decide: str | None = None


@dataclass(frozen=True, slots=True)
class Rule:
    """An immutable snapshot of one rule, as the request path holds it."""

    id: uuid.UUID
    kind: str
    name: str
    enabled: bool
    observe_only: bool
    position: int
    conditions: tuple[Condition, ...]
    action: Mapping[str, Any]

    @property
    def action_type(self) -> str:
        return str(self.action.get("type") or "")

    @property
    def protective(self) -> bool:
        return self.action_type in PROTECTIVE


@dataclass(frozen=True, slots=True)
class Issue:
    """One save-time warning, in words an administrator can act on."""

    code: str
    message: str
    #: The other rule involved, for shadowing.
    rule_id: uuid.UUID | None = None
    rule_name: str | None = None


@dataclass(slots=True)
class ProfileInfo:
    """What the checks need to know about a detector profile."""

    id: uuid.UUID
    name: str
    available: bool
    fallback: str = "refuse"
    can_decide: bool = True


@dataclass(slots=True)
class CheckContext:
    """Facts about the deployment the checks consult. All optional."""

    profiles: Mapping[str, ProfileInfo] = field(default_factory=dict)
    #: Names of readers this deployment cannot run now (for example docling).
    unavailable_readers: Sequence[str] = ()
    has_local_embedding: bool = True
    #: Names of the models that are local right now (active, deployment-owned, on
    #: an `is_local` provider). None: not known, so a named model is not checked.
    local_models: Collection[str] | None = None


def condition_from_json(raw: Mapping[str, Any]) -> Condition:
    return Condition(
        key=str(raw.get("key") or ""),
        values=tuple(str(v) for v in raw.get("values") or ()),
        negate=bool(raw.get("negate", False)),
        decide=(str(raw["decide"]) if raw.get("decide") else None),
    )


def condition_to_json(condition: Condition) -> dict[str, Any]:
    out: dict[str, Any] = {
        "key": condition.key,
        "values": list(condition.values),
        "negate": condition.negate,
    }
    if condition.decide is not None:
        out["decide"] = condition.decide
    return out


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


def _validate_value(kind: str, key: str, value: str) -> str:
    if key == "filetype":
        value = value.lower()
        if value not in FILETYPES:
            raise RuleError(f"File type '{value}' is not one of: {', '.join(FILETYPES)}.")
    elif key == "filename":
        if not value.strip("*?[]"):
            raise RuleError("A file name pattern needs at least one ordinary character.")
    elif key == "who":
        prefix, _, rest = value.partition(":")
        if prefix not in ("user", "group") or not rest.strip():
            raise RuleError(f"'{value}' must look like 'user:name@example.org' or 'group:Name'.")
        value = f"{prefix}:{rest.strip()}"
        if prefix == "user":
            value = f"user:{rest.strip().lower()}"
    elif key == "target":
        if value.lower() in ("local", "cloud"):
            return value.lower()
        prefix, _, rest = value.partition(":")
        if prefix not in ("provider", "model") or not rest.strip():
            raise RuleError(
                f"'{value}' must be 'local', 'cloud', 'provider:<name>' or 'model:<name>'."
            )
        value = f"{prefix}:{rest.strip()}"
    elif key == "verdict":
        value = value.lower()
        if value not in VERDICTS:
            raise RuleError(f"Verdict '{value}' is not one of: {', '.join(VERDICTS)}.")
    elif key == "lang":
        value = value.lower()
        if not _LANG.match(value):
            raise RuleError(f"'{value}' is not a language code such as 'it' or 'en'.")
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


def validate(
    kind: str,
    name: str,
    conditions: Sequence[Mapping[str, Any]],
    action: Mapping[str, Any],
    *,
    profile_ids: Iterable[str] = (),
) -> tuple[str, list[Condition], dict[str, Any]]:
    """The clean name, conditions and action, or :class:`RuleError`."""
    if kind not in KINDS:
        raise RuleError("Choose what the rule is for: reading a file, or embedding.")
    if kind not in CREATABLE_KINDS:
        raise RuleError("Chat rules are not available yet; reading and embedding are.")
    name = (name or "").strip()
    if not name:
        raise RuleError("Give the rule a name.")
    if len(name) > _MAX_NAME:
        raise RuleError(f"The name is longer than {_MAX_NAME} characters.")

    known_profiles = set(profile_ids)
    seen: set[str] = set()
    clean: list[Condition] = []
    for raw in conditions:
        key = str(raw.get("key") or "")
        if key not in CONDITION_KEYS[kind]:
            allowed = ", ".join(CONDITION_KEYS[kind])
            raise RuleError(
                f"'{key or '(none)'}' is not a condition for this kind. Use: {allowed}."
            )
        if key in seen:
            raise RuleError(f"'{key}' is used twice; put the values in one condition.")
        seen.add(key)
        values = _clean_values(key, raw.get("values") or ())
        checked = [_validate_value(kind, key, v) for v in values]
        decide = raw.get("decide")
        if key in CONTENT_KEYS:
            decide = str(decide) if decide else PATTERNS
            if decide != PATTERNS and decide not in known_profiles:
                raise RuleError("The detector profile chosen for this condition does not exist.")
        elif decide:
            raise RuleError(f"'{key}' is read from the request itself; it has no 'how to decide'.")
        else:
            decide = None
        clean.append(
            Condition(key=key, values=tuple(checked), negate=bool(raw.get("negate")), decide=decide)
        )

    kind_actions = ACTIONS[kind]
    action_type = str(action.get("type") or "")
    if action_type not in kind_actions:
        raise RuleError(
            f"A {kind} rule needs an action: {', '.join(kind_actions)}."
            if action_type == ""
            else f"'{action_type}' is not an action for this kind. Use: {', '.join(kind_actions)}."
        )
    unknown = set(action) - {"type", *kind_actions[action_type]}
    if unknown:
        raise RuleError(f"The action does not take: {', '.join(sorted(unknown))}.")
    clean_action: dict[str, Any] = {"type": action_type}
    if action_type == "reader":
        reader = str(action.get("reader") or "")
        if reader not in READERS:
            raise RuleError(f"Choose a reader: {', '.join(READERS)}.")
        clean_action["reader"] = reader
    if action_type == "local" and action.get("model"):
        clean_action["model"] = str(action["model"]).strip()[:200]
    return name, clean, clean_action


# -- save-time checks --------------------------------------------------------------


def _implied_by(rule_c: Condition, earlier_c: Condition) -> bool:
    """Whether every request satisfying *rule_c* also satisfies *earlier_c*.

    Conservative: ``False`` whenever unsure, because the result is shown as
    "never fires", and a false claim there would send an administrator to delete
    a rule that works.
    """
    if rule_c.key != earlier_c.key or rule_c.negate != earlier_c.negate:
        return False
    if rule_c.key in CONTENT_KEYS and rule_c.decide != earlier_c.decide:
        return False
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
        and rule_c.decide == earlier_c.decide
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
    """For each rule of one kind in order: ``("never"|"partly", the earlier rule)``.

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
    rules: Sequence[Rule], context: CheckContext | None = None
) -> dict[uuid.UUID, list[Issue]]:
    """Warnings for every rule, by id, for the list as given (all kinds)."""
    ctx = context or CheckContext()
    issues: dict[uuid.UUID, list[Issue]] = {r.id: [] for r in rules}
    by_kind: dict[str, list[Rule]] = {}
    for rule in rules:
        by_kind.setdefault(rule.kind, []).append(rule)
    for kind_rules in by_kind.values():
        ordered = sorted(kind_rules, key=lambda r: r.position)
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
    for rule in rules:
        out = issues[rule.id]
        if not rule.conditions:
            out.append(
                Issue("catches_all", "This rule has no conditions, so it matches every request.")
            )
        keys = {c.key for c in rule.conditions}
        if (
            rule.kind == KIND_READ
            and rule.enabled
            and not rule.observe_only
            and rule.protective
            and keys <= {"filename", "filetype"}
        ):
            out.append(
                Issue(
                    "convenience_only",
                    "A renamed file will not be caught. To protect content wherever it comes "
                    "from, add embedding rules that use 'contains health data'.",
                )
            )
        if rule.action_type == "reader" and rule.action.get("reader") in ctx.unavailable_readers:
            out.append(
                Issue(
                    "unavailable_now",
                    f"The {rule.action.get('reader')} reader is not installed here; "
                    "files fall back to the default reader.",
                )
            )
        if rule.action_type == "local" and not ctx.has_local_embedding:
            out.append(
                Issue(
                    "unavailable_now",
                    "No local embedding provider is set up, so this rule refuses instead.",
                )
            )
        named = rule.action.get("model")
        if (
            rule.action_type == "local"
            and named
            and ctx.local_models is not None
            and named not in ctx.local_models
        ):
            out.append(
                Issue(
                    "unavailable_now",
                    f"'{named}' is not a local embedding model now (switched off, or its "
                    "provider is no longer marked local), so requests are refused.",
                )
            )
        if (
            rule.kind == KIND_READ
            and rule.enabled
            and not rule.observe_only
            and any(c.key in ("filetype", "filename") for c in rule.conditions)
        ):
            out.append(
                Issue(
                    "unnamed_matches",
                    "A file whose name or type cannot be told (a link with no extension, "
                    "no name sent) is treated as matching, so this rule applies to it.",
                )
            )
        for condition in rule.conditions:
            if condition.key not in CONTENT_KEYS or condition.decide in (None, PATTERNS):
                continue
            profile = ctx.profiles.get(condition.decide or "")
            if profile is None or not profile.available or not profile.can_decide:
                name = profile.name if profile else "The chosen profile"
                out.append(
                    Issue(
                        "unavailable_now",
                        f"{name} is not available now; "
                        + (
                            "the profile's fallback decides instead."
                            if profile is not None and profile.fallback == "fallback_to"
                            else "the rule takes its protective action."
                        ),
                    )
                )
            if profile is not None and profile.fallback == "fallback_to":
                out.append(
                    Issue(
                        "weakens",
                        f"If {profile.name} is unavailable, a weaker method decides and the "
                        "log says so.",
                    )
                )
    for kind_rules in by_kind.values():
        if len(kind_rules) > SOFT_CAP:
            for rule in kind_rules:
                issues[rule.id].append(
                    Issue(
                        "soft_cap",
                        f"More than {SOFT_CAP} rules for this kind is hard to read aloud; "
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
