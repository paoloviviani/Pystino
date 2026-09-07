"""``POST /v1/ocr``: one surface, metered by the page.

What is pinned here is the part that would otherwise be found on an invoice:
that the page count comes from the counterparty's own `usage_info` and not from
the length of the text it returned, that a page is charged as a unit, that an
OCR model is refused on the surfaces it does not belong to, and that the
document reaches the provider in the shape the caller sent it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx
import orjson
from conftest import FakeUpstream, Seeded
from gateway.models import (
    ApiSurface,
    GroupModelAccess,
    ModelDef,
    ModelKind,
    ModelPrice,
    Provider,
    UsageRecord,
    UsageSource,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_admin import as_user, make_admin

DOCUMENT = {"type": "document_url", "document_url": "https://example.org/invoice.pdf"}

IBAN = "IT60X0542811101000000123456"


def install_detector(app: Any, findings: list[tuple[str, str]]) -> None:
    """Give the app a detector that finds exactly `(entity_type, needle)`.

    Uses the same fake-detector approach as test_redaction_http: what is under
    test here is the gateway's half — that the extracted text goes through
    detection at all and comes back substituted — not whether Presidio can
    recognise an IBAN, which is Presidio's test and runs in services/redaction.
    """
    from gateway.config import EntityMode, RedactionPolicy, RedactionSettings
    from gateway.redaction.http import HttpDetectionRedactor
    from pydantic import SecretStr

    def handler(request: httpx.Request) -> httpx.Response:
        payload = orjson.loads(request.content)
        out = []
        for index, text in enumerate(payload["texts"]):
            spans = []
            for entity_type, needle in findings:
                start = text.find(needle)
                while start != -1:
                    spans.append(
                        {
                            "start": start,
                            "end": start + len(needle),
                            "entity_type": entity_type,
                            "score": 0.99,
                        }
                    )
                    start = text.find(needle, start + 1)
            out.append({"index": index, "spans": spans})
        return httpx.Response(200, json={"findings": out, "engine": "fake"})

    # No resolver, so the redactor's own policy is the effective one. With a
    # resolver present and no rules written, ADR 0039's default applies and
    # nothing is substituted — correct behaviour, and it would make every
    # assertion below pass against a redactor that does nothing.
    app.state.redaction = None
    app.state.redactor = HttpDetectionRedactor(
        RedactionSettings(
            engine="http",
            endpoint="http://detector:8080",
            placeholder_key=SecretStr("ocr-test-key"),
            # Without this the policy default is "off" and every assertion
            # below would pass against a redactor that does nothing.
            policy=RedactionPolicy(default_mode=EntityMode.ANONYMISE_RESTORE),
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )



async def add_ocr_model(
    session: AsyncSession,
    seeded: Seeded,
    *,
    name: str = "reader",
    per_page: str | None = "0.01",
    upstream: str = "mistral-ocr-4.1",
) -> ModelDef:
    model = ModelDef(
        name=name, upstream_model=upstream, provider_id=seeded.provider.id, kind=ModelKind.OCR
    )
    session.add(model)
    await session.flush()
    session.add(
        ModelPrice(
            model_id=model.id,
            # An OCR model that charges nothing per token is the normal case,
            # and zero rates here prove the page charge is not coming from them.
            input_per_mtok=Decimal(0),
            output_per_mtok=Decimal(0),
            per_page=Decimal(per_page) if per_page else None,
            currency="EUR",
        )
    )
    session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
    await session.commit()
    return model


def ocr_response(pages: int = 3, **extra: Any) -> dict[str, Any]:
    """A response in the shape Cortecs and Mistral document."""
    body: dict[str, Any] = {
        "model": "mistral-ocr-4.1",
        "pages": [
            {"index": index, "markdown": f"page {index} text", "images": []}
            for index in range(pages)
        ],
        "usage_info": {"pages_processed": pages, "credits": 0.03},
    }
    body.update(extra)
    return body


async def latest_record(session: AsyncSession) -> UsageRecord:
    return (
        (
            await session.execute(
                select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1)
            )
        )
        .scalars()
        .one()
    )


class TestMetering:
    async def test_pages_are_read_from_usage_info_and_billed(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """`usage_info.pages_processed`, not `usage`, and not the text length."""
        model = await add_ocr_model(session, seeded, per_page="0.01")
        fake_upstream.set_json(ocr_response(pages=7))

        response = await client.post(
            "/v1/ocr", json={"model": model.name, "document": DOCUMENT}, headers=seeded.auth
        )
        assert response.status_code == 200

        record = await latest_record(session)
        assert record.api_surface is ApiSurface.OCR
        assert record.cost == Decimal("0.07")
        # Exactly known, because the counterparty counted it.
        assert record.usage_source is UsageSource.UPSTREAM_EXACT
        # No tokens on this surface: reporting some would bill one request twice.
        assert record.total_tokens == 0

    async def test_an_unpriced_ocr_model_records_zero_rather_than_guessing(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The same rule as an unpriced chat model: no cost ceiling at all, and
        the console's unpriced count is what warns about it."""
        model = await add_ocr_model(session, seeded, name="unpriced", per_page=None)
        fake_upstream.set_json(ocr_response(pages=4))

        response = await client.post(
            "/v1/ocr", json={"model": model.name, "document": DOCUMENT}, headers=seeded.auth
        )
        assert response.status_code == 200
        record = await latest_record(session)
        assert record.cost == Decimal(0)

    async def test_the_document_reaches_the_provider_unchanged(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Options are forwarded rather than validated: the set is long,
        provider-specific and grows without us."""
        model = await add_ocr_model(session, seeded, name="passthrough")
        fake_upstream.set_json(ocr_response(pages=1))

        await client.post(
            "/v1/ocr",
            json={
                "model": model.name,
                "document": DOCUMENT,
                "table_format": "html",
                "eu_native": True,
                "confidence_scores_granularity": "page",
            },
            headers=seeded.auth,
        )
        sent = fake_upstream.last_body
        assert sent["document"] == DOCUMENT
        assert sent["table_format"] == "html"
        assert sent["eu_native"] is True
        # The upstream's own model id, not ours.
        assert sent["model"] == "mistral-ocr-4.1"

    async def test_our_model_name_is_echoed_not_the_upstreams(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Clients compare the echoed name with what they sent."""
        model = await add_ocr_model(session, seeded, name="ours")
        fake_upstream.set_json(ocr_response(pages=1))

        response = await client.post(
            "/v1/ocr", json={"model": model.name, "document": DOCUMENT}, headers=seeded.auth
        )
        assert response.json()["model"] == "ours"
        record = await latest_record(session)
        assert record.upstream_model == "mistral-ocr-4.1"

    async def test_the_pages_come_back_to_the_caller(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_ocr_model(session, seeded, name="passthru2")
        fake_upstream.set_json(ocr_response(pages=2))

        body = (
            await client.post(
                "/v1/ocr", json={"model": model.name, "document": DOCUMENT}, headers=seeded.auth
            )
        ).json()
        assert [page["index"] for page in body["pages"]] == [0, 1]
        assert body["usage_info"]["pages_processed"] == 2


class TestRefusals:
    async def test_a_chat_model_is_refused_here(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        """Sent to the right route rather than forwarded to fail upstream with a
        provider-specific error nobody can act on."""
        response = await client.post(
            "/v1/ocr", json={"model": seeded.model.name, "document": DOCUMENT}, headers=seeded.auth
        )
        assert response.status_code == 400
        assert "/v1/chat/completions" in response.text

    async def test_an_ocr_model_is_refused_on_the_chat_route(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        model = await add_ocr_model(session, seeded, name="reader2")
        response = await client.post(
            "/v1/chat/completions",
            json={"model": model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 400
        assert "/v1/ocr" in response.text

    async def test_a_document_with_no_location_is_refused(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        model = await add_ocr_model(session, seeded, name="reader3")
        response = await client.post(
            "/v1/ocr",
            json={"model": model.name, "document": {"type": "document_url"}},
            headers=seeded.auth,
        )
        assert response.status_code == 400

    async def test_a_model_nobody_granted_is_invisible(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        """The allowlist holds on this surface too: absence of a grant means no
        access, and the answer must not distinguish "exists" from "yours"."""
        model = ModelDef(
            name="ungranted",
            upstream_model="mistral-ocr-4.1",
            provider_id=seeded.provider.id,
            kind=ModelKind.OCR,
        )
        session.add(model)
        await session.commit()

        response = await client.post(
            "/v1/ocr", json={"model": "ungranted", "document": DOCUMENT}, headers=seeded.auth
        )
        assert response.status_code == 404


# --------------------------------------------------------------------------
# The local backend
# --------------------------------------------------------------------------

INLINE_DOCX = (
    "data:application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ";base64,UEsDBBQAAAAIAA=="
)


class FakeExtractor:
    """The extraction service, answering on the control-plane client."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        self.headers: list[httpx.Headers] = []
        self._payload: dict[str, Any] = {
            "kind": "text",
            "text": "Invoice for Luca Bianchi",
            "extractor": "markitdown",
            "pages": 0,
        }

    def set(self, **payload: Any) -> None:
        self._payload = payload

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.bodies.append(request.content)
            self.headers.append(request.headers)
            return httpx.Response(200, json=self._payload)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def add_local_model(
    session: AsyncSession,
    seeded: Seeded,
    *,
    name: str = "local-reader",
    per_page: str | None = "0.001",
) -> ModelDef:
    """A model whose provider is this deployment's own extractor."""
    provider = Provider(
        name=f"{name}-provider",
        base_url="http://extractor:8080",
        plugin="extractor",
    )
    session.add(provider)
    await session.flush()
    model = ModelDef(
        name=name, upstream_model="markitdown", provider_id=provider.id, kind=ModelKind.OCR
    )
    session.add(model)
    await session.flush()
    session.add(
        ModelPrice(
            model_id=model.id,
            input_per_mtok=Decimal(0),
            output_per_mtok=Decimal(0),
            per_page=Decimal(per_page) if per_page else None,
            currency="EUR",
        )
    )
    session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
    await session.commit()
    return model


class TestLocalBackend:
    async def test_the_document_never_leaves_the_deployment(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The whole point of the local backend: no provider is called at all."""
        extractor = FakeExtractor()
        app.state.control_http = extractor.client()
        model = await add_local_model(session, seeded)

        response = await client.post(
            "/v1/ocr",
            json={
                "model": model.name,
                "document": {"type": "document_url", "document_url": INLINE_DOCX},
            },
            headers=seeded.auth,
        )
        assert response.status_code == 200
        # The extractor saw the bytes; the upstream saw nothing.
        assert extractor.bodies, "the extraction service was not called"
        assert not fake_upstream.bodies

        body = response.json()
        assert body["pages"][0]["markdown"] == "Invoice for Luca Bianchi"
        assert body["extractor"] == "markitdown"
        assert body["model"] == model.name

    async def test_a_url_is_refused_rather_than_fetched(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        """A security decision, not a missing feature: fetching a caller-supplied
        URL from the gateway is server-side request forgery against our own
        network."""
        extractor = FakeExtractor()
        app.state.control_http = extractor.client()
        model = await add_local_model(session, seeded, name="local-url")

        response = await client.post(
            "/v1/ocr",
            json={
                "model": model.name,
                "document": {"type": "document_url", "document_url": "http://169.254.169.254/"},
            },
            headers=seeded.auth,
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "document_url_not_supported"
        # Nothing was fetched, by us or by anyone.
        assert not extractor.bodies

    async def test_a_scan_is_refused_with_the_reason_not_an_empty_success(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        """An empty `pages` array is indistinguishable from a blank document, so
        a caller that indexed it would have indexed nothing and not known."""
        extractor = FakeExtractor()
        extractor.set(kind="no_text_layer", text="", extractor="markitdown", pages=12)
        app.state.control_http = extractor.client()
        model = await add_local_model(session, seeded, name="local-scan")

        response = await client.post(
            "/v1/ocr",
            json={
                "model": model.name,
                "document": {"type": "document_url", "document_url": INLINE_DOCX},
            },
            headers=seeded.auth,
        )
        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "no_text_layer"
        # It names the next step rather than just failing.
        assert "OCR model" in body["error"]["message"]

    async def test_a_refused_document_is_not_charged(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        extractor = FakeExtractor()
        extractor.set(kind="unsupported", text="", extractor="", pages=0)
        app.state.control_http = extractor.client()
        model = await add_local_model(session, seeded, name="local-unsupported")

        await client.post(
            "/v1/ocr",
            json={
                "model": model.name,
                "document": {"type": "document_url", "document_url": INLINE_DOCX},
            },
            headers=seeded.auth,
        )
        record = await latest_record(session)
        assert record.cost == Decimal(0)

    async def test_a_pdf_page_count_is_billed_locally(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        """A deployment may price its own extractor — an internal chargeback —
        and the count comes from the document, never from the text length."""
        extractor = FakeExtractor()
        extractor.set(kind="text", text="page one page two", extractor="markitdown", pages=2)
        app.state.control_http = extractor.client()
        model = await add_local_model(session, seeded, name="local-priced", per_page="0.001")

        response = await client.post(
            "/v1/ocr",
            json={
                "model": model.name,
                "document": {"type": "document_url", "document_url": INLINE_DOCX},
            },
            headers=seeded.auth,
        )
        assert response.json()["usage_info"]["pages_processed"] == 2
        record = await latest_record(session)
        assert record.cost == Decimal("0.002")

    async def test_a_format_without_pages_reports_zero_not_one(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        """A spreadsheet has no pages until something picks a paper size.
        Reporting one would be a billing figure invented to look tidy."""
        extractor = FakeExtractor()
        extractor.set(kind="text", text="a,b,c", extractor="markitdown", pages=0)
        app.state.control_http = extractor.client()
        model = await add_local_model(session, seeded, name="local-sheet", per_page="0.001")

        response = await client.post(
            "/v1/ocr",
            json={
                "model": model.name,
                "document": {"type": "document_url", "document_url": INLINE_DOCX},
            },
            headers=seeded.auth,
        )
        assert response.json()["usage_info"]["pages_processed"] == 0
        record = await latest_record(session)
        assert record.cost == Decimal(0)

    async def test_an_extractor_that_is_down_is_a_502_like_any_provider(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        app.state.control_http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        model = await add_local_model(session, seeded, name="local-down")

        response = await client.post(
            "/v1/ocr",
            json={
                "model": model.name,
                "document": {"type": "document_url", "document_url": INLINE_DOCX},
            },
            headers=seeded.auth,
        )
        assert response.status_code == 502


# --------------------------------------------------------------------------
# The response is the sensitive half
# --------------------------------------------------------------------------


class TestResponseRedaction:
    """Extraction is where a picture of a document becomes searchable text.

    Every other surface redacts the request and restores placeholders on the
    way back. Here the document left as bytes or as a URL — with a URL we never
    held it — so the only place PII can be caught is the response, and these
    assert it is caught there.
    """

    async def test_extracted_text_is_redacted_before_the_caller_sees_it(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        install_detector(app, [("IBAN_CODE", IBAN)])
        model = await add_ocr_model(session, seeded, name="redacted-ocr")
        fake_upstream.set_json(
            {
                "model": "mistral-ocr-4.1",
                "pages": [{"index": 0, "markdown": f"Pay to {IBAN} immediately."}],
                "usage_info": {"pages_processed": 1},
            }
        )

        response = await client.post(
            "/v1/ocr", json={"model": model.name, "document": DOCUMENT}, headers=seeded.auth
        )
        assert response.status_code == 200
        markdown = response.json()["pages"][0]["markdown"]
        assert IBAN not in markdown
        assert "<IBAN_CODE_" in markdown

    async def test_the_row_records_what_the_response_cost_and_what_it_hid(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The count is discovered after the row is created, so it has to be
        written at settle — a row saying zero for a request that had one
        replaced is worse than one saying nothing."""
        install_detector(app, [("IBAN_CODE", IBAN)])
        model = await add_ocr_model(session, seeded, name="counted-ocr", per_page="0.01")
        fake_upstream.set_json(
            {
                "model": "mistral-ocr-4.1",
                "pages": [{"index": 0, "markdown": f"Pay {IBAN}."}],
                "usage_info": {"pages_processed": 2},
            }
        )

        await client.post(
            "/v1/ocr", json={"model": model.name, "document": DOCUMENT}, headers=seeded.auth
        )
        record = await latest_record(session)
        assert record.redacted_entity_count == 1
        assert record.redaction_engine is not None
        # Charged for what was read, whatever was then hidden.
        assert record.cost == Decimal("0.02")

    async def test_every_page_is_walked_not_only_the_first(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        install_detector(app, [("IBAN_CODE", IBAN)])
        model = await add_ocr_model(session, seeded, name="multipage-ocr")
        fake_upstream.set_json(
            {
                "model": "mistral-ocr-4.1",
                "pages": [
                    {"index": 0, "markdown": "cover page"},
                    {"index": 1, "markdown": f"account {IBAN}"},
                ],
                "usage_info": {"pages_processed": 2},
            }
        )

        body = (
            await client.post(
                "/v1/ocr", json={"model": model.name, "document": DOCUMENT}, headers=seeded.auth
            )
        ).json()
        assert IBAN not in orjson.dumps(body).decode()

    async def test_locally_extracted_text_is_redacted_too(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        """The local path is not a shortcut past the policy: a document read
        here still has its text inspected before it is handed over."""
        extractor = FakeExtractor()
        extractor.set(
            kind="text", text=f"Transfer to {IBAN}", extractor="markitdown", pages=1
        )
        app.state.control_http = extractor.client()
        install_detector(app, [("IBAN_CODE", IBAN)])
        model = await add_local_model(session, seeded, name="local-redacted")

        response = await client.post(
            "/v1/ocr",
            json={
                "model": model.name,
                "document": {"type": "document_url", "document_url": INLINE_DOCX},
            },
            headers=seeded.auth,
        )
        assert response.status_code == 200
        assert IBAN not in response.json()["pages"][0]["markdown"]

    async def test_a_clean_document_is_returned_untouched(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        install_detector(app, [])
        model = await add_ocr_model(session, seeded, name="clean-ocr")
        fake_upstream.set_json(
            {
                "model": "mistral-ocr-4.1",
                "pages": [{"index": 0, "markdown": "Minutes of the meeting."}],
                "usage_info": {"pages_processed": 1},
            }
        )

        body = (
            await client.post(
                "/v1/ocr", json={"model": model.name, "document": DOCUMENT}, headers=seeded.auth
            )
        ).json()
        assert body["pages"][0]["markdown"] == "Minutes of the meeting."
        record = await latest_record(session)
        assert record.redacted_entity_count == 0


def test_the_wire_contract_accepts_the_ocr_kind() -> None:
    """Found live, not by a test.

    `ModelCreateRequest.kind` is a `Literal` spelled out by hand, so adding a
    value to `ModelKind` left the API answering "kind: Input should be 'chat',
    'embedding' or 'image'". An OCR model was impossible to create through the
    API or the console while the surface that serves them worked perfectly —
    which is why this asserts the two agree rather than trusting that they do.
    """
    from gateway.models import ModelKind
    from gateway.schemas import ModelCreateRequest, ModelUpdateRequest

    for kind in ModelKind:
        assert ModelCreateRequest(
            name=f"m-{kind.value}", upstream_model="x", provider_id=uuid.uuid4(), kind=kind.value
        ).kind == kind.value
        assert ModelUpdateRequest(kind=kind.value).kind == kind.value


class TestDiscoverAgainstTheBuiltInExtractor:
    """Found in the console, on a phone: pressing Discover on the local
    extractor answered

        Could not read the provider catalogue: could not fetch
        http://extractor:8080/models: 404 Not Found

    — an error naming a URL the operator never typed, for the one provider that
    cannot have a catalogue endpoint because it is not a counterparty. A plugin
    that *is* the thing being served answers the question itself.
    """

    async def test_it_offers_its_own_model_instead_of_failing(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:

        provider = Provider(
            name="local-extractor", base_url="http://extractor:8080", plugin="extractor"
        )
        session.add(provider)
        await session.commit()

        # No transport for /models at all: if the route reaches for the network
        # this fails, which is the point.
        def refuse(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"the route fetched {request.url}")

        app.state.control_http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))

        as_user(app, await make_admin(session_factory, seeded))
        response = await client.get(f"/api/admin/models/discover?provider_id={provider.id}")
        assert response.status_code == 200
        body = response.json()
        offered = {row["upstream_model"] for row in body["available"]}
        assert offered == {"markitdown"}

    async def test_its_model_is_offered_as_ocr_and_unpriced(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:
        """Unpriced rather than free: what a deployment charges for reading a
        document locally is nobody else's decision, and a rate invented here
        would be a ledger figure with no source."""

        provider = Provider(
            name="local-extractor-2", base_url="http://extractor:8080", plugin="extractor"
        )
        session.add(provider)
        await session.commit()

        as_user(app, await make_admin(session_factory, seeded))
        response = await client.get(f"/api/admin/models/discover?provider_id={provider.id}")
        row = response.json()["available"][0]
        assert row["kind"] == "ocr"
        assert row["input_per_mtok"] is None
        assert row["per_page"] is None
        assert row["blocked_reason"] is not None
