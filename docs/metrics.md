# Metrics

The gateway can report what it is doing to Prometheus: how many requests are in
flight, who is active (as a number, never as a name), how requests end, how long
they take, how many tokens and how much money went through, and whether its
dependencies answer. It is a text page at `GET /metrics`, off until you ask for it.

Metrics describe load and health. They are not the ledger: for what anyone was
charged, read the [usage reports](accounting-and-quotas.md). The cost counter
here is a float for dashboards and alerts.

## Turn it on

Two settings, both needed:

```bash
GATEWAY_METRICS__ENABLED='true'
GATEWAY_METRICS__TOKEN='…'        # openssl rand -hex 32; at least 16 characters
```

`deploy/.env.example` has them commented out. With `ENABLED=true` and no token
the gateway **refuses to start**: the endpoint is never served open. With
`ENABLED=false` (the default) the route does not exist and answers 404 like any
unknown path.

## Who may read it

One rule: a **bearer token**, `Authorization: Bearer <GATEWAY_METRICS__TOKEN>`,
compared in constant time. A missing, malformed or wrong token is a `401`. An
API key, an OIDC token or a console session is not the token.

The token is the second layer. The first is the proxy: this repository's
`deploy/caddy/Caddyfile` answers `404` for `/metrics` and `/metrics/*`, so the
public origin never serves it. Scrape the gateway directly instead:

- from the host the stack runs on, at `http://127.0.0.1:8000/metrics` (the
  gateway's loopback port, `GATEWAY_PORT`);
- from a Prometheus container on the compose network, at `http://gateway:8000/metrics`.

If you put your own proxy in front of the gateway, do the same there. Without
that block, the token is the only thing between the internet and your traffic
figures.

## Scrape it

```yaml
# prometheus.yml
scrape_configs:
  - job_name: pystino
    metrics_path: /metrics
    scrape_interval: 15s
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/pystino.token   # a file holding only the token
    static_configs:
      - targets: ["127.0.0.1:8000"]
```

Check it by hand:

```bash
curl -s -H "Authorization: Bearer $GATEWAY_METRICS__TOKEN" http://127.0.0.1:8000/metrics | head
```

## What is in it

Counters appear in the page with a `_total` suffix and histograms as
`_bucket`, `_sum` and `_count`, as Prometheus expects.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `pystino_requests_in_flight` | gauge | `surface` | Requests being served right now, a stream counted until its last byte. Summed over the workers. |
| `pystino_active_users` | gauge | `window` (`5m`, `15m`) | Distinct signed-in callers seen in the window ([how](#how-active-callers-are-counted)). |
| `pystino_active_api_keys` | gauge | `window` | Distinct API keys seen in the window. |
| `pystino_requests_total` | counter | `surface`, `provider`, `model`, `status` | Requests by how they ended. A routed alias counts each attempt. |
| `pystino_request_duration_seconds` | histogram | `surface`, `provider` | Start of the metered request to the end of its response. |
| `pystino_time_to_first_token_seconds` | histogram | `provider` | Until the first streamed token. Streamed responses only. |
| `pystino_upstream_errors_total` | counter | `provider`, `kind` | Failed provider calls: `timeout`, `network`, `4xx`, `5xx`. |
| `pystino_route_failovers_total` | counter | `alias`, `outcome` | A routed alias: `failover` (an attempt failed and the next was tried), `recovered` (a later attempt served), `exhausted` (the last attempt failed too). |
| `pystino_tokens_total` | counter | `provider`, `model`, `direction` | `prompt`, `completion`, `cached_read`, `cache_write`. The three prompt slices are disjoint: `prompt` is the part that was neither read from nor written to a cache, so their sum is the prompt. |
| `pystino_cost_total` | counter | `provider`, `currency` | Cost charged, in the currency it was charged in. Not for a caller's own provider key (BYOK), which is never charged. |
| `pystino_quota_refusals_total` | counter | `metric` (`requests`, `tokens`, `cost`, `own_search_requests`), `scope` (`global`, `group`, `user`, `api_key`) | A request refused by a quota rule; the kind of limit that ran out, never whose. Counted per attempt, like `requests_total`: a routed alias whose first target is refused and whose second serves counts one refusal. |
| `pystino_privacy_actions_total` | counter | `kind` (`content`, `extract`), `action` (`redacted`, `blocked`, `undecidable`) | What the privacy rules did. `undecidable` is a request refused because screening could not run or a condition could not be decided. `redacted` counts per attempt too, so a routed alias that fails over and redacts on both targets counts two. |
| `pystino_redaction_duration_seconds` | histogram | none | Time spent redacting one request. |
| `pystino_search_requests_total` | counter | `backend` | Calls the gateway made to its own search backends (not a provider's built-in search). |
| `pystino_ocr_pages_total` | counter | `provider` | Pages read by OCR. |
| `pystino_db_pool_connections` | gauge | `state` (`in_use`, `size`) | Database pool connections, summed over the workers. |
| `pystino_valkey_up` | gauge | none | 1 if Valkey answered the last probe, 0 if not. The series does not exist at all when no Valkey is configured, so an alert on `== 0` stays quiet there. Any worker seeing it down makes it 0. |
| `pystino_redaction_service_up` | gauge | none | 1 if the redaction service answered `/healthz`, 0 if not. The series does not exist for in-process engines (`noop` and the like), so an alert on `== 0` stays quiet there. |
| `pystino_build_info` | gauge | `version` | Always 1. |
| `pystino_process_start_time_seconds` | gauge | none | When the oldest live worker started. Uptime is `time() - pystino_process_start_time_seconds`. |
| `process_*`, `python_*` | | | The standard process collector: CPU, memory, file descriptors. With several workers these describe the worker that answered the scrape. |

`surface` is one of `chat_completions`, `responses`, `messages`, `embeddings`,
`images`, `ocr`, `transcription`, `search`, `rerank`, `v1_other`, and for the
in-flight gauge also `api`, `auth`, `console`, `scim` and `other`. `status` is one
of `completed`, `upstream_error`, `client_disconnected`, `blocked` (a redaction
policy refused it), `privacy_refused` (a privacy rule refused it),
`quota_exceeded`, `quota_unavailable` (the quota counters could not answer, a `503`), `privacy_undecidable` (screening could not decide, a `503`) and `auth_failed` (a `401` on a `/v1` route; its `provider` and
`model` are `none`).

`model` is the catalogue name, `provider` the provider's name. A request served
by a member's own provider and key ([BYOK](byok.md)) reports both as `byok`,
because those names are chosen by members and not by you. Health checks and the
scrape itself are not counted.

### What is never in it

No user, e-mail address, group, key, key prefix, prompt, reply, file name or id
of any kind appears as a label or a value, and a test scans the page for the
test user's email, ids, key and prompt. Label values are a closed vocabulary or a
name from your catalogue; a label that somehow gains more than 200 distinct
values folds the rest into `other`.

### How active callers are counted

Each authenticated caller (a key, an OIDC token or a console session) is reduced
to an 8-byte hash and noted in a one-minute bucket. A window is the buckets of its
last 5 or 15 minutes.

- **With Valkey** (`GATEWAY_VALKEY_URL`, the compose default) the hashes go into
  one HyperLogLog per minute, `pystino:active:user:<minute>` and
  `pystino:active:key:<minute>`, which expire after 20 minutes. A HyperLogLog
  holds a count, not its members, so even Valkey cannot list who was active. A
  window is the union of its minutes: one number for the whole deployment, with
  HyperLogLog's error of about 1%.
- **Without Valkey** each worker counts its own callers in memory and the
  scrape reports the busiest worker's figure. That is a lower bound, close to the
  truth when the proxy spreads callers evenly across the workers.

A worker sends its callers to Valkey every `GATEWAY_METRICS__SAMPLE_SECONDS`
(default 10), so the figures lag by that much.

## Several workers

`gateway serve --workers N` (the image runs two) starts N processes, and a plain
metrics library would report whichever one answered. The gateway uses
`prometheus_client`'s multiprocess mode instead: each worker writes its own
files under one directory, and a scrape merges them, so the numbers describe the
container.

- With metrics on and more than one worker, `gateway serve` creates the directory
  and **empties it at every start** (last run's counters would otherwise show
  requests that never happened here). It is `GATEWAY_METRICS__MULTIPROC_DIR`, else
  `PROMETHEUS_MULTIPROC_DIR` if you set it, else a fresh temporary directory.
  It must be a directory nothing else uses. With one worker nothing is merged and
  nothing is written to disk.
- Counters and histograms add up across workers. In-flight requests and pool
  connections are summed over the *live* workers, `pystino_valkey_up` and
  `pystino_redaction_service_up` take the lowest, the active-caller gauges the
  highest.
- A worker that exits cleanly removes its gauges; its counters stay, as they must.
  A worker that is killed (SIGKILL, the OOM killer) leaves its gauge files, but the
  next scrape (and every worker start) removes the files of workers whose process no
  longer exists, so its in-flight count and pool use do not linger.
- If you start the gateway some other way (`uvicorn` or `gunicorn` directly with
  several workers), export `PROMETHEUS_MULTIPROC_DIR` to an empty directory
  yourself before it starts.

## Settings

| Variable | Default | |
|---|---|---|
| `GATEWAY_METRICS__ENABLED` | `false` | Serve `/metrics`. Needs the token. |
| `GATEWAY_METRICS__TOKEN` | empty | The bearer token, at least 16 characters. |
| `GATEWAY_METRICS__MULTIPROC_DIR` | empty | Where the workers keep their files (see above). |
| `GATEWAY_METRICS__SAMPLE_SECONDS` | `10` | How often the sampled gauges (pool, health, active callers) refresh. |

## Some queries for Grafana

```promql
# concurrent requests, per surface
sum by (surface) (pystino_requests_in_flight)

# request rate and error share
sum(rate(pystino_requests_total[5m]))
sum(rate(pystino_requests_total{status=~"upstream_error|auth_failed"}[5m])) / sum(rate(pystino_requests_total[5m]))

# p95 latency per provider
histogram_quantile(0.95, sum by (le, provider) (rate(pystino_request_duration_seconds_bucket[5m])))

# time to first token, p95
histogram_quantile(0.95, sum by (le, provider) (rate(pystino_time_to_first_token_seconds_bucket[5m])))

# tokens per second by model and direction; spend per hour
sum by (model, direction) (rate(pystino_tokens_total[5m]))
sum by (provider, currency) (increase(pystino_cost_total[1h]))

# a provider failing, and routes giving up
sum by (provider, kind) (rate(pystino_upstream_errors_total[5m]))
sum by (alias) (increase(pystino_route_failovers_total{outcome="exhausted"}[15m]))

# is anything down? (a dependency that is not configured has no series, so this is quiet)
pystino_valkey_up == 0 or pystino_redaction_service_up == 0
```

## Later: OpenTelemetry

Every instrument lives in one module, `gateway/metrics.py`, and the rest of the
code calls functions there rather than Prometheus objects, so that the same
events can be exported through OpenTelemetry without touching the request path.
It is not built yet.
