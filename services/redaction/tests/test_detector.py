"""The detection service, with Presidio faked out.

Deliberately not testing whether Presidio finds a fiscal code — that is Presidio's
test and it has one. What is tested here is everything *around* it that this
service is responsible for and could get wrong: the contract shape, honest
capability reporting, language fallback, threshold and entity filtering, and that
inference does not run on the event loop.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from llmp_redaction.app import (
    _configured_languages,
    _configured_models,
    _nlp_engine_name,
    create_app,
)
from llmp_redaction.detector import MODEL_BACKED_ENTITIES, PATTERN_ONLY_ENTITIES, Detector


@dataclass
class Result:
    """Shaped like presidio_analyzer.RecognizerResult."""

    start: int
    end: int
    entity_type: str
    score: float


class FakeAnalyzer:
    """Finds fixed substrings. Records the thread it ran on."""

    def __init__(self, findings: dict[str, tuple[str, float]] | None = None) -> None:
        self.findings = findings or {}
        self.calls: list[dict[str, Any]] = []
        self.threads: set[int] = set()

    def get_supported_entities(self, language: str | None = None) -> list[str]:
        return sorted({entity for entity, _ in self.findings.values()})

    def analyze(self, text: str, language: str, **kwargs: Any) -> list[Result]:
        self.calls.append({"text": text, "language": language, **kwargs})
        self.threads.add(threading.get_ident())

        results = []
        for needle, (entity_type, score) in self.findings.items():
            start = text.find(needle)
            if start == -1:
                continue
            if score < (kwargs.get("score_threshold") or 0.0):
                continue
            wanted = kwargs.get("entities")
            if wanted and entity_type not in wanted:
                continue
            results.append(Result(start, start + len(needle), entity_type, score))
        return results


def detector_for(
    findings: dict[str, tuple[str, float]] | None = None,
    *,
    languages: list[str] | None = None,
    models: dict[str, str] | None = None,
) -> tuple[Detector, FakeAnalyzer]:
    analyzer = FakeAnalyzer(findings)
    detector = Detector(
        analyzer,
        languages=languages or ["en"],
        models=models if models is not None else {"en": "en_core_web_lg"},
        engine="fake",
        engine_version="0.0.1",
    )
    return detector, analyzer


class TestSpanConversion:
    async def test_results_become_contract_spans(self) -> None:
        detector, _ = detector_for({"Mario Rossi": ("PERSON", 0.85)})
        spans = (
            await detector.analyse(
                ["Ask Mario Rossi."], language="en", score_threshold=0.5, entity_types=None
            )
        )[0]
        assert len(spans) == 1
        assert (spans[0].start, spans[0].end) == (4, 15)
        assert spans[0].entity_type == "PERSON"
        assert spans[0].score == 0.85

    async def test_the_span_covers_exactly_the_entity(self) -> None:
        """Offsets are what the gateway substitutes on; one off in either
        direction leaves half a name in the prompt or eats a space."""
        text = "Ask Mario Rossi."
        detector, _ = detector_for({"Mario Rossi": ("PERSON", 0.9)})
        spans = (
            await detector.analyse([text], language="en", score_threshold=0.5, entity_types=None)
        )[0]
        assert spans[0].slice_of(text) == "Mario Rossi"

    async def test_scores_outside_the_range_are_clamped(self) -> None:
        """A recogniser returning 1.0000001 must not 422 the whole batch."""
        analyzer = FakeAnalyzer()
        analyzer.analyze = lambda **kwargs: [Result(0, 1, "X", 1.4)]  # type: ignore[method-assign]
        detector = Detector(analyzer, languages=["en"])
        spans = (
            await detector.analyse(["abc"], language="en", score_threshold=0.0, entity_types=None)
        )[0]
        assert spans[0].score == 1.0

    async def test_spans_come_back_in_document_order(self) -> None:
        detector, _ = detector_for({"Rossi": ("PERSON", 0.9), "Turin": ("LOCATION", 0.8)})
        spans = (
            await detector.analyse(
                ["Turin is where Rossi lives"],
                language="en",
                score_threshold=0.5,
                entity_types=None,
            )
        )[0]
        assert [span.start for span in spans] == sorted(span.start for span in spans)

    async def test_an_empty_text_is_not_analysed(self) -> None:
        detector, analyzer = detector_for({"x": ("PERSON", 0.9)})
        spans = await detector.analyse(
            ["", "x"], language="en", score_threshold=0.5, entity_types=None
        )
        assert spans[0] == []
        assert len(analyzer.calls) == 1

    async def test_the_batch_keeps_its_order(self) -> None:
        """Findings are matched to texts by index; a reorder silently redacts the
        wrong message."""
        detector, _ = detector_for({"Rossi": ("PERSON", 0.9)})
        spans = await detector.analyse(
            ["nothing here", "Rossi", "nor here"],
            language="en",
            score_threshold=0.5,
            entity_types=None,
        )
        assert [len(item) for item in spans] == [0, 1, 0]


class TestFiltering:
    async def test_the_threshold_is_passed_through(self) -> None:
        detector, analyzer = detector_for({"Rossi": ("PERSON", 0.4)})
        spans = (
            await detector.analyse(["Rossi"], language="en", score_threshold=0.8, entity_types=None)
        )[0]
        assert spans == []
        assert analyzer.calls[0]["score_threshold"] == 0.8

    async def test_entity_types_are_passed_through(self) -> None:
        detector, analyzer = detector_for(
            {"Rossi": ("PERSON", 0.9), "a@b.test": ("EMAIL_ADDRESS", 0.9)}
        )
        spans = (
            await detector.analyse(
                ["Rossi a@b.test"],
                language="en",
                score_threshold=0.5,
                entity_types=["EMAIL_ADDRESS"],
            )
        )[0]
        assert [span.entity_type for span in spans] == ["EMAIL_ADDRESS"]
        assert analyzer.calls[0]["entities"] == ["EMAIL_ADDRESS"]

    async def test_no_entity_types_means_everything_the_engine_knows(self) -> None:
        detector, analyzer = detector_for({"Rossi": ("PERSON", 0.9)})
        await detector.analyse(["Rossi"], language="en", score_threshold=0.5, entity_types=[])
        assert analyzer.calls[0]["entities"] is None


class TestLanguage:
    async def test_a_loaded_language_is_used(self) -> None:
        detector, analyzer = detector_for(languages=["en", "it"])
        await detector.analyse(["ciao"], language="it", score_threshold=0.5, entity_types=None)
        assert analyzer.calls[0]["language"] == "it"

    async def test_an_unloaded_language_falls_back_rather_than_failing(self) -> None:
        """Pattern-based entities are largely language-independent, so refusing
        would lose real detections. The fallback is logged."""
        detector, analyzer = detector_for(languages=["en"])
        await detector.analyse(["ciao"], language="it", score_threshold=0.5, entity_types=None)
        assert analyzer.calls[0]["language"] == "en"

    def test_the_fallback_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        detector, _ = detector_for(languages=["en"])
        with caplog.at_level("WARNING"):
            assert detector.resolve_language("it") == "en"
        assert "it" in caplog.text


class TestCapabilities:
    def test_a_language_without_a_model_is_reported_as_degraded(self) -> None:
        """The licence consequence, made visible: Italian identifiers work,
        Italian names do not, and nobody should have to infer that."""
        detector, _ = detector_for(languages=["en", "it"], models={"en": "en_core_web_lg"})
        capabilities = detector.capabilities()
        assert capabilities.degraded == ["it"]

    def test_nothing_is_degraded_when_every_language_has_a_model(self) -> None:
        detector, _ = detector_for(
            languages=["en", "it"],
            models={"en": "en_core_web_lg", "it": "it_core_news_lg"},
        )
        assert detector.capabilities().degraded == []

    def test_capabilities_come_from_the_analyzer_not_a_constant(self) -> None:
        """The bug this replaced: /healthz advertised IT_FISCAL_CODE while
        Presidio had dropped every Italian recogniser at startup."""
        detector, _ = detector_for({"x": ("ONLY_THIS", 0.9)})
        assert detector.capabilities().entities == ["ONLY_THIS"]

    def test_the_italian_identifiers_are_pattern_based(self) -> None:
        """The finding the licence decision rests on. If this ever stops being
        true, the MIT-only image quietly stops detecting Italian PII."""
        for entity in (
            "IT_FISCAL_CODE",
            "IT_VAT_CODE",
            "IT_DRIVER_LICENSE",
            "IT_IDENTITY_CARD",
            "IT_PASSPORT",
        ):
            assert entity in PATTERN_ONLY_ENTITIES
            assert entity not in MODEL_BACKED_ENTITIES


class TestEventLoop:
    async def test_analysis_does_not_run_on_the_event_loop(self) -> None:
        """spaCy is CPU-bound and synchronous. On the loop, one prompt stalls
        every other request in this process — the exact failure the service
        exists to keep away from the gateway."""
        detector, analyzer = detector_for({"Rossi": ("PERSON", 0.9)})
        loop_thread = threading.get_ident()
        await detector.analyse(["Rossi"], language="en", score_threshold=0.5, entity_types=None)
        assert analyzer.threads and loop_thread not in analyzer.threads

    async def test_concurrent_requests_are_not_serialised_by_the_loop(self) -> None:
        detector, _ = detector_for()
        await asyncio.gather(
            *[
                detector.analyse(
                    [f"text {n}"], language="en", score_threshold=0.5, entity_types=None
                )
                for n in range(5)
            ]
        )


@asynccontextmanager
async def serving(detector: Detector) -> AsyncIterator[httpx.AsyncClient]:
    """The real app over ASGI, with lifespan run.

    ASGITransport rather than TestClient: the repo runs with
    ``filterwarnings = error`` and starlette's TestClient emits a deprecation
    warning about httpx. This is also what the gateway's own tests use.
    """
    app = create_app(detector)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://detector"
        ) as client,
    ):
        yield client


class LifespanManager:
    """Runs an ASGI app's lifespan around a block.

    Without it the startup hook never fires and `app.state.detector` is absent —
    which is exactly the failure a first request would hit in production, so it
    is worth exercising rather than side-stepping by injecting the detector.
    """

    def __init__(self, app: Any) -> None:
        self._app = app
        self._receive: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._send: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Any:
        self._task = asyncio.create_task(
            self._app({"type": "lifespan"}, self._receive.get, self._send.put)
        )
        await self._receive.put({"type": "lifespan.startup"})
        message = await self._send.get()
        assert message["type"] == "lifespan.startup.complete", message
        return self._app

    async def __aexit__(self, *exc: Any) -> None:
        await self._receive.put({"type": "lifespan.shutdown"})
        await self._send.get()
        if self._task is not None:
            await self._task


class TestHttpSurface:
    @pytest.fixture
    async def client(self) -> Any:
        detector, _ = detector_for(
            {"Mario Rossi": ("PERSON", 0.9), "a@b.test": ("EMAIL_ADDRESS", 0.95)}
        )
        async with serving(detector) as client:
            yield client

    async def test_detect_returns_the_contract_shape(self, client: Any) -> None:
        response = await client.post(
            "/detect", json={"texts": ["Ask Mario Rossi.", "no pii"], "language": "en"}
        )
        assert response.status_code == 200
        body = response.json()
        assert [finding["index"] for finding in body["findings"]] == [0, 1]
        assert body["findings"][0]["spans"][0]["entity_type"] == "PERSON"
        assert body["findings"][1]["spans"] == []

    async def test_the_engine_identifies_itself(self, client: Any) -> None:
        """An auditor asking what redacted a conversation needs the version."""
        body = (await client.post("/detect", json={"texts": ["x"]})).json()
        assert body["engine"] == "fake"
        assert body["engine_version"] == "0.0.1"

    async def test_no_placeholder_text_is_ever_returned(self, client: Any) -> None:
        """The detector must not invent placeholders — that is the gateway's job
        and the reason engines are swappable."""
        body = (await client.post("/detect", json={"texts": ["Ask Mario Rossi."]})).json()
        span = body["findings"][0]["spans"][0]
        assert set(span) == {"start", "end", "entity_type", "score"}

    async def test_an_empty_batch_is_fine(self, client: Any) -> None:
        response = await client.post("/detect", json={"texts": []})
        assert response.json()["findings"] == []

    async def test_a_malformed_request_is_422(self, client: Any) -> None:
        response = await client.post("/detect", json={"texts": "not a list"})
        assert response.status_code == 422

    async def test_a_threshold_outside_the_range_is_refused(self, client: Any) -> None:
        response = await client.post("/detect", json={"texts": ["x"], "score_threshold": 2})
        assert response.status_code == 422

    async def test_types_the_engine_cannot_serve_are_filtered_and_named(
        self, client: Any
    ) -> None:
        """A policy naming an entity the engine has no recogniser for must not
        500 — Presidio raises on such a request — and must not pass silently
        either. The servable types are still detected; the rest are named back.
        """
        response = await client.post(
            "/detect",
            json={
                "texts": ["Ask Mario Rossi."],
                "language": "en",
                "entity_types": ["PERSON", "IT_FISCAL_CODE"],
            },
        )
        assert response.status_code == 200
        body = response.json()
        # The fake detector finds PERSON; IT_FISCAL_CODE has no recogniser.
        assert body["findings"][0]["spans"][0]["entity_type"] == "PERSON"
        assert body["unsupported_types"] == ["IT_FISCAL_CODE"]

    async def test_an_all_unservable_enumeration_skips_the_engine(
        self, client: Any
    ) -> None:
        """Handing Presidio an empty `entities` list raises the same way an
        unknown type does, so the all-filtered case must not reach it at all —
        and still answers 200 with nothing found."""
        response = await client.post(
            "/detect",
            json={
                "texts": ["Ask Mario Rossi."],
                "language": "en",
                "entity_types": ["IT_FISCAL_CODE"],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["findings"][0]["spans"] == []
        assert body["unsupported_types"] == ["IT_FISCAL_CODE"]

    async def test_healthz_reports_what_is_loaded(self, client: Any) -> None:
        body = (await client.get("/healthz")).json()
        assert body["status"] == "ok"
        assert body["languages"] == ["en"]
        assert body["models"] == {"en": "en_core_web_lg"}
        # Read from the analyzer, not a hardcoded list: the service must not be
        # able to advertise an entity its registry has quietly dropped.
        assert body["entities"] == ["EMAIL_ADDRESS", "PERSON"]

    async def test_healthz_declares_a_degraded_language(self) -> None:
        detector, _ = detector_for(languages=["en", "it"], models={"en": "en_core_web_lg"})
        async with serving(detector) as client:
            body = (await client.get("/healthz")).json()
        assert body["degraded_languages"] == ["it"]


class TestModelConfiguration:
    def test_an_empty_value_means_no_models(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Written by the Dockerfile: empty means the no-NER build, and reading
        it as "use the default" would send a model-less container off to load
        weights that are not on disk."""
        monkeypatch.setenv("REDACTION_SPACY_MODELS", "")
        assert _configured_models() == {}

    def test_models_are_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Written by the Dockerfile from the models it actually installed, so
        the service cannot claim a language whose weights are absent."""
        monkeypatch.setenv("REDACTION_SPACY_MODELS", "en=en_core_web_lg,it=it_core_news_lg")
        assert _configured_models() == {"en": "en_core_web_lg", "it": "it_core_news_lg"}

    def test_a_malformed_entry_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDACTION_SPACY_MODELS", "nonsense,en=en_core_web_lg")
        assert _configured_models() == {"en": "en_core_web_lg"}


class TestNlpEngineConfiguration:
    def test_the_default_nlp_engine_is_spacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REDACTION_NLP_ENGINE", raising=False)
        assert _nlp_engine_name() == "spacy"

    def test_disabled_is_selected_by_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REDACTION_NLP_ENGINE", "disabled")
        assert _nlp_engine_name() == "disabled"

    def test_an_unknown_nlp_engine_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Refused rather than fallen back: a service that silently loads the
        model its operator asked it not to load defeats the whole setting."""
        monkeypatch.setenv("REDACTION_NLP_ENGINE", "stanza")
        with pytest.raises(ValueError, match="REDACTION_NLP_ENGINE"):
            _nlp_engine_name()

    def test_disabled_languages_come_from_the_language_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REDACTION_NLP_ENGINE", "disabled")
        monkeypatch.setenv("REDACTION_SPACY_MODELS", "")
        monkeypatch.setenv("REDACTION_LANGUAGES", "en,it")
        assert _configured_languages() == ["en", "it"]

    def test_disabled_languages_default_to_english(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REDACTION_NLP_ENGINE", "disabled")
        monkeypatch.setenv("REDACTION_SPACY_MODELS", "")
        monkeypatch.delenv("REDACTION_LANGUAGES", raising=False)
        assert _configured_languages() == ["en"]

    def test_spacy_languages_come_from_the_model_map(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REDACTION_SPACY_MODELS", "en=en_core_web_lg,it=it_core_news_lg")
        assert _configured_languages() == ["en", "it"]


class TestBuildWithoutNer:
    def test_disabled_mode_loads_no_model_and_hides_model_backed_entities(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real builder, with the NLP engine off.

        Everything worth asserting is here: no spaCy model is loaded (that is
        the memory saving), the entity list carries no model-backed type (that
        is what keeps the console's selector honest), and the capabilities
        report every language degraded (that is how the screen says so).
        """
        from llmp_redaction.app import build_detector

        # The one test that runs the real builder, so it needs real Presidio.
        # Deliberately absent from the root workspace lockfile (services/redaction/
        # pyproject.toml), so from the root venv this skips rather than fails; in
        # the service's own environment it runs and is the test that matters.
        pytest.importorskip("presidio_analyzer", reason="presidio not installed")

        monkeypatch.setenv("REDACTION_NLP_ENGINE", "disabled")
        monkeypatch.setenv("REDACTION_SPACY_MODELS", "")
        monkeypatch.delenv("REDACTION_LANGUAGES", raising=False)

        detector = build_detector()

        assert detector.capabilities().models == {}
        assert detector.capabilities().degraded == ["en"]
        entities = set(detector.capabilities().entities)
        assert "CREDIT_CARD" in entities  # pattern recognisers survive
        assert not entities & {"PERSON", "LOCATION", "NRP", "ORGANIZATION"}

    def test_disabled_mode_still_finds_phones_at_the_default_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a lemma table Presidio's context enhancer never fires, and a
        phone match reports the bare 0.4 — under the gateway's default
        threshold, and the 0.75 that a context word earns in NER mode is a lift
        no-NER mode can never attain. Left alone, the entity list would
        advertise PHONE_NUMBER while nothing under 0.5 ever came back: the
        exact lie this service's capability reporting exists to prevent. The
        score is compensated to what NER mode could produce; the gateway policy
        still decides per type whether phones are redacted.
        """
        from llmp_redaction.app import build_detector

        pytest.importorskip("presidio_analyzer", reason="presidio not installed")

        monkeypatch.setenv("REDACTION_NLP_ENGINE", "disabled")
        monkeypatch.setenv("REDACTION_SPACY_MODELS", "")
        monkeypatch.delenv("REDACTION_LANGUAGES", raising=False)

        detector = build_detector()

        spans = detector.analyse_one(
            "call +39 011 227 6543 today",
            language="en",
            score_threshold=0.5,
            entity_types=None,
        )
        phones = [span for span in spans if span.entity_type == "PHONE_NUMBER"]
        assert phones, f"no phone found above 0.5: {spans}"
        assert phones[0].score >= 0.5
