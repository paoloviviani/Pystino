"""What a counterparty plugin is, and the one rule it must obey.

See ADR 0032. Three ideas
carry the design.

**Providers and routers are different kinds**, distinguished by a single
question: is the endpoint that serves a request determined by the model asked
for, or chosen per request by the counterparty? Fixed is a provider; chosen is a
router. Everything else follows — a router must have the serving endpoint
recorded per request, has model substitution as a real case rather than a
theoretical one, and needs its reconciliation grouped by endpoint.

**A plugin returns facts and never computes money.** It normalises *inputs* —
token counts, which endpoint served, what the counterparty says it charged, what
its catalogue offers — and ``accounting/cost.py`` remains the only code that
multiplies a count by a rate. That holds under both billing modes: where a
deployment bills from the counterparty's figure, the plugin still only *reports*
a number it read and the gateway decides to treat it as the charge. The plugin
multiplies nothing either way.

**Vendor knowledge belongs here rather than in a column.** Three columns on
``providers`` were added reactively, one per discovery — ``auth_scheme`` when
Anthropic's own API refused a bearer token, ``forward_stream_options`` when a
counterparty documented that unknown parameters can narrow its routing pool,
``upstream_cost_unit`` when one turned out to report cost in micro-EUR. Each was
the right local fix and the pattern was the problem: the next counterparty adds
a fourth. ``auth_headers`` and ``prepare_payload`` are where those answers live
now, so a new counterparty is a new plugin rather than a new migration.

The money rule is enforced by these return types rather than by review. There is
nothing here a plugin could return that would let it price a request.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from gateway.accounting.cost import TokenCounts
from gateway.models import ApiSurface


class ProviderKind(enum.StrEnum):
    """What kind of counterparty stands behind a provider row.

    The original two were distinguished by a single question: is the endpoint
    that serves a request determined by the model asked for, or chosen per
    request by the counterparty? Fixed is a provider; chosen is a router.
    Search and internal are not answers to that question — they are the
    discovery that the counterparty axis has more than inference on it — and
    they stay kinds rather than becoming a second discriminator beside
    ``providers.plugin``, because everything a row of this kind needs (an
    address, a credential, grants, a place in the ledger) is what a provider
    row already is. Mirrored in ``gateway.models`` so the model layer need not
    import the plugin package.
    """

    PROVIDER = "provider"
    ROUTER = "router"
    #: Not an inference endpoint at all: a web-search backend (Linkup, Exa, Jina).
    #: The third kind docs/web-search-plan.md asked for, and it is a *kind*
    #: rather than a separate table because everything a search backend needs —
    #: an address, a credential encrypted at rest, an activation switch, models
    #: an administrator grants to a group — is what a provider row already is.
    #: What differs is the one thing a row cannot express: the plugin runs a
    #: search instead of a completion, which is `plugins/search.py`.
    SEARCH = "search"
    #: Not a counterparty at all: this deployment's own service, reached over
    #: the compose network. The document extractor is the case
    #: (`plugins/extractor.py`) — a provider row naming it is infrastructure
    #: with a model row so grants, prices and the ledger have something to hang
    #: on, not a catalogue entry a caller picks. Listings therefore hide these
    #: models the way they hide search tiers, and for the same reason: a client
    #: presenting "markitdown" beside the chat models would present plumbing as
    #: a choice. Hiding is presentation, not access — a caller whose grants
    #: reach the model may still ask for it by name, and usage meters to the
    #: ledger exactly as any other model's does.
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class ServedBy:
    """Which endpoint actually ran a request, as the counterparty reported it.

    Only ever populated for a router, because for a provider the answer is
    already the provider. ``model`` is the model *it* says it served, which is
    how substitution is detected — a router may serve something other than what
    was asked for, and the requested model is what we priced.
    """

    endpoint: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ReportedCost:
    """What the counterparty says it charged, in its own unit.

    Three fields, because three separate claims are involved and conflating them
    is how a figure ends up billed from when it should not have been:

    * ``amount`` and ``currency`` — the figure, in the unit it arrived in. Never
      converted. If an upstream bills dollars, this is dollars and stays dollars.

      The unit is the plugin's to know, because nothing in a payload says it:
      the reference router sends ``"cost": 136`` meaning 136 micro-EUR, while
      OpenRouter sends a decimal number of credits, and reading either as the
      other is wrong by a factor of a million. This is why the answer moved out
      of ``providers.upstream_cost_unit`` and in here — a plugin knows its
      counterparty; an operator filling in a form is guessing.
    * ``authoritative`` — whether the plugin asserts this is the counterparty's
      *actual charge* rather than an estimate or an indicative figure. A
      deployment cannot bill from a plugin that does not assert it.
    * ``details`` — the counterparty's own breakdown, kept verbatim. The shape is
      theirs, not ours, so it is not normalised.
    """

    amount: Decimal
    currency: str
    authoritative: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CataloguePrice:
    """One model's rates as the counterparty lists them, per Mtok.

    ``sub_provider`` is null for "any endpoint", which is every entry today. A
    router that genuinely prices per endpoint would emit one of these per
    endpoint; the gateway then prefers an exact match over the null row.
    """

    input_per_mtok: Decimal | None = None
    output_per_mtok: Decimal | None = None
    cache_read_per_mtok: Decimal | None = None
    cache_write_per_mtok: Decimal | None = None
    per_image: Decimal | None = None
    currency: str = "EUR"
    sub_provider: str | None = None


def bearer_headers(credential: str) -> dict[str, str]:
    """``Authorization: Bearer`` — what every OpenAI-compatible endpoint wants.

    Shared by the plugins that agree rather than copied into each, so the day one
    of them needs to differ, the difference is a deliberate override and visible
    as one.
    """
    return {"authorization": f"Bearer {credential}"}


@runtime_checkable
class ProviderPlugin(Protocol):
    """Everything the gateway needs to know that is specific to a counterparty."""

    name: str
    #: A human name for the console's type selector. The plugin *is* the
    #: provider type as far as an operator is concerned, so this is the label
    #: they choose from.
    label: str
    #: One or two sentences on what picking this type means for billing — the
    #: thing an operator is actually deciding. Rendered verbatim under the
    #: console's type selector, so it wants that register: declarative, third
    #: person, one fact per clause. Not "the prices you set" — the reader is
    #: choosing between types, not being addressed.
    description: str
    kind: ProviderKind
    #: The counterparty's public endpoint, when it has one worth pre-filling.
    #: Optional — a plugin for self-hosted endpoints (vLLM, Ollama) has no
    #: meaningful default, and ``None`` reads as that. Read with ``getattr`` at
    #: the call sites, so an installed plugin omitting the attribute entirely
    #: behaves the same as one declaring ``None``.
    default_base_url: str | None
    #: Whether this plugin's ``read_reported_cost`` asserts the counterparty's
    #: *actual charge* rather than an indicative number. Declared rather than
    #: probed, so a deployment can be stopped from selecting pass-through
    #: billing against a plugin that cannot make the claim — before a request is
    #: ever made, not after an invoice is wrong.
    reports_authoritative_cost: bool

    # -- shaping a request --------------------------------------------------

    def auth_headers(self, credential: str) -> Mapping[str, str]:
        """How this counterparty wants the credential presented.

        Called with the decrypted key, so it is never logged and never stored.
        Returns an empty mapping for an endpoint that needs none — a local vLLM
        or Ollama usually does not.

        This replaced ``providers.auth_scheme``. The two known answers are a
        bearer token and Anthropic's ``x-api-key``, and the reason it is per
        counterparty rather than per route is that the reference router serves
        ``/v1/messages`` with bearer like everything else (ADR 0030).
        """
        ...

    def prepare_payload(self, payload: dict[str, Any], *, surface: ApiSurface) -> dict[str, Any]:
        """The body as this counterparty wants it, given the body we would send.

        Called after redaction and after the upstream model name is substituted,
        so what arrives here is the request as it would go out. A plugin adds
        only what its counterparty needs; whatever the *client* set is already in
        the payload and must survive, because forwarding exactly what was sent —
        including parameters this gateway knows nothing about — is what keeps a
        model's behaviour the caller's business (ADR 0028).

        This replaced ``providers.forward_stream_options``. Mutating in place is
        allowed; the return value is what is sent.
        """
        ...

    # -- reading a response: facts only ------------------------------------

    def read_usage(self, usage: dict[str, Any] | None, *, surface: ApiSurface) -> TokenCounts:
        """Token counts from a usage object.

        Where the two prompt conventions and the several cache-token spellings
        are resolved. Returns counts; computes no money.
        """
        ...

    def read_served_by(
        self, payload: dict[str, Any] | None, headers: Mapping[str, str]
    ) -> ServedBy | None:
        """Which endpoint ran this, if the counterparty says.

        Given both the body and the headers because counterparties disagree
        about where they put it: some name it in the body, and the reference
        router names it **only** in a response header.
        """
        ...

    def read_reported_cost(self, usage: dict[str, Any] | None) -> ReportedCost | None:
        """What the counterparty says it charged, or None if it does not say."""
        ...
    def builtin_catalogue(self) -> dict[str, Any] | None:
        """A catalogue this plugin can answer from itself, or ``None``.

        ``None`` for every counterparty on the internet: what it offers is a
        question only it can answer, and the gateway asks over HTTP. A plugin
        that *is* the thing being served knows already, and asking the network
        would mean asking a service with no such endpoint.

        Part of the protocol rather than duck-typed, because "can you answer
        this yourself" is a fact about a counterparty and facts are what a
        plugin is for (ADR 0032). The payload is in the shape
        ``parse_catalogue`` reads, so a built-in answer is parsed, priced and
        kind-detected by exactly the code a fetched one is.
        """
        ...
