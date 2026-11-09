"""Two NER engines in one process, picked per request (ADR 0113 section 0c).

No weights are needed: GLiNER is a fake model with the one method Presidio's
recogniser calls, and the detector-level tests use analyzer doubles.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, ClassVar

import pytest
from llmp_redaction import gliner_engine
from llmp_redaction.app import build_detector
from llmp_redaction.detector import Detector, EngineSlot
from llmp_redaction.gliner_engine import CUSTOM_LABELS
from test_detector import FakeAnalyzer, Result, serving

TEXT = "Mario Rossi lavora al progetto Aurora e prende metformina."


class FakeGliner:
    """The slice of ``gliner.GLiNER`` Presidio's recogniser uses."""

    GOLD: ClassVar[dict[str, str]] = {
        "person": "Mario Rossi",
        "project code name": "Aurora",
        "medication": "metformina",
    }

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def predict_entities(self, text: str, labels: list[str], **_: Any) -> list[dict[str, Any]]:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.02)
        self.calls.append(list(labels))
        out = []
        for label in labels:
            needle = self.GOLD.get(label)
            start = text.find(needle) if needle else -1
            if start != -1 and needle:
                out.append(
                    {"label": label, "start": start, "end": start + len(needle), "score": 0.9}
                )
        with self._lock:
            self.active -= 1
        return out


class GlinerAnalyzer(FakeAnalyzer):
    """A GLiNER-slot analyzer that honours the per-request labels like the real recogniser."""

    def analyze(self, text: str, language: str, **kwargs: Any) -> list[Result]:
        results = super().analyze(text, language, **kwargs)
        extra = CUSTOM_LABELS.get() or {}
        for label, entity in extra.items():
            needle = FakeGliner.GOLD.get(label)
            start = text.find(needle) if needle else -1
            if start != -1 and needle:
                results.append(Result(start, start + len(needle), entity, 0.8))
        return results


def two_engine_detector(*, default: str = "spacy", with_gliner: bool = True) -> Detector:
    spacy = FakeAnalyzer({"Mario Rossi": ("PERSON", 0.85), "a@b.it": ("EMAIL_ADDRESS", 1.0)})
    gliner_analyzer = GlinerAnalyzer(
        {
            "Mario Rossi": ("PERSON", 0.95),
            "metformina": ("MEDICATION", 0.9),
            "a@b.it": ("EMAIL_ADDRESS", 1.0),
        }
    )
    slot = EngineSlot(
        name="gliner",
        analyzer=gliner_analyzer,
        languages=["en", "it"],
        models={"en": "g", "it": "g"},
        ner_entities=["MEDICATION", "PERSON"],
        custom_labels=True,
        ms_per_token=9.5,
    )
    return Detector(
        spacy,
        languages=["en", "it"],
        models={"en": "m", "it": "m"},
        engine="fake",
        engine_version="0.0.1",
        gliner=slot if with_gliner else None,
        default_engine=default,  # type: ignore[arg-type]
    )


class TestHealthz:
    async def test_lists_both_engines_with_the_contract_shape(self) -> None:
        async with serving(two_engine_detector()) as client:
            body = (await client.get("/healthz")).json()
        engines = {e["name"]: e for e in body["ner_engines"]}
        assert list(engines) == ["spacy", "gliner"]
        for engine in engines.values():
            assert set(engine) == {
                "name",
                "available",
                "default",
                "entities",
                "custom_labels",
                "labels",
                "ms_per_token",
                "languages",
            }
        assert engines["spacy"]["default"] is True
        assert engines["gliner"]["default"] is False
        assert engines["spacy"]["custom_labels"] is False
        assert engines["gliner"]["custom_labels"] is True
        assert engines["gliner"]["available"] is True
        assert engines["gliner"]["entities"] == ["MEDICATION", "PERSON"]
        assert engines["gliner"]["ms_per_token"] == 9.5
        assert engines["gliner"]["languages"] == ["en", "it"]
        # Existing fields are untouched.
        assert body["family_partition"] is True
        assert "pattern_entities" in body

    async def test_an_image_without_gliner_lists_it_unavailable(self) -> None:
        async with serving(two_engine_detector(with_gliner=False)) as client:
            body = (await client.get("/healthz")).json()
        gliner = body["ner_engines"][1]
        assert gliner["name"] == "gliner"
        assert gliner["available"] is False
        assert gliner["custom_labels"] is True
        assert gliner["entities"] == []
        assert body["ner_engines"][0]["available"] is True

    async def test_the_default_follows_the_configuration(self) -> None:
        async with serving(two_engine_detector(default="gliner")) as client:
            body = (await client.get("/healthz")).json()
        assert [e["default"] for e in body["ner_engines"]] == [False, True]
        # The top-level fields describe the default engine.
        assert "MEDICATION" in body["entities"]


