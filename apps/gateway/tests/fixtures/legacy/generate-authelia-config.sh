#!/usr/bin/env bash
# Generate the bundled-Authelia configuration (IDP_BUNDLED=authelia).
#
# The installer calls this once, with every value minted locally, before the
# first `docker compose up`. It writes three files into the output directory
# (default <repo>/deploy/idp/):
#
#   authelia-configuration.yml   the IdP itself: file user backend, OIDC
#                                provider (three clients, fixed audience),
#                                subpath serving under /authelia
#   users_database.yml           the first human (installer-chosen address +
#                                password, hashed below — never stored plain)
#   authelia-jwks-rsa.pem        the OIDC signing key (chmod 600). The same
#                                PEM is embedded in authelia-configuration.yml
#                                (Authelia reads the key from its own config,
#                                not from a separate mount); this file is the
#                                operator's backup for the day the yml is ever
#                                regenerated.
#
# Inputs are environment variables, every one single-line (the installer's
# line-oriented invariant — a PEM never travels through the environment):
#
#   IDP_PUBLIC_ORIGIN        required. The deployment's public origin, e.g.
#                            https://llm.example.org. Redirect URIs are built
#                            from it: the gateway sends
#                            <origin>/auth/callback/default (the env-seeded
#                            provider is named "default", ADR 0051) and the
#                            chat sends <origin>/chat/login/callback.
#   IDP_COOKIE_DOMAIN        required. The bare hostname of the public origin
#                            (no scheme, no port), e.g. llm.example.org. Must
#                            be an FQDN for real browsers: Authelia requires a
#                            cookie domain, and browsers refuse a Domain cookie
#                            holding a bare IP — so IP installs need a name
#                            (DNS or a hosts entry) for browser login to keep
#                            its session. The test suite passes 127.0.0.1,
#                            which Authelia accepts and scripted HTTP ignores.
#   IDP_SESSION_SECRET       required. 64 hex chars (openssl rand -hex 32).
#                            Signs Authelia's own session cookies.
#   IDP_HMAC_SECRET          required. 64 hex chars. Signs the OIDC
#                            authorization codes / opaque tokens.
#   IDP_STORAGE_KEY          required. 64 hex chars. Encrypts Authelia's
#                            sqlite store (opaque subject ids, consent grants).
#   IDP_CONSOLE_CLIENT_SECRET required, plaintext. The gateway console's
#                            OIDC secret (becomes GATEWAY_OIDC__CLIENT_SECRET).
#   IDP_CHAT_CLIENT_SECRET   required, plaintext. The chat's OIDC secret
#                            (becomes CHAT_OIDC_CLIENT_SECRET).
#   IDP_ADMIN_USER           required. Login name, e.g. admin.
#   IDP_ADMIN_EMAIL          required. Must look like a real address (a dot
#                            in the domain): the chat validates the email
#                            claim and refuses `.local` with a Zod error that
#                            reads as a broken login.
#   IDP_ADMIN_NAME           display name (default: the login name).
#   IDP_ADMIN_PASSWORD       required, plaintext. Hashed with SHA512-crypt
#                            ($6$) below; the plaintext never lands in a file.
#   IDP_OUT_DIR              where to write (default: deploy/idp beside this
#                            script's repository).
#
# Only bash, coreutils and openssl. No python on the host, no container, no
# network: `openssl passwd -6` mints SHA512-crypt hashes (a format Authelia
# verifies — proven against the live 4.39.20 image, not assumed) and
# `openssl genrsa` mints the OIDC signing key.
#
# Deterministic by construction, which is the whole contract: the audience
# the gateway checks on /v1 is the literal `pystino-api` below (granted to
# all clients implicitly, so no client ever has to request it), the three
# client ids are fixed (`pystino-console`, `cerea`, `opencode-enrollment`),
# and the issuer is
# <IDP_PUBLIC_ORIGIN>/authelia — so the installer writes
# GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE=pystino-api with no prompting and no
# IdP API call.
#
# Re-running against an existing directory refuses (rotating the JWKS key
# orphans every issued token and re-provisions every user as a new row —
# users are keyed on (issuer, subject) and the subject is opaque per key).
# Delete the outputs deliberately to start over.
set -euo pipefail

