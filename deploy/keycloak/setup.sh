#!/usr/bin/env bash
# Build the development realm: groups, two clients, their mappers, a test user.
#
#   set -a; . deploy/.env; set +a
#   ./deploy/keycloak/setup.sh
#
# Idempotent, and it is the source of truth for the realm's shape rather than a
# one-off. A committed realm JSON was the obvious alternative and does not work
# here: every redirect URI and the issuer itself are built from $PUBLIC_ORIGIN,
# which a static file cannot interpolate, and the client secrets are generated
# by Keycloak — putting them in the repository would commit a credential for an
# IdP that is reachable on the published origin.
#
# ## The two mappers are the whole point of this script
#
# **The audience mapper.** A Keycloak access token carries no `aud` claim at
# all unless a mapper puts one there — only `azp`, naming the client that asked
# for it. `OIDCSettings.access_token_audience` is what enables OIDC tokens on
# `/v1`, and it is matched against `aud`, deliberately not against `azp`: `azp`
# says who *requested* the token, not who it is *for*, so matching it would
# accept a token a permitted client minted for any other purpose. So without
# the mapper below, `/v1` rejects every token — and it rejects them with the
# same message a bad API key gets, so the symptom names nothing.
#
# **The groups mapper.** Keycloak's default group claim is
# `realm_access.roles`; the gateway's default `groups_claim` is `groups`. One
# of the two has to move, and it is cheaper to name the claim `groups` here
# than to configure a dotted path on every provider row.
set -euo pipefail

: "${PUBLIC_ORIGIN:?source deploy/.env first}"
: "${KEYCLOAK_ADMIN_PASSWORD:?set KEYCLOAK_ADMIN_PASSWORD in deploy/.env}"
KEYCLOAK_ADMIN_USER="${KEYCLOAK_ADMIN_USER:-admin}"

REALM="${KEYCLOAK_REALM:-pystino}"
AUDIENCE="${GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE:-pystino-api}"
# A resolvable-looking address, not `chat@local`. The gateway would accept the
# latter — it keys users on `(issuer, subject)` and treats email as a label, so
# `admin@local` is a fine local-door convention. A chat client is stricter: this
# one validates the `email` claim as a real address and refuses `.local` with a
# Zod "Invalid email" from inside its OIDC callback, which reads as a broken
# login rather than a rejected claim. `.example.org` is reserved by RFC 2606
# and cannot collide with anything real.
TEST_USER="${KEYCLOAK_TEST_USER:-chat@example.org}"
TEST_PASSWORD="${KEYCLOAK_TEST_PASSWORD:-}"
CONTAINER="${KEYCLOAK_CONTAINER:-llm-platform-keycloak-1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

kc() { docker exec -i "$CONTAINER" /opt/keycloak/bin/kcadm.sh "$@"; }

# ---------------------------------------------------------------------------
# 0. Append Caddy's local authority to the gateway's trust bundle.
#
# Appended, not written: docker-compose.keycloak.yml's `ca-bundle` service
# seeded this file with the public roots, and SSL_CERT_FILE *replaces* the
# trust store rather than adding to it — so overwriting it here would leave the
# gateway unable to verify any real provider's certificate.
#
# Written through a container because that seed step created deploy/tls as
# root; the alternative was asking for sudo in a script that otherwise needs
# none.
# ---------------------------------------------------------------------------
PROXY_CONTAINER="${PROXY_CONTAINER:-llm-platform-proxy-1}"
TLS_DIR="$REPO_ROOT/deploy/tls"
CA_NAME="caddy-root.crt"

echo "== trust bundle"
if docker run --rm -v "$TLS_DIR:/out" caddy:2.11-alpine \
	sh -c "grep -q 'Caddy Local Authority' /out/$CA_NAME 2>/dev/null" 2>/dev/null; then
	echo "   Caddy's local authority already in the bundle"
else
	ROOT="$(docker exec "$PROXY_CONTAINER" cat /data/caddy/pki/authorities/local/root.crt)"
	[ -n "$ROOT" ] || {
		echo "   could not read Caddy's root from $PROXY_CONTAINER — is the proxy overlay up?" >&2
		exit 1
	}
	# The subject line goes in as a comment so the idempotence check above has
	# something to match, and so a person reading the bundle can see why a
	# local CA is in it.
	printf '\n# Caddy Local Authority (development, deploy/keycloak/setup.sh)\n%s\n' "$ROOT" |
		docker run --rm -i -v "$TLS_DIR:/out" caddy:2.11-alpine \
			sh -c "cat >> /out/$CA_NAME"
	echo "   appended Caddy's local authority"
fi
docker run --rm -v "$TLS_DIR:/out" caddy:2.11-alpine \
	sh -c "echo \"   bundle now holds \$(grep -c 'BEGIN CERTIFICATE' /out/$CA_NAME) certificates\""