class TestEnginePerRequest:
    async def test_null_engine_is_the_default_and_matches_a_spacy_only_service(self) -> None:
        async with (
            serving(two_engine_detector()) as both,
            serving(two_engine_detector(with_gliner=False)) as only_spacy,
        ):
            payload = {"texts": [TEXT, "write a@b.it"]}
            with_gliner = await both.post("/detect", json=payload)
            without = await only_spacy.post("/detect", json=payload)
            explicit_null = await both.post("/detect", json={**payload, "ner_engine": None})
        assert with_gliner.content == without.content == explicit_null.content

    async def test_the_request_picks_the_engine(self) -> None:
        async with serving(two_engine_detector()) as client:
            spacy = (
                await client.post("/detect", json={"texts": [TEXT], "ner_engine": "spacy"})
            ).json()
            gliner = (
                await client.post("/detect", json={"texts": [TEXT], "ner_engine": "gliner"})
            ).json()
        spacy_spans = spacy["findings"][0]["spans"]
        gliner_spans = gliner["findings"][0]["spans"]
        assert [(s["entity_type"], s["score"], s["engine"]) for s in spacy_spans] == [
            ("PERSON", 0.85, "spacy")
        ]
        assert [(s["entity_type"], s["score"], s["engine"]) for s in gliner_spans] == [
            ("PERSON", 0.95, "gliner"),
            ("MEDICATION", 0.9, "gliner"),
        ]

    async def test_pattern_hits_carry_the_pattern_engine_on_both(self) -> None:
        for name in ("spacy", "gliner"):
            async with serving(two_engine_detector()) as client:
                body = (
                    await client.post("/detect", json={"texts": ["a@b.it"], "ner_engine": name})
                ).json()
            span = body["findings"][0]["spans"][0]
            assert (span["entity_type"], span["engine"]) == ("EMAIL_ADDRESS", "pattern")

    async def test_the_default_can_be_gliner(self) -> None:
        async with serving(two_engine_detector(default="gliner")) as client:
            body = (await client.post("/detect", json={"texts": [TEXT]})).json()
        assert body["findings"][0]["spans"][0]["engine"] == "gliner"

    @pytest.mark.parametrize("default", ["spacy", "gliner"])
    async def test_an_engine_that_is_not_there_is_409_never_a_fallback(self, default: str) -> None:
        detector = two_engine_detector(default="spacy", with_gliner=False)
        async with serving(detector) as client:
            response = await client.post("/detect", json={"texts": [TEXT], "ner_engine": "gliner"})
        assert response.status_code == 409
        assert response.json() == {"detail": {"code": "engine_unavailable", "engine": "gliner"}}

    async def test_spacy_without_models_is_409_when_named_but_the_default_still_serves(
        self,
    ) -> None:
        detector = Detector(
            FakeAnalyzer({"a@b.it": ("EMAIL_ADDRESS", 1.0)}), languages=["en"], models={}
        )
        async with serving(detector) as client:
            named = await client.post("/detect", json={"texts": ["a@b.it"], "ner_engine": "spacy"})
            default = await client.post("/detect", json={"texts": ["a@b.it"]})
        assert named.status_code == 409
        assert named.json()["detail"]["engine"] == "spacy"
        assert default.status_code == 200
        assert default.json()["findings"][0]["spans"]

    async def test_an_unknown_engine_name_is_a_validation_error(self) -> None:
        async with serving(two_engine_detector()) as client:
            response = await client.post("/detect", json={"texts": ["x"], "ner_engine": "stanza"})
        assert response.status_code == 422


