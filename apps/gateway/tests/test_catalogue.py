"""The provider catalogue (backlog P4): schema, two-layer merge, startup failure.

Files are written to a temporary directory per test, so these never depend on
the shipped built-in entries except where a test says it does.
"""

from __future__ import annotations

import textwrap
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from gateway.catalogue import (
    BUILTIN,
    BUILTIN_DIR,
    OVERRIDE,
    CatalogueError,
    load_catalogue,
)
from gateway.config import Settings
from gateway.main import create_app, init_app_state


def write(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(textwrap.dedent(body))
    return path


@pytest.fixture
def builtin(tmp_path: Path) -> Path:
    directory = tmp_path / "builtin"
    write(
        directory,
        "a.yaml",
        """
        providers:
          - id: acme
            name: Acme AI
            plugin: openai
            base_url: https://api.acme.example/v1/
            region: us
            retention:
              zero_data_retention: false
              retention_days: 30
              note: built-in note
            prices:
              acme-large:
                - effective_from: 2026-01-01
                  input_per_mtok: "1.00"
                  output_per_mtok: "2.00"
                - effective_from: 2026-06-01
                  input_per_mtok: "0.80"
                  output_per_mtok: "1.60"
          - id: other
            name: Other
            plugin: generic
        """,
    )
    return directory


@pytest.fixture
def overrides(tmp_path: Path) -> Path:
    directory = tmp_path / "providers.d"
    directory.mkdir()
    return directory


class TestShippedCatalogue:
    def test_the_built_in_layer_loads_and_uses_installed_plugins(self) -> None:
        catalogue = load_catalogue(override_dir=None)
        assert {"openai", "mistral", "cortecs", "nebius"} <= set(catalogue.entries)
        assert all(e.origins["name"].layer == BUILTIN for e in catalogue.ordered())
        assert BUILTIN_DIR.is_dir()

    def test_an_entry_without_a_url_takes_the_plugins_default(self) -> None:
        # No base_url in the entry; the Cortecs plugin knows its own.
        entry = load_catalogue().entries["cortecs"]
        assert entry.effective_base_url == "https://api.cortecs.ai/v1"


#: Router entries: their location and retention are the routed upstream's.
ROUTERS = {"cortecs", "openrouter"}


class TestBuiltInFacts:
    """No built-in provider ships with a blank location or retention."""

    def test_every_built_in_says_where_it_runs_and_what_it_keeps(self) -> None:
        for entry in load_catalogue(override_dir=None).ordered():
            assert entry.region, f"{entry.id}: region is blank (use not-published if unknown)"
            assert entry.region_note, f"{entry.id}: region_note is blank"
            assert entry.retention.status, f"{entry.id}: retention.status is blank"
            assert entry.retention.note, f"{entry.id}: retention.note is blank"
            assert entry.sources, f"{entry.id}: no sources"
            for src in entry.sources:
                assert src.url.startswith("https://"), f"{entry.id}: {src.url}"
                assert src.retrieved <= date.today(), f"{entry.id}: {src.url} read in the future"

    def test_a_published_retention_states_at_least_one_fact(self) -> None:
        for entry in load_catalogue(override_dir=None).ordered():
            r = entry.retention
            if r.status == "published":
                assert (
                    r.retention_days is not None
                    or r.zero_data_retention is not None
                    or r.zero_data_retention_available is not None
                ), f"{entry.id}: status published but no fact"

    def test_routers_say_various_for_both(self) -> None:
        catalogue = load_catalogue(override_dir=None)
        for entry_id in ROUTERS:
            entry = catalogue.entries[entry_id]
            assert entry.region == "various", entry_id
            assert entry.retention.status == "various", entry_id
            assert entry.retention.retention_days is None, entry_id
            facts = entry.facts()
            # Various is not a place: a residency rule must see unknown.
            assert (facts.region, facts.zero_data_retention) == (None, None), entry_id

    def test_only_routers_say_various(self) -> None:
        for entry in load_catalogue(override_dir=None).ordered():
            if entry.id not in ROUTERS:
                assert entry.region != "various", entry.id
                assert entry.retention.status != "various", entry.id


class TestFactsSchema:
    def test_status_sources_and_notes_are_read(self, tmp_path: Path) -> None:
        directory = tmp_path / "b"
        write(
            directory,
            "a.yaml",
            """
            providers:
              - id: acme
                name: Acme
                plugin: generic
                region: not-published
                region_note: Says nothing.
                retention:
                  status: not_published
                  zero_data_retention_available: false
                sources:
                  - url: https://acme.example/privacy
                    retrieved: 2026-10-01
                    covers: retention
            """,
        )
        entry = load_catalogue(builtin_dir=directory).entries["acme"]
        assert entry.region_note == "Says nothing."
        assert entry.retention.status == "not_published"
        assert entry.retention.zero_data_retention_available is False
        assert [(x.url, x.retrieved, x.covers) for x in entry.sources] == [
            ("https://acme.example/privacy", date(2026, 10, 1), "retention")
        ]
        # Not published is unknown to routing, never a place.
        assert entry.facts().region is None

    def test_an_unknown_status_is_refused(self, builtin: Path, overrides: Path) -> None:
        write(
            overrides, "s.yaml", "providers:\n  - id: acme\n    retention:\n      status: maybe\n"
        )
        with pytest.raises(CatalogueError, match=r"retention\.status"):
            load_catalogue(builtin_dir=builtin, override_dir=overrides)

    def test_a_source_needs_a_date_and_an_http_url(self, builtin: Path, overrides: Path) -> None:
        write(
            overrides,
            "s.yaml",
            "providers:\n  - id: acme\n    sources:\n      - url: ftp://x.example/a\n"
            "        retrieved: 2026-10-01\n",
        )
        with pytest.raises(CatalogueError, match=r"sources\[0\]\.url"):
            load_catalogue(builtin_dir=builtin, override_dir=overrides)
        write(
            overrides,
            "s.yaml",
            "providers:\n  - id: acme\n    sources:\n      - url: https://x.example/a\n",
        )
        with pytest.raises(CatalogueError, match="retrieved"):
            load_catalogue(builtin_dir=builtin, override_dir=overrides)

    def test_sources_are_replaced_as_a_whole_by_an_override(self, tmp_path: Path) -> None:
        builtin = tmp_path / "b"
        write(
            builtin,
            "a.yaml",
            "providers:\n  - id: acme\n    name: A\n    plugin: generic\n    sources:\n"
            "      - url: https://a.example/1\n        retrieved: 2026-10-01\n"
            "      - url: https://a.example/2\n        retrieved: 2026-10-01\n",
        )
        over = tmp_path / "o"
        write(
            over,
            "o.yaml",
            "providers:\n  - id: acme\n    sources:\n      - url: https://a.example/3\n"
            "        retrieved: 2026-10-02\n",
        )
        entry = load_catalogue(builtin_dir=builtin, override_dir=over).entries["acme"]
        assert [x.url for x in entry.sources] == ["https://a.example/3"]
        assert entry.origins["sources"].layer == OVERRIDE


class TestValidFiles:
    def test_a_full_entry_is_read_with_typed_values(self, builtin: Path) -> None:
        entry = load_catalogue(builtin_dir=builtin).entries["acme"]
        assert entry.name == "Acme AI"
        assert entry.base_url == "https://api.acme.example/v1"  # trailing slash dropped
        assert entry.region == "us"
        assert entry.retention.zero_data_retention is False
        assert entry.retention.retention_days == 30
        assert {(h.effective_from, h.input_per_mtok) for h in entry.prices} == {
            (date(2026, 1, 1), Decimal("1.00")),
            (date(2026, 6, 1), Decimal("0.80")),
        }

    def test_the_latest_hint_in_force_is_chosen(self, builtin: Path) -> None:
        entry = load_catalogue(builtin_dir=builtin).entries["acme"]
        assert entry.price_hint_for("acme-large", date(2026, 3, 1)).input_per_mtok == Decimal(
            "1.00"
        )  # type: ignore[union-attr]
        assert entry.price_hint_for("acme-large", date(2026, 9, 1)).input_per_mtok == Decimal(
            "0.80"
        )  # type: ignore[union-attr]
        assert entry.price_hint_for("acme-large", date(2025, 1, 1)) is None
        assert entry.price_hint_for("nothing", date(2026, 9, 1)) is None

    def test_an_empty_file_is_fine(self, builtin: Path, overrides: Path) -> None:
        write(overrides, "empty.yaml", "")
        assert "acme" in load_catalogue(builtin_dir=builtin, override_dir=overrides).entries

    def test_files_that_are_not_yaml_are_ignored(self, builtin: Path, overrides: Path) -> None:
        write(overrides, "README.md", "providers: this is not read")
        load_catalogue(builtin_dir=builtin, override_dir=overrides)

    def test_facts_for_routing_say_unknown_as_none(self, builtin: Path) -> None:
        catalogue = load_catalogue(builtin_dir=builtin)
        facts = catalogue.facts_for("other")
        assert facts is not None
        assert (facts.region, facts.zero_data_retention, facts.retention_days) == (None, None, None)
        assert catalogue.facts_for("acme").region == "us"  # type: ignore[union-attr]
        assert catalogue.facts_for("nope") is None
        assert catalogue.facts_for(None) is None


class TestRefusals:
    def refused(self, builtin: Path, overrides: Path, name: str, body: str) -> str:
        write(overrides, name, body)
        with pytest.raises(CatalogueError) as raised:
            load_catalogue(builtin_dir=builtin, override_dir=overrides)
        return str(raised.value)

    def test_an_unknown_key_names_the_file_and_the_field(
        self, builtin: Path, overrides: Path
    ) -> None:
        message = self.refused(
            builtin,
            overrides,
            "typo.yaml",
            """
            providers:
              - id: acme
                regoin: eu
            """,
        )
        assert "override typo.yaml" in message
        assert "providers[0].regoin" in message
        assert "unknown key" in message

    def test_an_unknown_key_nested_in_retention_is_found(
        self, builtin: Path, overrides: Path
    ) -> None:
        message = self.refused(
            builtin,
            overrides,
            "n.yaml",
            """
            providers:
              - id: acme
                retention: {zero_data_retentoin: true}
            """,
        )
        assert "providers[0].retention.zero_data_retentoin" in message

    def test_a_wrong_type_is_refused_not_coerced(self, builtin: Path, overrides: Path) -> None:
        message = self.refused(
            builtin,
            overrides,
            "t.yaml",
            """
            providers:
              - id: acme
                retention: {zero_data_retention: "yes"}
            """,
        )
        assert "providers[0].retention.zero_data_retention" in message

    @pytest.mark.parametrize(
        "key", ["api_key", "apiKey", "secret", "password", "token", "authorization", "credentials"]
    )
    def test_a_field_named_like_a_secret_is_refused(
        self, builtin: Path, overrides: Path, key: str
    ) -> None:
        message = self.refused(
            builtin,
            overrides,
            "leak.yaml",
            f"""
            providers:
              - id: acme
                {key}: hunter2
            """,
        )
        assert "override leak.yaml" in message
        assert f"providers[0].{key}" in message
        assert "secret" in message

    def test_a_secret_nested_anywhere_is_refused(self, builtin: Path, overrides: Path) -> None:
        message = self.refused(
            builtin,
            overrides,
            "deep.yaml",
            """
            providers:
              - id: acme
                retention:
                  api_key: x
            """,
        )
        assert "providers[0].retention.api_key" in message

    def test_a_value_that_looks_like_a_key_is_refused_under_any_name(
        self, builtin: Path, overrides: Path
    ) -> None:
        message = self.refused(
            builtin,
            overrides,
            "val.yaml",
            """
            providers:
              - id: acme
                description: sk-abcdefghijklmnopqrstuvwxyz012345
            """,
        )
        assert "providers[0].description" in message
        assert "credential" in message

    def test_credentials_in_the_url_are_refused(self, builtin: Path, overrides: Path) -> None:
        message = self.refused(
            builtin,
            overrides,
            "url.yaml",
            """
            providers:
              - id: acme
                base_url: https://user:pw@api.example/v1
            """,
        )
        assert "override url.yaml" in message
        assert "providers[0].base_url" in message

    def test_a_non_http_url_is_refused(self, builtin: Path, overrides: Path) -> None:
        message = self.refused(
            builtin,
            overrides,
            "u.yaml",
            "providers:\n  - id: acme\n    base_url: ftp://x\n",
        )
        assert "providers[0].base_url" in message

    def test_an_unknown_plugin_names_the_provider_and_file(
        self, builtin: Path, overrides: Path
    ) -> None:
        message = self.refused(
            builtin,
            overrides,
            "p.yaml",
            "providers:\n  - id: acme\n    plugin: no-such-plugin\n",
        )
        assert "provider 'acme'" in message
        assert "override p.yaml" in message
        assert "plugin: unknown provider plugin 'no-such-plugin'" in message

    def test_a_new_provider_must_say_its_name_and_plugin(
        self, builtin: Path, overrides: Path
    ) -> None:
        message = self.refused(builtin, overrides, "new.yaml", "providers:\n  - id: fresh\n")
        assert "provider 'fresh'" in message
        assert "name: required" in message
        assert "plugin: required" in message

    def test_a_bad_region_tag_is_refused(self, builtin: Path, overrides: Path) -> None:
        message = self.refused(
            builtin, overrides, "r.yaml", "providers:\n  - id: acme\n    region: Europe\n"
        )
        assert "providers[0].region" in message

    def test_a_hint_without_rates_is_refused(self, builtin: Path, overrides: Path) -> None:
        message = self.refused(
            builtin,
            overrides,
            "h.yaml",
            """
            providers:
              - id: acme
                prices:
                  m: [{effective_from: 2026-02-02, input_per_mtok: "1"}]
            """,
        )
        assert "prices.m@2026-02-02" in message

    def test_a_negative_price_is_refused(self, builtin: Path, overrides: Path) -> None:
        message = self.refused(
            builtin,
            overrides,
            "neg.yaml",
            """
            providers:
              - id: acme
                prices:
                  m: [{effective_from: 2026-02-02, input_per_mtok: "-1", output_per_mtok: "1"}]
            """,
        )
        assert "providers[0].prices.m[0].input_per_mtok" in message

    def test_malformed_yaml_names_the_file(self, builtin: Path, overrides: Path) -> None:
        message = self.refused(builtin, overrides, "bad.yaml", "providers: [unclosed\n")
        assert "override bad.yaml" in message
        assert "YAML" in message

    def test_a_duplicate_id_in_one_file_is_refused(self, builtin: Path, overrides: Path) -> None:
        message = self.refused(
            builtin, overrides, "dup.yaml", "providers:\n  - id: acme\n  - id: acme\n"
        )
        assert "providers[1].id" in message

    def test_every_bad_file_is_reported_at_once(self, builtin: Path, overrides: Path) -> None:
        write(overrides, "one.yaml", "providers:\n  - id: acme\n    nope: 1\n")
        write(overrides, "two.yaml", "providers:\n  - id: acme\n    region: BAD\n")
        with pytest.raises(CatalogueError) as raised:
            load_catalogue(builtin_dir=builtin, override_dir=overrides)
        assert "one.yaml" in str(raised.value) and "two.yaml" in str(raised.value)

    def test_a_named_override_directory_that_is_missing_stops_startup(
        self, builtin: Path, tmp_path: Path
    ) -> None:
        with pytest.raises(CatalogueError, match="GATEWAY_PROVIDERS_DIR"):
            load_catalogue(builtin_dir=builtin, override_dir=tmp_path / "not-mounted")

    def test_a_bad_built_in_file_is_named_as_built_in(self, tmp_path: Path) -> None:
        directory = tmp_path / "b"
        write(directory, "x.yaml", "providers:\n  - id: acme\n    nope: 1\n")
        with pytest.raises(CatalogueError, match=r"builtin x\.yaml"):
            load_catalogue(builtin_dir=directory)


class TestMerge:
    def load(self, builtin: Path, overrides: Path) -> dict[str, object]:
        return dict(load_catalogue(builtin_dir=builtin, override_dir=overrides).entries)

    def test_an_override_changes_only_the_fields_it_names(
        self, builtin: Path, overrides: Path
    ) -> None:
        write(overrides, "o.yaml", "providers:\n  - id: acme\n    region: eu\n")
        entry = load_catalogue(builtin_dir=builtin, override_dir=overrides).entries["acme"]
        assert entry.region == "eu"
        # Everything else is still the built-in's.
        assert entry.name == "Acme AI"
        assert entry.base_url == "https://api.acme.example/v1"
        assert entry.retention.retention_days == 30
        assert entry.origins["region"].layer == OVERRIDE
        assert entry.origins["region"].file == "o.yaml"
        assert entry.origins["name"].layer == BUILTIN

    def test_retention_merges_per_sub_field(self, builtin: Path, overrides: Path) -> None:
        write(
            overrides,
            "o.yaml",
            "providers:\n  - id: acme\n    retention: {zero_data_retention: true}\n",
        )
        entry = load_catalogue(builtin_dir=builtin, override_dir=overrides).entries["acme"]
        assert entry.retention.zero_data_retention is True
        assert entry.retention.retention_days == 30
        assert entry.retention.note == "built-in note"
        assert entry.origins["retention.zero_data_retention"].layer == OVERRIDE
        assert entry.origins["retention.retention_days"].layer == BUILTIN

    def test_an_explicit_null_clears_a_built_in_value(self, builtin: Path, overrides: Path) -> None:
        write(overrides, "o.yaml", "providers:\n  - id: acme\n    region: null\n")
        entry = load_catalogue(builtin_dir=builtin, override_dir=overrides).entries["acme"]
        assert entry.region is None
        assert entry.origins["region"].layer == OVERRIDE

    def test_one_price_is_fixed_and_the_others_are_kept(
        self, builtin: Path, overrides: Path
    ) -> None:
        write(
            overrides,
            "o.yaml",
            """
            providers:
              - id: acme
                prices:
                  acme-large:
                    - effective_from: 2026-06-01
                      output_per_mtok: "1.20"
            """,
        )
        entry = load_catalogue(builtin_dir=builtin, override_dir=overrides).entries["acme"]
        by_date = {h.effective_from: h for h in entry.prices}
        assert len(by_date) == 2
        fixed = by_date[date(2026, 6, 1)]
        assert (fixed.input_per_mtok, fixed.output_per_mtok) == (Decimal("0.80"), Decimal("1.20"))
        assert by_date[date(2026, 1, 1)].output_per_mtok == Decimal("2.00")
        assert entry.origins["prices.acme-large@2026-06-01"].layer == OVERRIDE
        assert entry.origins["prices.acme-large@2026-01-01"].layer == BUILTIN

    def test_a_new_id_adds_a_provider_and_the_built_ins_stay(
        self, builtin: Path, overrides: Path
    ) -> None:
        write(
            overrides,
            "mine.yaml",
            """
            providers:
              - id: in-house
                name: In-house vLLM
                plugin: generic
                base_url: http://vllm.internal:8000/v1
                region: eu
            """,
        )
        entries = load_catalogue(builtin_dir=builtin, override_dir=overrides).entries
        assert set(entries) == {"acme", "other", "in-house"}
        assert entries["in-house"].origins["name"].layer == OVERRIDE
        assert entries["acme"].origins["name"].layer == BUILTIN

    def test_later_files_win_over_earlier_ones(self, builtin: Path, overrides: Path) -> None:
        write(overrides, "10-first.yaml", "providers:\n  - id: acme\n    region: eu\n")
        write(overrides, "20-second.yaml", "providers:\n  - id: acme\n    region: ch\n")
        entry = load_catalogue(builtin_dir=builtin, override_dir=overrides).entries["acme"]
        assert entry.region == "ch"
        assert entry.origins["region"].file == "20-second.yaml"

    def test_the_default_directory_is_optional_but_a_named_one_is_not(self, builtin: Path) -> None:
        # Neither named nor present: the built-in layer alone.
        assert "acme" in load_catalogue(builtin_dir=builtin, override_dir="").entries


class TestStartup:
    async def test_a_bad_override_file_stops_the_gateway_naming_file_and_field(
        self, settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        directory = tmp_path / "providers.d"
        write(directory, "oops.yaml", "providers:\n  - id: mistral\n    regoin: eu\n")
        broken = settings.model_copy(update={"providers_dir": str(directory)})
        app = create_app(broken)
        with pytest.raises(CatalogueError) as raised:
            await init_app_state(app, broken)
        assert "override oops.yaml: providers[0].regoin" in str(raised.value)
        # The refusal is also logged (at CRITICAL), where an operator reads it.
        assert "oops.yaml" in capsys.readouterr().out
        # Nothing was opened before the refusal.
        assert not hasattr(app.state, "engine")

    async def test_a_good_override_directory_is_loaded_into_app_state(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        from gateway.main import shutdown_app_state

        directory = tmp_path / "providers.d"
        write(directory, "ok.yaml", "providers:\n  - id: mistral\n    region: fr\n")
        configured = settings.model_copy(update={"providers_dir": str(directory)})
        app = create_app(configured)
        await init_app_state(app, configured)
        try:
            assert app.state.catalogue.entries["mistral"].region == "fr"
        finally:
            await shutdown_app_state(app)