OUT_DEFAULT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${IDP_OUT_DIR:-$OUT_DEFAULT}"
ORIGIN="${IDP_PUBLIC_ORIGIN:?set IDP_PUBLIC_ORIGIN, e.g. https://llm.example.org}"
SESSION_SECRET="${IDP_SESSION_SECRET:?set IDP_SESSION_SECRET (openssl rand -hex 32)}"
HMAC_SECRET="${IDP_HMAC_SECRET:?set IDP_HMAC_SECRET (openssl rand -hex 32)}"
STORAGE_KEY="${IDP_STORAGE_KEY:?set IDP_STORAGE_KEY (openssl rand -hex 32)}"
CONSOLE_SECRET="${IDP_CONSOLE_CLIENT_SECRET:?set IDP_CONSOLE_CLIENT_SECRET}"
CHAT_SECRET="${IDP_CHAT_CLIENT_SECRET:?set IDP_CHAT_CLIENT_SECRET}"
ADMIN_USER="${IDP_ADMIN_USER:?set IDP_ADMIN_USER, e.g. admin}"
ADMIN_EMAIL="${IDP_ADMIN_EMAIL:?set IDP_ADMIN_EMAIL}"
ADMIN_NAME="${IDP_ADMIN_NAME:-$ADMIN_USER}"
ADMIN_PASSWORD="${IDP_ADMIN_PASSWORD:?set IDP_ADMIN_PASSWORD}"

case "$ORIGIN" in
	*"$'\n'"* | *"$'\r'"*) echo "IDP_PUBLIC_ORIGIN must be single-line" >&2; exit 1 ;;
esac
ORIGIN="${ORIGIN%/}"
case "$ORIGIN" in
	http://* | https://*) ;;
	*) echo "IDP_PUBLIC_ORIGIN must be an absolute http(s) URL" >&2; exit 1 ;;
esac
# The cookie domain is the origin's bare hostname (validator: standard-use
# domain, at least one period — 127.0.0.1 qualifies for tests).
COOKIE_DOMAIN="${IDP_COOKIE_DOMAIN:-${ORIGIN#*://}}"
COOKIE_DOMAIN="${COOKIE_DOMAIN%%:*}"
case "$COOKIE_DOMAIN" in
	*.*) ;;
	*) echo "IDP_COOKIE_DOMAIN ($COOKIE_DOMAIN) needs at least one period" >&2; exit 1 ;;
esac
case "$ADMIN_EMAIL" in
	*@*.*) ;;
	*) echo "IDP_ADMIN_EMAIL must look like a real address (the chat refuses .local)" >&2; exit 1 ;;
esac

CONFIG="$OUT_DIR/authelia-configuration.yml"
USERS="$OUT_DIR/users_database.yml"
JWKS_PEM="$OUT_DIR/authelia-jwks-rsa.pem"
if [ -e "$CONFIG" ] || [ -e "$USERS" ] || [ -e "$JWKS_PEM" ]; then
	echo "refusing: $OUT_DIR already holds generated Authelia files (delete them deliberately to start over)" >&2
	exit 1
fi
mkdir -p "$OUT_DIR"

# SHA512-crypt ($6$) via openssl only. The salt is 16 hex chars — a subset
# of crypt's alphabet, so no translation step to get wrong.
mkhash() { # mkhash <plaintext> -> HASH_VAL
	# shellcheck disable=SC2034
	local salt
	salt="$(openssl rand -hex 8)"
	HASH_VAL="$(printf '%s' "$1" | openssl passwd -6 -salt "$salt" -stdin)"
}

mkhash "$ADMIN_PASSWORD"
ADMIN_HASH="$HASH_VAL"
mkhash "$CONSOLE_SECRET"
CONSOLE_HASH="$HASH_VAL"
mkhash "$CHAT_SECRET"
CHAT_HASH="$HASH_VAL"

# The OIDC signing key. 2048-bit RSA: Authelia requires at least one RSA key
# in the JWKS, and the gateway accepts RS256.
openssl genrsa -out "$JWKS_PEM" 2048 2>/dev/null
chmod 600 "$JWKS_PEM"
# Indent for the YAML block scalar (10 spaces: key sits at depth 5).
PEM_INDENTED="$(sed 's/^/          /' "$JWKS_PEM")"

# Single-quote escape for the two free-text fields (doubling).
YQ() { printf '%s' "$1" | sed "s/'/''/g"; }

cat >"$CONFIG" <<EOF
# Generated by deploy/idp/generate-authelia-config.sh — do not edit by hand.
# Re-generate (after deliberately deleting these files) to rotate anything;
# rotating the JWKS key re-provisions every user (opaque subject per key).
server:
  # Serve under the /authelia prefix on the already-published origin: Caddy
  # routes /authelia/* here WITHOUT stripping (handle, not handle_path), and
  # every URL Authelia advertises — issuer, discovery, endpoints — carries
  # the prefix. Issuer: $ORIGIN/authelia
  address: 'tcp://:9091/authelia'
log:
  level: 'info'
  format: 'text'
