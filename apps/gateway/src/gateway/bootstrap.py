"""The rows a deployment is seeded with, and the rule that seeds them.

Reading a document should work the moment a deployment does. Today that took
three manual steps in the console — register a provider naming the extractor
plugin, import its one model, grant it — and a deployment that skipped them
answered every `/v1/ocr` request with a model-not-found. The seed removes the
steps by creating the same rows the manual path would have, exactly as
migration 0003 seeded the `default` provider from the environment: the row
becomes the deployment's, and the manual path stays available beside it.

The seed is a migration's work (0041 calls `seed_extractor`) and never a
startup write, for one property that matters more than it looks:

* the compose `migrate` service applies `alembic upgrade head` exactly once,
  before the gateway serves, so the seed is not raced by replicas; and
* alembic runs each revision once per database, which is what makes
  "never resurrect a deliberately deleted row" structural rather than a
  matter of flags. An operator who deletes the extractor's rows has a reason;
  the revision will never run again, so nothing re-creates them. A startup
  seed cannot make that promise — "seed when absent" resurrects a deleted row
  on every boot, and respecting a deletion would take a marker column or
  table whose only job is to remember an operator's decision twice.

The rule the seed follows, therefore the one thing it must never do: a
surviving provider row means this deployment's configuration is already
there, and a missing model beside it is exactly what a deliberate deletion
leaves behind. So rows are seeded as a pair, and only when no extractor
provider exists at all — a present provider with no model is left alone,
however it came to be that way.

Writes nothing it does not own and commits nothing: the caller owns the
transaction, which is what lets a migration call it and a test drive it twice.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import Connection, insert, select, update

from gateway.models import BillingMode, ModelDef, ModelKind, Provider, ProviderKind
from gateway.plugins.extractor import LocalExtractorPlugin
from gateway.types import utcnow

logger = logging.getLogger(__name__)

#: The client-facing model name, and what the gateway sends upstream. Both are
#: the plugin's own catalogue id — the name Discover suggests and the manual
#: path therefore used — pinned here rather than parsed out of
#: `builtin_catalogue()` at seed time, so the seed is readable as the rows it
#: creates. `test_extractor_bootstrap` pins the seed against the catalogue, so
#: the two cannot drift apart silently.
MODEL_NAME = "markitdown"


def normalize_extractor_kind(connection: Connection) -> None:
    """Re-type a pre-existing extractor provider row as `internal`.

    The manual path pre-dated the kind: this deployment's row was created
    through the console, which set the plugin's then-kind, `provider`. Once
    the plugin declares `internal`, that row is a lie twice over — the
    console renders a "kind mismatch" badge beside it (a configuration it is
    meant to point at, and here the configuration is what is changing), and
    every listing that hides internal rows would keep showing the model this
    change exists to hide. So the upgrade retypes the rows that name the
    plugin, and only those: an operator who set a different kind on a row
    that is *not* the extractor keeps it.

    Runs beside `seed_extractor` in the same revision, before it: retyping
    what exists and then seeding what does not cannot collide, and the order
    reads as the story.
    """
    spent = connection.execute(
        update(Provider)
        .where(
            Provider.plugin == LocalExtractorPlugin.name,
            Provider.kind != ProviderKind.INTERNAL,
        )
        .values(kind=ProviderKind.INTERNAL, updated_at=utcnow())
    )
    if spent.rowcount:
        logger.info(
            "retyped %d extractor provider row(s) as %r",
            spent.rowcount,
            ProviderKind.INTERNAL.value,
        )


def seed_extractor(connection: Connection) -> None:
    """Create the extractor's provider and model rows, when neither exists.

    Idempotent in the way that matters for a migration: calling it against a
    database that already has the rows — in any state, active or not — writes
    nothing. See the module docstring for why it never backfills a missing
    model beside a surviving provider.
    """
    plugin = LocalExtractorPlugin()

    # The plugin row is the identity question, not the name: an operator who
    # happened to name some other endpoint "extractor" before this ran should
    # get the seed skipped, not a second row welded to theirs.
    if connection.execute(select(Provider.id).where(Provider.plugin == plugin.name)).first():
        return

    # Name collisions are logged and skipped rather than failed. A migration
    # that aborts on an unlucky name turns an upgrade every deployment runs
    # into a cliff only one deployment falls off; the operator who sees no
    # extractor row can create one by hand, which is where they already were.
    if connection.execute(select(Provider.id).where(Provider.name == plugin.name)).first():
        logger.warning(
            "a provider named %r exists but does not name the %r plugin, so the "
            "extractor was not seeded; create it by hand if this deployment wants it",
            plugin.name,
            plugin.name,
        )
        return
    if connection.execute(select(ModelDef.id).where(ModelDef.name == MODEL_NAME)).first():
        logger.warning(
            "a model named %r exists but no extractor provider to own it, so the "
            "extractor was not seeded; create it by hand if this deployment wants it",
            MODEL_NAME,
        )
        return

    provider_id = uuid.uuid4()
    connection.execute(
        insert(Provider).values(
            id=provider_id,
            name=plugin.name,
            description="This deployment's own extractor service, seeded automatically.",
            # The plugin's default, not the `GATEWAY_EXTRACTOR__ENDPOINT`
            # setting: the row is where an operator points a deployment that
            # scaled the service elsewhere, and the route falls back to the
            # setting when the row still says the default.
            base_url=plugin.default_base_url,
            # No credential, by the same construction the plugin documents:
            # the service has no authentication because it is not reachable
            # off the compose network.
            api_key_encrypted="",
            api_key_hint="",
            extra_headers={},
            is_active=True,
            prefix="",
            plugin=plugin.name,
            kind=ProviderKind.INTERNAL,
            billing_mode=BillingMode.OWN_PRICES,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    connection.execute(
        insert(ModelDef).values(
            id=uuid.uuid4(),
            name=MODEL_NAME,
            upstream_model=MODEL_NAME,
            provider_id=provider_id,
            kind=ModelKind.OCR,
            is_active=True,
            # Public access (ADR 0045) is what "serves by default" means on the
            # access plane: any authenticated caller may read a document
            # without an administrator first knowing which group to grant. The
            # model is unpriced by design, so the widened access charges
            # nothing; redaction on the extracted text applies as everywhere
            # else. An operator who wants extraction scoped narrows this in
            # the console, and the seed will never widen it back.
            is_public=True,
            input_modalities=["file"],
            output_modalities=["text"],
            supported_features=[],
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    logger.info("seeded the %r provider and its %r model", plugin.name, MODEL_NAME)
