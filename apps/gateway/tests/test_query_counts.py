"""How many SQL round trips one request costs.

A performance property, pinned as a test because that is the only way it stays
true. Measured at 20.5ms p50 against a 1.4ms upstream, the gateway's own cost is
dominated by per-request database round trips, and the number of them is the
kind of thing an innocent-looking `selectinload` changes without anybody
noticing.

The numbers here are upper bounds, deliberately loose enough not to fail on an
unrelated change and tight enough to catch a regression that adds a query to the
hot path. If one of these has to be raised, that is a decision worth making on
purpose.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from conftest import FakeUpstream, Seeded
from sqlalchemy import event
from sqlalchemy.engine import Engine


@contextmanager
def counted(engine: Engine) -> Iterator[list[str]]:
    """Every statement the engine executes while the block runs."""
    seen: list[str] = []

    def before(conn: object, cursor: object, statement: str, *args: object) -> None:
        seen.append(statement)

    # The async engine wraps a sync one; the events fire on the sync dialect.
    target = engine.sync_engine if hasattr(engine, "sync_engine") else engine
    event.listen(target, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(target, "before_cursor_execute", before)


def summarise(statements: list[str]) -> str:
    """A readable digest, for when an assertion fails."""
    lines = []
    for statement in statements:
        first = " ".join(statement.split())[:90]
        lines.append(f"    {first}")
    return "\n".join(lines)


class TestTheMeteredPath:
    async def test_a_chat_completion_stays_within_its_query_budget(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        app: object,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The hot path: auth, access, quota, and two ledger writes."""
        engine = app.state.engine  # type: ignore[attr-defined]
        fake_upstream.set_json(
            {
                "id": "c1",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
        )

        # Warm anything one-off — the first request of a process may load a
        # limit rule set or a dialect detail that later ones do not.
        await client.post(
            "/v1/chat/completions",
            json={"model": seeded.model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )

        with counted(engine) as statements:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": seeded.model.name, "messages": [{"role": "user", "content": "hi"}]},
                headers=seeded.auth,
            )
        assert response.status_code == 200

        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        writes = [
            s for s in statements if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]

        # Two writes by design: the in-progress row is created before the
        # upstream call so a crash mid-request is visible, then finalised.
        print(f"\n  metered path: {len(selects)} selects, {len(writes)} writes")
        print(summarise(selects))
        assert len(writes) <= 2, f"{len(writes)} writes:\n{summarise(writes)}"
        # Five: the key (joined to user and billing group), memberships, the
        # model (joined to its provider), its prices, and the limit rules.
        assert len(selects) <= 5, f"{len(selects)} selects:\n{summarise(selects)}"

    async def test_authentication_alone_is_cheap(
        self, client: httpx.AsyncClient, seeded: Seeded, app: object
    ) -> None:
        """`/v1/models` is auth plus the access-filtered catalogue and nothing else.

        Isolated from the metered path because authentication runs on *every*
        request, so a query added here is multiplied by everything.
        """
        engine = app.state.engine  # type: ignore[attr-defined]
        await client.get("/v1/models", headers=seeded.auth)

        with counted(engine) as statements:
            assert (await client.get("/v1/models", headers=seeded.auth)).status_code == 200

        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        print(f"\n  /v1/models: {len(selects)} selects")
        print(summarise(selects))
        # Three: the key joined to its user and group, memberships, the
        # catalogue. Was five before the loaders were changed from
        # `selectinload` to `joinedload` for the many-to-one relations.
        assert len(selects) <= 3, f"{len(selects)} selects:\n{summarise(selects)}"