class TestCustomLabels:
    CUSTOM: ClassVar[list[dict[str, str]]] = [
        {"entity": "PROJECT_NAME", "label": "project code name"}
    ]

    async def test_gliner_honours_them(self) -> None:
        async with serving(two_engine_detector()) as client:
            body = (
                await client.post(
                    "/detect",
                    json={"texts": [TEXT], "ner_engine": "gliner", "custom_labels": self.CUSTOM},
                )
            ).json()
        spans = {s["entity_type"]: s for s in body["findings"][0]["spans"]}
        assert TEXT[spans["PROJECT_NAME"]["start"] : spans["PROJECT_NAME"]["end"]] == "Aurora"
        assert spans["PROJECT_NAME"]["engine"] == "pattern"  # the double has no recogniser name
        assert body["unsupported_types"] == []

    async def test_spacy_ignores_them_and_says_so(self) -> None:
        async with serving(two_engine_detector()) as client:
            body = (
                await client.post(
                    "/detect",
                    json={"texts": [TEXT], "ner_engine": "spacy", "custom_labels": self.CUSTOM},
                )
            ).json()
        assert [s["entity_type"] for s in body["findings"][0]["spans"]] == ["PERSON"]
        assert body["unsupported_types"] == ["PROJECT_NAME"]

    async def test_they_are_left_out_when_the_request_names_other_types(self) -> None:
        async with serving(two_engine_detector()) as client:
            body = (
                await client.post(
                    "/detect",
                    json={
                        "texts": [TEXT],
                        "ner_engine": "gliner",
                        "entity_types": ["PERSON"],
                        "custom_labels": self.CUSTOM,
                    },
                )
            ).json()
        assert [s["entity_type"] for s in body["findings"][0]["spans"]] == ["PERSON"]

    async def test_a_request_naming_only_a_custom_type_gets_only_that(self) -> None:
        async with serving(two_engine_detector()) as client:
            body = (
                await client.post(
                    "/detect",
                    json={
                        "texts": [TEXT],
                        "ner_engine": "gliner",
                        "entity_types": ["PROJECT_NAME"],
                        "custom_labels": self.CUSTOM,
                    },
                )
            ).json()
        assert [s["entity_type"] for s in body["findings"][0]["spans"]] == ["PROJECT_NAME"]

    async def test_they_need_the_ner_family(self) -> None:
        async with serving(two_engine_detector()) as client:
            body = (
                await client.post(
                    "/detect",
                    json={
                        "texts": [TEXT],
                        "ner_engine": "gliner",
                        "presidio_ner": False,
                        "custom_labels": self.CUSTOM,
                    },
                )
            ).json()
        assert body["unsupported_types"] == ["PROJECT_NAME"]
        assert all(s["entity_type"] != "PROJECT_NAME" for s in body["findings"][0]["spans"])

    @pytest.mark.parametrize(
        "labels",
        [
            [{"entity": f"T{n}", "label": f"l{n}"} for n in range(31)],
            [{"entity": "X", "label": "l" * 81}],
            [{"entity": "X" * 65, "label": "l"}],
            [{"entity": "", "label": "l"}],
        ],
    )
    async def test_the_limits_are_enforced(self, labels: list[dict[str, str]]) -> None:
        async with serving(two_engine_detector()) as client:
            response = await client.post(
                "/detect", json={"texts": ["x"], "ner_engine": "gliner", "custom_labels": labels}
            )
        assert response.status_code == 422

    async def test_the_limits_allow_the_maximum(self) -> None:
        labels = [{"entity": "E" * 64, "label": "l" * 80}] * 30
        async with serving(two_engine_detector()) as client:
            response = await client.post(
                "/detect", json={"texts": ["x"], "ner_engine": "gliner", "custom_labels": labels}
            )
        assert response.status_code == 200


def _presidio_gliner_analyzer(model: FakeGliner) -> Any:
    """The real Presidio analyzer with the real recogniser class and a fake model."""
    pytest.importorskip("presidio_analyzer", reason="presidio not installed")
    from llmp_redaction.app import _assemble_analyzer, _no_op_engine

    recognizer = gliner_engine.make_recognizer_class()(
        supported_language="it",
        entity_mapping=gliner_engine.configured_labels(),
        threshold=0.5,
        map_location="cpu",
        shared_model=model,
    )
    return _assemble_analyzer(
        ["it"], _no_op_engine(["it"]), no_ner=True, gliner_recognizers=[recognizer]
    )


