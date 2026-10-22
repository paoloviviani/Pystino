"""The provider catalogue through the admin API, the price fill and routing (backlog P4).

How a catalogue entry relates to a provider row is the thing under test: the row
links to the entry by id, nothing is copied, and the entry's region and
retention reach a routing rule through the candidate.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from fastapi import FastAPI
from gateway.catalogue import load_catalogue
from gateway.models import ModelDef, ModelPrice, PriceSource, Provider
from gateway.routing import RULES, Candidate, SelectionContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin
from test_routing_policies import Upstreams, chat, make_route, post


@pytest.fixture
def upstreams(fake_upstream: FakeUpstream) -> Upstreams:
    return Upstreams(fake_upstream)


def use_catalogue(app: FastAPI, tmp_path: Path, files: dict[str, str]) -> None:
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    (builtin / "builtin.yaml").write_text(
        textwrap.dedent(
            """
            providers:
              - id: eu-vendor
                name: EU Vendor
                plugin: generic
                base_url: https://eu.example/v1/
                prefix: eu-
                region: eu
                retention: {zero_data_retention: true, retention_days: 0}
                prices:
                  vendor/unpriced-1:
                    - effective_from: 2026-01-01
                      input_per_mtok: "0.50"
                      output_per_mtok: "1.50"
              - id: us-vendor
                name: US Vendor
                plugin: generic
                region: us
                retention: {zero_data_retention: false, retention_days: 30}
            """
        )
    )
    overrides = tmp_path / "providers.d"
    overrides.mkdir()
    for name, body in files.items():
        (overrides / name).write_text(textwrap.dedent(body))
    app.state.catalogue = load_catalogue(builtin_dir=builtin, override_dir=overrides)


@pytest.fixture
async def admin(
    app: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    as_user(app, await make_admin(session_factory, seeded))


class TestListing:
    async def test_the_console_reads_each_field_with_its_origin(
        self, admin: None, app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
    ) -> None:
        use_catalogue(
            app,
            tmp_path,
            {"fix.yaml": "providers:\n  - id: eu-vendor\n    region: ch\n"},
        )
        rows = (await client.get("/api/admin/provider-catalogue")).json()
        assert [r["id"] for r in rows] == ["eu-vendor", "us-vendor"]
        eu = rows[0]
        assert eu["region"] == "ch"
        assert eu["base_url"] == "https://eu.example/v1"
        assert eu["retention"]["zero_data_retention"] is True
        origins = {o["field"]: o for o in eu["origins"]}
        assert origins["region"] == {"field": "region", "layer": "override", "file": "fix.yaml"}
        assert origins["name"]["layer"] == "builtin"
        assert origins["name"]["file"] == "builtin.yaml"
        assert eu["price_hints"][0]["model"] == "vendor/unpriced-1"

    async def test_it_needs_an_administrator(
        self, app: FastAPI, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        assert (await client.get("/api/admin/provider-catalogue")).status_code in (401, 403)


class TestProviderRows:
    async def test_creating_from_an_entry_takes_what_the_request_leaves_out(
        self, admin: None, app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
    ) -> None:
        use_catalogue(app, tmp_path, {})
        response = await client.post(
            "/api/admin/providers",
            json={"name": "eu-one", "catalogue_id": "eu-vendor", "api_key": "k-123456789"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["catalogue_id"] == "eu-vendor"
        assert body["base_url"] == "https://eu.example/v1"
        assert body["plugin"] == "generic"
        assert body["prefix"] == "eu-"
        assert body["catalogue"]["region"] == "eu"
        assert body["catalogue_missing"] is False
        assert "k-123456789" not in response.text

    async def test_what_the_request_states_wins_over_the_entry(
        self, admin: None, app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
    ) -> None:
        use_catalogue(app, tmp_path, {})
        body = (
            await client.post(
                "/api/admin/providers",
                json={
                    "name": "eu-two",
                    "catalogue_id": "eu-vendor",
                    "base_url": "https://proxy.example/v1",
                    "prefix": "",
                },
            )
        ).json()
        assert body["base_url"] == "https://proxy.example/v1"
        assert body["prefix"] == ""

    async def test_an_unknown_entry_is_refused_naming_the_known_ones(
        self, admin: None, app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
    ) -> None:
        use_catalogue(app, tmp_path, {})
        response = await client.post(
            "/api/admin/providers",
            json={"name": "x", "catalogue_id": "nope", "base_url": "https://x/v1"},
        )
        assert response.status_code == 400
        assert "eu-vendor" in response.text

    async def test_a_row_is_linked_and_unlinked_without_copying(
        self,
        admin: None,
        app: FastAPI,
        client: httpx.AsyncClient,
        tmp_path: Path,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        use_catalogue(app, tmp_path, {})
        url = f"/api/admin/providers/{seeded.provider.id}"
        linked = (await client.patch(url, json={"catalogue_id": "us-vendor"})).json()
        assert linked["catalogue"]["id"] == "us-vendor"
        # The row's own fields are untouched: nothing was copied from the entry.
        assert linked["base_url"] == seeded.provider.base_url
        assert linked["plugin"] is None
        unlinked = (await client.patch(url, json={"catalogue_id": ""})).json()
        assert unlinked["catalogue_id"] is None and unlinked["catalogue"] is None
        assert (await client.patch(url, json={"catalogue_id": "nope"})).status_code == 400

    async def test_an_entry_the_catalogue_lost_leaves_the_row_working(
        self,
        admin: None,
        app: FastAPI,
        client: httpx.AsyncClient,
        tmp_path: Path,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        use_catalogue(app, tmp_path, {})
        async with session_factory() as session:
            row = await session.get(Provider, seeded.provider.id)
            assert row is not None
            row.catalogue_id = "was-removed"
            await session.commit()
        rows = (await client.get("/api/admin/providers")).json()["items"]
        row_json = next(r for r in rows if r["name"] == "fake")
        assert row_json["catalogue_id"] == "was-removed"
        assert row_json["catalogue"] is None
        assert row_json["catalogue_missing"] is True


async def _link(session_factory: Any, provider_id: Any, catalogue_id: str | None) -> None:
    async with session_factory() as session:
        row = await session.get(Provider, provider_id)
        assert row is not None
        row.catalogue_id = catalogue_id
        await session.commit()


class TestPriceHints:
    @pytest.fixture
    def gap(self, app: FastAPI) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"data": [{"id": "vendor/unpriced-1"}, {"id": "other"}]}
            )

        app.state.control_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_a_hint_fills_a_listed_unpriced_model_and_says_so(
        self,
        admin: None,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        gap: None,
        tmp_path: Path,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        use_catalogue(app, tmp_path, {})
        await _link(session_factory, seeded.provider.id, "eu-vendor")
        body = (
            await client.get(
                f"/api/admin/models/discover?fill_missing_prices=true&provider_id={seeded.provider.id}"
            )
        ).json()
        rows = {r["upstream_model"]: r for r in body["available"]}
        assert rows["vendor/unpriced-1"]["price_source"] == "hint"
        assert rows["vendor/unpriced-1"]["input_per_mtok"].startswith("0.5")
        # No hint for this one: still nobody's price.
        assert rows["other"]["price_source"] is None

    async def test_an_override_fixes_the_one_price_and_the_import_records_it(
        self,
        admin: None,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        gap: None,
        tmp_path: Path,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        use_catalogue(
            app,
            tmp_path,
            {
                "price.yaml": """
                providers:
                  - id: eu-vendor
                    prices:
                      vendor/unpriced-1:
                        - effective_from: 2026-01-01
                          output_per_mtok: "9.00"
                """
            },
        )
        await _link(session_factory, seeded.provider.id, "eu-vendor")
        response = await client.post(
            f"/api/admin/models/import?fill_missing_prices=true&provider_id={seeded.provider.id}",
            json={"models": [{"upstream_model": "vendor/unpriced-1"}]},
        )
        assert response.status_code == 201, response.text
        assert response.json()["results"][0]["price_source"] == "hint"
        price = (
            await session.execute(
                select(ModelPrice)
                .join(ModelDef, ModelDef.id == ModelPrice.model_id)
                .where(ModelDef.upstream_model == "vendor/unpriced-1")
            )
        ).scalar_one()
        assert price.source is PriceSource.HINT
        assert str(price.input_per_mtok).startswith("0.5")  # from the built-in layer
        assert str(price.output_per_mtok).startswith("9")  # the override's

    async def test_without_the_fill_switch_a_hint_is_not_used(
        self,
        admin: None,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        gap: None,
        tmp_path: Path,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        use_catalogue(app, tmp_path, {})
        await _link(session_factory, seeded.provider.id, "eu-vendor")
        body = (
            await client.get(f"/api/admin/models/discover?provider_id={seeded.provider.id}")
        ).json()
        assert all(r["price_source"] is None for r in body["available"])


class ResidencyRule:
    """A residency rule as P5 will write one: drop what is not in the EU."""

    name = "eu_only"
    seen: ClassVar[list[tuple[str, str | None, bool | None, int | None]]] = []

    async def order(
        self, candidates: list[Candidate], context: SelectionContext
    ) -> list[Candidate]:
        for c in candidates:
            self.seen.append((c.target.name, c.region, c.zero_data_retention, c.retention_days))
        return [c for c in sorted(candidates, key=lambda c: c.position) if c.region == "eu"]


class TestRoutingExposure:
    async def test_a_rule_can_filter_candidates_on_region_and_retention(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        upstreams: Upstreams,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        use_catalogue(app, tmp_path, {})
        rule = ResidencyRule()
        rule.seen = []
        monkeypatch.setitem(RULES, "eu_only", rule)
        # t-a is first but sits behind the US provider; t-b behind the EU one.
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1), rule="eu_only")
        async with session_factory() as session:
            us = Provider(name="us", base_url="https://us.example/v1", catalogue_id="us-vendor")
            eu = Provider(name="eu", base_url="https://eu.example/v1", catalogue_id="eu-vendor")
            session.add_all([us, eu])
            await session.flush()
            for name, provider in (("t-a", us), ("t-b", eu)):
                model = (
                    await session.execute(select(ModelDef).where(ModelDef.name == name))
                ).scalar_one()
                model.provider_id = provider.id
            await session.commit()

        response = await post(client, seeded, chat())
        assert response.status_code == 200, response.text
        # The rule saw both targets' facts, and only the EU one served.
        assert sorted(rule.seen) == [("t-a", "us", False, 30), ("t-b", "eu", True, 0)]
        assert upstreams.calls == ["up-t-b"]

    async def test_an_unlinked_provider_has_unknown_facts_not_false_ones(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        upstreams: Upstreams,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        use_catalogue(app, tmp_path, {})
        rule = ResidencyRule()
        rule.seen = []
        monkeypatch.setitem(RULES, "eu_only", rule)
        await make_route(session_factory, seeded, ("t-a", 1), rule="eu_only")
        response = await post(client, seeded, chat())
        # The seeded provider is not linked: region unknown, so this rule excludes it.
        assert rule.seen == [("t-a", None, None, None)]
        assert response.status_code == 503
