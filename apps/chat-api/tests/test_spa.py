"""Serving the built application, and the service worker in particular.

Every failure here is silent, which is why they are pinned. A 404 on `sw.js`
does not break the page — it installs, it opens, it just quietly has no offline
shell. A year-long cache on `sw.js` is worse: the browser is pinned to whichever
worker it saw first, and the thing that would normally fix that is the thing
that is stuck.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from chat_api.config import Settings
from chat_api.main import create_app, init_app_state, shutdown_app_state
from chat_api.routers import MOUNT_PATH as MOUNT
from chat_api.routers import spa


def build_dist(root: Path) -> Path:
    """A directory shaped like a real `vite build` with the PWA plugin."""
    directory = root / "web_static"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text("<!doctype html><div id=root></div>")
    (directory / "assets" / "index-abc123.js").write_text("console.log(1)")
    (directory / "sw.js").write_text("self.addEventListener('install', () => {})")
    (directory / "workbox-9c191d2f.js").write_text("// workbox runtime")
    (directory / "manifest.webmanifest").write_text('{"name":"Chat"}')
    (directory / "icon.svg").write_text("<svg/>")
    return directory


@pytest_asyncio.fixture
async def served(
    settings: Settings, fake_gateway: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    directory = build_dist(tmp_path)
    monkeypatch.setattr(spa, "default_spa_dir", lambda: directory)
    settings.spa_enabled = True

    application = create_app(settings)
    await init_app_state(
        application,
        settings,
        gateway_http=fake_gateway.client(),
        control_http=httpx.AsyncClient(),
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://chat") as http:
        yield http
    await shutdown_app_state(application)


class TestTheEntryDocument:
    @pytest.mark.asyncio
    async def test_the_app_is_served_at_the_mount_path(self, served: httpx.AsyncClient) -> None:
        for path in (MOUNT, f"{MOUNT}/", f"{MOUNT}/some/client/route"):
            response = await served.get(path)
            assert response.status_code == 200, path
            assert "text/html" in response.headers["content-type"]

    @pytest.mark.asyncio
    async def test_it_is_revalidated_not_cached_hard(self, served: httpx.AsyncClient) -> None:
        response = await served.get(f"{MOUNT}/")
        assert response.headers["cache-control"] == "no-cache, must-revalidate"

    @pytest.mark.asyncio
    async def test_it_carries_the_security_headers(self, served: httpx.AsyncClient) -> None:
        headers = (await served.get(f"{MOUNT}/")).headers
        csp = headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp
        # A blocked worker fails silently, so the policy has to name it.
        assert "worker-src 'self'" in csp
        assert "manifest-src 'self'" in csp

    @pytest.mark.asyncio
    async def test_a_missing_asset_is_a_404_not_the_app(self, served: httpx.AsyncClient) -> None:
        """Answering a missing script with HTML makes the browser name the
        wrong problem — a syntax error in what it thinks is JavaScript."""
        response = await served.get(f"{MOUNT}/assets/gone-deadbeef.js")
        assert response.status_code == 404
        response = await served.get(f"{MOUNT}/nope.js")
        assert response.status_code == 404


class TestTheServiceWorker:
    @pytest.mark.asyncio
    async def test_the_worker_is_served(self, served: httpx.AsyncClient) -> None:
        """The bug this exists for: `sw.js` has a dot and is not under
        `assets/`, so the SPA fallback used to 404 it and the application was
        quietly not a PWA."""
        response = await served.get(f"{MOUNT}/sw.js")
        assert response.status_code == 200
        assert "install" in response.text

    @pytest.mark.asyncio
    async def test_the_worker_is_never_cached_hard(self, served: httpx.AsyncClient) -> None:
        """Its name never changes, so `immutable` would pin a browser to
        whichever worker it saw first — and the update that would fix it is the
        thing that is stuck."""
        response = await served.get(f"{MOUNT}/sw.js")
        assert "immutable" not in response.headers["cache-control"]

    @pytest.mark.asyncio
    async def test_the_workbox_runtime_is_served_and_is_hashed(
        self, served: httpx.AsyncClient
    ) -> None:
        """Matched by shape rather than listed: the hash changes every build,
        and the worker imports it by that name."""
        response = await served.get(f"{MOUNT}/workbox-9c191d2f.js")
        assert response.status_code == 200
        assert "immutable" in response.headers["cache-control"]

    @pytest.mark.asyncio
    async def test_the_manifest_and_icon_are_served(self, served: httpx.AsyncClient) -> None:
        """Without the manifest there is no install prompt at all."""
        assert (await served.get(f"{MOUNT}/manifest.webmanifest")).status_code == 200
        assert (await served.get(f"{MOUNT}/icon.svg")).status_code == 200

    @pytest.mark.asyncio
    async def test_a_root_name_in_a_subdirectory_is_not_served(
        self, served: httpx.AsyncClient
    ) -> None:
        """The allow-list is for the root only, so it cannot be walked into a
        path traversal by prefixing a directory."""
        assert (await served.get(f"{MOUNT}/deep/sw.js")).status_code == 404
