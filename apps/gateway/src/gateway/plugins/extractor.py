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

from gateway.plugins.generic import GenericOpenAIPlugin


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
    #: The service on the compose network. A default rather than a requirement:
    #: a deployment that scales the extractor separately points the provider row
    #: at wherever it put it.
    default_base_url: str | None = "http://extractor:8080"

    def auth_headers(self, api_key: str | None) -> Mapping[str, str]:
        """None. The extractor is not reachable off the compose network, and a
        credential it would ignore is a credential someone has to rotate."""
        return {}
