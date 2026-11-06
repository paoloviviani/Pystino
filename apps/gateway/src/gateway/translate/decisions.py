"""Decision dialects: ``decisions`` and ``systemone``, translated both ways.

The decision counterpart of ``anthropic_chat``: a caller sends one of two
request forms and the provider speaks one of two dialects (``DecisionSupport.
dialect``, the way a chat provider declares ``protocol``). When they match the
body is forwarded untouched; when they differ it is translated here, and the
answer is translated back into the caller's form, so any caller can use any
decision provider.

* ``decisions`` (Cortecs): ``input`` (a string, or messages) and ``questions``
  an array of ``predicate``, ``choice`` (``choices``) and ``score``
  (``levels``) questions; ``answers`` is an array in question order.
* ``systemone`` (Melious): ``state`` and ``questions`` an object keyed by the
  caller's ids, with ``noul`` (a probability in [0, 1]), ``choice`` (``criteria``
  keyed by option) and ``score`` (``criteria`` an ordered array) questions;
  ``answers`` is an object keyed by the same ids.

Mapping: predicate is noul; choice is a choice whose ``choices`` become the
``criteria`` keys; score is a score whose ordered ``levels`` become the
``criteria`` array; message input is joined into one ``state`` text (and a
non-string ``state`` is serialised to JSON for ``input``). What has no
counterpart is a 400 naming it: ``preference`` (a routing hint) and any other
question type when the target is ``systemone``; any type but noul, choice and
score when the target is ``decisions``. The question's other fields
(``instructions`` and whatever the provider adds) travel as written.

Redaction and metering are untouched: this runs after redaction on the
caller's body, usage is read from the provider's own answer before it is
translated back, and the reservation counts the caller's text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from gateway.errors import BadRequestError
from gateway.plugins.base import DIALECT_DECISIONS, DIALECT_SYSTEMONE

#: What System One accepts as a question id. A name outside it (or a repeat) is
#: replaced by ``q<index>`` upstream and restored on the way back.
_KEY = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

_SHARED = ("type", "name", "choices", "levels", "criteria")


@dataclass(slots=True)
class Plan:
    """What the answer needs to come back: one entry per question, in order."""

    source: str
    items: list[dict[str, Any]] = field(default_factory=list)


def _refuse(message: str) -> BadRequestError:
    return BadRequestError(message, code="invalid_decisions_request")


def _label(item: Any, *, keys: tuple[str, ...], fallback: str) -> Any:
    if isinstance(item, dict):
        for key in keys:
            if item.get(key) not in (None, ""):
                return item[key]
    elif item is not None:
        return item
    return fallback


# -- request: decisions -> systemone -------------------------------------------


def _state_of(value: Any) -> Any:
    """The ``input`` form's text as System One's ``state``.

    A bare string stays. Messages (content a string or ``input_text`` parts)
    are joined: one message is its text, several are ``role: text`` lines, so a
    conversation keeps who said what.
    """
    if not isinstance(value, list):
        return value
    lines: list[tuple[str, str]] = []
    for message in value:
        content = message.get("content") if isinstance(message, dict) else message
        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        lines.append(
            (
                str(message.get("role", "user")) if isinstance(message, dict) else "user",
                str(content),
            )
        )
    if len(lines) == 1:
        return lines[0][1]
    return "\n".join(f"{role}: {text}" for role, text in lines)


def _decisions_to_systemone(body: dict[str, Any]) -> tuple[dict[str, Any], Plan]:
    questions = body.get("questions")
    if not isinstance(questions, list) or not questions:
        raise _refuse("'questions' must be a non-empty array when 'input' is used.")
    if body.get("preference") is not None:
        raise _refuse(
            "'preference' is a routing hint of the decisions dialect and has no counterpart "
            "on this provider, which speaks System One. Remove it from the request."
        )
    wire: dict[str, Any] = {}
    plan = Plan(source=DIALECT_DECISIONS)
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            raise _refuse(f"questions[{index}] must be an object.")
        kind = question.get("type")
        name = question.get("name")
        key = name if isinstance(name, str) and _KEY.match(name) and name not in wire else None
        key = key or f"q{index}"
        while key in wire:
            key += "_"
        rest = {k: v for k, v in question.items() if k not in _SHARED}
        item: dict[str, Any] = {"key": key, "name": name, "type": kind}
        if kind == "predicate":
            wire[key] = {"type": "noul", **rest}
        elif kind == "choice":
            choices = question.get("choices")
            if not isinstance(choices, list) or len(choices) < 2:
                raise _refuse(f"questions[{index}]: a choice needs at least two 'choices'.")
            criteria: dict[str, Any] = {}
            values: dict[str, Any] = {}
            for choice in choices:
                value = choice.get("value") if isinstance(choice, dict) else choice
                label = str(value)
                criteria[label] = _label(choice, keys=("description", "label"), fallback=label)
                values[label] = value
            item["values"] = values
            wire[key] = {"type": "choice", **rest, "criteria": criteria}
        elif kind == "score":
            levels = question.get("levels")
            if not isinstance(levels, list) or len(levels) < 2:
                raise _refuse(f"questions[{index}]: a score needs at least two 'levels'.")
            labels = [
                _label(level, keys=("label", "description", "value"), fallback=str(position))
                for position, level in enumerate(levels)
            ]
            item["labels"] = labels
            wire[key] = {"type": "score", **rest, "criteria": labels}
        else:
            raise _refuse(
                f"questions[{index}]: type {kind!r} has no counterpart on this provider, "
                "which speaks System One (predicate, choice and score are served)."
            )
        plan.items.append(item)
    out: dict[str, Any] = {
        "model": body["model"],
        "state": _state_of(body["input"]),
        "questions": wire,
    }
    if body.get("images") is not None:
        out["images"] = body["images"]
    return out, plan


def _systemone_answers_to_decisions(payload: dict[str, Any], plan: Plan) -> dict[str, Any]:
    """System One's keyed answers as the decisions form's array, in question order."""
    answers = payload.get("answers")
    answers = answers if isinstance(answers, dict) else {}
    out: list[dict[str, Any]] = []
    for item in plan.items:
        raw = answers.get(item["key"])
        if not isinstance(raw, dict):
            continue
        answer: dict[str, Any] = {"type": item["type"], "name": item["name"]}
        if raw.get("type") == "refusal" or "refusal" in raw:
            answer = {**raw, "name": item["name"]}
        elif item["type"] == "predicate":
            answer["probability"] = raw.get("noul")
        elif item["type"] == "choice":
            chosen = raw.get("choice")
            answer["choice"] = item["values"].get(chosen, chosen)
            answer["probabilities"] = [
                {"value": item["values"].get(label, label), "probability": probability}
                for label, probability in (raw.get("probabilities") or {}).items()
            ]
            answer["confidence"] = raw.get("confidence")
        else:
            answer["score"] = raw.get("score")
            labels = item["labels"]
            answer["probabilities"] = [
                {
                    "value": int(position),
                    "label": labels[int(position)] if int(position) < len(labels) else None,
                    "probability": probability,
                }
                for position, probability in sorted(
                    (raw.get("probabilities") or {}).items(), key=lambda pair: int(pair[0])
                )
            ]
            answer["confidence"] = raw.get("confidence")
        out.append(answer)
    return {**{k: v for k, v in payload.items() if k != "answers"}, "answers": out}


# -- request: systemone -> decisions -------------------------------------------


def _input_of(state: Any) -> str:
    """System One's ``state`` (a string, object or array) as the ``input`` text."""
    if isinstance(state, str):
        return state
    return json.dumps(state, ensure_ascii=False)