class TestRealRecogniserWithAFakeModel:
    def test_custom_labels_reach_the_model_for_that_request_only(self) -> None:
        model = FakeGliner()
        analyzer = _presidio_gliner_analyzer(model)
        slot = EngineSlot(
            name="gliner",
            analyzer=analyzer,
            languages=["it"],
            ner_entities=sorted(set(gliner_engine.configured_labels().values())),
            custom_labels=True,
        )
        detector = Detector(FakeAnalyzer(), languages=["it"], models={"it": "m"}, gliner=slot)
        extra = {"project code name": "PROJECT_NAME"}
        for entity_types in (None, ["PERSON", "PROJECT_NAME"], ["PROJECT_NAME"]):
            model.calls.clear()
            spans = detector.analyse_one(
                TEXT,
                language="it",
                score_threshold=0.5,
                entity_types=entity_types,
                engine="gliner",
                custom_labels=extra,
            )
            found = {s.entity_type for s in spans}
            assert "PROJECT_NAME" in found, entity_types
            assert all(s.engine in {"gliner", "pattern"} for s in spans)
            if entity_types == ["PROJECT_NAME"]:
                assert found == {"PROJECT_NAME"}
            assert any("project code name" in labels for labels in model.calls)
        # The next request, with no custom labels, neither asks for nor returns them.
        model.calls.clear()
        spans = detector.analyse_one(
            TEXT, language="it", score_threshold=0.5, entity_types=None, engine="gliner"
        )
        assert "PROJECT_NAME" not in {s.entity_type for s in spans}
        assert all("project code name" not in labels for labels in model.calls)
        gliner_spans = [s for s in spans if s.engine == "gliner"]
        assert {s.entity_type for s in gliner_spans} == {"PERSON", "MEDICATION"}
        assert all(0.0 < s.score <= 1.0 for s in gliner_spans)

    async def test_gliner_inference_runs_one_request_at_a_time(self) -> None:
        model = FakeGliner()
        slot = EngineSlot(
            name="gliner",
            analyzer=_presidio_gliner_analyzer(model),
            languages=["it"],
            custom_labels=True,
        )
        detector = Detector(FakeAnalyzer(), languages=["it"], models={"it": "m"}, gliner=slot)
        await asyncio.gather(
            *[
                detector.analyse(
                    [TEXT], language="it", score_threshold=0.5, entity_types=None, engine="gliner"
                )
                for _ in range(4)
            ]
        )
        assert model.calls
        assert model.max_active == 1


class TestBuild:
    def test_an_image_without_gliner_builds_with_it_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("presidio_analyzer", reason="presidio not installed")
        monkeypatch.setenv("REDACTION_NLP_ENGINE", "disabled")
        monkeypatch.setenv("REDACTION_SPACY_MODELS", "")
        monkeypatch.delenv("REDACTION_GLINER_MODEL", raising=False)
        monkeypatch.delenv("REDACTION_NER_DEFAULT", raising=False)
        detector = build_detector()
        assert [(s.name, s.available) for s in detector.slots] == [
            ("spacy", False),
            ("gliner", False),
        ]

    def test_the_default_cannot_be_gliner_without_the_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("presidio_analyzer", reason="presidio not installed")
        monkeypatch.setenv("REDACTION_NLP_ENGINE", "disabled")
        monkeypatch.setenv("REDACTION_SPACY_MODELS", "")
        monkeypatch.delenv("REDACTION_GLINER_MODEL", raising=False)
        monkeypatch.setenv("REDACTION_NER_DEFAULT", "gliner")
        with pytest.raises(ValueError, match="no GLiNER model"):
            build_detector()

    def test_a_bad_default_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from llmp_redaction.app import _ner_default

        monkeypatch.setenv("REDACTION_NER_DEFAULT", "stanza")
        with pytest.raises(ValueError, match="REDACTION_NER_DEFAULT"):
            _ner_default()

    def test_gliner_loads_at_startup_when_the_image_has_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("presidio_analyzer", reason="presidio not installed")
        monkeypatch.setenv("REDACTION_NLP_ENGINE", "disabled")
        monkeypatch.setenv("REDACTION_SPACY_MODELS", "")
        monkeypatch.setenv("REDACTION_LANGUAGES", "en,it")
        monkeypatch.setenv("REDACTION_GLINER_MODEL", "fake/model")
        monkeypatch.setenv("REDACTION_NER_DEFAULT", "gliner")
        model = FakeGliner()

        def fake_build(languages: list[str], model_name: str) -> list[Any]:
            assert model_name == "fake/model"
            recognizers = []
            for language in languages:
                recognizer = gliner_engine.make_recognizer_class()(
                    supported_language=language,
                    entity_mapping=gliner_engine.configured_labels(),
                    map_location="cpu",
                    shared_model=model,
                )
                recognizers.append(recognizer)
            return recognizers

        monkeypatch.setattr(gliner_engine, "build_recognizers", fake_build)
        detector = build_detector()
        assert detector.default_engine == "gliner"
        gliner = detector.slot("gliner")
        assert gliner.available and gliner.custom_labels
        assert gliner.languages == ["en", "it"]
        assert "MEDICAL_CONDITION" in gliner.ner_entities
        assert gliner.ms_per_token is not None  # timed once at startup
        assert model.calls  # and that run also warmed the model

    def test_label_map_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REDACTION_GLINER_LABELS", raising=False)
        assert gliner_engine.configured_labels() == gliner_engine.DEFAULT_GLINER_LABELS
        monkeypatch.setenv(
            "REDACTION_GLINER_LABELS", "person=person, blood type ,hospital=organization"
        )
        assert gliner_engine.configured_labels() == {
            "person": "PERSON",
            "blood type": "BLOOD_TYPE",
            "hospital": "ORGANIZATION",
        }


