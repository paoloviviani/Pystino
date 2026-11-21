"""Operational metrics for ``GET /metrics`` (Prometheus text format, docs/metrics.md).

Every instrument lives in this one module, and call sites use the functions
below instead of touching a Prometheus object. That is deliberate: the same
events are meant to be exportable through OpenTelemetry later, and when they
are, the change is made here, not at forty call sites.

**What is never a label or a value.** A user, an e-mail address, a key, a
prompt, a file name, a group name or an id of any kind. The labels are a closed
vocabulary (surface, status, kind, outcome, ...) or a name an administrator
chose in the catalogue (provider, model, alias). A caller's own provider and
model (BYOK) are an unbounded set of names a member typed, so they are folded
into the literal ``byok``. Every name-valued label also passes
:func:`_bounded`, which stops a surprise from becoming a cardinality problem.

**Several workers.** ``gateway serve --workers N`` runs N processes. With
``PROMETHEUS_MULTIPROC_DIR`` set before the first import of ``prometheus_client``
(``gateway serve`` does this when metrics are on and there is more than one
worker, see :mod:`gateway.metrics_env`), each worker writes its own files there
and a scrape merges them, so the numbers describe the container. Gauges say how
to merge: in-flight and pool use are summed over live workers, the health flags
take the minimum, and the active-caller counts the maximum (see below).

**Active callers** are counted without keeping an identity. Each authenticated
caller is reduced to an 8-byte hash and noted per minute. With Valkey the
hashes go into one HyperLogLog per minute (which cannot list its members) that
every worker shares, and a window is the union of its minutes: a deployment-wide
count, with HyperLogLog's ~1% error. Without Valkey each worker counts its own
callers, and the busiest worker's figure is reported: a lower bound that is
close to the truth when the proxy spreads callers evenly.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import defaultdict
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.multiprocess import MultiProcessCollector, mark_process_dead
from prometheus_client.process_collector import ProcessCollector

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

CONTENT_TYPE = CONTENT_TYPE_LATEST

#: Decided once, at the moment prometheus_client itself decides: it reads the
#: variable on import and cannot be switched afterwards.
MULTIPROCESS = bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR"))

REGISTRY = CollectorRegistry(auto_describe=True)

_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)
_TTFT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30)
_REDACTION_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)

IN_FLIGHT = Gauge(
    "pystino_requests_in_flight",
    "Requests being served right now, by API surface.",
    ["surface"],
    registry=REGISTRY,
    multiprocess_mode="livesum",
)
ACTIVE_USERS = Gauge(
    "pystino_active_users",
    "Distinct authenticated callers seen in the window (5m or 15m).",
    ["window"],
    registry=REGISTRY,
    multiprocess_mode="livemax",
)
ACTIVE_API_KEYS = Gauge(
    "pystino_active_api_keys",
    "Distinct API keys seen in the window (5m or 15m).",
    ["window"],
    registry=REGISTRY,
    multiprocess_mode="livemax",
)
REQUESTS = Counter(
    "pystino_requests",
    "Requests by how they ended.",
    ["surface", "provider", "model", "status"],
    registry=REGISTRY,
)
REQUEST_SECONDS = Histogram(
    "pystino_request_duration_seconds",
    "Seconds from the start of a metered request to the end of its response.",
    ["surface", "provider"],
    buckets=_DURATION_BUCKETS,
    registry=REGISTRY,
)
TTFT_SECONDS = Histogram(
    "pystino_time_to_first_token_seconds",
    "Seconds until the first streamed token, for streamed responses.",
    ["provider"],
    buckets=_TTFT_BUCKETS,
    registry=REGISTRY,
)
UPSTREAM_ERRORS = Counter(
    "pystino_upstream_errors",
    "Failed calls to a provider, by kind (timeout, network, 4xx, 5xx).",
    ["provider", "kind"],
    registry=REGISTRY,
)
FAILOVERS = Counter(
    "pystino_route_failovers",
    "Routed-alias attempts that moved on (failover), later served (recovered) or ran out.",
    ["alias", "outcome"],
    registry=REGISTRY,
)
TOKENS = Counter(
    "pystino_tokens",
    "Tokens by direction. prompt, cached_read and cache_write are disjoint slices.",
    ["provider", "model", "direction"],
    registry=REGISTRY,
)
COST = Counter(
    "pystino_cost",
    "Cost charged, in the currency it was charged in. The ledger is the source of truth.",
    ["provider", "currency"],
    registry=REGISTRY,
)
QUOTA_REFUSALS = Counter(
    "pystino_quota_refusals",
    "Requests refused by a quota rule, by the metric and the kind of scope that ran out.",
    ["metric", "scope"],
    registry=REGISTRY,
)
PRIVACY_ACTIONS = Counter(
    "pystino_privacy_actions",
    "What the privacy rules did, by pipeline kind (content, extract) and action.",
    ["kind", "action"],
    registry=REGISTRY,
)
REDACTION_SECONDS = Histogram(
    "pystino_redaction_duration_seconds",
    "Seconds spent redacting a request.",
    buckets=_REDACTION_BUCKETS,
    registry=REGISTRY,
)
SEARCH_REQUESTS = Counter(
    "pystino_search_requests",
    "Calls this gateway made to its own search backends.",
    ["backend"],
    registry=REGISTRY,
)
OCR_PAGES = Counter(
    "pystino_ocr_pages",
    "Pages read by OCR.",
    ["provider"],
    registry=REGISTRY,
)
DB_POOL = Gauge(
    "pystino_db_pool_connections",
    "Database pool connections, in use or the configured size, summed over workers.",
    ["state"],
    registry=REGISTRY,
    multiprocess_mode="livesum",
)
VALKEY_UP = Gauge(
    "pystino_valkey_up",
    "1 when Valkey answered the last probe; absent when none is configured.",
    registry=REGISTRY,
    multiprocess_mode="livemin",
)
REDACTION_UP = Gauge(
    "pystino_redaction_service_up",
    "1 when the redaction service answered the last probe; absent for in-process engines.",
    registry=REGISTRY,
    multiprocess_mode="livemin",
)
BUILD_INFO = Gauge(
    "pystino_build_info",
    "Always 1; the version is the label.",
    ["version"],
    registry=REGISTRY,
    multiprocess_mode="livemax",
)
START_TIME = Gauge(
    "pystino_process_start_time_seconds",
    "Unix time the oldest live worker started; uptime is time() minus this.",
    registry=REGISTRY,
    multiprocess_mode="livemin",
)

if not MULTIPROCESS:
    # One process: its own CPU, memory and file descriptors belong in the scrape.
    ProcessCollector(registry=REGISTRY)

# -- labels ----------------------------------------------------------------

_seen: dict[str, set[str]] = defaultdict(set)
_MAX_NAMES = 200


def _bounded(kind: str, value: str) -> str:
    """The value, until a label has had :data:`_MAX_NAMES` distinct ones; then ``other``.

    The catalogue is small, so this never fires in a healthy deployment. It is
    here so that an unforeseen unbounded source cannot turn into one series
    per request.
    """
    known = _seen[kind]
    if value in known:
        return value
    if len(known) >= _MAX_NAMES:
        return "other"
    known.add(value)
    return value


def provider_label(name: str | None, *, byok: bool = False) -> str:
    if byok:
        return "byok"
    return _bounded("provider", name) if name else "unknown"


def model_label(name: str | None, *, byok: bool = False) -> str:
    if byok:
        return "byok"
    return _bounded("model", name) if name else "unknown"


def provider_of(model: Any) -> str:
    """A model row's provider name, without ever triggering a lazy load."""
    if model is None:
        return "unknown"
    if getattr(model, "is_private", False):
        return "byok"
    try:
        from sqlalchemy import inspect

        loaded = inspect(model).attrs.provider.loaded_value
        name = getattr(loaded, "name", None)
    except Exception:
        name = None
    return provider_label(name)


