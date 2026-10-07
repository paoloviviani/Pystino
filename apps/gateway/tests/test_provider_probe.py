"""Testing a provider from the console: ``POST /admin/providers/{id}/test``.

The endpoint used to mean one thing for every provider type — ``GET
/models`` — which is wrong for two kinds already in the registry before this
file existed: the local extractor serves ``/healthz``, ``/extract`` and
``/detect`` and nothing resembling a catalogue, and a search backend (Linkup,
Exa, Jina, DuckDuckGo) is not an inference endpoint and has no catalogue
either. Both used to answer a 404 that read as "the provider is down, check
the base URL includes /v1" for a provider working exactly as documented.

Each plugin now says how it tests itself (``ProviderPlugin.probe``,
``plugins/base.py``), and these tests pin what each answer looks like,
directly against ``OpenAICompatibleUpstream.probe`` rather than through the
admin API and a database — the same level ``test_provider_plugins.py``
already tests plugin behaviour at.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from gateway.config import UpstreamSettings
from gateway.plugins.duckduckgo import DuckDuckGoSearchPlugin
from gateway.plugins.extractor import LocalExtractorPlugin
from gateway.plugins.generic import GenericOpenAIPlugin
from gateway.plugins.linkup import LinkupSearchPlugin
from gateway.upstream import OpenAICompatibleUpstream

Handler = Callable[[httpx.Request], httpx.Response]


def _upstream(
    plugin: object, handler: Handler, *, base_url: str = "http://counterparty.test"
) -> tuple[OpenAICompatibleUpstream, httpx.AsyncClient]:
    """An upstream wired to a plugin and a mock transport, the way
    ``ProviderRegistry.build_probe`` builds one from a stored row — minus the
    database, which none of these tests need to touch a plugin's own logic."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream = OpenAICompatibleUpstream(UpstreamSettings(base_url=base_url), client, plugin=plugin)
    return upstream, client


# --------------------------------------------------------------------------
# the local extractor
# --------------------------------------------------------------------------


class TestExtractorProbe:
    async def test_a_healthy_extractor_reports_a_sample_extraction(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/healthz"):
                return httpx.Response(200, json={"ok": True})
            assert request.url.path.endswith("/extract")
            return httpx.Response(
                200,
                json={
                    "kind": "text",
                    "text": "Pystino provider test",
                    "extractor": "markitdown",
                    "pages": 1,
                },
            )

        upstream, client = _upstream(LocalExtractorPlugin(), handler)
        result = await upstream.probe(client)
        assert result.ok is True
        assert result.detail == "reachable; extracted a sample page (markitdown)"

    async def test_a_down_healthz_is_the_reported_failure(self) -> None:
        """Never reaches ``/extract`` — the cheaper check fails first, and
        names itself rather than a base-URL guess that does not apply here."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/healthz")
            return httpx.Response(503)

        upstream, client = _upstream(LocalExtractorPlugin(), handler)
        result = await upstream.probe(client)
        assert result.ok is False
        assert result.status_code == 503
        assert "/healthz" in result.detail

    async def test_a_failing_extract_reports_the_real_reason(self) -> None:
        """Not "the provider answered 404" — the real upstream failure."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/healthz"):
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(500, json={"error": "boom"})

        upstream, client = _upstream(LocalExtractorPlugin(), handler)
        result = await upstream.probe(client)
        assert result.ok is False
        assert "500" in result.detail

    async def test_a_refused_sample_page_is_not_reported_as_reachable(self) -> None:
        """The extractor answered, but had nothing to say — a scan, in this
        built-in PDF's case would mean the service itself is misbehaving,
        since the probe document has a text layer. Still surfaced honestly
        rather than folded into "ok"."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/healthz"):
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(
                200,
                json={"kind": "no_text_layer", "text": "", "extractor": "markitdown", "pages": 0},
            )

        upstream, client = _upstream(LocalExtractorPlugin(), handler)
        result = await upstream.probe(client)
        assert result.ok is False
        assert "scan" in result.detail


# --------------------------------------------------------------------------
# an OpenAI-compatible provider: unchanged behaviour
# --------------------------------------------------------------------------


class TestGenericProbeUnchanged:
    """``GenericOpenAIPlugin`` still tests itself with ``list_models()`` — the
    ``/v1`` hint on a 404 is exactly what an operator needs for this kind."""

    async def test_a_404_still_suggests_the_version_path(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "not found"})

        upstream, client = _upstream(GenericOpenAIPlugin(), handler)
        result = await upstream.probe(client)
        assert result.ok is False
        assert "/v1" in result.detail

    async def test_a_reachable_catalogue_is_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})

        upstream, client = _upstream(GenericOpenAIPlugin(), handler)
        result = await upstream.probe(client)
        assert result.ok is True
        assert result.model_count == 2


# --------------------------------------------------------------------------
# search backends: no catalogue, so a real minimal search runs instead
# --------------------------------------------------------------------------


class TestSearchProbe:
    async def test_duckduckgo_reports_a_real_result_count(self) -> None:
        """The false negative this fixes: DuckDuckGo has no ``/models``, and
        the old check answered a 404 read as "the provider is down"."""

        page = """
        <html><body>
        <div class="result">
          <a class="result__a" href="https://example.org/a">Title</a>
          <a class="result__snippet">A snippet.</a>
        </div>
        </body></html>
        """

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/html/"
            return httpx.Response(200, content=page, headers={"content-type": "text/html"})

        upstream, client = _upstream(DuckDuckGoSearchPlugin(), handler)
        result = await upstream.probe(client)
        assert result.ok is True
        assert result.model_count == 1
        assert "result" in result.detail

    async def test_duckduckgo_a_bot_challenge_is_not_reported_as_reachable(self) -> None:
        challenge = (
            '<html><body><div class="anomaly-modal">'
            '<form id="challenge-form">bots</form></div></body></html>'
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=challenge, headers={"content-type": "text/html"})

        upstream, client = _upstream(DuckDuckGoSearchPlugin(), handler)
        result = await upstream.probe(client)
        assert result.ok is False
        assert "challenge" in result.detail

    async def test_a_json_search_backend_reports_a_result_count(self) -> None:
        """Linkup speaks JSON rather than DuckDuckGo's form/HTML — the other
        half of the search protocol the probe has to honour."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/search"
            return httpx.Response(
                200,
                json={
                    "results": [{"name": "Example", "url": "https://example.org", "content": "x"}]
                },
            )

        upstream, client = _upstream(LinkupSearchPlugin(), handler)
        result = await upstream.probe(client)
        assert result.ok is True
        assert result.model_count == 1

    async def test_a_rate_limited_search_backend_says_so(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": "slow down"})

        upstream, client = _upstream(LinkupSearchPlugin(), handler)
        result = await upstream.probe(client)
        assert result.ok is False
        assert result.status_code == 429
        assert "rate-limited" in result.detail
