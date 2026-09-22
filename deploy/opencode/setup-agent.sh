#!/usr/bin/env bash
# The agent-machine setup: one script, two trust axes (ADR 0085).
#
#   axis 1 (control)  paseo daemon -> self-hosted relay -> the chat's /code panel
#   axis 2 (LLM)     opencode -> the local enroll serve shim -> gateway /v1
#
# It complements install.sh (the pasted-key path, ADR 0010): that one wires
# the LLM axis with a minted key and no daemon; this one wires both axes so
# the agent is drivable from the chat and bills the signed-in person's own
# account (ADR 0040/0061) instead of a shared key.
#
# Usage:
#   ./setup-agent.sh [--relay HOST:PORT] [--relay-tls|--no-relay-tls]
#                   [--gateway ORIGIN] [--issuer ORIGIN]
#                   [--paseo-version X] [--opencode-version X]
#                   [--allow-opencode-provider]
#                   [--skip-daemon] [--skip-llm] [--skip-posture] [--yes]
#
#   PASEO_RELAY        default for --relay (the deployment's public endpoint).
#   PYSTINO_GATEWAY    default for --gateway.
#   PYSTINO_ISSUER     default for --issuer (defaults to the gateway origin
#                      when omitted: the bundled IdPs live on it, ADR 0084).
#
# The relay lives at the deployment's origin, path /ws — no dedicated port
# (ADR 0085: Caddy routes /ws to it; every paseo client builds <scheme>://
# <host>:<port>/ws and cannot address a nested prefix). So --relay names the
# origin's host:port (default :443), and TLS defaults the way the SDK itself
# defaults it: on at 443, off elsewhere, overridable either way.
#
# What it does, in order:
#   1. installs the paseo daemon (npm -g @getpaseo/cli, pinned) and opencode
#      (npm -g opencode-ai, pinned) unless they answer on PATH;
#   2. writes ~/.paseo/config.json's relay block (the control axis) and
#      starts the daemon, which dials the relay outbound — no port opened;
#   3. runs `enroll enroll` (browser loopback PKCE, device-code fallback:
#      RFC 8628) and leaves `enroll serve` running under nohup (the LLM
#      axis: opencode.json points its baseURL at the shim, never at /v1).
#      The written config also names the gateway in enabled_providers, so
#      the gateway's models are the only ones opencode offers — a built-in
#      provider with ambient credentials would take spend off the account
#      the enrollment bills (--allow-opencode-provider opts out);
#   4. writes the permission posture into the opencode config: edit/bash
#      ask — the daemon drops per-prompt permission rules, so the posture
#      lives here and the panel's PermissionCard surfaces the asks;
#   5. prints the pairing link of `paseo daemon pair` — the paste into the
#      chat's /code > Pair a device dialog is the one step that must be a
#      human with both sides (the offer is the credential).
#
# Requires: bash, node/npm, python3, go 1.24+ (only when the enroll binary
# is not built yet — the script builds it from this directory).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENROLL_DIR="$SCRIPT_DIR/enroll"
PASEO_HOME="${PASEO_HOME:-$HOME/.paseo}"
PASEO_CONFIG="$PASEO_HOME/config.json"
OPENCODE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
OPENCODE_CONFIG="$OPENCODE_DIR/opencode.json"

PASEO_VERSION="0.8.0"
OPENCODE_VERSION="1.18.31"

RELAY="${PASEO_RELAY:-}"
RELAY_TLS=""
GATEWAY="${PYSTINO_GATEWAY:-}"
ISSUER="${PYSTINO_ISSUER:-}"
SKIP_DAEMON=0
SKIP_LLM=0
SKIP_POSTURE=0
ALLOW_OPENCODE_PROVIDER=0
ASSUME_YES=0

