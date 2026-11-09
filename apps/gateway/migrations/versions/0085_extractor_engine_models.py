"""One OCR model per built-in extractor engine; rules name the model, not an engine.

Revision ID: 0085
Revises: 0084
Create Date: 2026-11-09

The built-in extractor reads with markitdown, markitdown plus OCR, or Docling, and
until now only "markitdown" was a model: the engine was a hidden hint a privacy rule
sent as ``X-Extractor-Engine``, so a client that lists OCR models (Cerea) never saw
the others. Now each engine is a model under the extractor provider.

* **Models.** ``markitdown-ocr`` and ``docling`` are created beside ``markitdown``
  when an extractor provider exists and the name is free
  among the deployment's models. The built-in readers are always there: with no
  ``markitdown`` row (deleted before this rule) it is recreated with 0041's seed defaults
  (active, public) and logged. They copy what the operator
  decided about ``markitdown``: active or not, public or not, and its group and user
  grants, so nobody gains or loses access to the reader they had. Prices are *not*
  copied (the rows are unpriced like the original; set a per-page rate if the
  deployment charges for reading). This is a migration and not a startup sync, for the
  reason 0041 gave: a row an operator deleted or deactivated is never put back. Whether
  an engine can run is not stored: the gateway asks the extractor (``/healthz``
  ``extractor_engines``) and offers only the models it can serve.
* **Rules.** The "builtin" reader and the separate engine are gone from the read
  rules of every pipeline. ``builtin`` with engine ``markitdown`` reads with
  ``markitdown``, ``ocr`` with ``markitdown-ocr``, ``docling`` with ``docling``;
  ``builtin`` with no engine reads with ``markitdown``. **A rule is only ever pointed at
  a model on the extractor provider**: if the name is taken by another model (a remote
  service, an alias) it is not the extractor's reader, so the rule reads with
  ``markitdown`` instead and the log says so loudly; a local-only rule is never
  redirected off the stack. Every changed rule is named in the log.

The downgrade turns the extractor provider's model names back into ``builtin`` and an
engine (a same-named model elsewhere is left alone); the models
stay (they are harmless to an older gateway, which lists them as it does markitdown).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0085"
down_revision: str | None = "0084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

DEFAULT_READER = "markitdown"
MODEL_OF_ENGINE = {
    "markitdown": "markitdown",
    "ocr": "markitdown-ocr",
    "docling": "docling",
}
NEW_MODELS = ("markitdown-ocr", "docling")


def _tables() -> dict[str, sa.Table]:
    return {
        "models": sa.table(
            "models",
            sa.column("id", sa.Uuid()),
            sa.column("name", sa.String()),
            sa.column("upstream_model", sa.String()),
            sa.column("provider_id", sa.Uuid()),
            sa.column("kind", sa.String()),
            sa.column("is_active", sa.Boolean()),
            sa.column("is_public", sa.Boolean()),
            sa.column("owner_id", sa.Uuid()),
            sa.column("input_modalities", sa.JSON()),
            sa.column("output_modalities", sa.JSON()),
            sa.column("supported_features", sa.JSON()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        ),
        "providers": sa.table(
            "providers", sa.column("id", sa.Uuid()), sa.column("plugin", sa.String())
        ),
        "group_model_access": sa.table(
            "group_model_access",
            sa.column("group_id", sa.Uuid()),
            sa.column("model_id", sa.Uuid()),
            sa.column("created_at", sa.DateTime(timezone=True)),
        ),
        "user_model_access": sa.table(
            "user_model_access",
            sa.column("user_id", sa.Uuid()),
            sa.column("model_id", sa.Uuid()),
            sa.column("created_at", sa.DateTime(timezone=True)),
        ),
        "privacy_pipelines": sa.table(
            "privacy_pipelines",
            sa.column("id", sa.Uuid()),
            sa.column("name", sa.String()),
            sa.column("rules", sa.JSON()),
            sa.column("otherwise", sa.JSON()),
        ),
    }


def _seed_models(bind: sa.Connection, t: dict[str, sa.Table]) -> list[str]:
    models, providers = t["models"], t["providers"]
    log: list[str] = []
    provider = bind.execute(
        sa.select(providers.c.id).where(providers.c.plugin == "extractor").limit(1)
    ).first()
    if provider is None:
        return log
    base = bind.execute(
        sa.select(models)
        .where(models.c.provider_id == provider.id, models.c.name == DEFAULT_READER)
        .limit(1)
    ).first()
    now = datetime.now(UTC)
    if base is None:
        # The built-in readers are always there. 0041 never re-seeds, so a deployment whose
        # markitdown row was deleted before this rule gets it back with 0041's defaults
        # (active, public, unpriced), unless its name is taken by another model.
        if bind.execute(
            sa.select(models.c.id).where(
                models.c.name == DEFAULT_READER, models.c.owner_id.is_(None)
            )
        ).first():
            log.append(
                f"a model named '{DEFAULT_READER}' exists but is not the extractor's, so the "
                "built-in readers were not recreated; create them by hand."
            )
            return log
        base_id = uuid.uuid4()
        bind.execute(
            models.insert().values(
                id=base_id,
                name=DEFAULT_READER,
                upstream_model=DEFAULT_READER,
                provider_id=provider.id,
                kind="ocr",
                is_active=True,
                is_public=True,
                input_modalities=["file"],
                output_modalities=["text"],
                supported_features=[],
                created_at=now,
                updated_at=now,
            )
        )
        log.append(
            f"the extractor's '{DEFAULT_READER}' model was missing; recreated it (active, public, "
            "as seeded by 0041): the built-in readers can't be removed."
        )
        base = bind.execute(sa.select(models).where(models.c.id == base_id)).first()
        assert base is not None
    for name in NEW_MODELS:
        if bind.execute(
            sa.select(models.c.id).where(models.c.name == name, models.c.owner_id.is_(None))
        ).first():
            log.append(
                f"a model named '{name}' exists already, so it was not created; rules never "
                "point at it unless it is on the extractor provider."
            )
            continue
        new_id = uuid.uuid4()
        bind.execute(
            models.insert().values(
                id=new_id,
                name=name,
                upstream_model=name,
                provider_id=provider.id,
                kind="ocr",
                is_active=bool(base.is_active),
                is_public=bool(base.is_public),
                input_modalities=["file"],
                output_modalities=["text"],
                supported_features=[],
                created_at=now,
                updated_at=now,
            )
        )
        copied = 0
        for table, column in (
            (t["group_model_access"], "group_id"),
            (t["user_model_access"], "user_id"),
        ):
            for row in bind.execute(
                sa.select(table.c[column]).where(table.c.model_id == base.id)
            ).all():
                bind.execute(
                    table.insert().values(**{column: row[0], "model_id": new_id, "created_at": now})
                )
                copied += 1
        log.append(
            f"created the OCR model '{name}' (access copied from '{DEFAULT_READER}': "
            f"{'public' if base.is_public else 'not public'}, "
            f"{copied} grant(s))."
        )
    return log


def _extractor_names(bind: sa.Connection, t: dict[str, sa.Table]) -> set[str]:
    """Names of the deployment models on the extractor provider: the only ones that are the
    built-in reader. A same-named model elsewhere (a remote service, an alias) is not."""
    models, providers = t["models"], t["providers"]
    return {
        str(r.name)
        for r in bind.execute(
            sa.select(models.c.name)
            .join(providers, providers.c.id == models.c.provider_id)
            .where(providers.c.plugin == "extractor", models.c.owner_id.is_(None))
        )
    }


def _convert_action(
    action: dict[str, Any], local: set[str]
) -> tuple[dict[str, Any], str | None]:
    """The action with the model for its reader, and a log phrase when it changed.

    The model is the engine's only if it is on the extractor provider (*local*); otherwise
    the rule reads with ``markitdown``, so a local-only rule is never sent off the stack."""
    if action.get("level") != "read_with":
        # Not a reading rule: an engine cannot be here (it was refused when saved).
        action = {k: v for k, v in action.items() if k != "engine"}
        return action, None
    reader = action.get("reader") or "builtin"
    engine = action.get("engine")
    if reader != "builtin":
        if "engine" in action:
            return {k: v for k, v in action.items() if k != "engine"}, f"engine dropped on '{reader}'"
        return action, None
    wanted = MODEL_OF_ENGINE.get(str(engine), DEFAULT_READER) if engine else DEFAULT_READER
    model = wanted if wanted in local else DEFAULT_READER
    fixed = {k: v for k, v in action.items() if k != "engine"}
    fixed["reader"] = model
    said = (
        f"read with the built-in extractor, engine {engine}"
        if engine
        else "read with the built-in extractor"
    ) + f" -> read with '{model}'"
    if model != wanted:
        said += (
            f" (LOUD: a model named '{wanted}' exists but is not on the extractor provider, "
            "so the rule was not pointed at it; check this rule)"
        )
    return fixed, said


def _convert_pipelines(bind: sa.Connection, t: dict[str, sa.Table]) -> list[str]:
    pipelines = t["privacy_pipelines"]
    local = _extractor_names(bind, t)
    log: list[str] = []
    for row in bind.execute(
        sa.select(pipelines.c.id, pipelines.c.name, pipelines.c.rules, pipelines.c.otherwise)
    ).all():
        changed = False
        rules = []
        for rule in row.rules or []:
            action, said = _convert_action(dict(rule.get("action") or {}), local)
            if said:
                changed = True
                log.append(f"pipeline '{row.name}', rule '{rule.get('name')}': {said}.")
            rules.append({**rule, "action": action} if "action" in rule else rule)
        otherwise = row.otherwise
        if otherwise:
            fixed, said = _convert_action(dict(otherwise), local)
            if said:
                changed = True
                log.append(f"pipeline '{row.name}', last rule: {said}.")
            otherwise = fixed
        if changed:
            bind.execute(
                pipelines.update()
                .where(pipelines.c.id == row.id)
                .values(rules=rules, otherwise=otherwise)
            )
    return log


def upgrade() -> None:
    bind = op.get_bind()
    t = _tables()
    log = _seed_models(bind, t) + _convert_pipelines(bind, t)
    for line in log:
        logger.warning("extractor engine models 0085: %s", line)


def downgrade() -> None:
    bind = op.get_bind()
    t = _tables()
    pipelines = t["privacy_pipelines"]
    local = _extractor_names(bind, t)
    back = {"markitdown-ocr": ("builtin", "ocr"), "docling": ("builtin", "docling")}

    def undo(action: dict[str, Any]) -> dict[str, Any]:
        reader = action.get("reader")
        # Only the extractor provider's models: a same-named model elsewhere is a
        # different reader and keeps its name.
        if action.get("level") == "read_with" and reader in local and reader in (*back, DEFAULT_READER):
            new_reader, engine = back.get(str(reader), ("builtin", None))
            fixed = {**action, "reader": new_reader}
            if engine:
                fixed["engine"] = engine
            return fixed
        return action

    for row in bind.execute(
        sa.select(pipelines.c.id, pipelines.c.rules, pipelines.c.otherwise)
    ).all():
        rules = [
            {**rule, "action": undo(dict(rule["action"]))} if rule.get("action") else rule
            for rule in row.rules or []
        ]
        otherwise = undo(dict(row.otherwise)) if row.otherwise else row.otherwise
        bind.execute(
            pipelines.update()
            .where(pipelines.c.id == row.id)
            .values(rules=rules, otherwise=otherwise)
        )