# File backend: the bundled user store. No LDAP, no extra container.
authentication_backend:
  # Password reset by link needs a mail server this box does not have (the
  # bundled notifier is a file). Disabled here — sibling of file:, not under
  # it (the validator rejects password_reset anywhere else), and with no
  # identity_validation.reset_password block (that key does not exist; its
  # absence is what would demand a jwt_secret for a flow that could never
  # deliver).
  password_reset:
    disable: true
  file:
    path: '/config/users_database.yml'
    # Pick up installer-added users without a restart.
    watch: true
    password:
      # Only governs passwords Authelia itself hashes (resets via the
      # portal). Stored hashes verify regardless of this setting.
      algorithm: 'argon2'
      argon2:
        variant: 'argon2id'
        iterations: 3
        memory: 65536
        parallelism: 4
        key_length: 32
        salt_length: 16
# This IdP authenticates logins; it does not front applications (no
# forward-auth use), so the default is the strictest the schema allows with
# no rules. 'deny' is rejected without rules; 'one_factor' only matters if
# anything ever points an authz endpoint here, and then it requires a login.
access_control:
  default_policy: 'one_factor'
# Password reset by link needs a mail server this box does not have (the
# bundled notifier is a file). There is deliberately no
# identity_validation.reset_password block: that key does not exist, and its
# absence is what would demand a jwt_secret for a flow that could never
# deliver.
session:
  name: 'authelia_session'
  secret: '$SESSION_SECRET'
  expiration: '1h'
  inactivity: '5m'
  cookies:
    - name: 'authelia_session'
      # The bare hostname (no port) of the public origin, e.g. llm.example.org.
      # An FQDN is the supported shape. A bare IP passes Authelia's own
      # validation but browsers refuse a Domain cookie holding an IP, so a
      # real browser on an IP install cannot keep the session: IP-based boxes
      # need a name (DNS, nip.io-style, or a hosts entry) for browser login.
      domain: '$COOKIE_DOMAIN'
      # The portal URL Authelia shows and secures its own flows with: the
      # public origin plus the served prefix. https in production; the
      # validator decides what it tolerates elsewhere (see resumption notes
      # in the installer integration — edge/plain-http was never proven).
      authelia_url: '$ORIGIN/authelia'
      same_site: 'lax'
      expiration: '1h'
      inactivity: '5m'
storage:
  # sqlite on a named volume: no second Postgres, no role for the installer
  # to create. Holds opaque subject ids and consent grants — user accounts
  # themselves stay in users_database.yml.
  local:
    path: '/data/db.sqlite3'
  encryption_key: '$STORAGE_KEY'
notifier:
  # No mail server on a fresh box; reset addresses land here instead of in
  # mailboxes. The operator wires SMTP later by editing this section.
  filesystem:
    filename: '/data/notification.txt'
identity_providers:
  oidc:
    hmac_secret: '$HMAC_SECRET'
    jwks:
      - key_id: 'pystino'
        algorithm: 'RS256'
        use: 'sig'
        key: |
