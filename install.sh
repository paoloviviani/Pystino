#!/bin/sh
# Pystino installer: profiles plus component selection, deliberately simpler
# than the Cerea TUI (which remains the main entry point and the only flow
# that orchestrates the two-phase bring-up).
#
# This script writes deploy/.env from a profile fragment, generating every
# secret locally, then prints the exact commands that finish the job. It
# offers to run the phase-1 bring-up (database + gateway) and nothing more:
# the admin password and the chat database are operator steps between the
# phases, printed verbatim. Nothing mints the chat a key — it boots
# anonymously against the gateway's public GET /v1/models (ADR 0081).
#
# POSIX sh, no dependencies beyond docker and (for secret generation) python3
# or openssl. Usage, from the repository root:
#
#   ./install.sh
#
# Three rules, shared with the TUI:
# - deploy/.env is never sourced into this shell. One chat variable holds
#   JSON and bash quote removal mangles it; compose prefers the shell
#   environment over --env-file, so compose children run with every managed
#   variable scrubbed (see run_compose) and the file travels only via
#   --env-file. The single exception is CHAT_REPO, exported for the one
#   overlays.sh call that interpolates it — a targeted export, not a source.
# - Exposure is edge or proxy, never loopback alongside the chat: the chat
#   publishes no port, so loopback leaves it unreachable (fourth ground
#   rule — nothing on a routable address except through those two overlays).
# - Existing secrets are never regenerated silently (see keep_or_generate).

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROFILES_DIR="$ROOT/deploy/profiles"
ENV_FILE="$ROOT/deploy/.env"

die() { echo "error: $*" >&2; exit 1; }
note() { echo "$*" >&2; }

command -v docker >/dev/null 2>&1 || die "docker is not available."
docker compose version >/dev/null 2>&1 || die "docker compose is not available."
command -v git >/dev/null 2>&1 || die "git is not available (needed for the Cerea checkout check)."

# --- secret generation -------------------------------------------------------
# deploy/.env.example documents secrets.token_urlsafe: prefer it, fall back to
# openssl with the alphabet converted and the padding stripped (same bytes out).
token_urlsafe() {
	if command -v python3 >/dev/null 2>&1; then
		python3 -c "import secrets,sys; print(secrets.token_urlsafe(int(sys.argv[1])))" "$1"
	elif command -v openssl >/dev/null 2>&1; then
		# shellcheck disable=SC2039
		openssl rand -base64 "$1" | tr '+/' '-_' | tr -d '=\n'
		echo
	else
		die "neither python3 nor openssl found — cannot generate secrets locally."
	fi
}

idp_signing_key() {
	if command -v openssl >/dev/null 2>&1 && openssl ecparam -genkey -name prime256v1 2>/dev/null | grep -q "BEGIN EC PRIVATE KEY"; then
		openssl ecparam -genkey -name prime256v1
	elif command -v python3 >/dev/null 2>&1; then
		python3 -c "from cryptography.hazmat.primitives.asymmetric import ec; from cryptography.hazmat.primitives import serialization; k = ec.generate_private_key(ec.SECP256R1()); print(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode())" 2>/dev/null || die "openssl cannot mint the IdP key and python3 lacks cryptography — install one, or paste a key by hand."
	else
		die "openssl cannot mint the IdP key — paste GATEWAY_IDP__SIGNING_KEY by hand (openssl ecparam -genkey -name prime256v1)."
	fi
}

# --- prompting ----------------------------------------------------------------
ask() {
	# ask <varname> <prompt> [default]. Assigned single-quoted, so an
	# operator value holding $, quotes or backslashes survives verbatim.
	__q=$2
	if [ -n "${3-}" ]; then __q="$__q [$3]"; fi
	printf '%s: ' "$__q" >&2
	IFS= read -r __a || __a=""
	if [ -z "$__a" ]; then __a="${3-}"; fi
	__esc=$(printf '%s' "$__a" | sed "s/'/'\\\\''/g")
	eval "$1='$__esc'"
}

