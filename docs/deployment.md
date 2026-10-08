# Deployment

`deploy/` in this repository is a self-contained deployment of Pystino: the
gateway and its console, alone, with no chat. Everything is readable files you
edit and `git pull`: a compose file, a documented `.env.example`, and the
Caddy and Authelia configuration mounted into their stock images.

**Want the chat too?** Use the Cerea deploy kit (`kit/` in the [Cerea repository](https://github.com/paoloviviani/Cerea)) instead. It is the same variable names and `.env` conventions, so
moving to it later is: copy this `.env` and the compose project name over,
`./configure` again to add the chat's secrets, then `docker compose up -d`.

## Serving chats you don't deploy here

A central Pystino can serve chat deployments that live elsewhere: each one
(the `satellite` preset in the deploy kit) runs only the chat, pointed here
with `OPENAI_BASE_URL` at this gateway's public `/v1`, signing people in
against this gateway's identity provider, and reading this gateway's
ledger with each person's own token. Quotas, accounting and model access
stay central, administered here.

Two limits are structural, and worth knowing before you promise anything:

- **One chat client.** `GATEWAY_OIDC__CHAT_CLIENT_ID` is a single value, and
  `/v1/session/announce` refuses tokens issued to any other client. Several
  satellite chats must share that one client id, with each chat's redirect
  URI registered on it. `GATEWAY_OIDC__ACCEPTED_CLIENTS` names the clients
  that may sign in at all — add each satellite's identity-provider client
  if it differs from the bundled one.
- **Erasure reaches one chat.** `GATEWAY_CHAT__ERASURE_URL` is a single
  URL: deleting a person here erases their data on _that_ chat, not on any
  other satellite deployment. A person's data on a second chat must be
  erased there, by its operator.

## What runs

`deploy/compose.yaml` builds nothing: every service names an image, and
add-ons are compose **profiles** chosen by `COMPOSE_PROFILES` in `.env`.

| Service                        | Profile     | What it is                                                                                                                    |
| ------------------------------ | ----------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `postgres`                     | always      | the gateway's database (with pgvector)                                                                                        |
| `bootstrap`                    | always      | one-shot on every `up`: the bundled Authelia's signing key and first user — created only if absent, never overwritten         |
| `valkey`, `migrate`, `gateway` | always      | the gateway and its console; there is no `gateway` profile here, because the gateway is the only thing this deployment serves |
| `proxy`                        | always      | stock Caddy, reading `deploy/caddy/` (mounted read-only)                                                                      |
| `authelia`                     | `authelia`  | the bundled identity provider at `/authelia`, stock image, reading `deploy/authelia/`                                         |
| `redaction`, `extractor`       | `redaction` | Presidio, pattern-only in the published image                                                                                 |

## Setting up

```bash
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
$EDITOR deploy/.env      # every variable is explained where it stands
cd deploy && docker compose up -d --wait
```

There is no `./configure` here (unlike the full stack) — filling in `.env` by
hand is the point of a self-contained, gateway-only deployment: nothing to
read but a compose file and a `.env.example`. Every secret names the command
that mints it (`openssl rand -hex 32`, or, for the bundled Authelia's
digests, `docker run --rm authelia/authelia:4.39.22 authelia crypto hash
generate …`).

The first person to sign in with the email in `PYSTINO_BOOTSTRAP_ADMIN_EMAIL`
becomes an administrator. That bootstrap is honoured once per deployment:
once any administrator exists it never fires again, so deactivating every
administrator does not re-arm it. With the bundled Authelia, the
first account's password is whatever `AUTHELIA_ADMIN_PASSWORD_DIGEST` is a
digest of — you chose it when you minted the digest, so there is nothing to
read back afterwards.

**Images.** The gateway and redaction images are public on the GitHub
Container Registry (`ghcr.io/paoloviviani/pystino-gateway`,
`pystino-redaction`), at the version `deploy/compose.yaml` pins; `docker compose
pull` needs no login.

## TLS modes

Three, chosen by `TLS_MODE` (`deploy/.env.example` has the exact
`SITE_ADDRESS`/`TLS_DIRECTIVE` pair each needs):

- **`acme`** — a public DNS name, ports 80 and 443 reachable: Caddy gets a
  Let's Encrypt certificate.
- **`internal`** — Caddy's own CA; browsers warn. Development and private
  networks.
- **`upstream`** — TLS ends in front of this host (a load balancer, an edge
  proxy such as a NetBird gateway) and reaches Caddy as plain HTTP on
  `HTTP_PORT`. Point the edge at `http://<this host>:<HTTP_PORT>`, and have
  it: pass the original `Host`; **forward WebSocket upgrades**; and not
  buffer responses, because streamed completions need that. The proxy trusts
  `X-Forwarded-For` only from `TRUSTED_PROXIES` (default `private_ranges`) —
  widen it to the edge's own range to see real client addresses in logs.

## Identity

People sign in through an OpenID Connect provider — the bundled Authelia or
your own (`OIDC_ISSUER`, `OIDC_INTERNAL_BASE_URL`, `OIDC_CONSOLE_CLIENT_ID`/
`_SECRET`). There is no password door. One provider at a time, named by the
`OIDC_*` variables in `deploy/.env`; the gateway re-reads them at every
start and the console shows the provider read-only. To combine several
sources of users, federate them in your own IdP (Keycloak, Authentik and the
like) and point the stack at it. Who is an administrator, how groups sync,
and linking accounts by email are all environment settings too — see
[Identity](identity.md) for every variable and what the gateway
checks at startup.

Servers never call the public origin: the gateway reaches the IdP at its
internal URL (`OIDC_INTERNAL_BASE_URL`, `http://authelia:9091/authelia` for
the bundled one), with forwarded headers naming the public issuer. There is no
CA bundle to maintain.

**The bundled Authelia's accounts** are managed from the console's Users
page; see [Bundled accounts](bundled-accounts.md). Groups and roles live in
the console.

**Break-glass**, when nobody can administer or the IdP is gone, is one
command; its preconditions, what it changes and what to do if the deployment
itself may be compromised are in [Identity: break-glass](identity.md#break-glass).

## Upgrading

```bash
git pull
docker compose pull
docker compose up -d --wait
```

A release that changes `deploy/caddy/` or `deploy/authelia/` also changes
their service's `pystino.config-rev` label in `deploy/compose.yaml`
(`deploy/pin.py` keeps that label in step; CI runs `deploy/pin.py --check`),
so `up -d` recreates the proxy or Authelia exactly when their mounted files
changed — compose otherwise never notices a mounted file changing. Snapshot
the volumes first: down-migrations are not promised.

Editing `deploy/caddy/` or `deploy/authelia/` yourself, without a release?
`docker compose up -d --force-recreate proxy authelia`, or run
`deploy/pin.py` and commit the result.

## Adding a component

A Caddy snippet for `deploy/proxy.d/` (git-ignored, so `git pull` never
conflicts with yours) — a mounted **directory**, so adding a file never meets
the stale-inode trap of a single-file mount. Reload with
`docker compose exec proxy caddy reload --config /etc/caddy/Caddyfile`.

## Backups

`deploy/.env` (every secret: `GATEWAY_SECRET_KEY` decrypts provider keys,
`AUTHELIA_STORAGE_KEY` every user's subject — neither can be regenerated
without losing data), and the volumes `postgres-data`, `authelia-config` and
`authelia-data`.