usage() {
	cat <<EOF
Usage: $(basename "$0") [options]

Sets up an opencode agent machine against a Pystino deployment: the paseo
daemon (control axis, via the relay) plus the enrollment shim (LLM axis,
via gateway /v1).

Options:
  --relay HOST:PORT      relay endpoint daemons dial, e.g. llm.example.org:443
                         (or PASEO_RELAY). This is the deployment's origin and
                         port — the relay answers at /ws on it.
  --relay-tls|--no-relay-tls
                         wss vs ws to the relay. Default follows the port the
                         way the SDK does: on at 443, off elsewhere.
  --gateway ORIGIN       gateway origin, e.g. https://llm.example.org
                         (or PYSTINO_GATEWAY). Prompted when neither set.
  --issuer ORIGIN        OIDC issuer when it differs from the gateway origin
                         (bundled IdPs live ON the origin: /authelia, /idp —
                         leave unset and enroll's discovery handles it).
  --paseo-version X      @getpaseo/cli version (default $PASEO_VERSION, the
                         one ADR 0085 verified; pin, never float).
  --opencode-version X   opencode-ai version (default $OPENCODE_VERSION).
  --skip-daemon          only the LLM axis (what install.sh does, keyed).
  --skip-llm             only the control axis (bring your own provider).
  --skip-posture         leave the existing opencode permission config.
  --allow-opencode-provider
                         leave opencode's built-in providers enabled. By
                         default the enrollment names the gateway in
                         enabled_providers, so the gateway's models are
                         the only ones opencode offers — a built-in with
                         ambient credentials would bypass both the
                         gateway and the billing it exists to enforce.
  --yes                  skip the overwrite confirmation for config files.
  --help                 this text.
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
	--relay) RELAY="${2:?--relay needs a value}"; shift 2 ;;
	--relay-tls) RELAY_TLS=true; shift ;;
	--no-relay-tls) RELAY_TLS=false; shift ;;
	--gateway) GATEWAY="${2:?--gateway needs a value}"; shift 2 ;;
	--issuer) ISSUER="${2:?--issuer needs a value}"; shift 2 ;;
	--paseo-version) PASEO_VERSION="${2:?needs a value}"; shift 2 ;;
	--opencode-version) OPENCODE_VERSION="${2:?needs a value}"; shift 2 ;;
	--skip-daemon) SKIP_DAEMON=1; shift ;;
	--skip-llm) SKIP_LLM=1; shift ;;
	--skip-posture) SKIP_POSTURE=1; shift ;;
	--allow-opencode-provider) ALLOW_OPENCODE_PROVIDER=1; shift ;;
	--yes) ASSUME_YES=1; shift ;;
	--help) usage; exit 0 ;;
	*) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
	esac
done

command -v node >/dev/null || { echo "error: node/npm required" >&2; exit 5; }
command -v python3 >/dev/null || { echo "error: python3 required" >&2; exit 5; }

# The relay endpoint, normalised to host:port — the daemon's config and the
# pairing offer both want that shape (no scheme), and a pasted URL is the
# common operator input. No dedicated relay port exists anymore (the relay
# rides the origin at /ws), so a bare hostname defaults to :443, not :4000.
if [ "$SKIP_DAEMON" -ne 1 ] && [ -z "$RELAY" ]; then
	if [ -t 0 ]; then
		printf 'Relay endpoint host:port (e.g. llm.example.org:443): ' >&2
		IFS= read -r RELAY || { echo "error: no relay given" >&2; exit 2; }
	else
		echo "error: set --relay or PASEO_RELAY (stdin is not a terminal)" >&2
		exit 2
	fi
fi
if [ -n "$RELAY" ]; then
	RELAY="${RELAY#*://}"
	RELAY="${RELAY%/}"
	if ! printf '%s' "$RELAY" | grep -qE ':[0-9]+$'; then
		RELAY="$RELAY:443"
		echo "note: no port in relay endpoint — using $RELAY (the origin's port)" >&2
	fi
fi
if [ -z "$RELAY_TLS" ]; then
	case "$RELAY" in
	*:443) RELAY_TLS=true ;;
	*) RELAY_TLS=false ;;
	esac
fi

if [ "$SKIP_LLM" -ne 1 ] && [ -z "$GATEWAY" ]; then
	if [ -t 0 ]; then
		printf 'Gateway origin (e.g. https://llm.example.org): ' >&2
		IFS= read -r GATEWAY || { echo "error: no gateway given" >&2; exit 2; }
	else
		echo "error: set --gateway or PYSTINO_GATEWAY (stdin is not a terminal)" >&2
		exit 2
	fi
