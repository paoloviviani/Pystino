"""Deployment presets: what ADR 0076's profiles became.

A preset is data — compose profiles to switch on and `.env` values to write —
never topology. The overlay list the old profiles derived is gone because
compose profiles select services directly; what survives is the named answer
to "which features for my size".

`satellite` and `generic` (Cerea without a local gateway, ADR 0082) wait on the
report's decision D9.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    name: str
    summary: str
    profiles: tuple[str, ...]
    values: tuple[tuple[str, str], ...]


_COMMON_CHAT = (
    ("CHAT_APP_NAME", "Cerea"),
    ("CHAT_CODE_TOOL_ENABLED", "true"),
    ("CHAT_CONSOLE_ENABLED", "true"),
    ("CHAT_KNOWLEDGE_ENABLED", "true"),
    ("CHAT_MEMORY_ENABLED", "true"),
)

PRESETS: dict[str, Preset] = {
    "homelab": Preset(
        name="homelab",
        summary="gateway + chat, no ledger or quotas, no redaction service",
        profiles=("gateway", "chat"),
        values=(
            ("GATEWAY_ACCOUNTING__ENABLED", "false"),
            ("GATEWAY_QUOTA__ENABLED", "false"),
            ("GATEWAY_REDACTION__ENGINE", "noop"),
            ("FETCH_BACKEND", "direct"),
            # No ledger, nothing for the Usage tab to read (ADR 0065).
            ("CHAT_USAGE_ENABLED", ""),
            *_COMMON_CHAT,
        ),
    ),
    "team": Preset(
        name="team",
        summary="homelab + ledger, quotas and pattern-only redaction",
        profiles=("gateway", "chat", "redaction"),
        values=(
            ("GATEWAY_ACCOUNTING__ENABLED", "true"),
            ("GATEWAY_QUOTA__ENABLED", "true"),
            ("GATEWAY_REDACTION__ENGINE", "http"),
            ("GATEWAY_EXTRACTOR__ENDPOINT", "http://extractor:8080"),
            ("REDACTION_NLP_ENGINE", "disabled"),
            ("REDACTION_LANGUAGE", "en"),
            ("FETCH_BACKEND", "direct"),
            ("CHAT_USAGE_ENABLED", "true"),
            *_COMMON_CHAT,
        ),
    ),
    "enterprise": Preset(
        name="enterprise",
        summary="team + NER redaction (built locally) and the headless-browser fetch",
        profiles=("gateway", "chat", "redaction", "fetch"),
        values=(
            ("GATEWAY_ACCOUNTING__ENABLED", "true"),
            ("GATEWAY_QUOTA__ENABLED", "true"),
            ("GATEWAY_REDACTION__ENGINE", "http"),
            ("GATEWAY_EXTRACTOR__ENDPOINT", "http://extractor:8080"),
            # NER needs the -ner redaction image, which is built locally
            # (en_core_web_lg is CC BY-NC-SA 3.0 and is not published; D6).
            ("REDACTION_NLP_ENGINE", "spacy"),
            ("SPACY_MODELS", "en_core_web_lg"),
            ("REDACTION_LANGUAGE", "en"),
            ("FETCH_BACKEND", "playwright"),
            ("CHAT_USAGE_ENABLED", "true"),
            *_COMMON_CHAT,
        ),
    ),
}


def get(name: str) -> Preset:
    try:
        return PRESETS[name]
    except KeyError:
        raise ValueError(f"unknown preset {name!r}; choose one of {', '.join(PRESETS)}") from None
