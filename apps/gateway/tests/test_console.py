"""Serving the console.

The gateway serves HTML for the first time here, and the failures worth testing
are all silent ones: a console that half-mounts, an entry document cached until
the next deploy breaks it, a missing asset that comes back as HTML, or a CSP that
was loosened to make the build work.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from fastapi import FastAPI
from gateway.config import Settings
from gateway.main import create_app, init_app_state, shutdown_app_state
from gateway.models import Base
from gateway.routers.console import MOUNT_PATH, mount_console, resolve_console_dir

INDEX_HTML = (
    "<!doctype html><html><head><title>console</title></head>"
    '<body><div id="root"></div></body></html>'
)


def build_assets(directory: Path) -> Path:
    """A directory shaped like a real Vite build."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(INDEX_HTML)
    assets = directory / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "index-abc123.js").write_text("export const ok = 1;\n")
    (assets / "index-abc123.css").write_text(":root { --x: 1px; }\n")
    return directory


@pytest.fixture
def console_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(update={"console_dir": str(build_assets(tmp_path / "console"))})


@pytest.fixture
async def console_client(
    console_settings: Settings, fake_upstream: FakeUpstream
) -> httpx.AsyncClient:
    """The real app with console assets present, wired as production wires it.

    Deliberately not the shared `client` fixture: mounting happens inside
    create_app(), so the settings have to be right before the app exists — which
    means repeating the state wiring here rather than reusing the `app` fixture.
    """
    app = create_app(console_settings)
    await init_app_state(
        app,
        console_settings,
        upstream_http=fake_upstream.client(),
        control_http=httpx.AsyncClient(),
    )
    async with app.state.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as http:
            yield http
    finally:
        await shutdown_app_state(app)


