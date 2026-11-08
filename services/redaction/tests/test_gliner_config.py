"""Configuration parsing for the opt-in GLiNER engine (no model is loaded)."""

import pytest
from llmp_redaction.app import DEFAULT_GLINER_LABELS, _gliner_labels, _nlp_engine_name


def test_gliner_is_an_accepted_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDACTION_NLP_ENGINE", "gliner")
    assert _nlp_engine_name() == "gliner"


def test_default_labels_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDACTION_GLINER_LABELS", raising=False)
    assert _gliner_labels() == DEFAULT_GLINER_LABELS


def test_labels_with_and_without_explicit_entity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "REDACTION_GLINER_LABELS", "person=person, blood type ,hospital=organization"
    )
    assert _gliner_labels() == {
        "person": "PERSON",
        "blood type": "BLOOD_TYPE",
        "hospital": "ORGANIZATION",
    }