def _systemone_to_decisions(body: dict[str, Any]) -> tuple[dict[str, Any], Plan]:
    questions = body.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise _refuse(
            "'questions' must be a non-empty object keyed by question id when 'state' is used."
        )
    wire: list[dict[str, Any]] = []
    plan = Plan(source=DIALECT_SYSTEMONE)
    for key, question in questions.items():
        if not isinstance(question, dict):
            raise _refuse(f"questions[{key!r}] must be an object.")
        kind = question.get("type")
        rest = {k: v for k, v in question.items() if k not in _SHARED}
        item: dict[str, Any] = {"key": key, "type": kind}
        if kind == "noul":
            wire.append({"type": "predicate", "name": key, **rest})
        elif kind == "choice":
            criteria = question.get("criteria")
            if isinstance(criteria, dict):
                pairs = list(criteria.items())
            elif isinstance(criteria, list):
                pairs = [(str(c), str(c)) for c in criteria]
            else:
                pairs = []
            if len(pairs) < 2:
                raise _refuse(f"questions[{key!r}]: a choice needs at least two 'criteria'.")
            choices = [
                {"value": value}
                if description == value
                else {"value": value, "description": description}
                for value, description in pairs
            ]
            wire.append({"type": "choice", "name": key, **rest, "choices": choices})
        elif kind == "score":
            criteria = question.get("criteria")
            if not isinstance(criteria, list) or len(criteria) < 2:
                raise _refuse(
                    f"questions[{key!r}]: a score needs 'criteria' as an ordered array of "
                    "at least two labels to be served by this provider."
                )
            item["labels"] = [str(label) for label in criteria]
            wire.append(
                {"type": "score", "name": key, **rest, "levels": [{"label": c} for c in criteria]}
            )
        else:
            raise _refuse(
                f"questions[{key!r}]: type {kind!r} has no counterpart on this provider, "
                "which speaks the decisions dialect (noul, choice and score are served)."
            )
        plan.items.append(item)
    out: dict[str, Any] = {
        "model": body["model"],
        "input": _input_of(body["state"]),
        "questions": wire,
    }
    if body.get("images") is not None:
        out["images"] = body["images"]
    return out, plan


