#!/usr/bin/env bash
# Point opencode at the Pystino gateway with a pasted API key (ADR 0010).
#
# The human mints a `gwk_...` key in the browser console first and pastes it
# here; this script never mints keys because key minting (POST /api/me/keys)
# is session-cookie only — a bearer client cannot call it. It writes an
# opencode.json wiring the gateway's OpenAI-compatible /v1 as a provider, so
# agent usage lands in the same billing/quota ledger as chat usage.
#
# Inputs are prompts, flags, or environment — never deploy/.env, which does
# not exist in every checkout (gitignored, main checkout only):
#
#   ./install.sh [--base-url https://llm.example.org/v1] [--output opencode.json]
#              [--model <id>] [--yes] [--no-discover]
#
#   PYSTINO_API_KEY  the pasted key (avoids typing it twice when scripting).
#   PYSTINO_BASE_URL the gateway /v1 base URL (same as --base-url).
#
# The key is never taken as an argv flag: argv leaks through ps and shell
# history, and a billing credential is exactly what must not. It travels via
# a hidden prompt or the environment, and lands only in the written file
# (chmod 600) — nothing here is committed anywhere.
#
# Only bash, curl (or python3 as fallback), and python3. No network except
# one GET against the gateway itself for model discovery.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/opencode.json.template"

BASE_URL="${PYSTINO_BASE_URL:-}"
OUTPUT="./opencode.json"
WANT_MODEL=""
ASSUME_YES=0
DISCOVER=1

usage() {
	cat <<EOF
Usage: $(basename "$0") [options]

Writes an opencode.json pointing opencode at a Pystino gateway /v1.

Options:
  --base-url URL   gateway /v1 base URL (e.g. https://llm.example.org/v1).
                   A bare origin without /v1 gets /v1 appended. Also read
                   from PYSTINO_BASE_URL; prompted when neither is set.
  --output PATH    where to write (default: ./opencode.json). Point this at
                   ~/.config/opencode/opencode.json for a global install.
  --model ID       default model id; picked from GET /v1/models when omitted.
  --yes            skip the overwrite confirmation for an existing file.
  --no-discover    skip GET /v1/models and write a placeholder models map.
  --help           this text.

The API key comes from PYSTINO_API_KEY or a hidden prompt — never argv.
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
	--base-url)
		BASE_URL="${2:?--base-url needs a value}"
		shift 2
		;;
	--output)
		OUTPUT="${2:?--output needs a value}"
		shift 2
		;;
	--model)
		WANT_MODEL="${2:?--model needs a value}"
		shift 2
		;;
	--yes) ASSUME_YES=1; shift ;;
	--no-discover) DISCOVER=0; shift ;;
	--help) usage; exit 0 ;;
	*)
		echo "unknown option: $1 (see --help)" >&2
		exit 2
		;;
	esac
done

command -v python3 >/dev/null || {
	echo "error: python3 is required (JSON rendering + discovery parsing)" >&2
	exit 5
}
[ -f "$TEMPLATE" ] || {
	echo "error: template missing: $TEMPLATE" >&2
	exit 5
}

# The /v1 base URL, normalised: trailing slashes stripped, /v1 ensured. Users
# paste whatever the console shows them — origin or full path — and a missing
# /v1 would otherwise send chat completions at the wrong path.
if [ -z "$BASE_URL" ]; then
	if [ -t 0 ]; then
		printf 'Gateway /v1 base URL (e.g. https://llm.example.org/v1): ' >&2
		IFS= read -r BASE_URL || {
			echo "error: no base URL given" >&2
			exit 2
		}
	else
		echo "error: set --base-url or PYSTINO_BASE_URL (stdin is not a terminal)" >&2
		exit 2
	fi
fi
BASE_URL="${BASE_URL%/}"
case "$BASE_URL" in
http://* | https://*) ;;
*)
	echo "error: base URL must be absolute http(s): $BASE_URL" >&2
	exit 2
	;;
esac
case "$BASE_URL" in
*/v1) ;;
*)
	BASE_URL="$BASE_URL/v1"
	echo "note: using $BASE_URL (/v1 appended)" >&2
	;;
esac

# The key: env first so scripts can run unattended, hidden prompt otherwise.
# Nothing is echoed — the file it lands in is chmod 600 below.
API_KEY="${PYSTINO_API_KEY:-}"
if [ -z "$API_KEY" ]; then
	if [ -t 0 ]; then
		printf 'Paste your gateway API key (minted in the console, starts with gwk_): ' >&2
		IFS= read -rs API_KEY || {
			echo "error: no key given" >&2
			exit 2
		}
		echo >&2
	else
		echo "error: set PYSTINO_API_KEY (stdin is not a terminal)" >&2
		exit 2
	fi