$PEM_INDENTED
    claims_policies:
      pystino:
        # Explicit rather than inherited from scope defaults: /v1 access
        # tokens are validated locally (no userinfo round trip), so whatever
        # is not IN the token is not seen. groups must be in the access
        # token or group billing silently sees nobody.
        id_token: ['groups', 'email', 'email_verified', 'preferred_username', 'name']
        access_token: ['groups', 'email', 'email_verified', 'preferred_username', 'name']
    lifespans:
      custom:
        # Authelia's built-in default refresh_token lifespan is 90 minutes
        # (fine for a browser session, fatal for an agent machine that goes
        # idle over a weekend: the shim finds a dead refresh token on Monday
        # with no user watching to notice). opencode-enrollment opts into
        # this profile below instead of narrowing the default, so the
        # console and chat clients (short-lived browser sessions) are
        # unaffected.
        agent_machine:
          access_token: '1h'
          refresh_token: '90d'
    clients:
      # The gateway console. client_secret_post (not basic): the gateway
      # sends the secret in the token POST body (oidc.py exchange_code),
      # never as an Authorization header.
      - client_id: 'pystino-console'
        client_name: 'Pystino Console'
        client_secret: '$CONSOLE_HASH'
        public: false
        authorization_policy: 'one_factor'
        require_pkce: true
        pkce_challenge_method: 'S256'
        redirect_uris:
          - '$ORIGIN/auth/callback/default'
          - '$ORIGIN/auth/callback'
        scopes: ['openid', 'profile', 'email']
        response_types: ['code']
        grant_types: ['authorization_code']
        access_token_signed_response_alg: 'RS256'
        token_endpoint_auth_method: 'client_secret_post'
        # The deterministic audience: implicitly granted, so neither client
        # ever requests it and the installer never prompts for it.
        audience: ['pystino-api']
        requested_audience_mode: 'implicit'
        claims_policy: 'pystino'
        # Remembered consent (default 1 week): one click on first login,
        # then the chat's server-side flow never meets a consent screen.
        consent_mode: 'pre-configured'
      # The chat. client_secret_basic: openid-client's default, which is
      # what Cerea speaks.
      - client_id: 'cerea'
        client_name: 'Cerea Chat'
        client_secret: '$CHAT_HASH'
        public: false
        authorization_policy: 'one_factor'
        require_pkce: true
        pkce_challenge_method: 'S256'
        redirect_uris:
          - '$ORIGIN/chat/login/callback'
        scopes: ['openid', 'profile', 'email', 'groups']
        response_types: ['code']
        grant_types: ['authorization_code']
        access_token_signed_response_alg: 'RS256'
        token_endpoint_auth_method: 'client_secret_basic'
        audience: ['pystino-api']
        requested_audience_mode: 'implicit'
        claims_policy: 'pystino'
        consent_mode: 'pre-configured'
      # The opencode enrollment CLI (ADR 0084): one static public client, the
      # gh/gcloud pattern — Authelia has no dynamic client registration, so
      # the id every CLI already knows is baked in here. Public because a
      # binary on a user's machine cannot keep a secret, hence no
      # client_secret and token_endpoint_auth_method none; PKCE S256 stays
      # mandatory so a stolen code is useless without the verifier.
      # Two grants because the CLI runs in two shapes: authorization_code
      # with a loopback redirect for laptops (a browser that can call back
      # to 127.0.0.1), and the device code grant (RFC 8628) for headless
      # boxes that never see a callback. The device grant needs Authelia
      # 4.39.22 or later — 4.39.0 introduced it but later 4.39.x fixed its
      # bugs, so the overlay pins accordingly.
      - client_id: 'opencode-enrollment'
        client_name: 'Opencode Enrollment'
        public: true
        authorization_policy: 'one_factor'
        require_pkce: true
        pkce_challenge_method: 'S256'
        # Loopback only, never the public origin: the secret-less client is
        # only as safe as its redirect, and a loopback address keeps the
        # code on the machine that started the flow. The CLI binds an
        # ephemeral port and calls back to http://127.0.0.1:<port>/callback;
        # these portless entries rely on Authelia's RFC 8252 loopback
        # handling matching any port on 127.0.0.1/localhost. NEEDS A LIVE
        # CHECK on the pinned Authelia — if it demands an exact port, the
        # CLI's per-run redirect would have to be registered another way.
        redirect_uris:
          - 'http://127.0.0.1/callback'
          - 'http://localhost/callback'
        # groups is what the ledger bills (without it usage lands nowhere),
        # so it is a scope here as well as a token claim; the pystino claims
        # policy below is what actually puts it in the access token /v1
        # validates locally (ADR 0040). offline_access lets the device
        # flow hand the shim its refresh credential — the shim's whole
        # reason to exist.
        scopes: ['openid', 'profile', 'email', 'groups', 'offline_access']
        response_types: ['code']
        # refresh_token must be in the list: the token endpoint only issues
        # a refresh token to a client whose grant_types carry the refresh
        # token grant — offline_access alone is not enough (found live: the
        # device flow approved, then the shim got no refresh credential).
        grant_types: ['authorization_code', 'urn:ietf:params:oauth:grant-type:device_code', 'refresh_token']
        access_token_signed_response_alg: 'RS256'
        token_endpoint_auth_method: 'none'
        # The deterministic audience: implicitly granted like the other two
        # clients, so the CLI never requests it and the /v1 bearer check
        # still sees it.
        audience: ['pystino-api']
        requested_audience_mode: 'implicit'
        claims_policy: 'pystino'
        # Remembered consent: the device flow's own approval at the
        # verification URI is the user's decision, so no second consent
        # screen the polling CLI could not drive anyway.
        consent_mode: 'pre-configured'
        # The agent_machine lifespan profile (defined above): a 90-day
        # refresh token so a machine left idle over a weekend still has a
        # live credential Monday, instead of the 90-minute platform default
        # dying the first time nobody is at the keyboard.
        lifespan: 'agent_machine'
EOF

cat >"$USERS" <<EOF
# Generated by deploy/idp/generate-authelia-config.sh — the first human.
# Later users are appended by the installer/operator (same shape) and picked
# up without a restart (watch: true above). Passwords are SHA512-crypt
# hashes (openssl passwd -6); the plaintext lives nowhere.
users:
  $ADMIN_USER:
    disabled: false
    displayname: '$(YQ "$ADMIN_NAME")'
    password: '$ADMIN_HASH'
    email: '$(YQ "$ADMIN_EMAIL")'
    # The group the gateway bills until the operator maps directories.
    # Authorisation is a gateway fact (no admin-group magic here): make this
    # user an administrator in the console after first login.
    groups:
      - 'users'
EOF
chmod 600 "$USERS" "$JWKS_PEM"

echo "wrote $CONFIG"
echo "wrote $USERS"
echo "wrote $JWKS_PEM"
