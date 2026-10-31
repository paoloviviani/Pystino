"""How long an uploaded audio file is.

Transcription is billed by the minute, so the gateway needs a duration, and it
has three sources of unequal quality. In order of preference:

1. **The provider's own figure** (``duration`` on a ``verbose_json`` reply,
   ``usage.seconds`` on a ``json`` one). It is what the provider will invoice,
   so it is what we bill. Not available for ``text``, ``srt`` or ``vtt``.
2. **Measured from the file.** Read from the container header by ``tinytag``
   (MIT, pure Python, no native code) and the standard library's ``wave``. Exact
   for the formats it understands, and available *before* the call, which is
   what lets the quota reservation be the real figure rather than a bound.
3. **Estimated from the size**, when the file is a format neither reader knows
   (or is damaged). Labelled ``estimated`` in the ledger, and the row's usage
   source says so. The estimate and the reservation bound deliberately differ:
   see the two constants.
"""

from __future__ import annotations

import io
import logging
import wave
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

#: Bytes per second assumed when billing a file whose duration could not be
#: read: 32 kbit/s, ordinary speech in MP3 or AAC. An estimate, labelled as one.
ESTIMATE_BYTES_PER_SECOND = 4_000
#: Bytes per second assumed when *reserving* quota for such a file: 8 kbit/s,
#: the thinnest speech codecs. A bound has to be pessimistic, and a file at that
#: rate is the longest the same bytes could plausibly be.
RESERVE_BYTES_PER_SECOND = 1_000

#: ``source`` values, as recorded on ``usage_records.audio_seconds_source``.
UPSTREAM = "upstream"
MEASURED = "measured"
ESTIMATED = "estimated"


@dataclass(frozen=True, slots=True)
class Duration:
    seconds: Decimal
    source: str


def measure(data: bytes) -> Decimal | None:
    """The duration of *data* in seconds, read from its header, or None."""
    try:
        with wave.open(io.BytesIO(data)) as wav:
            rate = wav.getframerate()
            if rate > 0:
                return _seconds(wav.getnframes() / rate)
    except (wave.Error, EOFError, ValueError):
        pass
    try:
        from tinytag import TinyTag

        tag = TinyTag.get(file_obj=io.BytesIO(data), tags=False, duration=True)
        if tag.duration:
            return _seconds(tag.duration)
    except Exception:  # unsupported or damaged: every parser failure is "unknown"
        logger.debug("could not read an audio duration", exc_info=True)
    return None


def estimate(size: int, *, bytes_per_second: int = ESTIMATE_BYTES_PER_SECOND) -> Decimal:
    return _seconds(size / bytes_per_second)


def reported(payload: dict[str, Any] | None) -> Decimal | None:
    """The duration a provider reported, from either place it puts it."""
    if not payload:
        return None
    candidates: list[Any] = [payload.get("duration")]
    usage = payload.get("usage")
    if isinstance(usage, dict) and usage.get("type") in (None, "duration"):
        candidates.append(usage.get("seconds"))
    for value in candidates:
        if isinstance(value, bool) or value is None:
            continue
        try:
            seconds = Decimal(str(value))
        except ArithmeticError:
            continue
        if seconds.is_finite() and seconds > 0:
            return seconds
    return None


def _seconds(value: float) -> Decimal:
    """Rounded to a millisecond, and never zero for a clip that has any length."""
    return max(Decimal("0.001"), Decimal(str(round(value, 3))))