# ---------------------------------------------------------------------------
# 1. Wait for Keycloak, then authenticate against the master realm.
# ---------------------------------------------------------------------------
echo "== waiting for Keycloak"
for _ in $(seq 1 60); do
	if kc config credentials --server http://localhost:8080 --realm master \
		--user "$KEYCLOAK_ADMIN_USER" --password "$KEYCLOAK_ADMIN_PASSWORD" >/dev/null 2>&1; then
		echo "   authenticated as $KEYCLOAK_ADMIN_USER"
		break
	fi
	sleep 3
done
kc config credentials --server http://localhost:8080 --realm master \
	--user "$KEYCLOAK_ADMIN_USER" --password "$KEYCLOAK_ADMIN_PASSWORD" >/dev/null

# ---------------------------------------------------------------------------
# 2. The realm.
# ---------------------------------------------------------------------------
if kc get "realms/$REALM" >/dev/null 2>&1; then
	echo "== realm $REALM exists"
else
	echo "== creating realm $REALM"
	kc create realms -s "realm=$REALM" -s enabled=true \
		-s "displayName=Pystino (development)" >/dev/null
fi

# ---------------------------------------------------------------------------
# 3. Groups. These are the names the gateway will see in the `groups` claim,
#    and `auto_create_groups` means it will make matching rows on first login.
# ---------------------------------------------------------------------------
for group in research platform-admins; do
	if kc get groups -r "$REALM" --fields name 2>/dev/null | grep -q "\"$group\""; then
		echo "== group $group exists"
	else
		echo "== creating group $group"
		kc create groups -r "$REALM" -s "name=$group" >/dev/null
	fi
done

# ---------------------------------------------------------------------------
# 4. The clients, and their mappers.
# ---------------------------------------------------------------------------
# The console's callback is the gateway's; chat-ui's is its own. Both are on the
# single published origin, which is what makes one Caddy site enough.
add_client() {
	# Every argument after the first is a redirect URI, joined into the JSON
	# array Keycloak wants. More than one is normal: see the console below.
	#
	# `post.logout.redirect.uris` is a wildcard under the published origin, and
	# **not** `+`. `+` means "exactly the registered redirect URIs", which for
	# the chat is `.../chat/login/callback` and nothing else — so signing out,
	# which sends the browser back to `.../chat/`, gets a **400** from Keycloak
	# and leaves the person on the provider's error page still signed in. That
	# was measured against this realm, not guessed.
	#
	# The wildcard is safe here for a specific reason rather than by default:
	# this deployment serves exactly one origin (ADR 0035), so it grants a
	# post-logout redirect only to surfaces the deployment already owns.
	local client_id="$1"
	shift
	local redirects=""
	for uri in "$@"; do
		redirects="${redirects:+$redirects,}\"$uri\""
	done
	local uuid
	uuid="$(kc get clients -r "$REALM" -q "clientId=$client_id" --fields id --format csv --noquotes 2>/dev/null | tr -d '\r' | head -1)"

	if [ -n "$uuid" ]; then
		echo "== client $client_id exists ($uuid); updating redirect"
		kc update "clients/$uuid" -r "$REALM" \
			-s "redirectUris=[$redirects]" \
			-s "webOrigins=[\"$PUBLIC_ORIGIN\"]" \
			-s "attributes.\"post.logout.redirect.uris\"=$PUBLIC_ORIGIN/*" >/dev/null
	else
		echo "== creating client $client_id"
		kc create clients -r "$REALM" \
			-s "clientId=$client_id" \
			-s enabled=true \
			-s publicClient=false \
			-s standardFlowEnabled=true \
			-s directAccessGrantsEnabled=true \
			-s serviceAccountsEnabled=false \
			-s "redirectUris=[$redirects]" \
			-s "webOrigins=[\"$PUBLIC_ORIGIN\"]" \
			-s "attributes.\"post.logout.redirect.uris\"=$PUBLIC_ORIGIN/*" >/dev/null
		uuid="$(kc get clients -r "$REALM" -q "clientId=$client_id" --fields id --format csv --noquotes | tr -d '\r' | head -1)"
	fi

	# The audience mapper. Without it `/v1` rejects every token this client
	# mints — see the header comment for why `azp` is not an acceptable
	# substitute.
	if kc get "clients/$uuid/protocol-mappers/models" -r "$REALM" --fields name 2>/dev/null | grep -q '"pystino-audience"'; then
		echo "   audience mapper present"
	else
		echo "   adding audience mapper -> $AUDIENCE"
		kc create "clients/$uuid/protocol-mappers/models" -r "$REALM" \
			-s name=pystino-audience \
			-s protocol=openid-connect \
			-s protocolMapper=oidc-audience-mapper \
			-s 'config."included.custom.audience"='"$AUDIENCE" \
			-s 'config."access.token.claim"=true' \
			-s 'config."id.token.claim"=false' >/dev/null
	fi

	# The groups mapper, named `groups` to match the gateway's default claim.
	# `full.path=false` so the claim reads "research" rather than "/research":
	# the gateway matches group names, and a leading slash would make every
	# mapping and allowlist entry need one too.
	if kc get "clients/$uuid/protocol-mappers/models" -r "$REALM" --fields name 2>/dev/null | grep -q '"groups"'; then
		echo "   groups mapper present"
	else
		echo "   adding groups mapper -> claim 'groups'"
		kc create "clients/$uuid/protocol-mappers/models" -r "$REALM" \
			-s name=groups \
			-s protocol=openid-connect \
			-s protocolMapper=oidc-group-membership-mapper \
			-s 'config."claim.name"=groups' \
			-s 'config."full.path"=false' \
			-s 'config."access.token.claim"=true' \
			-s 'config."id.token.claim"=true' \
			-s 'config."userinfo.token.claim"=true' >/dev/null
	fi
}

