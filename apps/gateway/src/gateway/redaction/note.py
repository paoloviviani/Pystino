"""The system note that tells a model what a placeholder is.

A model that sees ``<IT_FISCAL_CODE_Y5RD4GZTEK>`` where a fiscal code belongs
will, left to itself, call it a malformed value, comment on how many characters
it has, and ask the user for the real one — the exact thing the redaction was
meant to make unnecessary. One short instruction removes that.

Added **when a rule is in force**, not when a request happened to match: the
text depends on the policy alone, so every turn of a conversation carries the
same bytes and the provider's prompt cache survives. It is appended to the end
of the system content, after the caller's own text, and goes onto the outbound
payload *after* redaction, so it is never itself screened and nothing downstream
counts it as the user's text.

The wording lives here, in one place.
"""

from __future__ import annotations

from typing import Any

from gateway.config import EntityMode, PlaceholderStyle, RedactionPolicy
from gateway.models import ApiSurface
from gateway.redaction.base import RedactionOutcome

#: The real values come back: tell the model to use them as if they were real.
#: ``{where}`` is where the restoration happens, which depends on whether tool
#: arguments are restored too.
RESTORABLE_NOTE = (
    "Values written like <LABEL_TOKEN> stand in for the user's real data, which is hidden "
    "from you for privacy. The real values are put back automatically {where}. "
    "Use them exactly as written wherever the real value belongs{including}. They are not "
    "the real values: do not validate them (format, check digits, plausibility), comment on "
    "their format or length, or ask the user for the real value."
)

#: The values stay hidden: tell the model not to try to see through them.
HIDDEN_NOTE = (
    "Values written like <LABEL_TOKEN> are hidden stand-ins for the user's "
    "real data, and they stay hidden. Use them exactly as written when you need them. "
    "They are not the real values: do not validate them (format, check digits, plausibility) "
    "and do not try to guess the real value."
)


#: Appended when a rule removes values outright, leaving only their label.
REDACT_NOTE = (
    "Values written as a bare <LABEL>, with no token, were removed entirely and cannot "
    "be told apart or recovered."
)

#: Appended when any stand-in was written as ``#TOKEN#``, with the length of the
#: value it replaced.
SAME_LENGTH_NOTE = (
    "A value written as #TOKEN# is a stand-in of the same kind. The whole token, both # "
    "signs included, is exactly as long as the real value. It is not the real value: do not "
    "check or comment on its format, letters, digits or check characters; use it exactly as "
    "written."
)


_ANONYMISING = (EntityMode.ANONYMISE_RESTORE, EntityMode.ANONYMISE, EntityMode.REDACT)


def system_note(
    policy: RedactionPolicy, *, restore_text: bool = True, restore_arguments: bool = True
) -> str | None:
    """The note the policy in force calls for, or None when it calls for none.

    A pure function of the **policy** and the restore settings, never of what a
    request happened to contain. Providers cache the start of a request, and the
    system prompt is the start: a note that appeared on the turns that matched
    something and vanished on the ones that did not would change the prefix
    from turn to turn and throw the cache away for the whole history. The same
    policy gives the same bytes on every turn.

    "In force" means at least one entity type, custom pattern or the default
    names an anonymising mode (``anonymise_restore``, ``anonymise``,
    ``redact``). Redaction off, or only ``off`` and ``block``, gets nothing.
    """
    modes = [entry.mode for entry in policy.service_types().values()]
    modes += [pattern.mode for pattern in policy.patterns]
    modes += [item.mode for item in policy.keywords]
    modes.append(policy.default_mode)
    if not any(mode in _ANONYMISING for mode in modes):
        return None
    restorable = restore_text and EntityMode.ANONYMISE_RESTORE in modes
    tokens = any(mode in (EntityMode.ANONYMISE_RESTORE, EntityMode.ANONYMISE) for mode in modes)
    if not tokens:
        # Only `redact`: there are no tokens to explain, just removed values.
        return REDACT_NOTE
    if restorable:
        text = RESTORABLE_NOTE.format(
            where="in your answer and in tool calls" if restore_arguments else "in your answer",
            including=", including in tool arguments and documents"
            if restore_arguments
            else ", including in documents",
        )
    else:
        text = HIDDEN_NOTE
    same_length = (
        any(
            entry.placeholder is PlaceholderStyle.SAME_LENGTH
            and entry.mode in (EntityMode.ANONYMISE_RESTORE, EntityMode.ANONYMISE)
            for entry in policy.service_types().values()
        )
        or any(
            pattern.placeholder is PlaceholderStyle.SAME_LENGTH
            and pattern.mode in (EntityMode.ANONYMISE_RESTORE, EntityMode.ANONYMISE)
            for pattern in policy.patterns
        )
        or any(
            item.placeholder is PlaceholderStyle.SAME_LENGTH
            and item.mode in (EntityMode.ANONYMISE_RESTORE, EntityMode.ANONYMISE)
            for item in policy.keywords
        )
    )
    if EntityMode.REDACT in modes:
        text = f"{text} {REDACT_NOTE}"
    if same_length:
        text = f"{text} {SAME_LENGTH_NOTE}"
    return text


def inject_system_note(
    payload: dict[str, Any], outcome: RedactionOutcome, *, surface: ApiSurface
) -> None:
    """Add the note to *payload*'s system prompt, in the surface's own way.

    Appended to the existing system prompt rather than sent as a second one:
    several chat templates refuse, or silently drop, a system message that is
    not the only one. The caller's own instructions come first, and on
    Anthropic Messages the note is a separate final block, so blocks the caller
    marked with ``cache_control`` are not touched.
    """
    note = outcome.note
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
