"""DuckDuckGo as a first-class search backend: form POST in, unified shape out.

The one backend that does not speak JSON — ``POST .../html/`` with a
form-encoded ``q``, answered as an HTML results page — so what the tests hold
is the whole of the exception: the request travels as form data to the
configured base URL plus ``/html/`` with no credential and no caller data in
the URL, the page parses by the rules re-derived live 2026-09-16
(``result__a``/``result__snippet`` pairing, ``/l/?uddg=`` unwrap, HTTPS-only,
vendor-hosted ad slots dropped), a bot challenge degrades to a named 502
rather than an empty answer, and the metering is the same count-before-the-call
every other backend keeps.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
from conftest import UPSTREAM_BASE, FakeUpstream, Seeded
from gateway.models import (
    ApiSurface,
    GroupModelAccess,
    ModelDef,
    ModelKind,
    Provider,
    ProviderKind,
    UsageRecord,
    UsageStatus,
)
from gateway.plugins import registry as plugin_registry
from gateway.plugins.duckduckgo import (
    DuckDuckGoSearchPlugin,
    is_duckduckgo_challenge,
    parse_duckduckgo_results,
)
from gateway.redaction import RedactionOutcome
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_oidc_policy import admin_session as admin_session
from test_search_surface import latest_record
from test_unified_search import set_policy


def results_page() -> str:
    """Two results in DDG's shape: wrapped redirect links, paired snippets."""
    return """
    <html><body>
    <div class="result">
      <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.org%2Fa&amp;rut=abc">T</a>
      <a class="result__snippet" href="/l/?uddg=https%3A%2F%2Fexample.org%2Fa">First.</a>
    </div>
    <div class="result">
      <a class="result__a" href="https://example.org/b">Second Title</a>
      <a class="result__snippet" href="https://example.org/b">Second snippet text.</a>
    </div>
    </body></html>
    """


def challenge_page() -> str:
    """Bot detection instead of results, as served to flagged egress IPs."""
    return (
        '<html><body><div class="anomaly-modal">'
        '<form id="challenge-form">bots</form></div></body></html>'
    )


async def add_duckduckgo(
    session: AsyncSession,
    seeded: Seeded,
    *,
    granted: bool = True,
    base_url: str | None = None,
) -> ModelDef:
    """The backend the way the admin API builds it: keyless, with its anchor.

    No credential on purpose — this is the backend that proves a provider row
    without one searches — so the decrypt-and-send path is exercised with an
    empty key rather than around it.
    """
    provider = Provider(
        name="duckduckgo",
        base_url=base_url or UPSTREAM_BASE,
        api_key_encrypted="",
        api_key_hint="",
        plugin="duckduckgo",
        kind=ProviderKind.SEARCH,
    )
    session.add(provider)
    await session.flush()

    anchor = ModelDef(
        name="duckduckgo",
        upstream_model="search",
        provider_id=provider.id,
        kind=ModelKind.SEARCH,
    )
    session.add(anchor)
    await session.flush()
    if granted:
        session.add(GroupModelAccess(group_id=seeded.group.id, model_id=anchor.id))
    await session.commit()
    return anchor


def set_html(fake_upstream: FakeUpstream, html: str, *, status: int = 200) -> None:
    fake_upstream.set_raw(html.encode(), status=status, content_type="text/html")


class TestParse:
    def test_wrapped_and_bare_links_come_back_unwrapped(self) -> None:
        assert parse_duckduckgo_results(results_page(), 5) == [
            {
                "title": "T",
                "url": "https://example.org/a",
                "snippet": "First.",
            },
            {
                "title": "Second Title",
                "url": "https://example.org/b",
                "snippet": "Second snippet text.",
            },
        ]

    def test_non_https_and_duckduckgo_links_are_dropped_not_repaired(self) -> None:
        html = """
        <a class="result__a" href="http://example.org/plain">Plain</a>
        <a class="result__a" href="/l/?uddg=http%3A%2F%2Fexample.org%2Fclear">Clear</a>
        <a class="result__a" href="https://duckduckgo.com/about">About</a>
        <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.org%2Fkept">Kept</a>
        <a class="result__snippet" href="https://example.org/kept">Kept snippet.</a>
        """
        assert parse_duckduckgo_results(html, 5) == [
            {"title": "Kept", "url": "https://example.org/kept", "snippet": "Kept snippet."}
        ]

    def test_a_snippet_belongs_to_the_result_before_it(self) -> None:
        html = """
        <a class="result__a" href="https://example.org/a">A</a>
        <a class="result__a" href="https://example.org/b">B</a>
        <a class="result__snippet" href="https://example.org/b">Belongs to B.</a>
        """
        assert parse_duckduckgo_results(html, 5) == [
            {"title": "A", "url": "https://example.org/a", "snippet": ""},
            {"title": "B", "url": "https://example.org/b", "snippet": "Belongs to B."},
        ]

    def test_missing_titles_and_snippets_degrade_to_untitled_and_empty(self) -> None:
        html = '<a class="result__a" href="https://example.org/a"></a>'
        assert parse_duckduckgo_results(html, 5) == [
            {"title": "untitled", "url": "https://example.org/a", "snippet": ""}
        ]

    def test_max_results_is_clamped_to_ten(self) -> None:
        html = "".join(
            f'<a class="result__a" href="https://example.org/{i}">T{i}</a>' for i in range(12)
        )
        assert len(parse_duckduckgo_results(html, 50)) == 10
        assert len(parse_duckduckgo_results(html, 3)) == 3

    def test_an_unparseable_page_is_no_hits_not_an_error(self) -> None:
        assert parse_duckduckgo_results("<html><body>restyled</body></html>", 5) == []

    def test_challenge_markers_are_detected(self) -> None:
        assert is_duckduckgo_challenge(challenge_page()) is True
        assert is_duckduckgo_challenge(results_page()) is False