fi
case "$API_KEY" in
gwk_*) ;;
*)
	echo "warning: key does not start with gwk_ — continuing, the gateway will judge it" >&2
	;;
esac

if [ -e "$OUTPUT" ] && [ "$ASSUME_YES" -ne 1 ]; then
	if [ -t 0 ]; then
		printf '%s exists; overwrite? [y/N] ' "$OUTPUT" >&2
		IFS= read -r answer || answer=""
		case "$answer" in
		[yY]*) ;;
		*)
			echo "aborted" >&2
			exit 1
			;;
		esac
	else
		echo "error: $OUTPUT exists; pass --yes to overwrite (stdin is not a terminal)" >&2
		exit 2
	fi
fi

# Discovery doubles as key validation: GET /v1/models answers the caller's
# allowed catalogue, and its status is the only key health signal /v1 offers
# — there is no /v1/usage and no rate-limit headers (only retry-after on a
# real 429), so spend is never shown here; that lives in the console.
MODELS_JSON=""
DISCOVER_STATUS=""
if [ "$DISCOVER" -eq 1 ]; then
	if command -v curl >/dev/null; then
		CURL_OUT="$(curl -sS -m 20 -o /tmp/opencode-models.$$.json -w '%{http_code}' \
			-H "Authorization: Bearer $API_KEY" \
			"$BASE_URL/models" 2>/tmp/opencode-curl-err.$$.txt || true)"
		if [ -n "$CURL_OUT" ]; then
			if [ "$CURL_OUT" = "000" ]; then
				DISCOVER_STATUS="unreachable: $(cat /tmp/opencode-curl-err.$$.txt 2>/dev/null || echo curl failed)"
			else
				DISCOVER_STATUS="$CURL_OUT"
			fi
			if [ "$CURL_OUT" = "200" ]; then
				MODELS_JSON="$(python3 -c '
import json,sys
doc=json.load(open(sys.argv[1]))
out={}
for m in doc.get("data",[]):
    mid=m.get("id")
    if not mid: continue
    entry={"name": m.get("display_name") or mid}
    limit={}
    try:
        cw=int(m.get("context_window") or 0)
        if cw>0: limit["context"]=cw
    except (TypeError,ValueError): pass
    try:
        mo=int(m.get("max_output_tokens") or 0)
        if mo>0: limit["output"]=mo
    except (TypeError,ValueError): pass
    if limit: entry["limit"]=limit
    out[mid]=entry
print(json.dumps(out))' "/tmp/opencode-models.$$.json")"
			fi
		else
			DISCOVER_STATUS="unreachable: $(cat /tmp/opencode-curl-err.$$.txt 2>/dev/null || echo curl failed)"
		fi
		rm -f /tmp/opencode-models.$$.json /tmp/opencode-curl-err.$$.txt
	else
		# No curl (minimal boxes): the same GET through the stdlib.
		DISCOVER_STATUS="$(PYSTINO_KEY="$API_KEY" PYSTINO_BASE="$BASE_URL" python3 -c '
import json,os,urllib.request,urllib.error
try:
    req=urllib.request.Request(os.environ["PYSTINO_BASE"]+"/models",
        headers={"Authorization":"Bearer "+os.environ["PYSTINO_KEY"]})
    with urllib.request.urlopen(req,timeout=20) as r:
        open("/tmp/opencode-models.py.json","wb").write(r.read())
    print("200")
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print("unreachable: %s" % e)' 2>/dev/null || true)"
		if [ "$DISCOVER_STATUS" = "200" ]; then
			MODELS_JSON="$(python3 -c '
import json
doc=json.load(open("/tmp/opencode-models.py.json"))
out={}
for m in doc.get("data",[]):
    mid=m.get("id")
    if not mid: continue
    entry={"name": m.get("display_name") or mid}
    limit={}
    try:
        cw=int(m.get("context_window") or 0)
        if cw>0: limit["context"]=cw
    except (TypeError,ValueError): pass
    try:
        mo=int(m.get("max_output_tokens") or 0)
        if mo>0: limit["output"]=mo
    except (TypeError,ValueError): pass
    if limit: entry["limit"]=limit
    out[mid]=entry
print(json.dumps(out))')"
			rm -f /tmp/opencode-models.py.json
		fi
	fi
fi

case "$DISCOVER_STATUS" in
401 | 403)
	# The gateway refuses bad keys with 401/403 on every /v1 route: the
	# paste is wrong or revoked. Writing a config around it would only
	# fail later inside opencode, so stop here.
	echo "error: gateway refused the key (HTTP $DISCOVER_STATUS)." >&2
	echo "Mint a fresh key in the console and try again — check for a truncated paste." >&2
	exit 3
	;;
