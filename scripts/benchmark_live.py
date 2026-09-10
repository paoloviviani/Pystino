#!/usr/bin/env python3
"""What the gateway costs, measured against the running stack.

Two numbers matter and they are measured differently:

* **Added latency** — the gateway against the same upstream called directly,
  taken at concurrency 1 so nothing queues. On a small box a saturating test
  measures contention, not overhead.
* **CPU per request** — read from each container's cgroup ``usage_usec``, which
  is cumulative and therefore immune to contention. This is what extrapolates to
  "requests per second per core"; a throughput number from a development box
  does not.

The capacity model this produces is::

    cores needed = target req/s * (CPU seconds per request) / target utilisation

Modes:

    ladder      per-layer cost, by bracketing endpoints (default)
    load        throughput and latency against concurrency; a second argument
                is the sweep, e.g. `load 1,2,4,8,16,32`
    stream      cost against answer length, to separate fixed from per-frame
    redaction   cost against prompt length — detection is the dominant term
    all

Usage:
    set -a; . deploy/.env; set +a
    uv run python scripts/benchmark_live.py [mode]

It creates an **unpriced** model on the fake upstream and a key of its own, so a
benchmark can never distort billing, and deletes both plus its ledger rows on
the way out. Costs no real money: nothing here touches a real provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import statistics
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import httpx
from live_session import GATEWAY, admin_credentials, login, request

BENCH_MODEL = "benchmark-live"
BENCH_KEY_NAME = "benchmark-live"
UPSTREAM_DIRECT = os.environ.get("FAKE_UPSTREAM_URL", "http://localhost:8081")

CONTAINERS = {
    "gateway": "llm-platform-gateway-1",
    "redaction": "llm-platform-redaction-1",
    "postgres": "llm-platform-postgres-1",
    "valkey": "llm-platform-valkey-1",
}
COMPOSE = [
    "docker", "compose", "--env-file", "deploy/.env",
    "-f", "deploy/compose/docker-compose.yml",
]
ROOT = pathlib.Path(__file__).resolve().parent.parent


# -- CPU accounting ---------------------------------------------------------


def _cgroup(container: str) -> pathlib.Path | None:
    try:
        cid = subprocess.run(  # noqa: S603
            ["docker", "inspect", container, "--format", "{{.Id}}"],  # noqa: S607
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    path = pathlib.Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat")
    return path if path.exists() else None


CGROUPS = {label: _cgroup(name) for label, name in CONTAINERS.items()}
HAVE_CPU = any(path is not None for path in CGROUPS.values())


def cpu_usec() -> dict[str, int]:
    """Cumulative CPU microseconds per container, or an empty dict off-host."""
    out: dict[str, int] = {}
    for label, path in CGROUPS.items():
        if path is None:
            continue
        try:
            for line in path.read_text().splitlines():
                if line.startswith("usage_usec"):
                    out[label] = int(line.split()[1])
                    break
        except OSError:
            pass
    return out


def cpu_delta(before: dict[str, int], after: dict[str, int], n: int) -> dict[str, float]:
    return {
        label: (after[label] - before[label]) / max(1, n) / 1000
        for label in after
        if label in before
    }


# -- setup and teardown -----------------------------------------------------


def sql(query: str) -> str:
    result = subprocess.run(  # noqa: S603
        [*COMPOSE, "exec", "-T", "postgres", "psql", "-U", "gateway", "-d", "gateway",
         "-tAc", query],
        capture_output=True, text=True, cwd=str(ROOT), check=False,
    )
    return result.stdout.strip()


def setup() -> str | None:
    """An unpriced model on the fake upstream, and a key. Returns the secret."""
    credentials = admin_credentials()
    if credentials is None:
        print("FAILED: GATEWAY_LOCAL_ADMIN_PASSWORD is not set (source deploy/.env)")
        return None
    admin = login(*credentials)
    if admin is None:
        return None

    _, _, body = request(admin, f"{GATEWAY}/api/admin/providers?limit=200")
    fake = next(
        (p for p in json.loads(body)["items"] if "fake-upstream" in p["base_url"]), None
    )
    if fake is None:
        print("  skipped: no provider points at the fake upstream in this deployment.")
        print("  Bring the smoke overlay up — a benchmark must not call a real provider.")
        return None

    _, _, body = request(admin, f"{GATEWAY}/api/admin/models?limit=200")
    existing = next(
        (m for m in json.loads(body)["items"] if m["name"] == BENCH_MODEL), None
    )
    if existing is None:
        # Unpriced on purpose: cost is exactly zero, so a stray row cannot
        # distort a billing reconciliation even if cleanup fails.
        status, _, body = request(
            admin, f"{GATEWAY}/api/admin/models", method="POST",
            json_body={"name": BENCH_MODEL, "upstream_model": "upstream/smoke-model",
                       "provider_id": fake["id"], "kind": "chat",
                       # What `upstream/smoke-model` declares in the fake
                       # upstream's catalogue. Omitting them left the row
                       # advertising no capabilities at all on `/v1/models`
                       # (ADR 0031), which a client reads as "can do nothing".
                       "input_modalities": ["text"],
                       "output_modalities": ["text"],
                       "supported_features": ["tools", "json_mode"]},
        )
        if status != 201:
            print(f"  could not create the benchmark model: HTTP {status}")
            return None
        existing = json.loads(body)

    _, _, body = request(admin, f"{GATEWAY}/api/admin/groups")
    groups = json.loads(body)["items"]
    # The admin's own group, so no other group's quota rules are involved.
    mine = next((g for g in groups if g["name"] == "platform-admins"), groups[0])
    request(
        admin, f"{GATEWAY}/api/admin/groups/{mine['id']}/models/{existing['id']}",
        method="PUT",
    )

    status, _, body = request(
        admin, f"{GATEWAY}/api/me/keys", method="POST",
        json_body={"name": BENCH_KEY_NAME},
    )
    if status != 201:
        print(f"  could not mint a key: HTTP {status}")
        return None
    return str(json.loads(body)["secret"])


def teardown() -> None:
    """Remove everything this script created, and nothing else.

    Scoped strictly by the benchmark's own model and key names. The usage rows
    are zero-cost by construction, so the worst case of a failed cleanup is
    clutter rather than a wrong figure.
    """
    rows = f"delete from usage_records where model_name = '{BENCH_MODEL}'"  # noqa: S608
    grants = (
        "delete from group_model_access where model_id in "  # noqa: S608
        f"(select id from models where name = '{BENCH_MODEL}')"
    )
    model = f"delete from models where name = '{BENCH_MODEL}'"  # noqa: S608
    key = (
        "update api_keys set revoked_at = now() "  # noqa: S608
        f"where name = '{BENCH_KEY_NAME}' and revoked_at is null"
    )
    removed = sql(f"with gone as ({rows} returning 1) select count(*) from gone;")  # noqa: S608
    for statement in (grants, model, key):
        sql(statement + ";")
    print(f"\ncleaned up: {removed or 0} benchmark usage row(s), model and key removed.")
    print("Quota counters rebuild from usage_records, so flush Valkey if a rule looks wrong.")


# -- probes -----------------------------------------------------------------


async def probe(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    n: int,
    concurrency: int = 1,
    bodies: list[dict[str, Any]] | None = None,
    stream: bool = False,
) -> dict[str, Any]:
    latencies: list[float] = []
    ttfbs: list[float] = []
    frames: list[int] = []
    errors = 0
    semaphore = asyncio.Semaphore(concurrency)

    async def once(index: int) -> None:
        nonlocal errors
        payload = bodies[index % len(bodies)] if bodies else body
        async with semaphore:
            start = time.perf_counter()
            try:
                if stream:
                    count = 0
                    first: float | None = None
                    async with client.stream(method, url, json=payload) as response:
                        if response.status_code != 200:
                            errors += 1
                            return
                        async for line in response.aiter_lines():
                            if line.startswith("data: "):
                                if first is None:
                                    first = (time.perf_counter() - start) * 1000
                                count += 1
                    frames.append(count)
                    if first is not None:
                        ttfbs.append(first)
                else:
                    response = await client.request(method, url, json=payload)
                    if response.status_code != 200:
                        errors += 1
                        return
            except Exception:
                errors += 1
                return
            latencies.append((time.perf_counter() - start) * 1000)

    # Warm connections and any per-process lazy initialisation.
    for index in range(min(5, n)):
        await once(index)
    latencies.clear()
    ttfbs.clear()
    frames.clear()
    errors = 0

    before = cpu_usec()
    wall = time.perf_counter()
    await asyncio.gather(*(once(index) for index in range(n)))
    elapsed = time.perf_counter() - wall
    after = cpu_usec()

    def quantile(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))]

    return {
        "n": n, "concurrency": concurrency, "errors": errors, "wall_s": elapsed,
        "rps": len(latencies) / elapsed if elapsed else 0.0,
        "p50": statistics.median(latencies) if latencies else 0.0,
        "p95": quantile(latencies, 0.95), "p99": quantile(latencies, 0.99),
        "ttfb_p50": statistics.median(ttfbs) if ttfbs else None,
        "frames": statistics.median(frames) if frames else None,
        "cpu_ms": cpu_delta(before, after, len(latencies)),
    }


def show(label: str, result: dict[str, Any], *, wide: bool = False) -> None:
    note = f" [{result['errors']} errors]" if result["errors"] else ""
    line = f"  {label:36} p50={result['p50']:7.2f}ms  p95={result['p95']:8.2f}ms"
    if wide:
        line += f"  {result['rps']:7.1f} req/s"
    if result["ttfb_p50"] is not None:
        line += f"  ttfb={result['ttfb_p50']:6.2f}ms"
    if HAVE_CPU:
        total = sum(result["cpu_ms"].values())
        gateway = result["cpu_ms"].get("gateway", 0.0)
        line += f"  cpu={total:6.2f}ms (gw {gateway:5.2f})"
    print(line + note)


def capacity(cpu_ms: float, label: str) -> None:
    if not HAVE_CPU or cpu_ms <= 0:
        return
    print(f"\n  capacity for {label} at {cpu_ms:.1f}ms CPU per request:")
    for utilisation in (1.0, 0.7):
        per_core = 1000.0 / cpu_ms * utilisation
        at = "saturated" if utilisation == 1.0 else "70% utilised"
        print(f"    {per_core:6.1f} req/s per core ({at})"
              f"   → {per_core * 4:6.1f} on 4 cores, {per_core * 8:6.1f} on 8")


# -- modes ------------------------------------------------------------------


def chat(content: str = "hello there", **extra: Any) -> dict[str, Any]:
    return {"model": BENCH_MODEL, "messages": [{"role": "user", "content": content}], **extra}


async def ladder(client: httpx.AsyncClient, plain: httpx.AsyncClient) -> None:
    print("\n=== per-layer cost, concurrency 1 ===")
    print("  each row adds one layer to the one above, so the deltas attribute the cost")
    show("upstream called directly", await probe(
        plain, "POST", f"{UPSTREAM_DIRECT}/v1/chat/completions",
        body={"model": "upstream/smoke-model", "messages": [{"role": "user", "content": "hi"}]},
        n=150))
    show("/healthz  (framework floor)", await probe(client, "GET", f"{GATEWAY}/healthz", n=150))
    show("/readyz   (+ database)", await probe(client, "GET", f"{GATEWAY}/readyz", n=100))
    show("/v1/models (+ auth, access)", await probe(client, "GET", f"{GATEWAY}/v1/models", n=150))
    full = await probe(client, "POST", f"{GATEWAY}/v1/chat/completions", body=chat(), n=150)
    show("/v1/chat/completions", full)
    if HAVE_CPU:
        capacity(sum(full["cpu_ms"].values()), "a short non-streamed request")


#: The default sweep. Coarse on purpose — it is the shape that matters, and a
#: finer one costs minutes. Override it to find the knee on a particular box:
#: ``benchmark_live.py load 1,2,4,8,16,24,32``.
LOAD_STEPS = (1, 2, 8, 32)


async def load(client: httpx.AsyncClient, steps: tuple[int, ...] = LOAD_STEPS) -> None:
    print("\n=== throughput against concurrency ===")
    print("  the load generator shares this machine's cores, so req/s is a floor")
    results: list[tuple[int, dict[str, Any]]] = []
    for concurrency in steps:
        count = max(200, concurrency * 8)
        # A distinct prompt per request, at the same length. With redaction on,
        # one repeated prompt hits the detection cache after the first and this
        # measures the cache rather than the pipeline — silently, and only when
        # redaction happens to be enabled, which is the worst kind of artefact
        # for a number somebody plans capacity from.
        bodies = [chat(f"hello there [{index}]") for index in range(count)]
        result = await probe(
            client, "POST", f"{GATEWAY}/v1/chat/completions", bodies=bodies,
            n=count, concurrency=concurrency)
        show(f"concurrency {concurrency}", result, wide=True)
        results.append((concurrency, result))

    # The knee is the actionable number: the last step whose throughput still
    # improved. Past it, latency grows and throughput does not, which is where
    # a queue in front of the gateway starts paying for itself.
    best = max(results, key=lambda item: item[1]["rps"])
    if best is not results[-1] and len(results) > 1:
        print(
            f"\n  peak throughput at concurrency {best[0]}: {best[1]['rps']:.1f} req/s, "
            f"p95 {best[1]['p95']:.0f}ms"
        )
        last = results[-1][1]
        print(
            f"  at concurrency {results[-1][0]} it is {last['rps']:.1f} req/s with "
            f"p95 {last['p95']:.0f}ms — {last['p95'] / best[1]['p95']:.1f}x the latency "
            "for no more work done."
        )
    print("\n  A falling req/s with rising concurrency is saturation collapse, not noise:")
    print("  it is the case for shedding load at the edge rather than queueing it here.")


async def stream(client: httpx.AsyncClient) -> None:
    print("\n=== streaming: cost against answer length ===")
    result = await probe(client, "POST", f"{GATEWAY}/v1/chat/completions",
                         body=chat(**{"stream": True}), n=120, stream=True)
    show(f"streamed ({result['frames']:.0f} frames)", result)
    print("\n  The committed fake upstream emits a fixed number of frames, so this")
    print("  cannot separate fixed cost from per-frame cost. Point it at an upstream")
    print("  whose answer length varies to fit that slope.")


async def redaction(client: httpx.AsyncClient) -> None:
    print("\n=== redaction: cost against prompt length ===")
    print("  the dominant term for agent workloads, whose prompts carry whole files")
    # Unique text every time: identical prompts hit the detection cache after the
    # first, which flatters this enormously.
    sentence = (
        "Please review the contract for Mario Rossi at mario.rossi@links.example, "
        "IBAN IT60X0542811101000000123456, phone +39 011 123 4567, in Torino. "
    )
    for repeats, n in ((1, 40), (8, 30), (32, 20), (128, 10)):
        bodies = [
            chat(f"[{index}] " + sentence * repeats) for index in range(n)
        ]
        approx_tokens = int(len(sentence) * repeats / 4)
        result = await probe(client, "POST", f"{GATEWAY}/v1/chat/completions",
                            bodies=bodies, n=n)
        label = f"~{approx_tokens:,} prompt tokens"
        show(label, result)
    if HAVE_CPU:
        print("\n  Detection is CPU-bound named-entity recognition in a separate service,")
        print("  so this scales by scaling that service — or by not running it where")
        print("  policy does not require it (docs/redaction-scoping-plan.md).")


def _steps(argument: str | None) -> tuple[int, ...]:
    """A concurrency sweep from the command line, or the default."""
    if not argument:
        return LOAD_STEPS
    try:
        parsed = tuple(int(part) for part in argument.split(",") if part.strip())
    except ValueError:
        print(f"not a concurrency list: {argument!r}")
        return LOAD_STEPS
    return tuple(step for step in parsed if step > 0) or LOAD_STEPS


async def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "ladder"
    steps = _steps(sys.argv[2] if len(sys.argv) > 2 else None)
    if not HAVE_CPU:
        print("note: cgroup CPU counters unreadable (not the docker host?);")
        print("      latency is still measured, capacity is not.")

    secret = setup()
    if secret is None:
        return 0

    auth = {"authorization": f"Bearer {secret}", "content-type": "application/json"}
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=64)
    try:
        async with (
            httpx.AsyncClient(timeout=120, headers=auth, limits=limits) as client,
            httpx.AsyncClient(timeout=120, limits=limits) as plain,
        ):
            if mode in ("ladder", "all"):
                await ladder(client, plain)
            if mode in ("load", "all"):
                await load(client, steps)
            if mode in ("stream", "all"):
                await stream(client)
            if mode in ("redaction", "all"):
                await redaction(client)
    finally:
        teardown()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
