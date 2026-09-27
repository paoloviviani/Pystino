"""One normalisation for every address comparison the redesign makes (ADR 0093 §6.1).

Shared with Cerea: `normalize_email_vectors.json` (checked into both
repositories' test fixtures) is the contract, and both suites read the same
file. Python and TypeScript agree on every address that can take part in an
*automatic* trust decision — link-by-email, `OIDC_ADMIN_EMAIL`, the
bootstrap — because those require ASCII, and ASCII `casefold` and
`toLowerCase` never disagree. Outside ASCII the two diverge (`casefold`
expands `ß` to `ss`; `toLowerCase` does not), which is exactly why an
automatic trust decision never reaches that case.
"""

from __future__ import annotations

import unicodedata


def normalize_email(value: str) -> str:
    """NFKC, then strip, then casefold — in that order, always.

    NFKC first: a fullwidth or otherwise compatibility-equivalent character
    becomes its ordinary form before anything else looks at it, so a
    homoglyph address does not slip past casefold looking merely unusual
    (review R2). Strip before casefold: trimming after folding could change
    what counts as whitespace under some casefold expansion, however
    theoretical that is here.
    """
    return unicodedata.normalize("NFKC", value).strip().casefold()


def is_trusted_email(value: str) -> tuple[str, bool]:
    """The normalised address, and whether it may decide anything automatically.

    "Trusted" (ADR 0093 §6.1) means ASCII after normalisation *and* a
    syntactically plausible address: exactly one ``@``, a local part with no
    leading, trailing or doubled dot, and a domain with at least one dot.
    This is deliberately looser than the bundled Authelia's own validator
    (`gw/directory/authelia_users.py`, unchanged by this module) — that one
    guards what a console may write into a login file; this one guards
    whether an address may make a link, an admin grant or a bootstrap
    promotion, which is a different, narrower question asked in many more
    places.
    """
    normalized = normalize_email(value)
    if not normalized or not normalized.isascii():
        return normalized, False
    if normalized.count("@") != 1:
        return normalized, False
    local, _, domain = normalized.partition("@")
    if not local or not domain:
        return normalized, False
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return normalized, False
    if "." not in domain:
        return normalized, False
    return normalized, True