fi
if [ -n "$GATEWAY" ]; then
	GATEWAY="${GATEWAY%/}"
	case "$GATEWAY" in
	http://* | https://*) ;;
	*) echo "error: gateway must be an absolute http(s) origin: $GATEWAY" >&2; exit 2 ;;
	esac
fi

# ------------------------------------------------------------------ #
# axis 1: the daemon and the relay                                     #
# ------------------------------------------------------------------ #
if [ "$SKIP_DAEMON" -eq 1 ]; then
	echo "skipping the daemon (--skip-daemon)" >&2
else
	if ! command -v paseo >/dev/null; then
		echo "installing @getpaseo/cli@$PASEO_VERSION (pinned: the relay protocol may change without notice — ADR 0085)" >&2
		HOME="$HOME" npm install -g --no-audit --no-fund "@getpaseo/cli@$PASEO_VERSION"
	else
		echo "paseo already on PATH ($(paseo --version 2>/dev/null || echo unknown)) — not reinstalling" >&2
	fi

	# The daemon config's relay block. Written with python3 (json, not sed:
	# a config file the daemon rewrites itself must round-trip, and an
	# unconditional overwrite would drop everything else in it — profiles,
	# settings — that a working daemon carries). useTls rides with the
	# endpoint: the offer the daemon prints must name the same wss/ws the
	# daemon itself dials, or the pairing probe answers from nowhere.
	PASEO_CONFIG="$PASEO_CONFIG" PASEO_RELAY_ENDPOINT="$RELAY" PASEO_RELAY_TLS="$RELAY_TLS" python3 - <<'EOF'
import json, os
path = os.environ["PASEO_CONFIG"]
config = {}
if os.path.exists(path):
    with open(path) as f:
        config = json.load(f)
daemon = config.setdefault("daemon", {})
relay = daemon.get("relay", {})
relay["enabled"] = True
relay["endpoint"] = os.environ["PASEO_RELAY_ENDPOINT"]
relay["useTls"] = os.environ["PASEO_RELAY_TLS"] == "true"
daemon["relay"] = relay
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")
EOF
	echo "relay wired in $PASEO_CONFIG: $RELAY (tls $RELAY_TLS, outbound only — no port opened)" >&2

	# A quiet idle daemon: dictation/voice download models nobody asked for
	# on a headless box (the PoC found the pairing RPC racing a model
	# extraction otherwise). The web UI stays off: the panel in the chat is
	# the surface, and a local UI is one more door to mind.
	#
	# Detached, not --foreground: a setup script must return. nohup so the
	# daemon outlives this shell; the relay reconnects with backoff if the
	# network was not up yet, so no wait-for-health loop here — status
	# below is the check, and its failure is the error.
	# shellcheck disable=SC2086
	if [ "$RELAY_TLS" = "true" ]; then
		TLS_FLAG="--relay-use-tls"
	else
		TLS_FLAG=""
	fi
	PASEO_DICTATION_ENABLED=false PASEO_VOICE_MODE_ENABLED=false \
		nohup paseo daemon start --no-web-ui --relay $TLS_FLAG \
		>/tmp/paseo-daemon.log 2>&1 &
	disown
	sleep 5
	paseo daemon status >/dev/null 2>&1 || {
		echo "error: the daemon did not become healthy (see /tmp/paseo-daemon.log); run 'paseo daemon status' for detail" >&2
		exit 6
	}
fi

# ------------------------------------------------------------------ #
# axis 2: enrollment and the shim                                      #
# ------------------------------------------------------------------ #
if [ "$SKIP_LLM" -eq 1 ]; then
	echo "skipping the LLM axis (--skip-llm)" >&2
