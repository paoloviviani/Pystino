"""`normalize_email` / `is_trusted_email` against the shared vectors (ADR 0093 §6.1).

The fixture is shared byte-for-byte with Cerea's own copy; a change here that
is not also made there is exactly the drift §6.1 exists to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gateway.email_normalize import is_trusted_email, normalize_email

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "normalize_email_vectors.json").read_text()
)["vectors"]


@pytest.mark.parametrize("vector", VECTORS, ids=[v["input"] or "(empty)" for v in VECTORS])
def test_shared_vectors(vector: dict[str, object]) -> None:
    normalized, trusted = is_trusted_email(vector["input"])  # type: ignore[arg-type]
    assert trusted is vector["trusted"]
    if vector["normalized"] is not None:
        assert normalized == vector["normalized"]
        assert normalize_email(vector["input"]) == vector["normalized"]  # type: ignore[arg-type]


def test_a_non_ascii_result_is_never_trusted() -> None:
    _, trusted = is_trusted_email("héllo@example.org")
    assert trusted is False
