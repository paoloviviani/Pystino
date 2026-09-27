"""This deployment's own document extractor.

The one counterparty that is not one. A provider row naming this plugin is not
an address on the internet — it is the `extractor` service on the compose
network, reading office documents and text-layer PDFs locally with markitdown.

Registering it as a plugin rather than inventing a second discriminator is the
point: `providers.plugin` already answers "what kind of counterparty is this",
the console already renders a type selector from the registry, and access,
grants, prices and the ledger all work on a model whose provider happens to be
local. What changes is the *route's* dispatch — `/v1/ocr` sends the document to
the extraction service instead of an upstream `/ocr` — and nothing else.

The kind is `internal`, and that is the row's visibility contract. A model
served here is plumbing the deployment stands up by default (migration 0041
seeds the provider and its model, in the shape the manual path used), not a
catalogue entry a caller picks: `/v1/models` leaves it out and the console's
model list does not render it, the way search tiers are left out for the same
reason. It is hidden, not unaccounted — usage meters to the ledger, an
administrator can still grant or price the row, and a caller whose grants
reach it may ask for it by name. It can be deactivated, never deleted
(`routers/admin.py`'s `delete_provider`/`delete_model`, both 409): this row is
the deployment's own infrastructure, and "deleted" would only mean "re-seeded
absent, until the next fresh install brings it back" — not a real removal.

Two claims it makes, both true by construction rather than by configuration:

* **It reports no cost.** There is no counterparty to charge us, so
  `reported_cost` stays the generic "nothing", and a model served this way is
  billed from our own price row or not at all. A `per_page` rate on a local
  model is a perfectly reasonable thing for a deployment to set — an internal
  chargeback — and an unpriced one costs nothing, visibly.
* **It never sees a credential.** `auth_headers` is empty: the service has no
  authentication of its own because it is not reachable off the compose
  network, exactly like the detection service beside it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import httpx

from gateway import extraction
from gateway.plugins.base import ProbeResult, ProviderKind
from gateway.plugins.generic import GenericOpenAIPlugin
from gateway.upstream import UpstreamError

if TYPE_CHECKING:
    from gateway.upstream import OpenAICompatibleUpstream


def _probe_document() -> bytes:
    """A minimal, valid one-page PDF: a real document the extractor can read
    text back from, built rather than checked into the repo as a fixture —
    this is the whole of what "one page of text" requires, computed so the
    xref table (which some readers use instead of scanning for ``obj``) is
    correct rather than approximately so.
    """
    stream = b"BT /F1 24 Tf 20 100 Td (Pystino provider test) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, content in enumerate(objects, start=1):
        offsets.append(len(body))
        body += b"%d 0 obj\n%s\nendobj\n" % (index, content)

    xref_offset = len(body)
    body += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets[1:]:
        body += b"%010d 00000 n \n" % offset
    body += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objects) + 1,
        xref_offset,
    )
    return bytes(body)


class LocalExtractorPlugin(GenericOpenAIPlugin):
    """Documents read here, by us, with nothing leaving the deployment."""

    name = "extractor"
    label = "Local document extractor"
    description = (
        "This deployment's own extractor: Word, Excel, PowerPoint and "
        "text-layer PDFs, read locally with markitdown. Nothing leaves the "
        "deployment, and scanned pages are refused rather than guessed at — "
        "reading those needs an OCR model."
    )
    #: The compose service is infrastructure, not a counterparty. Its model
    #: row exists so grants, prices and the ledger have something to hang on;
    #: `ProviderKind.INTERNAL` is what listings read to leave it out.
    kind = ProviderKind.INTERNAL
    #: The service on the compose network. A default rather than a requirement:
    #: a deployment that scales the extractor separately points the provider row
    #: at wherever it put it.
    default_base_url: str | None = "http://extractor:8080"

    def auth_headers(self, api_key: str | None) -> Mapping[str, str]:
        """None. The extractor is not reachable off the compose network, and a
        credential it would ignore is a credential someone has to rotate."""
        return {}

    def builtin_catalogue(self) -> dict[str, Any] | None:
        """What this deployment's extractor offers: one model, itself.

        Answered here rather than fetched, because there is nothing to fetch —
        the service serves `/extract`, not `/models`, and Discover against it
        used to fail with a 404 naming a URL the operator never typed.

        No price. What a deployment charges its own users for reading a
        document locally is a decision nobody else can make, and inventing a
        rate here would put a number in the ledger with no source. It imports
        unpriced and the console's unpriced warning says so — the same
        treatment any model with no published price gets.
        """
        return {
            "data": [
                {
                    "id": "markitdown",
                    "description": (
                        "Word, Excel, PowerPoint and text-layer PDFs, read inside this "
                        "deployment. Scanned pages are refused rather than guessed at."
                    ),
                    "tags": ["OCR"],
                    "input_modalities": ["file"],
                    "output_modalities": ["text"],
                    # No `pricing`, so the parser reports it unpriced rather
                    # than free — the distinction the console renders as a
                    # warning.
                }
            ]
        }

    def catalogue_tag_all(self) -> str | None:
        """No tag filter here — the catalogue is one row and all of it."""
        return None

    async def probe(
        self, upstream: OpenAICompatibleUpstream, client: httpx.AsyncClient
    ) -> ProbeResult:
        """``GET /healthz``, then a real ``/extract`` on a tiny built-in PDF.

        Not ``list_models()``: this service serves ``/healthz``, ``/extract``
        and ``/detect`` and nothing resembling a catalogue, so the generic
        probe answered a 404 with "check the base URL includes the version
        path, e.g. /v1" — a false failure for a provider working exactly as
        documented, and the bug this method exists to fix. The extract call
        goes through ``gateway.extraction``, the same function `/v1/ocr`
        itself calls, so a passing probe means the real route works too.
        """
        base = upstream.base_url
        try:
            health = await client.get(f"{base.rstrip('/')}/healthz")
        except httpx.HTTPError as exc:
            return ProbeResult(ok=False, detail=f"could not reach {base}: {exc}")
        if health.status_code >= 400:
            return ProbeResult(
                ok=False,
                status_code=health.status_code,
                detail=f"the extractor answered {health.status_code} at /healthz",
            )

        try:
            outcome = await extraction.extract_document(
                client, base, _probe_document(), media_type="application/pdf"
            )
        except UpstreamError as exc:
            return ProbeResult(ok=False, detail=str(exc))

        try:
            extraction.as_ocr_response(outcome, model_name="markitdown")
        except extraction.DocumentRefused as exc:
            return ProbeResult(
                ok=False, detail=f"reachable, but the sample page was refused: {exc}"
            )

        return ProbeResult(
            ok=True,
            status_code=200,
            detail="reachable; extracted a sample page (markitdown)",
        )