class TestReview:
    def test_the_model_is_not_asked_for_pattern_type_names(self) -> None:
        model = FakeGliner()
        slot = EngineSlot(
            name="gliner",
            analyzer=_presidio_gliner_analyzer(model),
            languages=["it"],
            ner_entities=sorted(set(gliner_engine.configured_labels().values())),
            custom_labels=True,
        )
        detector = Detector(FakeAnalyzer(), languages=["it"], models={"it": "m"}, gliner=slot)
        detector.analyse_one(
            TEXT,
            language="it",
            score_threshold=0.5,
            entity_types=None,
            engine="gliner",
            custom_labels={"project code name": "PROJECT_NAME"},
        )
        asked = {label for labels in model.calls for label in labels}
        allowed = set(gliner_engine.configured_labels()) | {"project code name"}
        assert asked and asked <= allowed, asked - allowed

    async def test_a_custom_label_equal_to_a_default_one_is_refused(self) -> None:
        async with serving(two_engine_detector()) as client:
            health = (await client.get("/healthz")).json()
            gliner = next(e for e in health["ner_engines"] if e["name"] == "gliner")
            assert "person" in gliner["labels"]
            response = await client.post(
                "/detect",
                json={
                    "texts": [TEXT],
                    "ner_engine": "gliner",
                    "custom_labels": [{"entity": "WHO", "label": "Person"}],
                },
            )
            assert response.status_code == 422
            assert response.json()["detail"] == {"code": "label_taken", "labels": ["Person"]}
            ok = await client.post(
                "/detect",
                json={
                    "texts": [TEXT],
                    "ner_engine": "gliner",
                    "custom_labels": [{"entity": "WHO", "label": "project owner"}],
                },
            )
            assert ok.status_code == 200
            # spaCy never takes labels, so the clash cannot arise there.
            spacy = await client.post(
                "/detect",
                json={
                    "texts": [TEXT],
                    "ner_engine": "spacy",
                    "custom_labels": [{"entity": "WHO", "label": "person"}],
                },
            )
            assert spacy.status_code == 200

    async def test_a_full_gliner_queue_is_a_503_engine_busy(self) -> None:
        detector = two_engine_detector()
        detector.gliner_queue_seconds = 0.05
        async with serving(detector) as client:
            await detector._gliner_gate.acquire()  # someone else is running a long text
            try:
                busy = await client.post("/detect", json={"texts": [TEXT], "ner_engine": "gliner"})
                assert busy.status_code == 503
                assert busy.json()["detail"] == {"code": "engine_busy", "engine": "gliner"}
                # spaCy is not behind that gate.
                fine = await client.post("/detect", json={"texts": [TEXT], "ner_engine": "spacy"})
                assert fine.status_code == 200
            finally:
                detector._gliner_gate.release()
            after = await client.post("/detect", json={"texts": [TEXT], "ner_engine": "gliner"})
            assert after.status_code == 200
