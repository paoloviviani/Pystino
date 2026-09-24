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
  -e PYSTINO_DEPLOY_DIR="$PWD" ghcr.io/paoloviviani/pystino-gateway:<version> pystino init \
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
| `--agents` | the coding-agent panel: machines dial `wss://<origin>/chat/api/v2/code/machine` with client `opencode-enrollment` — installing galopin, the Cerea machine agent, on them is documented in the chat repository (`docs/agent-machines.md`) |

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

## Distributed mode without the registry

Until the image workflows publish, a distributed install can run on images
built on the host itself. This is how the reference deployment runs today.

**Build from clean clones, never from a worktree.** Cerea's image build runs
`git` inside the build, and a worktree's `.git` file points outside the build
context (the build fails with exit 128). A clean clone also proves that
everything the images need is committed: `.gitignore`'s broad `*.env` rule
once swallowed `deploy/stack/release.env`, and every build from the working
copy that wrote it passed while every clean clone lacked it. The file is
re-included explicitly now; a new `.env`-suffixed file under `deploy/stack/`
needs the same.

```bash
git clone --branch main https://github.com/paoloviviani/Pystino.git pystino
git clone --branch main https://github.com/paoloviviani/Cerea.git cerea
P=$(git -C pystino rev-parse --short HEAD); C=$(git -C cerea rev-parse --short HEAD)
cd pystino
docker build --build-arg INCLUDE_CONSOLE=true --build-arg CONSOLE_BUILD_SHA=$P \
  -t local/pystino-gateway:g$P -f apps/gateway/Dockerfile .
docker build -t local/pystino-proxy:g$P deploy/stack/proxy
docker build -t local/pystino-authelia:g$P deploy/stack/authelia
docker build --build-arg SPACY_MODELS= \
  -t local/pystino-redaction:g$P-pattern -f services/redaction/Dockerfile .   # redaction profile only
cd ../cerea
docker build --build-arg APP_BASE=/chat --build-arg PUBLIC_COMMIT_SHA=$C -t local/cerea:g$C .
cd .. && rm -rf pystino cerea
```

The images must be named `<registry>/pystino-<name>:<version>` (and
`-pattern` for redaction), because that is how `compose.yaml` spells them.
`APP_BASE=/chat` is compile-time in SvelteKit, so an image built without it
cannot be served under `/chat`.

Then run `init` from the local gateway image, and pin the local tags with
`set`. The first `set` also goes through `docker run`, because the `./pystino`
helper runs the image `.env` names, and that is GHCR until the pins change:

```bash
cd /srv/pystino
pst() { docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/deploy" -w /deploy \
  -e PYSTINO_DEPLOY_DIR="$PWD" local/pystino-gateway:g$P pystino "$@"; }
pst init --mode dist --origin https://llm.example.org --admin-email you@example.org \
  --preset team --agents > init.out; chmod 600 init.out     # the first password is in it
pst set PYSTINO_REGISTRY=local PYSTINO_VERSION=g$P CEREA_REGISTRY=local CEREA_VERSION=g$C
./pystino doctor && docker compose up -d --wait
```

From here on, `./pystino` runs the local image. Never run `docker compose
pull`, because nothing is published under `local/`. To move to newer local
images, build them with new tags, then take the new release's `compose.yaml`
from its gateway image, since `set` changes only `.env` and a release that adds
a variable would otherwise never pass it to the container. Then set the new
tags and bring the stack up:

```bash
cp compose.yaml compose.yaml.bak-$(date +%Y%m%d)
docker run --rm local/pystino-gateway:g$P cat /app/deploy/stack/compose.yaml > compose.yaml
./pystino set PYSTINO_VERSION=g$P CEREA_VERSION=g$C
./pystino doctor && docker compose up -d --wait
```

`pystino upgrade` does not fit this case: it sets every pin from the release
manifest and refuses an image that is not the release it names.

## TLS ending in front: an edge

When a reverse proxy in front already terminates TLS (for example a NetBird
edge that owns the public name and its certificate), the stack serves plain
HTTP to it:

```bash
… pystino init --tls upstream --http-port 8443 --origin https://llm.example.org …
```

`--tls upstream` makes the proxy listen for any host on plain HTTP, and
`--http-port` is the host port it is published on. The origin stays the public
`https://` one, since the apps build every URL from it. `--https-port` is
still published but nothing answers behind it. Point the edge at
`http://<this host>:8443`, and have it:

- pass the original `Host` (the proxy itself sets `X-Forwarded-Proto: https`
  on the way to the apps);
- **forward WebSocket upgrades**, at least for `/chat/api/v2/code/machine`,
  where every agent machine holds its link. Without them the machine logs a
  handshake failure (`expected … 101 but got 502`) and retries forever;
- not buffer responses, because chat and completions stream.

The proxy trusts `X-Forwarded-For` only from `TRUSTED_PROXIES` (default
`private_ranges`). An edge that reaches the host over NetBird comes from
`100.64.0.0/10`, which is outside those ranges, so logs show the edge's
address, not the client's. Set `TRUSTED_PROXIES` in `.env` to the edge's
range to get the client's.

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

**Signing out** ends the session in both applications and at the identity
provider, so the next sign-in asks for a password. The bundled Authelia
publishes no `end_session_endpoint`, so each application sends the browser to
Authelia's own `/logout?rd=<page>`, which ends its session and comes back
(`rd` is honoured because the page is on the session cookie's domain). The
console uses the provider's **Logout URL** (Settings → Identity providers),
which is empty by default. It then falls back to the provider's
`end_session_endpoint`, and for `kind=authelia` to `<issuer>/logout?rd={redirect}`.
The chat uses `OIDC_LOGOUT_URL` in `.env`, which `init` writes for the bundled
Authelia; `doctor` warns when it is missing. `{redirect}` is replaced by the
page to come back to.

The console and the chat share one origin but keep separate sessions, so each
one's sign-out also expires the other's session cookie
(`GATEWAY_LOGOUT_ALSO_CLEAR_COOKIES` and `LOGOUT_ALSO_CLEAR_COOKIES` in
`compose.yaml`). Signing out of either leaves neither signed in; there is no
state where the console is signed out while the chat is not, or the reverse.
With the chat on another origin (satellite), only the identity provider's
session is shared, so signing out of one leaves the other's own session until
it expires.

**The bundled Authelia's provider** is `kind=authelia` (from `OIDC_KIND`): its
users file is on a volume the gateway mounts, so the users-file sync and the
console's user management (People) are available. The sync starts unconfirmed:
the first run is a dry run, and nothing is applied until an administrator
confirms it. Installs made before this (`kind=generic`, no adapter) are
corrected by migration 0046, which touches only a row still at those defaults
that points at the bundled Authelia's internal URL.

Break-glass, when nobody can administer:
`docker compose exec gateway pystino admin grant you@example.org`.

## Upgrading

```bash
./pystino upgrade 1.5.0          # runs the 1.5.0 image: new compose.yaml, new pins
docker compose pull && docker compose up -d --wait
```

`upgrade` backs up `compose.yaml` and `.env` as `*.bak-<old version>`, keeps
every secret, re-creates the `./pystino` helper at the new version, and never
starts anything itself; `migrate` and `bootstrap` run on the `up`. Snapshot the
volumes first — down-migrations are not promised.

**Does what runs match `.env`?** `./pystino doctor --against-running` adds, to
the usual checks, a read-only comparison with the project's containers: an
image other than the one `.env` names (or a tag rebuilt since the container
started, with each image's source revision), environment keys whose values
differ — named, never printed — named volumes not mounted, and services
stopped, unhealthy or failed. The helper runs `docker inspect` on the host and
pipes the result in, because the gateway image has no Docker access and is not
given the socket. A helper written before this existed lacks that step, and
`upgrade` replaces it. Until then, run it from a checkout on the host, which
inspects directly: `uv run pystino doctor --dir <deploy dir> --against-running`.

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