else
	# opencode itself: pinned for the same reason as everything here.
	if ! command -v opencode >/dev/null; then
		echo "installing opencode-ai@$OPENCODE_VERSION" >&2
		HOME="$HOME" npm install -g --no-audit --no-fund "opencode-ai@$OPENCODE_VERSION"
	else
		echo "opencode already on PATH — not reinstalling" >&2
	fi

	# The enrollment CLI: build from this checkout when no binary yet. The
	# module's own .gitignore names the artifact pystino-enroll — build
	# with that name so `go build ./...` and this script agree on one.
	ENROLL_BIN="$ENROLL_DIR/pystino-enroll"
	if [ ! -x "$ENROLL_BIN" ]; then
		# go.mod requires 1.24; an older go would try to auto-download the
		# toolchain and die with "toolchain not available" — check the
		# version here so the failure names the remedy.
		GO_MINOR="$(go version 2>/dev/null | grep -oE 'go1\.[0-9]+' | head -1 | cut -d. -f2 || true)"
		[ -n "$GO_MINOR" ] && [ "$GO_MINOR" -ge 24 ] || {
			echo "error: building the enroll CLI needs go 1.24+ (apt's golang is usually older — install the official tarball from https://go.dev/dl/, or place a built binary at $ENROLL_BIN)" >&2
			exit 5
		}
		echo "building the enroll CLI" >&2
		(cd "$ENROLL_DIR" && go build -o pystino-enroll .)
	fi

	# enroll writes the global opencode.json (the daemon's opencode reads
	# the user config, not a per-project one) and stores the refresh
	# credential beside opencode's own state. --issuer only when explicitly
	# given: the issuer is deployment-specific (bundled Authelia answers
	# under <origin>/authelia, Keycloak under <origin>/idp/realms/pystino),
	# and a forced default would point discovery at the wrong document —
	# enroll prompts for it instead, which is the honest failure.
	mkdir -p "$OPENCODE_DIR"
	ENROLL_ARGS=(enroll --gateway "$GATEWAY" --output "$OPENCODE_CONFIG")
	if [ -n "$ISSUER" ]; then ENROLL_ARGS+=(--issuer "$ISSUER"); fi
	if [ "$ASSUME_YES" -eq 1 ]; then ENROLL_ARGS+=(--yes); fi
	if [ "$ALLOW_OPENCODE_PROVIDER" -eq 1 ]; then ENROLL_ARGS+=(--allow-opencode-provider); fi
	"$ENROLL_BIN" "${ENROLL_ARGS[@]}"

	# The shim: the LLM axis's local owner of the token opencode cannot
	# hold. nohup + disown: it must outlive this script, and the machine's
	# process supervisor is out of scope here (a systemd unit belongs to
	# the operator's packaging, not this setup).
	if pgrep -f "pystino-enroll serve" >/dev/null 2>&1; then
		echo "an 'enroll serve' is already running — leaving it" >&2
	else
		nohup "$ENROLL_BIN" serve >/tmp/enroll-serve.log 2>&1 &
		disown
		sleep 1
		pgrep -f "pystino-enroll serve" >/dev/null || {
			echo "error: the shim failed to start (see /tmp/enroll-serve.log)" >&2
			exit 6
		}
		echo "shim running (log: /tmp/enroll-serve.log); opencode's baseURL points at it" >&2
	fi
fi

# ------------------------------------------------------------------ #
# the posture                                                          #
# ------------------------------------------------------------------ #
if [ "$SKIP_POSTURE" -eq 1 ]; then
	echo "skipping the posture (--skip-posture)" >&2
else
	# The permission posture belongs in the machine's opencode config: the
	# daemon drops per-prompt permission rules (verified in the PoC), so
	# ask-on-edit/bash here is what makes the panel's PermissionCard the
	# gate it claims to be. Merged, not overwritten — the config enroll just
	# wrote (or an existing one) keeps its provider block.
	OPENCODE_CONFIG="$OPENCODE_CONFIG" python3 - <<'EOF'
import json, os
path = os.environ["OPENCODE_CONFIG"]
config = {}
if os.path.exists(path):
    with open(path) as f:
        config = json.load(f)
config["permission"] = {"edit": "ask", "bash": "ask"}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")
EOF
	echo "posture set in $OPENCODE_CONFIG: edit/bash ask (the panel approves, not the machine)" >&2
fi

# ------------------------------------------------------------------ #
# the pairing                                                          #
# ------------------------------------------------------------------ #
if [ "$SKIP_DAEMON" -eq 1 ]; then
	exit 0
fi
echo
echo "=== last step, and only yours ===" >&2
echo "Run:  paseo daemon pair" >&2
echo "Paste the link it prints into the chat's /code > Pair a device dialog." >&2
echo "The offer is the credential: serverId + the daemon's public key, E2EE" >&2
echo "from the chat server to this machine — the relay relays, it never reads." >&2
