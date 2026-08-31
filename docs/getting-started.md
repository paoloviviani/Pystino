# Getting started

Two ways in: the Docker compose stack (the whole platform, PostgreSQL included)
or a bare `uv` environment (the Python services only, tests included). The
compose route is the one that gets you a billed completion and a console.

## Run the stack

```bash
cp deploy/.env.example deploy/.env
# edit deploy/.env: set POSTGRES_PASSWORD, GATEWAY_SESSION_SECRET,
# and GATEWAY_UPSTREAM_API_KEY
docker compose -f deploy/compose/docker-compose.yml up --build
```

That starts PostgreSQL and Valkey, runs migrations once, and starts the gateway
on `localhost:8000`. The base compose file publishes **127.0.0.1 only** — that
is deliberate, and [Deployment](deployment.md) covers what it takes to change
it.

## Create something to talk to

```bash
docker compose -f deploy/compose/docker-compose.yml exec gateway \
  gateway seed --model my-model --upstream-model gpt-4o-mini
```

It prints an API key (once) and a ready-made `curl`. Any OpenAI client works:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="gwk_...")
client.chat.completions.create(
    model="my-model",
    messages=[{"role": "user", "content": "hello"}],
)
```

The request is redacted, admitted against the caller's quotas, metered and
settled into the ledger. Streaming works the same way, with `stream=True` or
`stream_options={"include_usage": true}` — the gateway forces usage out of the
upstream either way ([ADR 0007](adr/0007-sse-streaming.md)).

## Try it without a provider key

`docker-compose.smoke.yml` adds a fake OpenAI-compatible upstream, so the whole
topology can be exercised with no provider account:

```bash
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml up --build
```

It is a separate overlay on purpose: a fake upstream in the base file would be
one careless `-f` away from production.

## Sign in to the console

Enable local sign-in and create the first administrator — it prompts, so
nothing lands in shell history:

```bash
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml exec gateway \
  gateway passwd admin@local
```

The console is at <http://localhost:8000/console>. Sign in with the account you
just created. OIDC against GitLab, Entra ID or any other provider is a `.env`
change, not a new component — see
[OIDC against any provider](oidc-generic-provider.md). Local authentication and
OIDC are two doors into the same session ([ADR 0043](adr/0043-local-authentication.md)).

## Reach it from another machine

On a server over SSH, forward that one port rather than exposing anything:

```bash
ssh -L 8000:localhost:8000 user@your-server
```

Then open <http://localhost:8000/console> in your own browser. **The local port
must match the remote one.** The session cookie is scoped to the origin the
login happened on, so `localhost:8000` on both ends is what keeps it.

## Develop without Docker

```bash
uv sync
uv run pytest -q                  # ~950 tests, SQLite, no services, no network
uv run ruff check .
uv run mypy apps/gateway/src services   # mypy is --strict
```

Tests run against SQLite and a fake upstream transport, so they need no
PostgreSQL, no Valkey and no network. `services/redaction` is not a workspace
member on purpose — spaCy must never enter the gateway's lockfile — but its
tests still run from the repo root.

For an end-to-end check over a real socket — real server, real database, real
streaming upstream, no Docker:

```bash
./scripts/smoke_test.sh
```

It starts everything on temporary ports, drives the endpoints, prints the
resulting ledger and cleans up. It also demonstrates the quota overrun policy:
the request that crosses the limit is admitted, the next one gets a 429.

Against a real database, without Docker:

```bash
export GATEWAY_DATABASE_URL=postgresql+asyncpg://gateway:gateway@localhost:5432/gateway
uv run alembic -c apps/gateway/alembic.ini upgrade head
uv run gateway seed
uv run gateway serve --reload
```

## Where to next

- [Accounting and quotas](accounting-and-quotas.md) — what the ledger now
  contains, and how a cost is computed.
- [Operations](operations.md) — the live checks, and what they can only find
  against a running stack.
- [Deployment](deployment.md) — overlays, TLS, and the loopback-only rule.
