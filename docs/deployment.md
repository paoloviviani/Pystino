# Deployment

One topology, one configuration file, two ways to get the images. The design
and its reasons are ADRs 0086 (the stack), 0087 (two repositories, Pystino
pins Cerea) and 0088 (OIDC-only identity).

## What runs

`deploy/stack/compose.yaml` is the whole deployment. It builds nothing: every
service names an image, and add-ons are compose **profiles** chosen by
`COMPOSE_PROFILES` in `.env`.

| Service | Profile | What it is |
|---|---|---|
| `postgres` | always | the gateway's database and the chat's (knowledge store) |
| `bootstrap` | always | one-shot on every `up`: the chat database role and `vector` extension, the bundled Authelia's key and first user — created only if absent, never overwritten |
| `proxy` | always | Caddy with the one Caddyfile baked in (`pystino-proxy`) |
| `valkey`, `migrate`, `gateway` | `gateway` | the gateway and its console; off only for a Cerea-only install |
| `chat`, `chat-mongo` | `chat` | Cerea at `/chat` |
| `authelia` | `authelia` | the bundled identity provider at `/authelia` (`pystino-authelia`) |
| `redaction`, `extractor` | `redaction` | Presidio, pattern-only in published images |
| `playwright` | `fetch` | the headless browser for the chat's web fetch |

## Setting up: `pystino init`

```bash
mkdir /srv/pystino && cd /srv/pystino
docker run --rm -it -u "$(id -u):$(id -g)" -v "$PWD:/deploy" -w /deploy \
  ghcr.io/paoloviviani/pystino-gateway:<version> pystino init \
  --origin https://llm.example.org --admin-email you@example.org --preset team
./pystino doctor
docker compose up -d --wait
```

`init` writes `.env` (mode 0600, every secret minted — nothing is asked that
can be generated), `compose.yaml`, an empty `proxy.d/` and the `./pystino`
helper. It refuses what cannot work: `--tls acme` on an IP, the bundled
Authelia on a dotless host (browsers refuse its cookie), an `http://` origin.

| Flag | Choices |
|---|---|
| `--preset` | `homelab` (no ledger), `team` (ledger, quotas, pattern redaction), `enterprise` (+ NER redaction built locally, headless fetch), `satellite` / `generic` (the chat alone — also `cerea init`) |
| `--tls` | `acme` (a public name, Let's Encrypt), `internal` (Caddy's own CA; development), `upstream` (TLS ends in front — the NetBird edge) |
| `--idp` | `authelia` (bundled) or `external` with `--oidc-issuer` and the client secrets in `PYSTINO_OIDC_*_CLIENT_SECRET` |
| `--agents` | the coding-agent panel: machines dial `wss://<origin>/chat/api/v2/code/machine` with client `opencode-enrollment` |

The first sign-in whose verified email is `--admin-email` becomes the
administrator, once, on a deployment that has none. With the bundled Authelia,
`init` prints that person's password once.

**Private images.** While the repositories are private, so are their GHCR
packages: log the host in once with a token holding `read:packages` —
`echo "$TOKEN" | docker login ghcr.io -u <github-user> --password-stdin`.
`init`, `doctor` and the `./pystino` helper say so when the login is missing.

## Development mode

The same `compose.yaml`, plus `compose.build.yaml`, which only adds `build:`
stanzas pointing at your checkouts:

```bash
uv run pystino init --dir ~/pystino-dev --mode dev --tls internal \
  --origin https://dev.example.test:8443 --admin-email you@example.org \
  [--cerea-src ../Cerea]
cd ~/pystino-dev && docker compose up -d --build --wait
```

Images are tagged `local/…:dev` and stamped with the checkout's revision
(`-dirty` when the tree was). Without `--cerea-src` the chat is pulled at the
version the release pins. Development fixtures, never shipped:
`deploy/dev/smoke.yml` (a fake upstream) and `deploy/dev/keycloak/` (an
external IdP to develop against, attached through the component hook).

## Identity

People sign in through an OpenID Connect provider — the bundled Authelia or
your own. There is no password door. Per provider, the console decides where
groups come from (the token's claim, the directory, or the console only), how
often the provider's answer applies, and whether admin comes from the console
or from a claim. Directory sync pulls from Authelia's users file or Keycloak's
admin API, or accepts SCIM 2.0 pushes at `/scim/v2/<provider>` (Entra ID,
Okta, Authentik). The bundled Authelia's people are managed in the console
(Settings → Identity providers → People).

Servers never call the public origin: the gateway and the chat reach the IdP
at its internal URL (`OIDC_INTERNAL_BASE_URL`, `http://authelia:9091/authelia`
for the bundled one), with forwarded headers naming the public issuer. There
is no CA bundle to maintain.

Break-glass, when nobody can administer:
`docker compose exec gateway pystino admin grant you@example.org`.

## Upgrading

```bash
./pystino upgrade 1.5.0          # runs the 1.5.0 image: new compose.yaml, new pins
docker compose pull && docker compose up -d --wait
```

`upgrade` backs up `compose.yaml` and `.env` as `*.bak-<old version>`, keeps
every secret, and never starts anything itself; `migrate` and `bootstrap` run
on the `up`. Snapshot the volumes first — down-migrations are not promised.

## Adding a component

A component ships its own compose file (append it to `COMPOSE_FILE`) and, if
it needs a route, a Caddy snippet for `proxy.d/` — a mounted **directory**, so
adding a file never meets the stale-inode trap of a single-file mount. Reload
with `docker compose exec proxy caddy reload --config /etc/caddy/Caddyfile`.

## Backups

`.env` (every secret: `GATEWAY_SECRET_KEY` decrypts provider keys,
`AUTHELIA_STORAGE_KEY` every user's subject), and the volumes `postgres-data`,
`chat-mongo-data`, `authelia-config` and `authelia-data`. `pystino doctor`
prints the list.

## Moving an install made by the old installer

```bash
./pystino adopt /path/to/old/Pystino/deploy --tls upstream   # the edge shape
```

`adopt` reads the old `deploy/.env` and generated Authelia files — it never
writes there — and carries every secret over verbatim, under the old compose
project name so the same volumes are mounted. It prints the cutover: stop the
old stack **without `-v`**, import the Authelia users file and key with
`docker compose run --rm --no-deps … bootstrap pystino bootstrap --import-only`,
then `docker compose up -d --wait`. The way back is `docker compose down` and
starting the old stack as before.

When the new Cerea carries the thin-agent machine link, its `codeDevices`
index `userId_1_updatedAt_-1` changes from partial to non-partial under the
same name; drop the old one before the new chat starts (`adopt` prints the
command).
