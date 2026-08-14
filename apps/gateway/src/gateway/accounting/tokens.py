"""Local token estimation — the fallback that stops accounting reporting zero.

This exists because of a specific, observed failure mode. Some OpenAI-compatible
upstreams emit the token usage of a stream in a trailing frame *after* the
``finish_reason`` frame, and intermediaries that stop reading at
``finish_reason`` drop it. LiteLLM does exactly this for vLLM backends
(BerriAI/litellm#25389, closed as not-planned). Other providers ignore
``stream_options.include_usage`` altogether.

When that happens the honest options are to record zero or to estimate. Zero is
worse than an estimate: it is indistinguishable from a free request, it silently
understates a group's spend, and nobody notices until an invoice arrives. So the
gateway estimates, and stamps the row ``usage_source='estimated'`` so that every
report can separate measured spend from inferred spend.

The estimate is deliberately simple and has no model-specific tokeniser. A real
BPE count would need the upstream's exact vocabulary, which we do not have for an
arbitrary configurable provider, and a wrong tokeniser produces confidently wrong
numbers rather than obviously approximate ones.
"""

from __future__ import annotations

import math
import re
from typing import Any, Protocol

# Characters that are usually one token each rather than part of a ~4-character
# token: CJK ideographs, kana, Hangul. Latin text averages close to 4 characters
# per token; CJK is closer to 1, so treating everything as chars/4 would
# understate a Chinese prompt by roughly a factor of four.
_DENSE_CHAR_RE = re.compile(
    r"[぀-ヿ"  # hiragana, katakana
    r"㐀-䶿"  # CJK ext A
    r"一-鿿"  # CJK unified
    r"豈-﫿"  # CJK compatibility
    r"가-힯"  # Hangul syllables
    r"]"
)

# Rough per-message framing overhead (role tokens and separators) in the
# ChatML-style encodings every OpenAI-compatible provider uses.
_PER_MESSAGE_OVERHEAD = 4
_REPLY_PRIMING_OVERHEAD = 3

_CHARS_PER_TOKEN = 4


class TokenEstimator(Protocol):
    def count_text(self, text: str) -> int: ...

    def count_messages(self, messages: list[dict[str, Any]]) -> int: ...


class HeuristicTokenEstimator:
    """Character-class heuristic. Approximate by construction, never zero."""

    def count_text(self, text: str) -> int:
        if not text:
            return 0
        dense = len(_DENSE_CHAR_RE.findall(text))
        sparse = len(text) - dense
        return dense + math.ceil(sparse / _CHARS_PER_TOKEN)

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        total = _REPLY_PRIMING_OVERHEAD
        for message in messages:
            total += _PER_MESSAGE_OVERHEAD
            total += self.count_text(str(message.get("role") or ""))
            total += self._count_content(message.get("content"))
            # Tool calls carry their arguments as JSON strings, which are charged
            # like any other input text.
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                if isinstance(function, dict):
                    total += self.count_text(str(function.get("name") or ""))
                    total += self.count_text(str(function.get("arguments") or ""))
            if name := message.get("name"):
                total += self.count_text(str(name))
        return total

    def _count_content(self, content: Any) -> int:
        """Handle both plain strings and the multimodal parts array."""
        if content is None:
            return 0
        if isinstance(content, str):
            return self.count_text(content)
        if isinstance(content, list):
            total = 0
            for part in content:
                if isinstance(part, str):
                    total += self.count_text(part)
                elif isinstance(part, dict) and (text := part.get("text")):
                    total += self.count_text(str(text))
                    # Images cost tokens as a function of resolution, which we
                    # cannot see from a URL. Left uncounted, and the row is
                    # already labelled as an estimate.
            return total
        return self.count_text(str(content))


DEFAULT_ESTIMATOR: TokenEstimator = HeuristicTokenEstimator()