class TestPlugin:
    def test_registered_as_a_search_backend(self) -> None:
        plugin = plugin_registry.resolve("duckduckgo")
        assert isinstance(plugin, DuckDuckGoSearchPlugin)
        assert plugin.kind == ProviderKind.SEARCH

    def test_keyless_with_the_html_endpoint(self) -> None:
        plugin = DuckDuckGoSearchPlugin()
        assert plugin.auth_headers("") == {}
        assert plugin.auth_headers("a-mistaken-key") == {}
        assert plugin.search_path == "/html/"
        assert plugin.default_base_url == "https://html.duckduckgo.com"
        assert plugin.requires_api_key is False
        assert plugin.build_search_form("capital of france", 5) == {"q": "capital of france"}

    def test_described_as_keyless_for_the_console(self) -> None:
        entry = next(entry for entry in plugin_registry.describe() if entry["name"] == "duckduckgo")
        assert entry["kind"] == "search"
        assert entry["requires_api_key"] is False
        assert entry["default_base_url"] == "https://html.duckduckgo.com"


class TestUnifiedFormSearch:
    async def test_the_query_travels_as_form_data_not_json(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The vendor speaks form-encoded POST: ``q`` as data, never in a URL,
        through the same never-redirecting client as the JSON backends."""
        anchor = await add_duckduckgo(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        set_html(fake_upstream, results_page())

        response = await client.post(
            "/v1/search",
            json={"query": "capital of france", "max_results": 7},
            headers=seeded.auth,
        )
        assert response.status_code == 200, response.text
        assert fake_upstream.last_body == {"q": "capital of france"}
        assert fake_upstream.urls[-1] == f"{UPSTREAM_BASE}/html/"
        sent = fake_upstream.headers[-1]
        assert sent["content-type"] == "application/x-www-form-urlencoded"
        assert sent["accept"] == "text/html"
        assert "chrome" in sent["user-agent"].lower()
        assert "authorization" not in sent

    async def test_answers_come_back_in_the_unified_shape(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        anchor = await add_duckduckgo(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        set_html(fake_upstream, results_page())

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "results": [
                {
                    "title": "T",
                    "url": "https://example.org/a",
                    "snippet": "First.",
                },
                {
                    "title": "Second Title",
                    "url": "https://example.org/b",
                    "snippet": "Second snippet text.",
                },
            ],
            "backend": "duckduckgo",
        }

    async def test_one_search_is_one_count_with_no_cost(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The same ledger shape as the JSON backends: one request counted,
        no price, no tier — a wrong count here is a wrong report."""
        anchor = await add_duckduckgo(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        set_html(fake_upstream, results_page())

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 200

        record = await latest_record(session)
        assert record.api_surface is ApiSurface.SEARCH
        assert record.status is UsageStatus.COMPLETED
        assert record.own_search_requests == 1
        assert record.own_search_backend == "duckduckgo"
        assert record.own_search_tier is None
        assert record.cost == Decimal(0)
        assert record.total_tokens == 0

    async def test_an_unparseable_page_is_an_empty_answer_not_an_error(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """A 2xx the reader cannot parse still counts — the vendor received
        the request — but answers nothing rather than failing the turn."""
        anchor = await add_duckduckgo(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        set_html(fake_upstream, "<html><body>restyled</body></html>")

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert response.json() == {"results": [], "backend": "duckduckgo"}
        record = await latest_record(session)
        assert record.own_search_requests == 1
        assert record.status is UsageStatus.COMPLETED

    async def test_a_challenge_is_a_named_502_not_an_empty_answer(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """A block is not "no results": zero hits would state a fact the
        vendor never gave. The degradation names the challenge — and keeps
        the count, because a ceiling that forgave a failure would be raisable
        by making the search fail."""
        anchor = await add_duckduckgo(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        # The shape flagged egress sees: HTTP 202 carrying the challenge.
        set_html(fake_upstream, challenge_page(), status=202)

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 502, response.text
        assert "bot challenge" in response.json()["error"]["message"]
        assert "duckduckgo" in response.json()["error"]["message"]

        record = await latest_record(session)
        assert record.own_search_requests == 1
        assert record.own_search_backend == "duckduckgo"
        assert record.upstream_status == 202
        assert record.status is UsageStatus.UPSTREAM_ERROR

    async def test_a_vendor_error_keeps_its_status_and_its_count(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Summarised rather than verbatim — an HTML error cannot travel as
        JSON — but the status is the vendor's own, and the count stays."""
        anchor = await add_duckduckgo(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        set_html(fake_upstream, "<html><body>error</body></html>", status=500)

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 500, response.text
        assert response.json() == {"error": {"message": "duckduckgo answered 500."}}

        record = await latest_record(session)
        assert record.own_search_requests == 1
        assert record.own_search_backend == "duckduckgo"

    async def test_the_query_is_screened_before_duckduckgo_sees_it(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The form field carries the redacted query too — the route screens
        the one field it reads, whichever media type the policy backend
        speaks."""
        anchor = await add_duckduckgo(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        set_html(fake_upstream, results_page())
        app.state.redactor = _ShoutyRedactor()  # type: ignore[attr-defined]

        response = await client.post(
            "/v1/search", json={"query": "contact Mario Rossi"}, headers=seeded.auth
        )
        assert response.status_code == 200, response.text
        assert fake_upstream.last_body == {"q": "contact <PERSON_X>"}


class _ShoutyRedactor:
    """Replaces one fixed name, deterministically — the seam, not the policy."""

    name = "test"

    async def redact_request(
        self, messages: list[dict], *, policy: object | None = None
    ) -> RedactionOutcome:
        rewritten = []
        for message in messages:
            text = str(message.get("content") or "")
            text = text.replace("Mario Rossi", "<PERSON_X>")
            rewritten.append({**message, "content": text})
        return RedactionOutcome(messages=rewritten, entity_count=0, engine="test")

    def response_stage(self, outcome: RedactionOutcome):  # type: ignore[no-untyped-def]
        from gateway.sse.pipeline import passthrough

        return passthrough

    async def restore_response(
        self, text: str, outcome: RedactionOutcome
    ):  # type: ignore[no-untyped-def]
        from gateway.redaction import Restored

        return Restored(text=text, restored=False)


class TestPassthroughRefusal:
    async def test_the_passthrough_refuses_a_form_backend(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """There is no verbatim JSON in an HTML page, so the verbatim route
        names the unified one instead — before metering, so nothing is
        counted for a request that never ran."""
        await add_duckduckgo(session, seeded)
        set_html(fake_upstream, results_page())

        response = await client.post(
            "/v1/search/duckduckgo", json={"q": "anything"}, headers=seeded.auth
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "form_backend_has_no_passthrough"
        assert len(fake_upstream.bodies) == 0
        rows = (await session.execute(select(UsageRecord))).scalars().all()
        assert rows == []


class TestAdminFlow:
    async def test_a_keyless_backend_is_creatable_with_its_anchor(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        admin_session: dict[str, str],
    ) -> None:
        """The whole first-class shape through the existing admin API: no
        credential, no base URL — the plugin's default is the whole answer —
        and the grant anchor arrives with the provider."""
        response = await client.post(
            "/api/admin/providers",
            json={"name": "duckduckgo", "plugin": "duckduckgo", "kind": "search"},
            headers=admin_session,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["has_api_key"] is False
        assert body["base_url"] == "https://html.duckduckgo.com"

        anchor = (
            await session.execute(
                select(ModelDef).where(
                    ModelDef.provider_id == uuid.UUID(body["id"]),
                    ModelDef.kind == ModelKind.SEARCH,
                )
            )
        ).scalar_one()
        assert anchor.name == "duckduckgo"
        assert anchor.upstream_model == "search"

    async def test_policy_assignment_then_search_end_to_end(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
        admin_session: dict[str, str],
    ) -> None:
        """Grant the anchor, point the group's policy at it, search: the
        unified route serves DDG like it serves the JSON backends."""
        created = await client.post(
            "/api/admin/providers",
            json={
                "name": "duckduckgo",
                "plugin": "duckduckgo",
                "kind": "search",
                "base_url": UPSTREAM_BASE,
            },
            headers=admin_session,
        )
        assert created.status_code == 201, created.text
        anchor_id = (
            await session.execute(
                select(ModelDef.id).where(ModelDef.provider_id == uuid.UUID(created.json()["id"]))
            )
        ).scalar_one()

        granted = await client.put(
            f"/api/admin/groups/{seeded.group.id}/models/{anchor_id}",
            headers=admin_session,
        )
        assert granted.status_code == 204, granted.text
        pointed = await client.put(
            f"/api/admin/groups/{seeded.group.id}/search-backend",
            json={"model_id": str(anchor_id)},
            headers=admin_session,
        )
        assert pointed.status_code == 204, pointed.text
        set_html(fake_upstream, results_page())

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert response.json()["backend"] == "duckduckgo"
        record = await latest_record(session)
        assert record.own_search_requests == 1
        assert record.own_search_backend == "duckduckgo"
        assert record.own_search_tier is None
