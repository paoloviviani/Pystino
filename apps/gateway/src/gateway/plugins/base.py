"""What a counterparty plugin is, and the one rule it must obey.

See [ADR 0032](../../../../docs/adr/0032-provider-plugins.md). Two ideas carry
the design.

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

The rule is enforced by these return types rather than by review. There is
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
    """Whether the serving endpoint is implied by the model or chosen per request."""

    PROVIDER = "provider"
    ROUTER = "router"


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


@runtime_checkable
class ProviderPlugin(Protocol):
    """Everything the gateway needs to know that is specific to a counterparty."""

    name: str
    #: A human name for the console's type selector. The plugin *is* the
    #: provider type as far as an operator is concerned, so this is the label
    #: they choose from.
    label: str
    #: One sentence on what picking this type means for billing — the thing an
    #: operator is actually deciding. Rendered under the selector.
    description: str
    kind: ProviderKind
    #: Whether this plugin's ``read_reported_cost`` asserts the counterparty's
    #: *actual charge* rather than an indicative number. Declared rather than
    #: probed, so a deployment can be stopped from selecting pass-through
    #: billing against a plugin that cannot make the claim — before a request is
    #: ever made, not after an invoice is wrong.
    reports_authoritative_cost: bool

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