_SURFACES = {
    "chat/completions": "chat_completions",
    "completions": "chat_completions",
    "responses": "responses",
    "messages": "messages",
    "embeddings": "embeddings",
    "images": "images",
    "ocr": "ocr",
    "audio": "transcription",
    "search": "search",
    "rerank": "rerank",
}


def surface_for_path(path: str) -> str:
    """A bounded name for where a request came in, from its path alone."""
    if path.startswith("/v1/"):
        rest = path[4:]
        for prefix, name in _SURFACES.items():
            if rest == prefix or rest.startswith(prefix + "/"):
                return name
        return "v1_other"
    for prefix, name in (
        ("/api/", "api"),
        ("/auth/", "auth"),
        ("/console", "console"),
        ("/scim/", "scim"),
    ):
        if path.startswith(prefix) or path == prefix.rstrip("/"):
            return name
    return "other"


# -- instrumentation points ------------------------------------------------


def request_started(surface: str) -> None:
    IN_FLIGHT.labels(surface).inc()


def request_finished(surface: str) -> None:
    IN_FLIGHT.labels(surface).dec()


def auth_failed(surface: str) -> None:
    REQUESTS.labels(surface, "none", "none", "auth_failed").inc()


def refused(
    surface: str,
    provider: str,
    model: str,
    status: Literal["blocked", "privacy_refused", "quota_exceeded"],
) -> None:
    """A request ended before any provider saw it (no ledger finalise follows)."""
    REQUESTS.labels(surface, provider, model, status).inc()


