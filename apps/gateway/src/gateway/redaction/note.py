"""The system note that tells a model what a placeholder is.

A model that sees ``<IT_FISCAL_CODE_Y5RD4GZTEK>`` where a fiscal code belongs
will, left to itself, call it a malformed value, comment on how many characters
it has, and ask the user for the real one — the exact thing the redaction was
meant to make unnecessary. One short instruction removes that.

Added **only when the request was actually rewritten**: a policy that exists but
matched nothing leaves the prompt exactly as the caller sent it. It is added to
the outbound payload *after* redaction, so it is never itself screened, and it
is never part of what the caller sent, so nothing downstream counts it as the
user's text.

The wording lives here, in one place.
"""

from __future__ import annotations

from typing import Any

from gateway.models import ApiSurface
from gateway.redaction.base import RedactionOutcome

#: The real values come back: tell the model to use them as if they were real.
#: ``{where}`` is where the restoration happens, which depends on whether tool
#: arguments are restored too.
RESTORABLE_NOTE = (
    "Values written like <LABEL_TOKEN> stand in for the user's real data, which is hidden "
    "from you for privacy. The real values are put back automatically {where}. "
    "Use them exactly as written wherever the real value belongs{including}, and do not "
    "comment on their format or length or ask the user for the real value."
)

#: The values stay hidden: tell the model not to try to see through them.
HIDDEN_NOTE = (
    "Values written like <LABEL_TOKEN> or <LABEL> are hidden stand-ins for the user's "
    "real data, and they stay hidden. Use them exactly as written when you need them, "
    "and do not try to guess the real value."
)


def system_note(outcome: RedactionOutcome) -> str | None:
    """The note for this request, or None when nothing was substituted."""
    restorable = outcome.restorable_substitutions if outcome.restore_text else 0
    hidden = outcome.hidden_substitutions + (
        0 if outcome.restore_text else outcome.restorable_substitutions
    )
    if restorable + hidden == 0:
        return None
    if hidden:
        # Mixed, or none restorable: the safe statement is the one that is true
        # of every placeholder in the request.
        text = HIDDEN_NOTE
    else:
        tools = outcome.restore_arguments
        text = RESTORABLE_NOTE.format(
            where="in your answer and in tool calls" if tools else "in your answer",
            including=", including in tool arguments and documents"
            if tools
            else ", including in documents",
        )
    return text


def inject_system_note(
    payload: dict[str, Any], outcome: RedactionOutcome, *, surface: ApiSurface
) -> None:
    """Add the note to *payload*'s system prompt, in the surface's own way.

    Appended to the existing system prompt rather than sent as a second one:
    several chat templates refuse, or silently drop, a system message that is
    not the only one. The caller's own instructions come first.
    """
    note = system_note(outcome)
    if note is None:
        return
    if surface is ApiSurface.RESPONSES:
        existing = payload.get("instructions")
        payload["instructions"] = f"{existing}\n\n{note}" if isinstance(existing, str) else note
    elif surface is ApiSurface.MESSAGES:
        _into_anthropic_system(payload, note)
    else:
        _into_chat_system(payload, note)


def _into_anthropic_system(payload: dict[str, Any], note: str) -> None:
    system = payload.get("system")
    if isinstance(system, str) and system:
        payload["system"] = f"{system}\n\n{note}"
    elif isinstance(system, list):
        payload["system"] = [*system, {"type": "text", "text": note}]
    else:
        payload["system"] = note


def _into_chat_system(payload: dict[str, Any], note: str) -> None:
    messages = list(payload.get("messages") or [])
    for position, message in enumerate(messages):
        if message.get("role") not in ("system", "developer"):
            continue
        copied = dict(message)
        content = copied.get("content")
        if isinstance(content, str):
            copied["content"] = f"{content}\n\n{note}" if content else note
        elif isinstance(content, list):
            copied["content"] = [*content, {"type": "text", "text": note}]
        else:
            copied["content"] = note
        messages[position] = copied
        payload["messages"] = messages
        return
    payload["messages"] = [{"role": "system", "content": note}, *messages]
