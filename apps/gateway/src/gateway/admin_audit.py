"""Writing to `admin_audit_events` (issue P9).

One helper, `record_audit`, used by every admin route that changes
configuration. It only `session.add`s the row, so the event commits (or rolls
back) with the change it describes: a change that fails leaves no event, and an
event never exists for a change that did not happen. Callers must therefore call
it *before* their `commit()`, never after.

**Secrets never reach this table.** Three rules, all enforced here rather than
left to each call site:

* A view of an object (`*_view`) lists the fields worth auditing by name; a
  credential is simply not in the list. Provider credentials are reported only
  as the marker `api_key: rotated|set|cleared`, never a value or a hint.
* Provider `extra_headers` can carry an `Authorization` header, so the view holds
  the header *names* only; a change of values under the same names is reported
  as `extra_headers_values: rotated`.
* `snapshot` replaces the value of any field whose name looks like a credential
  (`key`, `secret`, `token`, `password`, `authorization`, `credential`) with
  `[redacted]`, so a field added to a view later cannot leak by accident.
  A provider's `base_url` has any `user:pass@` stripped.

Retention: none. Rows are kept until an operator removes them by hand.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models import AdminAuditEvent, LimitRule, ModelDef, ModelPrice, Provider, User

_SECRET_NAME = re.compile(r"key|secret|token|password|authorization|credential", re.IGNORECASE)
REDACTED = "[redacted]"

MODEL_FIELDS = (
    "name",
    "upstream_model",
    "provider_id",
    "kind",
    "display_name",
    "description",
    "context_window",
    "max_output_tokens",
    "max_input_tokens",
    "input_modalities",
    "output_modalities",
    "supported_features",
    "is_active",
    "is_public",
)
ALIAS_FIELDS = ("name", "display_name", "description", "is_active", "is_public")
PRICE_FIELDS = (
    "input_per_mtok",
    "output_per_mtok",
    "cache_read_per_mtok",
    "cache_write_per_mtok",
    "per_image",
    "per_image_by_size",
    "per_page",
    "per_search",
    "currency",
    "effective_from",
    "source",
)
PROVIDER_FIELDS = (
    "name",
    "description",
    "plugin",
    "kind",
    "billing_mode",
    "prefix",
    "is_active",
)
LIMIT_FIELDS = (
    "name",
    "scope",
    "scope_id",
    "metric",
    "window_seconds",
    "period",
    "limit_value",
    "is_active",
    "applies_to",
)


def _plain(value: Any) -> Any:
    """A JSON-safe copy: exact decimals as strings, ids and enums as text."""
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    return value


def snapshot(obj: object, fields: Iterable[str]) -> dict[str, Any]:
    """The named fields of `obj`, JSON-safe, credential-looking names redacted."""
    out: dict[str, Any] = {}
    for name in fields:
        value = getattr(obj, name)
        out[name] = REDACTED if _SECRET_NAME.search(name) and value else _plain(value)
    return out


def model_view(model: ModelDef) -> dict[str, Any]:
    return snapshot(model, MODEL_FIELDS)


def alias_view(alias: ModelDef, target: str | None) -> dict[str, Any]:
    return {**snapshot(alias, ALIAS_FIELDS), "target": target}


def price_view(price: ModelPrice) -> dict[str, Any]:
    return snapshot(price, PRICE_FIELDS)


def limit_view(rule: LimitRule) -> dict[str, Any]:
    return snapshot(rule, LIMIT_FIELDS)


def _without_credentials(url: str) -> str:
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    _, at, host = rest.rpartition("@")
    return f"{scheme}://{host}" if at else url


def provider_view(provider: Provider) -> dict[str, Any]:
    """A provider without its key, and with header names instead of header values."""
    view = snapshot(provider, PROVIDER_FIELDS)
    view["base_url"] = _without_credentials(provider.base_url)
    view["extra_headers"] = sorted((provider.extra_headers or {}).keys())
    return view


def headers_snapshot(provider: Provider) -> dict[str, Any]:
    """A copy of the headers, for comparing values without ever storing them."""
    return dict(provider.extra_headers or {})


def _diff(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for key in sorted({*(before or {}), *(after or {})}):
        old = (before or {}).get(key)
        new = (after or {}).get(key)
        if before is not None and after is not None and old == new:
            continue
        entry: dict[str, Any] = {}
        if before is not None:
            entry["before"] = old
        if after is not None:
            entry["after"] = new
        changes[key] = entry
    return changes


def record_audit(
    session: AsyncSession,
    admin: User,
    action: str,
    target_type: str,
    target_id: uuid.UUID | str | None,
    target_label: str,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    markers: Mapping[str, str] | None = None,
) -> bool:
    """Add one audit row to `session`; the caller's commit persists it.

    `before` only: a delete. `after` only: a create. Both: an update, and a
    call that changed nothing writes no row (returns False). `markers` are
    fixed words for fields whose value must never be recorded, e.g.
    `{"api_key": "rotated"}`; they are written as the field's `after`.
    """
    changes = _diff(before, after)
    for field, word in (markers or {}).items():
        changes[field] = {"after": word}
    if before is not None and after is not None and not changes:
        return False
    session.add(
        AdminAuditEvent(
            id=uuid.uuid4(),
            actor_user_id=admin.id,
            actor_label=admin.email or "",
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            target_label=target_label[:320],
            changes=changes,
        )
    )
    return True