def finished_request(
    *,
    surface: str,
    provider: str,
    model: str,
    status: str,
    duration_seconds: float,
    ttfb_seconds: float | None = None,
    byok: bool = False,
    prompt: int = 0,
    completion: int = 0,
    cached_read: int = 0,
    cache_write: int = 0,
    pages: int = 0,
    cost: float | None = None,
    currency: str = "",
    searches: int = 0,
    search_backend: str | None = None,
) -> None:
    """One metered request's end: what the ledger's finalise knows, said once."""
    provider = provider_label(provider, byok=byok)
    model = model_label(model, byok=byok)
    REQUESTS.labels(surface, provider, model, status).inc()
    REQUEST_SECONDS.labels(surface, provider).observe(max(duration_seconds, 0.0))
    if ttfb_seconds is not None:
        TTFT_SECONDS.labels(provider).observe(max(ttfb_seconds, 0.0))
    # Disjoint slices of the prompt, so that their sum is the prompt.
    fresh = max(prompt - cached_read - cache_write, 0)
    for direction, count in (
        ("prompt", fresh),
        ("completion", completion),
        ("cached_read", cached_read),
        ("cache_write", cache_write),
    ):
        if count > 0:
            TOKENS.labels(provider, model, direction).inc(count)
    if pages > 0:
        OCR_PAGES.labels(provider).inc(pages)
    if cost is not None and cost > 0 and not byok and currency:
        COST.labels(provider, _bounded("currency", currency.upper())).inc(cost)
    if searches > 0:
        SEARCH_REQUESTS.labels(_bounded("backend", search_backend or "unknown")).inc(searches)


def upstream_error(provider: str, kind: str, *, byok: bool = False) -> None:
    UPSTREAM_ERRORS.labels(provider_label(provider, byok=byok), kind).inc()


def upstream_error_kind(status_code: int | None = None, exc: BaseException | None = None) -> str:
    if exc is not None:
        # The upstream client wraps the transport error and keeps it as `cause`.
        name = type(getattr(exc, "cause", None) or exc).__name__.lower()
        return "timeout" if "timeout" in name else "network"
    if status_code is not None and status_code >= 500:
        return "5xx"
    return "4xx"


def route_failover(alias: str, outcome: Literal["failover", "recovered", "exhausted"]) -> None:
    FAILOVERS.labels(_bounded("alias", alias), outcome).inc()


def quota_refusal(metric: str, scope: str) -> None:
    QUOTA_REFUSALS.labels(metric, scope).inc()


