# 0035 — Serving the stack on a real address, behind a TLS proxy

- Date: 2026-08-25
- Status: **accepted, built**
- Requested as: "expose the service to the public IP of this VM with self signed
  certificates", then "just add a reverse proxy in front of all the services
  (caddy?) with two supported configurations (dev, exposing on an IP with self
  signed certs) and prod, requiring an fqdn and 80/443 ports open for
  let'sencrypt".
- Related: [0023](0023-console.md) (the console the proxy fronts),
  [0027](0027-inference-providers.md) (the credentials a session holder can
  spend), `deploy/compose/docker-compose.overlay.yml` (the private-mesh answer
  this supersedes for public use, and does not replace for private use).
- **Note (2026-08-29):** [ADR 0044](0044-keycloak-removed.md) removed Keycloak
  from the stack, so the Keycloak-specific holes and mechanisms this ADR closes
  (the dev-mode admin console, the seeded users, the `/kc` origin, the
  credential-rotation job) no longer exist. The TLS, forwarded-headers and
  SNI reasoning here is unaffected and remains in force; the overlay compose
  files it mentions are gone too.

## The rule this overrides, stated first

CLAUDE.md's fourth ground rule, and the header of the overlay compose file, both
say the same thing: **never put the dev stack on a routable address**, and "a
private overlay address satisfies this rule; a public one does not, whatever else
is done to it."

That was correct about the stack as it stood, and it is worth being precise about
why, because the reason is a list and not a principle. Four things:

1. everything was plaintext HTTP;
2. Keycloak served its admin console on `admin`/`admin`;
3. the session cookie was not `Secure`;
4. five seeded users had passwords published in this repository, one of them
   (`dave`) an administrator.

The decision here is to close all four rather than to argue with the rule, and to
rewrite the rule to say what it was actually protecting. What remains true
afterwards is in "What this does not fix", below — read it before deciding this
is a production deployment, because it is not one.

## Decision

### 1. One reverse proxy, two configurations, chosen by variables

Caddy (Apache-2.0) terminates TLS in front of everything.
`deploy/caddy/Caddyfile` is one file whose behaviour is set by
`deploy/.env`:

| | `PUBLIC_HOST` | `TLS_DIRECTIVE` | certificate |
|---|---|---|---|
| dev | an IP address | `tls internal` | Caddy's own CA, trusted by nothing |
| prod | an FQDN | *(empty)* | Let's Encrypt, obtained and renewed automatically |

They are not interchangeable, and the variables say which one you are in rather
than the file inferring it: Let's Encrypt will not issue for an IP address, and
Caddy's internal CA is not in any trust store. Automatic HTTPS additionally
requires the name to resolve here and ports 80 and 443 to be reachable, because
that is where the challenge arrives — which is a fact about the network, not
about this repository, and is why it is stated as a requirement rather than
attempted and diagnosed.

### 2. One origin, with Keycloak under `/kc`

The obvious arrangement is two ports, because the gateway owns `/auth/callback`
and Keycloak owns `/realms`, `/resources` and `/admin`, and those collide at the
root. It was built that way first and abandoned on measurement: the development
host reaches the internet through a firewall that permits **8443 and 22, and
nothing else** — 80, 443, 8000 and 8080 are all closed from outside. A design
needing two ports is a design that does not come up here.

`KC_HTTP_RELATIVE_PATH=/kc` moves every Keycloak URL under one prefix, including
the ones its own login page generates for its stylesheets, so nothing collides
and the proxy rewrites nothing. Stripping the prefix at the proxy instead is the
trap: the proxy is then correct and the login page asks the *gateway* for
`/resources/...`.

### 3. The credentials in the repository stop working, on every `up`

A one-shot `kcadm` container rewrites, idempotently: the client's redirect URI
(the exact public callback, never a wildcard — an open redirect on an OIDC client
hands the authorization code to whoever asks), its web origins, its client
secret, and the password of every seeded user. All from `deploy/.env`, none with
a default, so the deployment cannot start without someone having chosen them.