class TestTheFrontDoor:
    @pytest.mark.asyncio
    async def test_the_bare_origin_reaches_the_console(
        self, console_client: httpx.AsyncClient
    ) -> None:
        """Typing the address you were given has to land somewhere.

        Every path that matters here is under a prefix — `/v1`, `/api`,
        `/auth`, `/console` — so nothing claimed `/`, and the deployment's front
        door answered `{"detail":"Not Found"}`. The only person who ever sees
        that is the one who typed the address.
        """
        response = await console_client.get("/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/console"

    @pytest.mark.asyncio
    async def test_and_it_actually_arrives(self, console_client: httpx.AsyncClient) -> None:
        response = await console_client.get("/", follow_redirects=True)
        assert response.status_code == 200

    def test_a_headless_deployment_has_no_root_route(self, settings: Settings) -> None:
        """A redirect to a 404 is worse than the 404.

        With no console there is nowhere to send anyone, so `/` keeps its
        honest answer — and something is driving a headless deployment
        programmatically anyway.
        """
        app = create_app(settings.model_copy(update={"console_dir": "/nonexistent"}))
        assert app.state.console_mounted is False
        assert not any(getattr(route, "path", None) == "/" for route in app.routes)


class TestMounting:
    def test_assets_present_and_enabled_mounts(self, console_settings: Settings) -> None:
        assert create_app(console_settings).state.console_mounted is True

    def test_no_assets_means_no_console(self, settings: Settings) -> None:
        """The headless image. Not an error — the API is the product."""
        app = create_app(settings.model_copy(update={"console_dir": "/nonexistent"}))
        assert app.state.console_mounted is False

    def test_disabled_by_configuration(self, console_settings: Settings) -> None:
        """An operator can turn it off without rebuilding the image."""
        app = create_app(console_settings.model_copy(update={"console_enabled": False}))
        assert app.state.console_mounted is False

    def test_a_directory_without_an_index_is_not_served(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        """Half a build is not a console. Serving the assets without the entry
        document would give 200s for scripts and a 404 for the page."""
        empty = tmp_path / "half"
        (empty / "assets").mkdir(parents=True)
        assert resolve_console_dir(settings.model_copy(update={"console_dir": str(empty)})) is None

    def test_mounting_is_reported(self, console_settings: Settings) -> None:
        app = FastAPI()
        assert mount_console(app, console_settings) is True

    async def test_the_api_is_unchanged_by_the_console(
        self, console_client: httpx.AsyncClient
    ) -> None:
        """ADR 0023 promises the HTTP API is identical between the two images.
        A console route that shadowed an API one would break that quietly."""
        assert (await console_client.get("/healthz")).status_code == 200
        assert (await console_client.get("/api/me")).status_code == 401


class TestEntryDocument:
    async def test_the_root_serves_the_page(self, console_client: httpx.AsyncClient) -> None:
        response = await console_client.get(MOUNT_PATH)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert '<div id="root">' in response.text

    async def test_a_client_side_route_serves_the_same_page(
        self, console_client: httpx.AsyncClient
    ) -> None:
        """Deep-linking and refresh. Without this, /console/admin/reports 404s
        for anyone who bookmarks it or hits F5."""
        response = await console_client.get(f"{MOUNT_PATH}/admin/reports")
        assert response.status_code == 200
        assert '<div id="root">' in response.text

    async def test_the_entry_document_is_never_cached_immutably(
        self, console_client: httpx.AsyncClient
    ) -> None:
        """index.html is not content-hashed. Cached, it would keep asking for
        yesterday's asset URLs and the console would break on every deploy."""
        for path in (MOUNT_PATH, f"{MOUNT_PATH}/admin/reports"):
            cache = (await console_client.get(path)).headers["cache-control"]
            assert "no-cache" in cache
            assert "immutable" not in cache


class TestAssets:
    async def test_an_asset_is_served(self, console_client: httpx.AsyncClient) -> None:
        response = await console_client.get(f"{MOUNT_PATH}/assets/index-abc123.js")
        assert response.status_code == 200
        assert "export const ok" in response.text

    async def test_hashed_assets_are_immutable(self, console_client: httpx.AsyncClient) -> None:
        cache = (await console_client.get(f"{MOUNT_PATH}/assets/index-abc123.js")).headers[
            "cache-control"
        ]
        assert "immutable" in cache
        assert "max-age=31536000" in cache

    async def test_a_missing_asset_is_404_not_html(self, console_client: httpx.AsyncClient) -> None:
        """The SPA fallback must not swallow this. HTML returned for a missing
        .js file surfaces as a syntax error in the browser with no hint that the
        real cause is a bad deploy."""
        response = await console_client.get(f"{MOUNT_PATH}/assets/index-deadbeef.js")
        assert response.status_code == 404
        assert '<div id="root">' not in response.text

    async def test_a_missing_file_outside_assets_is_also_404(
        self, console_client: httpx.AsyncClient
    ) -> None:
        response = await console_client.get(f"{MOUNT_PATH}/favicon.ico")
        assert response.status_code == 404


class TestSecurityHeaders:
    async def test_the_page_carries_a_content_security_policy(
        self, console_client: httpx.AsyncClient
    ) -> None:
        csp = (await console_client.get(MOUNT_PATH)).headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp

    async def test_scripts_may_not_be_inline_or_evaluated(
        self, console_client: httpx.AsyncClient
    ) -> None:
        """The build was changed to fit this policy, not the other way round:
        Vite's inline module-preload polyfill is switched off in vite.config.ts.
        If this ever needs relaxing, the build is what should change."""
        csp = (await console_client.get(MOUNT_PATH)).headers["content-security-policy"]
        script = next(part for part in csp.split("; ") if part.startswith("script-src"))
        assert "'unsafe-inline'" not in script
        assert "'unsafe-eval'" not in script

    async def test_assets_carry_the_headers_too(self, console_client: httpx.AsyncClient) -> None:
        """A policy on the page but not on the files it loads is a policy with a
        hole in it."""
        headers = (await console_client.get(f"{MOUNT_PATH}/assets/index-abc123.js")).headers
        assert "content-security-policy" in headers
        assert headers["x-content-type-options"] == "nosniff"

    async def test_the_console_cannot_be_framed(self, console_client: httpx.AsyncClient) -> None:
        headers = (await console_client.get(MOUNT_PATH)).headers
        assert headers["x-frame-options"] == "DENY"


class TestAuthenticationIsUnchanged:
    async def test_the_page_itself_is_public(self, console_client: httpx.AsyncClient) -> None:
        """The HTML shell is not secret — it is an empty div and a script tag.
        The data behind it is what needs a session, and that is enforced by the
        API. Gating the shell too would mean a login redirect that loses the
        route the reader asked for."""
        assert (await console_client.get(MOUNT_PATH)).status_code == 200

    async def test_the_data_still_needs_a_session(
        self, console_client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        assert (await console_client.get("/api/me")).status_code == 401
        assert (await console_client.get("/api/admin/reports/usage")).status_code == 401