429)
	# 429 is the quota door, not an auth failure: the key is fine but its
	# billing group hit the cap. Nothing to write until spend frees up.
	echo "error: gateway reports quota exhausted (HTTP 429)." >&2
	echo "The key works; its billing group is over cap. Wait for the next window" >&2
	echo "or raise the cap in the console, then re-run this script." >&2
	exit 4
	;;
200)
	COUNT="$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])))' "$MODELS_JSON")"
	echo "discovered $COUNT model(s) for this key via GET $BASE_URL/models" >&2
	;;
"")
	if [ "$DISCOVER" -eq 0 ]; then
		echo "note: discovery skipped (--no-discover); writing a placeholder models map." >&2
	else
		echo "warning: discovery produced no answer; writing a placeholder models map." >&2
	fi
	;;
unreachable*)
	echo "warning: model discovery failed ($DISCOVER_STATUS)." >&2
	echo "warning: writing a placeholder models map — replace it with ids from GET $BASE_URL/models once reachable." >&2
	;;
*)
	echo "warning: GET /models answered HTTP $DISCOVER_STATUS; writing a placeholder models map." >&2
	;;
esac

if [ -z "$MODELS_JSON" ]; then
	MODELS_JSON='{"REPLACE-WITH-MODEL-ID": {"name": "Replace with a model id from GET /v1/models"}}'
fi

# The default model: explicit --model wins when it exists, otherwise ask on a
# terminal, otherwise the first discovered id. A wrong default is harmless
# (opencode lets you switch with /models), a missing one is not.
DEFAULT_MODEL="$WANT_MODEL"
if [ -n "$DEFAULT_MODEL" ]; then
	python3 -c 'import json,sys; sys.exit(0 if sys.argv[2] in json.loads(sys.argv[1]) else 1)' \
		"$MODELS_JSON" "$DEFAULT_MODEL" || {
		echo "warning: --model $DEFAULT_MODEL not in the discovered list; keeping it anyway" >&2
	}
elif [ -t 0 ] && [ "$DISCOVER_STATUS" = "200" ]; then
	echo "Available models:" >&2
	python3 -c 'import json,sys; [print("  %d) %s"%(i+1,m)) for i,m in enumerate(json.loads(sys.argv[1]))]' "$MODELS_JSON" >&2
	printf 'Pick a default model [1]: ' >&2
	IFS= read -r pick || pick=""
	pick="${pick:-1}"
	DEFAULT_MODEL="$(python3 -c 'import json,sys; ids=list(json.loads(sys.argv[1])); print(ids[int(sys.argv[2])-1] if sys.argv[2].isdigit() and 1<=int(sys.argv[2])<=len(ids) else ids[0])' "$MODELS_JSON" "$pick")"
else
	DEFAULT_MODEL="$(python3 -c 'import json,sys; print(next(iter(json.loads(sys.argv[1]))))' "$MODELS_JSON")"
fi
echo "default model: $DEFAULT_MODEL" >&2

# Render through the template so the file's shape is defined once: the two
# string slots are JSON-escaped by construction, and the models slot is a
# parsed object (a corrupt discovery payload fails here, not in opencode).
PYSTINO_KEY="$API_KEY" PYSTINO_BASE="$BASE_URL" PYSTINO_MODELS="$MODELS_JSON" \
	PYSTINO_DEFAULT="$DEFAULT_MODEL" PYSTINO_TEMPLATE="$TEMPLATE" PYSTINO_OUT="$OUTPUT" \
	python3 - <<'EOF'
import json, os
with open(os.environ["PYSTINO_TEMPLATE"]) as f:
    template = f.read()
models = json.loads(os.environ["PYSTINO_MODELS"])
rendered = template.replace("__BASE_URL__", json.dumps(os.environ["PYSTINO_BASE"])[1:-1])
rendered = rendered.replace("__API_KEY__", json.dumps(os.environ["PYSTINO_KEY"])[1:-1])
rendered = rendered.replace('"__MODELS__"', json.dumps(models, indent=2))
doc = json.loads(rendered)  # the file must parse before it is written
with open(os.environ["PYSTINO_OUT"], "w") as f:
    json.dump(doc, f, indent=2)
    f.write("\n")
EOF
chmod 600 "$OUTPUT"

# Deliberately no x-bill-to anywhere in the file: a key's billing group is
# fixed at mint time and the gateway refuses a key that sends the header
# (ADR 0061). Usage attribution needs no client help.
KEY_PREFIX="$(printf '%s' "$API_KEY" | cut -c1-8)"
echo "wrote $OUTPUT (provider pystino, key ${KEY_PREFIX}..., default $DEFAULT_MODEL)" >&2
echo "spend is not visible to opencode — /v1 has no usage endpoint; watch it in the console." >&2
echo "if opencode later answers 401/403, the key was revoked: mint a new one and re-run." >&2
echo "if it answers 429 with retry-after, the billing group hit its cap: wait or raise it." >&2