ask_secret() {
	# ask_secret <varname> <prompt> — no echo where stty allows it.
	printf '%s: ' "$2" >&2
	if stty -echo 2>/dev/null; then
		IFS= read -r __a || __a=""
		stty echo 2>/dev/null || true
		echo >&2
	else
		note "(input will echo — stdin is not a TTY)"
		IFS= read -r __a || __a=""
	fi
	__esc=$(printf '%s' "$__a" | sed "s/'/'\\\\''/g")
	eval "$1='$__esc'"
}

confirm() {
	# confirm <prompt> [default-y|default-n] -> 0/1
	if [ "${2-default-y}" = "default-y" ]; then __hint="Y/n"; __def=0; else __hint="y/N"; __def=1; fi
	for __ in 1 2 3 4 5; do
		printf '%s (%s): ' "$1" "$__hint" >&2
		IFS= read -r __a || __a=""
		case "$__a" in
			"") return "$__def" ;;
			[yY]|[yY][eE][sS]) return 0 ;;
			[nN]|[nN][oO]) return 1 ;;
		esac
	done
	die "too many invalid answers."
}

choose_profile() {
	echo "One stack, three sizes:" >&2
	echo "  1) homelab — single box, no ledger, no redaction (~700 MB RSS)" >&2
	echo "  2) team — ledger, quotas, pattern-only redaction (+~300 MB RSS)" >&2
	echo "  3) enterprise — NER redaction, browser fetch, external OIDC" >&2
	for __ in 1 2 3 4 5; do
		printf 'Profile [1-3] (1): ' >&2
		IFS= read -r __a || __a=""
		: "${__a:=1}"
		case "$__a" in
			1) PROFILE=homelab; return 0 ;;
			2) PROFILE=team; return 0 ;;
			3) PROFILE=enterprise; return 0 ;;
		esac
	done
	die "too many invalid answers."
}

choose_exposure() {
	echo "Exposure (the chat publishes no port — loopback alone is not offered):" >&2
	echo "  1) edge — NetBird edge terminates TLS upstream" >&2
	echo "  2) proxy — Caddy terminates TLS on this box" >&2
	for __ in 1 2 3 4 5; do
		printf 'Exposure [1-2] (1): ' >&2
		IFS= read -r __a || __a=""
		: "${__a:=1}"
		case "$__a" in
			1) EXPOSURE=edge; return 0 ;;
			2) EXPOSURE=proxy; return 0 ;;
		esac
	done
	die "too many invalid answers."
}

choose_redaction() {
	# choose_redaction <profile-default>
	echo "Redaction (needs a rebuild to change later — SPACY_MODELS is a build argument):" >&2
	echo "  1) off — nothing (+0)" >&2
	echo "  2) pattern-only — identifiers and checksums (+~300 MB with the extractor)" >&2
	echo "  3) NER — names and places too (+~750 MB; Italian names need a CC BY-NC-SA 3.0 model)" >&2
	for __ in 1 2 3 4 5; do
		printf 'Redaction [1-3] (%s): ' "$1" >&2
		IFS= read -r __a || __a=""
		: "${__a:=$1}"
		case "$__a" in
		1|off) REDACTION=off; return 0 ;;
		2|pattern) REDACTION=pattern; return 0 ;;
		3|ner) REDACTION=ner; return 0 ;;
		esac
	done
	die "too many invalid answers."
}

choose_code_panel() {
	# The /code remote-agent panel (ADR 0085): the chat surface where each
	# person drives an opencode agent on their own machine, through the
	# relay this installer then deploys. Off unless asked for: unlike the
	# other optional components there is a container behind it, and a
	# deployment that did not ask for that shape must not wake up with one.
	# No published port either way: the relay rides the origin already
	# published, at the /ws subpath.
	echo "Coding agents (/code panel): each person drives an opencode agent on" >&2
	echo "their own machine through a relay this box then hosts (one container," >&2
	echo "no new port — it rides the published origin at /ws; the traffic is" >&2
	echo "E2E-encrypted, the relay sees no plaintext). Off hides the panel entirely." >&2
	if confirm "Enable the /code panel and host its relay?" "default-n"; then
		CODE_PANEL=true
	else
		CODE_PANEL=false
		note "The /code panel stays hidden; deploy/compose/docker-compose.code-relay.yml documents the manual path if that changes."
	fi
}