def privacy_action(kind: str, action: Literal["redacted", "blocked", "undecidable"]) -> None:
    PRIVACY_ACTIONS.labels(kind, action).inc()


def redaction_seconds(seconds: float) -> None:
    REDACTION_SECONDS.observe(max(seconds, 0.0))


# -- active callers --------------------------------------------------------

_WINDOWS = {"5m": 5, "15m": 15}
_KEY_TTL = 20 * 60


def _digest(kind: str, identity: object) -> str:
    """8 bytes of a hash: enough to count distinct callers, useless to find one."""
    return hashlib.blake2b(f"{kind}:{identity}".encode(), digest_size=8).hexdigest()


class ActiveTracker:
    """Distinct callers per window, without keeping who they are (module docstring)."""

    def __init__(self, valkey: Any | None = None, *, clock: Any = time.time) -> None:
        self._valkey = valkey
        self._clock = clock
        # kind -> minute -> hashes not yet sent to Valkey, and, without Valkey,
        # the whole history this worker keeps.
        self._minutes: dict[str, dict[int, set[str]]] = {"user": {}, "key": {}}
        self._unsent: dict[str, dict[int, set[str]]] = {"user": {}, "key": {}}

    def observe(self, user_id: object, api_key_id: object | None = None) -> None:
        minute = int(self._clock() // 60)
        self._note("user", user_id, minute)
        if api_key_id is not None:
            self._note("key", api_key_id, minute)

    def _note(self, kind: str, identity: object, minute: int) -> None:
        digest = _digest(kind, identity)
        self._minutes[kind].setdefault(minute, set()).add(digest)
        if self._valkey is not None:
            self._unsent[kind].setdefault(minute, set()).add(digest)

    def _prune(self, minute: int) -> None:
        horizon = minute - max(_WINDOWS.values())
        for series in self._minutes.values():
            for old in [m for m in series if m <= horizon]:
                del series[old]

    @staticmethod
    def _key(kind: str, minute: int) -> str:
        return f"pystino:active:{kind}:{minute}"

    async def flush(self) -> None:
        """Send what this worker has seen to Valkey's per-minute HyperLogLogs."""
        if self._valkey is None:
            return
        pending, self._unsent = self._unsent, {"user": {}, "key": {}}
        try:
            async with self._valkey.pipeline(transaction=False) as pipe:
                for kind, series in pending.items():
                    for minute, digests in series.items():
                        key = self._key(kind, minute)
                        pipe.pfadd(key, *digests)
                        pipe.expire(key, _KEY_TTL)
                await pipe.execute()
        except Exception:
            logger.warning("could not record active callers in valkey", exc_info=True)
            # Keep them for the next try, so a blip loses nothing.
            for kind, series in pending.items():
                for minute, digests in series.items():
                    self._unsent[kind].setdefault(minute, set()).update(digests)

    async def counts(self) -> dict[str, dict[str, int]]:
        """``{kind: {window: count}}``; deployment-wide with Valkey, per worker without."""
        now_minute = int(self._clock() // 60)
        self._prune(now_minute)
        out: dict[str, dict[str, int]] = {"user": {}, "key": {}}
        for kind in out:
            for window, minutes in _WINDOWS.items():
                span = range(now_minute - minutes + 1, now_minute + 1)
                if self._valkey is not None:
                    try:
                        out[kind][window] = int(
                            await self._valkey.pfcount(*[self._key(kind, m) for m in span])
                        )
                        continue
                    except Exception:
                        logger.warning("could not read active callers from valkey", exc_info=True)
                seen: set[str] = set()
                for minute in span:
                    seen |= self._minutes[kind].get(minute, set())
                out[kind][window] = len(seen)
        return out


_tracker: ActiveTracker | None = None


def observe_caller(user_id: object, api_key_id: object | None = None) -> None:
    """Note an authenticated caller. A no-op unless metrics are on."""
    if _tracker is not None:
        _tracker.observe(user_id, api_key_id)


# -- lifecycle -------------------------------------------------------------


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("gateway")
    except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
        return "unknown"


def configure(app: FastAPI, *, valkey: Any | None) -> None:
    """Turn collection on for this worker (called from ``init_app_state``)."""
    global _tracker
    _tracker = ActiveTracker(valkey)
    BUILD_INFO.labels(_version()).set(1)
    START_TIME.set(time.time())
    app.state.metrics_started = True


def shutdown(app: FastAPI) -> None:
    """Forget this worker: its gauge files must not outlive it in a merged scrape."""
    global _tracker
    _tracker = None
    if MULTIPROCESS:
        with suppress(Exception):
            mark_process_dead(os.getpid())  # type: ignore[no-untyped-call]


async def sample(app: FastAPI) -> None:
    """Refresh the gauges that are read rather than counted."""
    tracker = _tracker
    if tracker is not None:
        await tracker.flush()
        counts = await tracker.counts()
        for window, value in counts["user"].items():
            ACTIVE_USERS.labels(window).set(value)
        for window, value in counts["key"].items():
            ACTIVE_API_KEYS.labels(window).set(value)

    pool = getattr(getattr(getattr(app.state, "engine", None), "sync_engine", None), "pool", None)
    if pool is not None and hasattr(pool, "checkedout"):
        with suppress(Exception):
            DB_POOL.labels("in_use").set(pool.checkedout())
            DB_POOL.labels("size").set(pool.size() + max(pool.overflow(), 0))

    valkey = getattr(app.state, "valkey", None)
    if valkey is not None:
        try:
            await valkey.ping()
            VALKEY_UP.set(1)
        except Exception:
            VALKEY_UP.set(0)
    elif getattr(app.state, "counter_store_error", None):
        VALKEY_UP.set(0)

    redactor = getattr(app.state, "redactor", None)
    probe = getattr(redactor, "service_reachable", None)
    if probe is not None:
        try:
            REDACTION_UP.set(1 if await probe() else 0)
        except Exception:
            REDACTION_UP.set(0)


class SamplerLoop:
    """Refreshes the sampled gauges every few seconds, so a scrape that lands on
    another worker still reads figures that are at most one interval old."""

    def __init__(self, app: FastAPI, interval: float) -> None:
        self._app = app
        self._interval = interval
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="metrics-sampler")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await sample(self._app)
            except Exception:
                logger.warning("metrics sampling failed", exc_info=True)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)

    async def stop(self) -> None:
        if self._task is not None:
            from gateway.loops import stop_between_passes

            await stop_between_passes(self._task, self._stopping)


