#!/bin/sh
# Overlay selection for the deployment profiles (deploy/profiles/*.env).
#
# A profile is an env fragment plus a derived overlay list — never new compose
# topology (docs/deployment.md: a fake upstream in the base file would be one
# careless -f away from production). This file is the single machine-readable
# source of that derivation; docs/deployment.md carries the same mapping as a
# table for humans. Both installers (the Cerea TUI and Pystino's install.sh)
# call this rather than restating the lists.
#
# Two functions. `profile_overlays` is the named profiles; `custom_overlays`
# is the same derivation from explicit component choices, for installers whose
# operator deviated from a profile's defaults (the Cerea TUI's per-component
# toggles). Profiles delegate to it, so there is one mapping, not two.
#
# No profile selects the smoke overlay, and that is the point of the exercise:
# a fake upstream in any production set is one careless `-f` from the failure
# the overlay model exists to prevent. Smoke stays a development shape,
# combined by hand with whatever it verifies
# (docs/deployment.md: base + smoke + redaction for the redaction check).
#
# Usage, from the repository root:
#   . deploy/profiles/overlays.sh
#   docker compose --env-file deploy/.env $(profile_overlays team edge) up -d --build
#   docker compose --env-file deploy/.env $(custom_overlays edge ner playwright) up -d --build
#
# Profiles: homelab | team | enterprise
# Exposure: edge | proxy (required — there is no loopback profile, because the
#   chat publishes no port: without an exposure overlay it is unreachable, and
#   its depends_on the proxy fails compose validation outright)
#   edge    the NetBird shape: plain HTTP on loopback, TLS terminated upstream
#   proxy   Caddy terminates TLS locally; the only shape allowed a routable
#           address with a locally held certificate
# Redaction: off | pattern | ner
# Fetch: direct | playwright
#
# Profile defaults: homelab is (off, direct); team is (pattern, direct);
# enterprise is (ner, playwright).
#
# The override, smoke and keycloak overlays are never selected here: mounted
# development sources, a testing fixture, and a development identity provider
# (ADR 0044 stays removed) are none of them production topology. The
# enterprise Playwright overlay lives with its consumer in Cerea and is
# addressed through CHAT_REPO.

profile_overlays() {
	profile=${1:?usage: profile_overlays <homelab|team|enterprise> <edge|proxy>}
	exposure=${2:?usage: profile_overlays <homelab|team|enterprise> <edge|proxy>}

	case $profile in
		homelab) custom_overlays "$exposure" off direct ;;
		team) custom_overlays "$exposure" pattern direct ;;
		enterprise) custom_overlays "$exposure" ner playwright ;;
		*)
			echo "profile_overlays: unknown profile '$profile'" >&2
			return 1
			;;
	esac
}

custom_overlays() {
	exposure=${1:?usage: custom_overlays <edge|proxy> <off|pattern|ner> <direct|playwright>}
	redaction=${2:?usage: custom_overlays <edge|proxy> <off|pattern|ner> <direct|playwright>}
	fetch=${3:?usage: custom_overlays <edge|proxy> <off|pattern|ner> <direct|playwright>}

	case $exposure in
		edge | proxy) ;;
		*)
			echo "custom_overlays: exposure must be 'edge' or 'proxy' (got '$exposure'): the chat publishes no port, so loopback leaves it unreachable" >&2
			return 1
			;;
	esac
	case $redaction in
		off | pattern | ner) ;;
		*)
			echo "custom_overlays: redaction must be 'off', 'pattern' or 'ner' (got '$redaction')" >&2
			return 1
			;;
	esac
	case $fetch in
		direct | playwright) ;;
		*)
			echo "custom_overlays: fetch must be 'direct' or 'playwright' (got '$fetch')" >&2
			return 1
			;;
	esac

	set -- -f deploy/compose/docker-compose.yml

	# Pattern or NER redaction, plus the local extractor as the second
	# deployment of the same image. Off drops the overlay and runs
	# GATEWAY_REDACTION__ENGINE=noop. No smoke alongside: the redaction
	# overlay carries no fixture stanza anymore, so no testing fixture rides
	# into any production set.
	case $redaction in
		pattern | ner)
			set -- "$@" -f deploy/compose/docker-compose.redaction.yml
			;;
	esac

	# The chat (Cerea, built from CHAT_REPO) with its own MongoDB.
	set -- "$@" -f deploy/compose/docker-compose.chat.yml

	# The headless-browser fetch backend. No ports: reaching it is
	# unauthenticated remote code execution by design, and it stays inside
	# the compose network.
	if [ "$fetch" = "playwright" ]; then
		: "${CHAT_REPO:?playwright fetch needs CHAT_REPO set — the playwright overlay lives in the Cerea checkout}"
		set -- "$@" -f "$CHAT_REPO/deploy/compose/docker-compose.playwright.yml"
	fi

	# Exposure last: both overlays adjust gateway trust and cookies for life
	# behind a reverse proxy, and must win over the base file.
	case $exposure in
		edge) set -- "$@" -f deploy/compose/docker-compose.edge.yml ;;
		proxy) set -- "$@" -f deploy/compose/docker-compose.proxy.yml ;;
	esac

	printf '%s ' "$@"
	echo
}
