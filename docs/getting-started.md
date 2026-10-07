# Getting started

Two ways in: the Docker compose stack (the whole platform, PostgreSQL included)
or a bare `uv` environment (the Python services only, tests included). The
compose route is the one that gets you a billed completion and a console.

## Run the stack

`deploy/compose.yaml` from this checkout, with a fake upstream so no provider
account is needed (the full set of options is in [Deployment](deployment.md)):

```bash
cp deploy/.env.example deploy/.env && chmod 600 deploy/.env
# fill in deploy/.env: TLS_MODE=internal, PUBLIC_ORIGIN=https://dev.example.test:8443,
# SITE_ADDRESS=https://dev.example.test, TLS_DIRECTIVE='tls internal', HTTPS_PORT=8443,
# then mint every secret and, for the bundled Authelia, its digests — the comment above
# each variable in deploy/.env.example names the exact command.
export PYSTINO_SRC="$PWD"    # the fake upstream (deploy/dev/smoke.yml) needs it
cd deploy && docker compose -f compose.yaml -f dev/smoke.yml up -d --wait
```

`dev.example.test` must resolve to this machine for a browser (a hosts entry
is enough). The gateway itself also listens on `127.0.0.1:8000`, loopback
only.

## Create something to talk to

```bash
docker compose exec gateway gateway seed --model my-model --upstream-model gpt-4o-mini
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
upstream either way.

## Sign in to the console

The console is at `https://dev.example.test:8443/console`. Sign in through the
bundled Authelia with `AUTHELIA_ADMIN_USER` and the password behind
`AUTHELIA_ADMIN_PASSWORD_DIGEST`; because its email
(`AUTHELIA_ADMIN_EMAIL`/`PYSTINO_BOOTSTRAP_ADMIN_EMAIL`) matches, that first
sign-in makes it the administrator. The gateway itself has no password form: even this first sign-in goes through the bundled Authelia's page, and the provider is set in `deploy/.env`, not in the console; see [Identity](identity.md).

## Use it from opencode

[opencode](https://opencode.ai) is a coding agent that runs in your terminal.
To point it at your gateway, mint an API key in the console (**API keys** on the
Overview page, which also shows this command with your address filled in), then
run this on the machine where you use opencode, replacing `llm.example.org` with
your gateway's address:

```bash
curl -fsSL https://llm.example.org/opencode/install.sh | bash
```

It asks for the key, adds the gateway as a provider in opencode's global config,
keeps the rest of that file, and backs it up first. Then start opencode and pick
a model with `/models`. See [Coding agents](coding-agents.md) for the
options, how to keep the key out of the file, and how to undo it.

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
uv run pytest -q                  # about 1,900 tests, SQLite, no services, no network
uv run ruff check .
uv run mypy apps/gateway/src packages/shared-py/src services   # mypy is --strict
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
- [Deployment](deployment.md) — TLS, identity, upgrades and backups.
