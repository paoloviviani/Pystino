# Deployment

## The compose overlay model

Everything ships as docker compose overlays on one base file
(`deploy/compose/docker-compose.yml`), each overlay adding one deliberate
capability:

| Overlay | Adds |
|---|---|
| *(base)* | PostgreSQL, Valkey, migrations, the gateway — published on **127.0.0.1 only** |
| `docker-compose.override.yml` | development conveniences (mounted sources) |
| `docker-compose.smoke.yml` | a fake OpenAI-compatible upstream — exercise the whole topology with no provider account |
| `docker-compose.redaction.yml` | the Presidio detection service |
| `docker-compose.chat.yml` | *(on the `chat` branch)* chat-api and the chat SPA at `/chat` |
| `docker-compose.playwright.yml` | a headless browser for web search phases 2 and 3 — **deployed ahead of its consumer**, nothing reads from it yet, and it publishes no port because `run-server` is unauthenticated remote code execution ([browser.md](browser.md)) |
| `docker-compose.proxy.yml` | Caddy terminating TLS — the only route to a routable address (ADR 0035) |

```bash
# the common development shape:
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.redaction.yml up -d --build
```

The overlays are separate files rather than profiles on purpose: a fake
upstream in the base file would be one careless `-f` away from production.

## Configuration

`deploy/.env.example` is the annotated source of truth — copy it to
`deploy/.env` (gitignored) and edit. The variables that carry weight:

| Variable | Why it exists |
|---|---|
| `POSTGRES_PASSWORD` | the ledger of record |
| `GATEWAY_SESSION_SECRET` | signs management session cookies |
| `GATEWAY_SECRET_KEY` | encrypts provider API keys at rest. Comma-separated to rotate (first encrypts, any decrypt). Back it up with the database — losing every value means re-entering each provider credential by hand |
| `GATEWAY_SESSION_COOKIE_SECURE` | set `true` behind TLS; left false for local http, the cookie would be dropped |
| `GATEWAY_BILLING_CURRENCY` | all prices and quotas share it; a model priced otherwise is **refused rather than converted** — a silent exchange rate produces invoices that look right and are wrong |
| `GATEWAY_UPSTREAM_BASE_URL` / `GATEWAY_UPSTREAM_API_KEY` | the default upstream provider |
| `GATEWAY_REDACTION_ENGINE`, `GATEWAY_REDACTION_PLACEHOLDER_KEY` | see [Redaction](redaction.md) — the key must be backed up with the transcripts it labelled |
| `GATEWAY_OIDC__*` | any OIDC provider. Discovery is read **once at startup** — changing any value needs a gateway restart. `iss` is part of user identity, so changing the issuer re-provisions every user as a new row with no memberships. See [OIDC against any provider](oidc-generic-provider.md) |
| `GATEWAY_LOCAL_AUTH__ENABLED` | local email + password sign-in beside OIDC (ADR 0043); passwords are set out-of-band with `gateway passwd` and never through the environment |
| `PUBLIC_HOST`, `HTTPS_PORT`, `TLS_DIRECTIVE`, `PUBLIC_ORIGIN`, `ACME_EMAIL`, `PUBLIC_BIND` | only with the proxy overlay (below) |
| `CHAT_PORT`, chat OIDC settings | only with the chat overlay; the chat's OIDC client is distinct from the gateway's — two services that authenticate people are two clients, each revocable on its own |

## Reaching the stack from another machine

The default shape publishes nothing but loopback. From your laptop, forward
one port:

```bash
ssh -L 8000:localhost:8000 user@your-server
```

Then open <http://localhost:8000/console>. **The local port must match the
remote one**: the session cookie is scoped to the origin the login happened
on, so `localhost:8000` on both ends is what keeps it. `localhost` on the far
end still serves `/v1` and the API to the live scripts either way.

## On a public address, behind TLS

`docker-compose.proxy.yml` puts Caddy in front of everything and terminates
TLS. Which of two configurations you are in is decided by whether you have a
name, and `deploy/.env` says so rather than the file guessing:

| | `PUBLIC_HOST` | `TLS_DIRECTIVE` | certificate |
|---|---|---|---|
| dev | an IP address | `tls internal` | Caddy's own CA — a browser warns once |
| prod | an FQDN | *(empty)* | Let's Encrypt, obtained and renewed automatically |

Let's Encrypt will not issue for an IP address, so the first is the only
option without a name. Automatic HTTPS additionally needs the name to resolve
to this host and **ports 80 and 443 reachable from the internet**, because
that is where the challenge arrives.

```bash
# deploy/.env
PUBLIC_HOST=llm.example.org
HTTPS_PORT=443
PUBLIC_ORIGIN=https://llm.example.org
TLS_DIRECTIVE=
ACME_EMAIL=platform@example.org

docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.redaction.yml \
  -f deploy/compose/docker-compose.proxy.yml up -d --build
```

One origin, one port, everything behind TLS — the console, the chat and `/v1`
alike.

**Behind the proxy, local sign-in is on by default.** The only management
credential this shape seeds is the local admin's (ADR 0043),
and its password comes from `deploy/.env`, which is gitignored and never
committed. Create the account the usual way
(`docker compose ... exec gateway gateway passwd admin@local`).

### The three traps behind a proxy

All three were found building it, and all are recorded in
ADR 0035:

1. **SNI may not carry an IP address**, so an address-only deployment offers no
   certificate at all until Caddy's `default_sni` names one — every handshake
   fails with a TLS "internal error" and nothing above debug in the log.
2. **uvicorn trusts forwarded headers from `127.0.0.1` only**, and the proxy
   arrives from the compose network — without `FORWARDED_ALLOW_IPS` the app
   believes every request is http. The only place that shows is the
   post-logout URL built from `request.base_url`, which an https-registered
   OIDC provider then refuses with a 400 *after a login that worked*.
3. **The session cookie is scoped to the origin the login happened on** — sign
   in on the address you will keep using. The live scripts follow
   `PUBLIC_HOST` when it is set, so source `deploy/.env` before running them.

### Verify what is exposed

```bash
# Caddy's CA, so the checks can verify properly rather than skip verification
docker compose --env-file deploy/.env -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.proxy.yml exec proxy \
  cat /data/caddy/pki/authorities/local/root.crt > deploy/tls/caddy-root.crt

set -a; . deploy/.env; set +a     # the live scripts follow PUBLIC_HOST
./scripts/test_public_tls_live.py
```

It asserts the certificate verifies, that a wrong password is refused over
TLS, that the session cookie is `Secure`, that a completion still streams
through the proxy, and that PostgreSQL, Valkey, the fake upstream and the
plaintext application ports are reachable on loopback and **refused on this
host's routable address**.

!!! warning "This is still not a production deployment"

    Read ADR 0035 before calling it one.
    The self-signed configuration still trains people to click through a
    warning, and a session holder can still spend real provider credentials.