async def render(app: FastAPI) -> bytes:
    """The exposition text: this worker's instruments, or every worker's merged."""
    await sample(app)
    if MULTIPROCESS:
        registry = CollectorRegistry()
        MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
        # The process collector describes the worker that answered the scrape.
        ProcessCollector(registry=registry)
        return generate_latest(registry)
    return generate_latest(REGISTRY)


_UNCOUNTED_PATHS = frozenset({"/metrics", "/healthz", "/readyz"})
_CALLER_SURFACES = frozenset(_SURFACES.values()) | {"v1_other"}


class MetricsMiddleware:
    """Pure ASGI: holds the in-flight gauge for the whole life of a response.

    ASGI rather than ``BaseHTTPMiddleware`` because that one buffers streamed
    bodies, and a stream is exactly the request this gauge exists to show. A
    ``401`` on a ``/v1`` route is counted here as ``auth_failed``: the request
    never reached the metered path, so nothing else would count it.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] in _UNCOUNTED_PATHS:
            await self.app(scope, receive, send)
            return
        surface = surface_for_path(scope["path"])

        async def watching(message: Any) -> None:
            if (
                message["type"] == "http.response.start"
                and message["status"] == 401
                and surface in _CALLER_SURFACES
            ):
                auth_failed(surface)
            await send(message)

        request_started(surface)
        try:
            await self.app(scope, receive, watching)
        finally:
            request_finished(surface)