# The console's callback carries the **provider name**: ADR 0051 made providers
# rows, and `/auth/callback/{provider_name}` is how the gateway tells one
# directory's answer from another's. Registering the bare `/auth/callback`
# alone is what produced Keycloak's "Invalid parameter: redirect_uri" — the
# button appeared, and pressing it failed. The wildcard covers whatever an
# operator names the next provider; the bare path stays because the gateway
# still accepts a single-provider deployment without one.
add_client pystino-console "$PUBLIC_ORIGIN/auth/callback/*" "$PUBLIC_ORIGIN/auth/callback"
add_client pystino-chat "$PUBLIC_ORIGIN/chat/login/callback"

# ---------------------------------------------------------------------------
# 5. A test user. email_verified matters: ADR 0056's linking gate requires the
#    boolean true, and both the absent claim and the string "true" are declined.
# ---------------------------------------------------------------------------
USER_ID="$(kc get users -r "$REALM" -q "username=$TEST_USER" --fields id --format csv --noquotes 2>/dev/null | tr -d '\r' | head -1)"
if [ -z "$USER_ID" ]; then
	echo "== creating user $TEST_USER"
	kc create users -r "$REALM" \
		-s "username=$TEST_USER" \
		-s "email=$TEST_USER" \
		-s emailVerified=true \
		-s enabled=true \
		-s firstName=Chat -s lastName=Tester >/dev/null
	USER_ID="$(kc get users -r "$REALM" -q "username=$TEST_USER" --fields id --format csv --noquotes | tr -d '\r' | head -1)"
else
	echo "== user $TEST_USER exists ($USER_ID)"
fi

if [ -n "$TEST_PASSWORD" ]; then
	echo "   setting password from KEYCLOAK_TEST_PASSWORD"
	kc set-password -r "$REALM" --userid "$USER_ID" --new-password "$TEST_PASSWORD" >/dev/null
else
	echo "   KEYCLOAK_TEST_PASSWORD unset — leaving the password alone"
fi

echo "   joining group research"
kc update "users/$USER_ID/groups/$(kc get groups -r "$REALM" -q 'search=research' --fields id --format csv --noquotes | tr -d '\r' | head -1)" \
	-r "$REALM" -s "realm=$REALM" -s "userId=$USER_ID" -n >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 6. What to put in deploy/.env. Printed rather than written: this script does
#    not edit a file holding every other secret in the deployment.
# ---------------------------------------------------------------------------
secret_of() {
	local uuid
	uuid="$(kc get clients -r "$REALM" -q "clientId=$1" --fields id --format csv --noquotes | tr -d '\r' | head -1)"
	kc get "clients/$uuid/client-secret" -r "$REALM" --fields value --format csv --noquotes | tr -d '\r' | head -1
}

ISSUER="$PUBLIC_ORIGIN/idp/realms/$REALM"
echo
echo "=============================================================="
echo "Add to deploy/.env, then restart the gateway:"
echo
echo "GATEWAY_OIDC__ENABLED=true"
echo "GATEWAY_OIDC__ISSUER=$ISSUER"
echo "GATEWAY_OIDC__CLIENT_ID=pystino-console"
echo "GATEWAY_OIDC__CLIENT_SECRET=$(secret_of pystino-console)"
echo "GATEWAY_OIDC__REDIRECT_URI=$PUBLIC_ORIGIN/auth/callback"
echo "GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE=$AUDIENCE"
echo "GATEWAY_OIDC__ADMIN_GROUPS=[\"platform-admins\"]"
echo
echo "# chat-ui (pystino-chat/.env.local)"
echo "OPENID_CONFIG={\"PROVIDER_URL\":\"$ISSUER\",\"CLIENT_ID\":\"pystino-chat\",\"CLIENT_SECRET\":\"$(secret_of pystino-chat)\",\"SCOPES\":\"openid profile email\"}"
echo "=============================================================="
echo
echo "Admin console: $PUBLIC_ORIGIN/idp/admin/  ($KEYCLOAK_ADMIN_USER)"
echo "Discovery:     $ISSUER/.well-known/openid-configuration"
