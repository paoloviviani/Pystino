<p align="center">
  <img src="logo.png" alt="The Pystino logo" width="256" />
</p>

# Pystino

A self-hosted **OpenAI-compatible LLM gateway** with per-user and per-group
accounting and quotas, PII redaction and OIDC sign-in, plus an admin console.
Point any OpenAI (or Anthropic) client at it; every request is admitted
against quotas, redacted, metered and written to a ledger you can report on.

> The name is *pistino*: Turin dialect for a nitpicker, the person who checks
> every last detail. Which is what an accounting gateway is for.

## Features

- **One API for many providers.** OpenAI-compatible surfaces in front of any
  number of upstreams (OpenAI, Anthropic, Mistral, OpenRouter, Cortecs and
  any OpenAI-compatible endpoint), with per-group model access.
- **Accounting you can bill from.** Every request lands in a PostgreSQL
  ledger: caller, billing group, tokens, and cost computed from your prices
  and recorded beside the provider's own figure. Usage is never silently
  zero: if the upstream reports none, tokens are counted locally and marked
  estimated.
- **Quotas that hold under concurrency**, per user and per group, with a
  reservation between the check and the upstream call.
- **PII redaction** before anything leaves: detected entities become
  deterministic placeholders and are restored in the answer, with per-group
  and per-model policy.
- **OIDC-only sign-in**, against the bundled Authelia or any issuer, with
  groups from claims, directory sync or SCIM. Programs use revocable API keys
  or OIDC access tokens.
- **An admin console** for spend, reports, quotas, providers, models and
  prices, users, groups and redaction rules.

## Quick start

`deploy/` is a self-contained Pystino deployment: the gateway, its console
and, optionally, the bundled Authelia and redaction. No chat.

```sh
cp deploy/.env.example deploy/.env && chmod 600 deploy/.env
$EDITOR deploy/.env          # every variable is explained; every secret names its command
cd deploy && docker compose up -d --wait
```

The first sign-in with `PYSTINO_BOOTSTRAP_ADMIN_EMAIL` becomes the
administrator. Upgrading is `git pull && docker compose pull && docker
compose up -d --wait`. For the full stack with the Cerea chat, use the
**cerea-deploy** repository instead; it uses the same variable names, so a
`deploy/.env` carries over. Details: [docs/deployment.md](docs/deployment.md).

## Configuration

Settings come from the environment (`deploy/.env`). The groups that matter:

| | |
|---|---|
| Origin and TLS | `PUBLIC_ORIGIN`, `TLS_MODE` (`acme`, `internal`, or `upstream` when TLS ends in front) |
| Identity | `OIDC_ISSUER`, `OIDC_CONSOLE_CLIENT_*`, `PYSTINO_BOOTSTRAP_ADMIN_EMAIL`; the `authelia` profile for the bundled IdP |
| Secrets | `GATEWAY_SECRET_KEY` (encrypts provider keys at rest), `GATEWAY_SESSION_SECRET`, `POSTGRES_PASSWORD` |
| Features | `GATEWAY_ACCOUNTING__ENABLED`, `GATEWAY_QUOTA__ENABLED`, `GATEWAY_REDACTION__ENGINE` and the `redaction` profile |
| First provider | `GATEWAY_UPSTREAM__BASE_URL`, `GATEWAY_UPSTREAM__API_KEY`; more are added in the console |

Every gateway setting is in `apps/gateway/src/gateway/config.py`
(`GATEWAY_*`, nested with `__`).

## The console

Served at `/console` and signed in through OIDC. Administrators manage
providers and their credentials, models with prices and access, users and
groups, quotas, redaction rules and identity providers, and read spend and
usage reports. Everyone else sees their own usage, limits and API keys. The
management API behind it is under `/api`, with a generated reference at
`/docs`.

## The API

| Surface | Path |
|---|---|
| Chat completions (streaming and not) | `POST /v1/chat/completions` |
| Responses | `POST /v1/responses` |
| Anthropic Messages | `POST /v1/messages` |
| Embeddings | `POST /v1/embeddings` |
| Image generation | `POST /v1/images/generations` |
| Document extraction (OCR) | `POST /v1/ocr` |
| Web search, priced per search | `POST /v1/search` |
| Models, the caller's view | `GET /v1/models` |
| Who am I, my billing groups, my usage | `GET /v1/me`, `GET /v1/billing/groups`, `GET /v1/pystino/usage` |

Authenticate with an API key (`Authorization: Bearer gwk_…`) or an OIDC access
token carrying the configured audience. A caller in several groups picks the
one to bill with `x-bill-to`.

## Development

```sh
uv sync
uv run pytest -q                                  # SQLite and a fake upstream: no services needed
uv run ruff check . && uv run mypy apps/gateway/src packages/shared-py/src
./scripts/smoke_test.sh                           # end to end over real HTTP, on temporary ports
```

`deploy/dev/smoke.yml` adds a fake upstream to the compose deployment, and
`deploy/dev/keycloak/` a Keycloak to develop the external-IdP paths against.
The console is `apps/console` (React, pnpm), built into the gateway image.
Live checks against a running stack are in `scripts/*_live.py`.

## Documentation

The documentation site is `docs/` (`uv run mkdocs build --strict`). Start at
[docs/index.md](docs/index.md):
[getting started](docs/getting-started.md) ·
[gateway](docs/gateway.md) ·
[accounting and quotas](docs/accounting-and-quotas.md) ·
[redaction](docs/redaction.md) ·
[deployment](docs/deployment.md) ·
[identity](docs/oidc-generic-provider.md) ·
[coding agents](docs/coding-agents.md) ·
[operations](docs/operations.md). The gateway package has its own
[README](apps/gateway/README.md).

## Contributing, security, licence

See [CONTRIBUTING.md](CONTRIBUTING.md) and, for vulnerabilities,
[SECURITY.md](SECURITY.md). Pystino is licensed under the
[EUPL-1.2](LICENCE).