It has to run on every `up` and not only the first: `start-dev` keeps the realm in
an in-memory H2 database, so recreating the container re-imports
`realm-llm-platform.json` and restores the published passwords with it. The realm
fixture stays honest about being a fixture; the deployment stops using its values.

### 4. `scripts/test_public_tls_live.py` asserts the claims

The other live scripts ask whether the platform works. This one asks whether it
is *only* what it claims to be, which is the question that matters once there is
an address the world can reach. It checks the certificate verifies against the CA
file — never an unverified context, which would pass against anything that
answered — that `admin`/`admin` and `dave-password` and the published client
secret are all refused, that the session cookie is `Secure`, that a completion
still streams, and that PostgreSQL, Valkey, the fake upstream and the two
plaintext application ports answer on loopback and are **refused on this host's
routable address**. Both halves of that last one matter: reachable on loopback is
what proves the refusal is a binding rather than an empty port.

## What building it found

Four things, none of which any unit test could have reached.

**A literal IP address is never sent as SNI.** RFC 6066 forbids it, so Caddy is
asked for a certificate with no server name, falls back to matching the
connection's *local* address — which behind Docker's NAT is the container's
`172.x` — and has nothing to offer. Every handshake fails with a TLS "internal
error" alert and nothing above debug level in the log. `default_sni` in the global
options is what makes an address-only deployment serve at all.

**uvicorn discards forwarded headers from the proxy.** Its default
`forwarded_allow_ips` is `127.0.0.1` and the proxy arrives from the compose
network, so `X-Forwarded-Proto: https` was dropped and the app believed every
request was http. Almost nothing showed it: `/v1` never builds a URL and the OIDC
redirect URI is configured literally. The one place that reads
`request.base_url` is the post-logout landing URL, which went to Keycloak as
`http://`, did not match the `https` URI registered on the client, and came back
`400` — after a login that had worked perfectly. `FORWARDED_ALLOW_IPS` on the
gateway is the fix.

**Keycloak's management interface inherits the relative path.** `/health/ready`
moved to `/kc/health/ready` with everything else, so the compose healthcheck
failed against a Keycloak that had started perfectly, and the gateway and the
proxy never started: *"dependency failed to start: container
llm-platform-keycloak-1 is unhealthy"*, with nothing wrong in Keycloak's log.
`KC_HTTP_MANAGEMENT_RELATIVE_PATH=/` pins it, which keeps the base file's
healthcheck true wherever the public path goes.

**Changing the advertised hostname re-provisions every user.** Users are keyed on
`(issuer, subject)`, and `iss` is whatever `KC_HOSTNAME` advertises. Moving from
`http://localhost:8080` to a public origin makes every existing user a *new* row
at their next login — same person, no memberships, no default billing group. Free
on a fresh deployment; on one with history it is a migration nobody has written.

One near-miss worth recording because it cost time: `kcadm get client --fields
attributes` prints `{}` for a client whose attributes are populated. The
attributes were never lost; the tool does not render nested maps under `--fields`.
Fetching the whole client shows them. Anyone auditing what the setup job wrote
should not trust the narrow read.

## What this does not fix

The stack is now TLS-only and no longer uses any credential published in this
repository. It is still not a production deployment:

- **Keycloak runs `start-dev` on an in-memory H2 database.** The realm is
  re-imported on every container recreate, with the published passwords, and the
  setup job repairs it seconds later — a real if narrow window on each `up`. A
  production identity provider needs its own database and `start`.
- **The certificate is self-signed in the IP configuration.** Users are trained
  to click through a browser warning, and the only thing that makes the warning
  meaningful is comparing the CA fingerprint out of band. The Let's Encrypt
  configuration is the answer to this, and it needs a name.
- **A session holder can still spend real provider credentials.** Nothing here
  changes what an authenticated user can do; it changes who can reach the door.
- **The seeded demo users still exist.** They are what the live scripts assert
  on. Their passwords are now a secret rather than a fixture, which is a
  different thing from their not being demo accounts.
- **Caddy's internal CA issues leaf certificates that last about twelve hours**
  and renews them silently. That is fine, and it means the thing to trust and to
  compare fingerprints on is the *root* (ten years), never the leaf.