# --- existing values ----------------------------------------------------------
# Span-aware reader for one KEY from an existing deploy/.env: a value runs to
# the next blank line, comment, PEM END marker (included), or UPPER_SNAKE
# assignment (the naive KEY= pattern matches base64 padding like AB==, and
# real names are never that short). Used only to KEEP existing secrets — never
# to source.
getval() {
	awk -v key="$1" '
		$0 ~ "^[ \t]*" key "[ \t]*=" {
			sub(/^[ \t]*[^=]+=[ \t]*/, "")
			value = $0
			found = 1
			next
		}
		found && /^\s*-----END / { print value "\n" $0; exit }
		found && /^\s*(#|$)/ { print value; exit }
		found && /^\s*[A-Z][A-Z0-9_][A-Z0-9_]+[ \t]*=/ { print value; exit }
		found { value = value "\n" $0; next }
		END { if (found) print value }
	' "$ENV_FILE"
}

keep_or_generate() {
	# keep_or_generate <varname> <generator> [args...] — existing non-empty
	# values are kept (rotating GATEWAY_SECRET_KEY orphans stored credentials;
	# rotating the placeholder key re-labels every transcript entity).
	_key=$1
	shift
	if [ -f "$ENV_FILE" ]; then
		__kept=$(getval "$_key" || true)
		if [ -n "$__kept" ]; then
			note "Keeping existing $_key."
			__esc=$(printf '%s' "$__kept" | sed "s/'/'\\\\''/g")
			eval "$_key='$__esc'"
			return 0
		fi
	fi
	__gen=$( "$@" )
	__esc=$(printf '%s' "$__gen" | sed "s/'/'\\\\''/g")
	eval "$_key='$__esc'"
}

# --- .env assembly -------------------------------------------------------------
# The fragment stays the single source of defaults: rewritten in place by key
# (awk, exact KEY= match on the assignment line), with installer-only keys
# appended under a section. Single-line values only reach this writer — the
# one multiline value (the IdP signing key) is spliced by the PEM branch.
putvar() {
	# putvar <file> <key> <value>
	awk -v key="$2" -v val="$3" '
		$0 ~ "^[ \t]*" key "[ \t]*=" && !done { print key "=" val; done = 1; next }
		{ print }
		END { if (!done) print key "=" val }
	' "$1" > "$1.tmp" && mv "$1.tmp" "$1"
}

# --- compose without the shell environment --------------------------------------
# Compose prefers same-named shell variables over --env-file, so children run
# with every managed variable scrubbed. Built from the keys just written.
run_compose() {
	# run_compose <env-file> <flags...> -- <compose args...>
	__envfile=$1
	shift
	__flags=""
	while [ "$1" != "--" ]; do __flags="$__flags -u $1"; shift; done
	shift
	# shellcheck disable=SC2086
	env $__flags docker compose --env-file "$__envfile" "$@"
}

main() {
	echo "Pystino installer — profiles plus component selection."
	echo "(The Cerea TUI remains the main entry point and the only orchestrated flow.)"
	echo

	choose_profile
	FRAGMENT="$PROFILES_DIR/$PROFILE.env"
	[ -f "$FRAGMENT" ] || die "missing fragment $FRAGMENT."

	case "$PROFILE" in
		homelab) REDACTION_DEF=1; FETCH_DEF=direct; METERING_DEF=n ;;
		team) REDACTION_DEF=2; FETCH_DEF=direct; METERING_DEF=y ;;
		enterprise) REDACTION_DEF=3; FETCH_DEF=playwright; METERING_DEF=y ;;
	esac
	choose_redaction "$REDACTION_DEF"

	printf 'URL fetching: direct, or playwright (rendered pages, +~175 MB RSS + 3.45 GB disk)? [%s]: ' "$FETCH_DEF" >&2
	IFS= read -r FETCH || FETCH=""
	: "${FETCH:=$FETCH_DEF}"
	[ "$FETCH" = direct ] || [ "$FETCH" = playwright ] || die "fetch must be direct or playwright."

	choose_exposure

	if confirm "Metering (ledger + quotas)? Unmetered is the ADR 0065 passthrough shape." "$([ "$METERING_DEF" = y ] && echo default-y || echo default-n)"; then
		METERING=true
		ACCOUNTING_ENABLED=true
		QUOTA_ENABLED=true
		USAGE_ENABLED=true
	else
		METERING=false
		ACCOUNTING_ENABLED=false
		QUOTA_ENABLED=false
		USAGE_ENABLED=
		note "Usage tab emptied too — with no ledger there is nothing to read."
	fi

	choose_code_panel

	# The chat checkout: absolute, validated, never guessed.
	if [ -n "${CHAT_REPO-}" ] && [ -f "$CHAT_REPO/Dockerfile" ]; then
		note "Using CHAT_REPO=$CHAT_REPO from the environment."
	else
		ask CHAT_REPO "Absolute path to the Cerea checkout"
		[ -f "$CHAT_REPO/Dockerfile" ] || die "no Dockerfile at $CHAT_REPO — not a Cerea checkout."
	fi
	case "$CHAT_REPO" in
		*" "*) die "the Cerea path contains a space, which the overlay derivation cannot quote." ;;
	esac

	# --- secrets (generated locally, kept on re-run) ---------------------------
	note
	note "Secrets are generated locally. Two durability warnings, shown here and not just in the file:"
	note "  GATEWAY_SECRET_KEY encrypts provider credentials at rest — back it up WITH the database."
	note "  REDACTION_PLACEHOLDER_KEY must stay stable while its transcripts are kept — rotating it re-labels every entity."
	keep_or_generate POSTGRES_PASSWORD token_urlsafe 32
	keep_or_generate GATEWAY_SECRET_KEY token_urlsafe 48
	keep_or_generate GATEWAY_SESSION_SECRET token_urlsafe 48
	keep_or_generate CHAT_SECRET_KEY token_urlsafe 48
	keep_or_generate CHAT_PG_PASSWORD token_urlsafe 24
	if [ "$REDACTION" != off ]; then
		keep_or_generate REDACTION_PLACEHOLDER_KEY token_urlsafe 32
	fi
	if [ "$PROFILE" != enterprise ]; then
		if [ -f "$ENV_FILE" ] && [ -n "$(getval GATEWAY_IDP__SIGNING_KEY || true)" ]; then
			note "Keeping existing GATEWAY_IDP__SIGNING_KEY."
			GATEWAY_IDP__SIGNING_KEY=$(getval GATEWAY_IDP__SIGNING_KEY)
		else
			GATEWAY_IDP__SIGNING_KEY=$(idp_signing_key)
		fi
		keep_or_generate GATEWAY_IDP__INTERNAL_TOKEN token_urlsafe 48
		keep_or_generate CHAT_IDP_CLIENT_SECRET token_urlsafe 48
	fi

	# --- operator values ---------------------------------------------------------
	ask UPSTREAM_BASE "Upstream OpenAI-compatible base URL" "https://api.cortecs.ai/v1"
	ask_secret UPSTREAM_KEY "Upstream API key (your provider account — cannot be generated)"
	[ -n "$UPSTREAM_KEY" ] || die "the gateway serves nothing without an upstream key."

	if [ "$EXPOSURE" = edge ]; then
		ask PUBLIC_HOST "Public hostname browsers use"
		[ -n "$PUBLIC_HOST" ] || die "a public hostname is required."
		ask HTTPS_PORT "Edge forward port" "8443"
		TLS_DIRECTIVE="tls internal"
	else
		ask PUBLIC_HOST "Public host (IP for self-signed, FQDN for Let's Encrypt)"
		[ -n "$PUBLIC_HOST" ] || die "a public host is required."
		case "$PUBLIC_HOST" in
			[0-9]*.[0-9]*.[0-9]*.[0-9]*) HTTPS_PORT_DEF=8443 ;;
			*) HTTPS_PORT_DEF=443 ;;
		esac
		ask HTTPS_PORT "HTTPS port" "$HTTPS_PORT_DEF"
		if confirm "Let's Encrypt automatically? (needs ports 80+443 reachable)" "default-n"; then
			TLS_DIRECTIVE=""
		else
			TLS_DIRECTIVE="tls internal"
		fi
		ask ACME_EMAIL "ACME email" ""
	fi
	if [ "$HTTPS_PORT" = 443 ]; then PUBLIC_ORIGIN_DEF="https://$PUBLIC_HOST"; else PUBLIC_ORIGIN_DEF="https://$PUBLIC_HOST:$HTTPS_PORT"; fi
	ask PUBLIC_ORIGIN "Public origin" "$PUBLIC_ORIGIN_DEF"

	if [ "$PROFILE" = enterprise ]; then
		note "External identity provider (docs/oidc-generic-provider.md has the per-provider checklist)."
		ask OIDC_ISSUER "OIDC issuer"
		[ -n "$OIDC_ISSUER" ] || die "an issuer is required."
		ask OIDC_CLIENT_ID "Client id (gateway console)"
		ask_secret OIDC_CLIENT_SECRET "Client secret (gateway console)"
		ask OIDC_GROUPS "Groups claim" "groups"
		ask OIDC_AUDIENCE "Access-token audience for /v1 (empty means API keys only — the chat's per-user calls then fail)"
		ask CHAT_OIDC_URL "Chat OIDC provider URL" "$OIDC_ISSUER"
		ask CHAT_OIDC_ID "Chat client id" "cerea"
		ask_secret CHAT_OIDC_SECRET "Chat client secret"
	fi

	# --- write --------------------------------------------------------------------
	if [ -f "$ENV_FILE" ]; then
		if ! confirm "Overwrite $ENV_FILE?" "default-n"; then
			die "aborted. Nothing was changed."
		fi
		BACKUP="$ENV_FILE.bak-$(date +%Y%m%dT%H%M%S)"
		cp "$ENV_FILE" "$BACKUP"
		note "(backup: $BACKUP)"
	fi
	cp "$FRAGMENT" "$ENV_FILE"
	chmod 600 "$ENV_FILE"

	putvar "$ENV_FILE" POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
	putvar "$ENV_FILE" GATEWAY_SECRET_KEY "$GATEWAY_SECRET_KEY"
	putvar "$ENV_FILE" GATEWAY_SESSION_SECRET "$GATEWAY_SESSION_SECRET"
	putvar "$ENV_FILE" GATEWAY_UPSTREAM__BASE_URL "$UPSTREAM_BASE"
	putvar "$ENV_FILE" GATEWAY_UPSTREAM__API_KEY "$UPSTREAM_KEY"
	putvar "$ENV_FILE" GATEWAY_ACCOUNTING__ENABLED "$ACCOUNTING_ENABLED"
	putvar "$ENV_FILE" GATEWAY_QUOTA__ENABLED "$QUOTA_ENABLED"
	if [ "$REDACTION" = off ]; then
		putvar "$ENV_FILE" GATEWAY_REDACTION__ENGINE "noop"
	else
		putvar "$ENV_FILE" GATEWAY_REDACTION__ENGINE "http"
		putvar "$ENV_FILE" REDACTION_PLACEHOLDER_KEY "$REDACTION_PLACEHOLDER_KEY"
		putvar "$ENV_FILE" REDACTION_LANGUAGE "en"
		if [ "$REDACTION" = ner ]; then
			putvar "$ENV_FILE" SPACY_MODELS "en_core_web_lg"
			putvar "$ENV_FILE" REDACTION_NLP_ENGINE "spacy"
		else
			putvar "$ENV_FILE" SPACY_MODELS ""
			putvar "$ENV_FILE" REDACTION_NLP_ENGINE "disabled"
		fi
	fi
	putvar "$ENV_FILE" GATEWAY_EXTRACTOR__ENDPOINT "http://extractor:8080"
	putvar "$ENV_FILE" CHAT_REPO "$CHAT_REPO"
	putvar "$ENV_FILE" CHAT_PG_URL "postgresql://chat:$CHAT_PG_PASSWORD@postgres:5432/chat"
	putvar "$ENV_FILE" CHAT_SECRET_KEY "$CHAT_SECRET_KEY"
	putvar "$ENV_FILE" FETCH_BACKEND "$FETCH"
	putvar "$ENV_FILE" CHAT_CODE_TOOL_ENABLED "true"
	if [ -n "$USAGE_ENABLED" ]; then putvar "$ENV_FILE" CHAT_USAGE_ENABLED "true"; else putvar "$ENV_FILE" CHAT_USAGE_ENABLED ""; fi
	putvar "$ENV_FILE" CHAT_KNOWLEDGE_ENABLED "true"
	# The /code panel: on only where the operator answered for it, because it
	# deploys the relay container (reachable from outside at <origin>/ws —
	# no published port; see docker-compose.code-relay.yml). The relay
	# endpoint Cerea dials is the compose-internal one; daemons outside dial
	# the origin Caddy already publishes, path /ws, and the pairing offer
	# they paste into the panel records that public endpoint.
	if [ "$CODE_PANEL" = true ]; then
		putvar "$ENV_FILE" CODE_AGENTS_ENABLED "true"
		putvar "$ENV_FILE" CODE_RELAY_URL "relay:4000"
	else
		putvar "$ENV_FILE" CODE_AGENTS_ENABLED ""
		putvar "$ENV_FILE" CODE_RELAY_URL ""
	fi
	putvar "$ENV_FILE" PUBLIC_HOST "$PUBLIC_HOST"
	putvar "$ENV_FILE" HTTPS_PORT "$HTTPS_PORT"
	putvar "$ENV_FILE" PUBLIC_ORIGIN "$PUBLIC_ORIGIN"
	putvar "$ENV_FILE" TLS_DIRECTIVE "\"$TLS_DIRECTIVE\""
	putvar "$ENV_FILE" ACME_EMAIL "${ACME_EMAIL-}"
	if [ "$PROFILE" = enterprise ]; then
		putvar "$ENV_FILE" GATEWAY_OIDC__ENABLED "true"
		putvar "$ENV_FILE" GATEWAY_OIDC__ISSUER "$OIDC_ISSUER"
		putvar "$ENV_FILE" GATEWAY_OIDC__CLIENT_ID "$OIDC_CLIENT_ID"
		putvar "$ENV_FILE" GATEWAY_OIDC__CLIENT_SECRET "$OIDC_CLIENT_SECRET"
		putvar "$ENV_FILE" GATEWAY_OIDC__GROUPS_CLAIM "$OIDC_GROUPS"
		putvar "$ENV_FILE" GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE "$OIDC_AUDIENCE"
		putvar "$ENV_FILE" GATEWAY_IDP__ENABLED "false"
		putvar "$ENV_FILE" CHAT_OIDC_PROVIDER_URL "$CHAT_OIDC_URL"
		putvar "$ENV_FILE" CHAT_OIDC_CLIENT_ID "$CHAT_OIDC_ID"
		putvar "$ENV_FILE" CHAT_OIDC_CLIENT_SECRET "$CHAT_OIDC_SECRET"
	else
		putvar "$ENV_FILE" GATEWAY_IDP__ENABLED "true"
		putvar "$ENV_FILE" GATEWAY_IDP__ISSUER "$PUBLIC_ORIGIN"
		putvar "$ENV_FILE" GATEWAY_IDP__INTERNAL_BASE_URL "http://gateway:8000"
		putvar "$ENV_FILE" GATEWAY_IDP__CLIENTS "[{\"client_id\":\"cerea\",\"redirect_path\":\"/chat/login/callback\",\"secret\":\"$CHAT_IDP_CLIENT_SECRET\"}]"
	fi
	# The one multiline value: splice, don't putvar (awk is line-based).
	# The stale END marker goes with its value, not after it.
	if [ "$PROFILE" != enterprise ]; then
		awk -v pem="$GATEWAY_IDP__SIGNING_KEY" '
			$0 ~ /^[ \t]*GATEWAY_IDP__SIGNING_KEY[ \t]*=/ && !done {
				print "GATEWAY_IDP__SIGNING_KEY=" pem
				done = 1
				inold = 1
				next
			}
			inold && /^\s*-----END / { inold = 0; next }
			inold && /^\s*(#|$)/ { inold = 0 }
			inold && /^\s*[A-Z][A-Z0-9_][A-Z0-9_]+[ \t]*=/ { inold = 0 }
			inold { next }
			{ print }
		' "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
	fi

	# Fail closed before anything starts: read back what was actually written
	# (shell names differ from file names here — UPSTREAM_KEY feeds
	# GATEWAY_UPSTREAM__API_KEY — so the file is the thing to check, not the
	# shell).
	for req in POSTGRES_PASSWORD GATEWAY_SECRET_KEY GATEWAY_SESSION_SECRET GATEWAY_UPSTREAM__API_KEY CHAT_SECRET_KEY PUBLIC_HOST PUBLIC_ORIGIN; do
		__v=$(getval "$req" || true)
		[ -n "$__v" ] || die "refusing to continue with $req empty in $ENV_FILE."
	done
	[ -n "${CHAT_REPO-}" ] || die "refusing to continue with CHAT_REPO empty."

	# --- overlay set (derived, never restated) --------------------------------------
	# shellcheck disable=SC1091
	. "$PROFILES_DIR/overlays.sh"
	FLAGS=$(CHAT_REPO="$CHAT_REPO" custom_overlays "$EXPOSURE" "$REDACTION" "$FETCH") || die "overlay derivation refused the selection."
	if [ "$CODE_PANEL" = true ]; then
		# The relay overlay joins the set, and its source arrives before
		# anything can build it: the image is built from the pinned
		# checkout deploy/code-relay/fetch.sh maintains (no published
		# image exists; ADR 0085 records why building from source is the
		# shape, not a workaround).
		FLAGS="$FLAGS -f deploy/compose/docker-compose.code-relay.yml"
		deploy/code-relay/fetch.sh || die "fetching the relay source failed (network?)."
		PROFILES="chat,code-relay"
		note "Overlay set: $FLAGS (the relay joins the set; its source is pinned by deploy/code-relay/fetch.sh)"
	else
		PROFILES="chat"
		note "Overlay set: $FLAGS"
	fi

	# Managed names, for scrubbing compose children (see run_compose). Only
	# real assignments: all-caps with =, so a base64 PEM tail (whose padding
	# matches a naive KEY= pattern) never contributes a bogus entry.
	MANAGED=$(grep -o '^[A-Z][A-Z0-9_]*=' "$ENV_FILE" | tr -d '=' | sort -u | tr '\n' ' ')

	echo
	echo "Wrote $ENV_FILE (mode 600)."
	echo
	echo "Next — phase 1 (database + gateway), then two operator steps, then everything:"
	echo "  1. env $MANAGED docker compose --env-file deploy/.env -f deploy/compose/docker-compose.yml up -d --build"
	echo "  2. docker compose --env-file deploy/.env -f deploy/compose/docker-compose.yml exec gateway gateway passwd admin@local"
	echo "     (prompts — nothing lands in shell history)"
	echo "  3. Create the chat role and database (password: the generated CHAT_PG_PASSWORD in $ENV_FILE):"
	echo "     docker compose --env-file deploy/.env -f deploy/compose/docker-compose.yml exec -T postgres psql -U gateway <<'SQL'"
	echo "     CREATE ROLE chat WITH LOGIN PASSWORD '<from $ENV_FILE>';"
	echo "     CREATE DATABASE chat OWNER chat;"
	echo "SQL"
	echo "  4. env $MANAGED docker compose --env-file deploy/.env $FLAGS --profile $PROFILES up -d --build"
	if [ "$CODE_PANEL" = true ]; then
		# The daemon dials the origin, not a relay port: proxy shape
		# terminates TLS on this box at HTTPS_PORT; edge shape terminates
		# upstream and this box's HTTPS_PORT is the plain hop behind it.
		if [ "$EXPOSURE" = "edge" ]; then
			RELAY_DIAL="$PUBLIC_HOST:443"
		else
			RELAY_DIAL="$PUBLIC_HOST:$HTTPS_PORT"
		fi
		echo
		echo "The /code panel is on: after step 4 the relay answers at <PUBLIC_ORIGIN>/ws"
		echo "(the same origin Caddy already publishes — no new port). Each person"
		echo "who wants an agent runs the paseo daemon on their own machine with"
		echo "PASEO_RELAY_ENDPOINT=$RELAY_DIAL and pastes the pairing link the"
		echo "daemon prints into the panel's Pair a device dialog. The agent's"
		echo "LLM traffic still bills through this gateway: opencode there uses"
		echo "the enrollment CLI (deploy/opencode/) against /v1, an axis the"
		echo "relay is never part of (ADR 0085)."
	fi
	echo
	if confirm "Run step 1 now (phase-1 bring-up)?" "default-y"; then
		# shellcheck disable=SC2086
		run_compose "$ENV_FILE" $MANAGED -- -f deploy/compose/docker-compose.yml up -d --build
		echo "Phase 1 is starting. Continue with steps 2-4 above when the gateway is healthy."
	fi
}

main "$@"
