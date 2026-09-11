# The headless browser

`deploy/compose/docker-compose.playwright.yml` adds one service: a Playwright
`run-server` with Chromium, Firefox and WebKit behind it, speaking Playwright's
own WebSocket protocol on port 3000 of the compose network.

**Nothing in this repository connects to it.** That is not an oversight, and it
is the first thing to know about it.

## What it is for

Web search, phases 2 and 3 of [web-search-plan.md](web-search-plan.md). A search
backend — Exa, Jina, Staan, Linkup — answers with URLs and short snippets. A
snippet is not an answer, so something has to fetch the page and turn it into
text the model can read, and a growing share of the web is an empty `<div>`
until JavaScript has run. An HTTP client returns that empty div and no error.

The reason it is deployed here rather than bought is the reason this deployment
exists at all. The search query is already a second egress — prompt-derived text
sent to a third party, which is why
[redaction-scoping-plan.md](redaction-scoping-plan.md) has to cover a tool
call's arguments. Handing the *rendering* to a hosted scraping API would add a
third party that sees every page this deployment reads on a user's behalf, and
the pages are often more revealing than the query. Rendering in-process would be
worse still: a browser in the gateway's container is a browser sharing an
address space with the ledger.

So: deployed ahead of its consumer, so that the consumer is a client of
something that already exists, and so that the isolation argument below is
settled before anything depends on the answer.

## Why it is not published, and never can be

`run-server` is **remote code execution as a feature, with no authentication of
any kind**. Playwright offers no token, no password and no TLS on this endpoint.
Whoever can open a WebSocket to it can navigate anywhere, execute arbitrary
JavaScript in a real browser, read this container's filesystem through `file://`
and reach every other service on the compose network from *inside* the trust
boundary — PostgreSQL, Valkey, the gateway's own port.

That is why the overlay has no `ports:` and why the comment beside the omission
is longer than the service definition. CLAUDE.md's fourth ground rule permits
exactly one published port, the proxy's, and this service is not a candidate for
an exception: Caddy could terminate TLS in front of it but has no credential to
check, so a reverse proxy in front of `run-server` publishes the same hole over
https.

`--host 0.0.0.0` in the command is not a contradiction. It binds every interface
*inside the container*, which is what another compose service needs in order to
reach it at all; a container binding `127.0.0.1` is reachable only by itself.
The two lines have to be read together, and `scripts/test_public_tls_live.py`
now lists 3000 among the ports that must be refused on a routable address — a
skip today, because nothing publishes it, and a failing check the moment
somebody does.

Reaching it from the host to debug is a deliberate and temporary act:

```bash
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.playwright.yml \
  run --rm --publish 127.0.0.1:3000:3000 playwright
```

## The version pin

The overlay pins **1.62.1**, and `PLAYWRIGHT_VERSION` feeds both the image tag
and the `npx` argument so the browser binaries and the driver cannot drift apart.

The pin is not taste. Measured on 2026-09-11 against real servers on this host:
Playwright's WebSocket handshake compares client and server **at minor
granularity** and refuses a mismatch outright. A Python 1.62.0 client against a
1.61.1 server got

```
BrowserType.connect: WebSocket error: ws://…:3000/ 428 Precondition Required
  Playwright version mismatch:
    - server version: v1.61
    - client version: v1.62
```

and no browser at all. Patch versions are not compared, which is what makes
1.62.1 usable.

So the server can only be as new as the version the *consumer* can also
install, and the consumer will be the Python gateway. On 2026-09-11 npm's newest
`playwright` is **1.63.0** and PyPI's newest is **1.62.0**: the Python port lags
the Node release, so pinning npm's latest would have shipped a server that no
released Python client can connect to. 1.62.1 is the newest patch of the newest
minor both ecosystems have.

The sibling [pystino-chat](https://gitlab.linksfoundation.com/viviani/pystino-chat)
repository is the cautionary tale already in the family: its `package.json` asks
for `^1.55.1` and it has 1.61.1 installed, which is exactly the drift this
protocol punishes. **A consumer of this service pins with `==`, and moves when
the overlay moves.** Changing `PLAYWRIGHT_VERSION` without changing the
consumer's pin is a 428, not a degraded render.

## Cost, and why it is opt-in

Measured on `130.192.84.103` (14 GB, 5 cores) with the pinned image:

| | |
|---|---|
| image on disk | **3.52 GB** — three browser engines plus ffmpeg |
| idle, no client connected | **187 MiB**, 20 processes |
| one client rendering three real pages (wikipedia.org, the *Turin* article at 98 K of text, news.ycombinator.com) | peak **367 MiB** |
| settled after the client disconnected | 218 MiB |

That is per *connected client*, not a ceiling: a browser is unbounded by design
and a second concurrent render adds another Chromium. Against the base stack's
footprint this is not free, and on the 3 GB `130.192.84.52` host — where
CLAUDE.md already warns that the stack plus a `pnpm test` will swap — a 3.52 GB
image and a browser that grows with the page is the wrong default.

So the overlay is **opt-in and stays opt-in**, like `smoke` and `proxy`: named
on the `docker compose` line when it is wanted, absent otherwise. It should join
the default set on the day something reads from it, and not before — a service
nothing calls, holding 187 MiB and three browser engines, is a liability with no
corresponding benefit.

## Two things in the file that look like details and are not

**`shm_size: 1gb`.** Chromium's renderers share memory through `/dev/shm`, and
Docker's default 64 MB is small enough that a heavy page kills the tab with
SIGBUS — surfaced as a crashed target with no explanation. Playwright's own
advice is `--ipc=host`, which fixes it by sharing the host's IPC namespace; a
bigger `/dev/shm` fixes the same failure without giving a browser that reach.

**`user: pwuser`.** A browser is the process in this stack most likely to be
running somebody else's code. The image provides the unprivileged account for
exactly this reason and the service uses it.

There is also a cold-start dependency worth knowing: the `mcr.microsoft.com`
image carries the browsers but **not** the `playwright` npm package — `npm ls -g`
lists only corepack, npm and yarn — so the container fetches the driver from the
npm registry on every start. A registry outage is a failed start rather than a
degraded service. That is accepted while nothing depends on the service; the
alternative is our own image layer on top of a 3.5 GB base, which is a build to
maintain for a consumer that does not exist yet.
