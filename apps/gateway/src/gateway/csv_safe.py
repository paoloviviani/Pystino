"""One escaper for every text cell the gateway writes into a CSV.

A cell a spreadsheet reads as a formula runs when an admin opens the export.
Names, labels and tags are chosen by members or come from an identity provider,
so every free-text cell goes through :func:`csv_text`. Numbers never do: a real
negative number must stay a number, so the writers escape text fields only.
"""

from __future__ import annotations

_TRIGGERS = frozenset("=+-@\t\r")


def csv_text(value: str | None) -> str:
    """A text cell that a spreadsheet will not evaluate (OWASP CSV injection).

    A leading apostrophe makes the cell text. Leading whitespace followed by a
    trigger is caught too, since some importers strip it before parsing.
    """
    if not value:
        return ""
    if value.lstrip(" ")[:1] in _TRIGGERS:
        return "'" + value
    return value