def _decisions_answers_to_systemone(payload: dict[str, Any], plan: Plan) -> dict[str, Any]:
    """The decisions form's answer array as System One's object keyed by question id."""
    raw_answers = payload.get("answers")
    raw_answers = raw_answers if isinstance(raw_answers, list) else []
    by_name = {a.get("name"): a for a in raw_answers if isinstance(a, dict) and a.get("name")}
    out: dict[str, Any] = {}
    for position, item in enumerate(plan.items):
        raw = by_name.get(item["key"])
        if raw is None and position < len(raw_answers) and isinstance(raw_answers[position], dict):
            raw = raw_answers[position]
        if raw is None:
            continue
        if raw.get("type") == "refusal" or "refusal" in raw:
            out[item["key"]] = {k: v for k, v in raw.items() if k != "name"}
        elif item["type"] == "noul":
            out[item["key"]] = {"type": "noul", "noul": raw.get("probability")}
        elif item["type"] == "choice":
            out[item["key"]] = {
                "type": "choice",
                "choice": None if raw.get("choice") is None else str(raw["choice"]),
                "probabilities": {
                    str(p.get("value")): p.get("probability")
                    for p in raw.get("probabilities") or []
                },
                "confidence": raw.get("confidence"),
            }
        else:
            out[item["key"]] = {
                "type": "score",
                "score": raw.get("score"),
                "probabilities": {
                    str(p.get("value")): p.get("probability")
                    for p in raw.get("probabilities") or []
                },
                "confidence": raw.get("confidence"),
                "legend": {str(i): label for i, label in enumerate(item["labels"])},
            }
    return {**{k: v for k, v in payload.items() if k != "answers"}, "answers": out}


# -- the seam ------------------------------------------------------------------


def translate_request(
    body: dict[str, Any], *, source: str, target: str
) -> tuple[dict[str, Any], Plan | None]:
    """*body* (in the caller's dialect) for a provider speaking *target*.

    Unchanged, with no plan, when the dialects match. Raises ``BadRequestError``
    (400) naming whatever the target cannot express.
    """
    if source == target:
        return body, None
    if source == DIALECT_DECISIONS:
        return _decisions_to_systemone(body)
    return _systemone_to_decisions(body)


def translate_response(payload: dict[str, Any], plan: Plan | None) -> dict[str, Any]:
    """The provider's answer in the caller's dialect (unchanged without a plan)."""
    if plan is None:
        return payload
    if plan.source == DIALECT_DECISIONS:
        return _systemone_answers_to_decisions(payload, plan)
    return _decisions_answers_to_systemone(payload, plan)
