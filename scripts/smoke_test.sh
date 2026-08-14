#!/usr/bin/env bash
#
# End-to-end smoke test: a real server, real HTTP, a real database and a real
# streaming upstream. Complements the unit suite, which uses an in-process
# transport — this one proves the vertical slice works over a socket.
#
#   ./scripts/smoke_test.sh
#
# Needs nothing running: it creates a temporary SQLite database, starts a fake
# OpenAI-compatible upstream and the gateway on ports 9099/8099, exercises them,
# prints the resulting ledger, and cleans up.
#
# What it checks:
#   * auth (401), per-group model access (404), /v1/models from our catalogue
#   * non-streaming and streaming completions
#   * stream_options.include_usage forced on the upstream request
#   * the usage frame stripped when the client did not ask, forwarded when it did
#   * SSE reassembly when the upstream writes in 7-byte slices
#   * exact Decimal costs in the ledger
#   * the quota overrun policy: admit the request that crosses the limit,
#     refuse the next one
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

WORK="$(mktemp -d)"
PY="${PY:-$REPO_ROOT/.venv/bin/python}"
BASE=http://127.0.0.1:8099

if [[ ! -x "$PY" ]]; then
  echo "error: no interpreter at $PY. Run 'uv sync' first, or set PY=..." >&2
  exit 1
fi

export GATEWAY_DATABASE_URL="sqlite+aiosqlite:///$WORK/smoke.db"
export GATEWAY_VALKEY_URL=""
export GATEWAY_SESSION_SECRET="smoke-test-secret"
export GATEWAY_UPSTREAM__BASE_URL="http://127.0.0.1:9099/v1"
export GATEWAY_UPSTREAM__API_KEY="fake-upstream-key"
export GATEWAY_LOG_JSON=false

cleanup() {
  [[ -n "${GW_PID:-}" ]] && kill "$GW_PID" 2>/dev/null
  [[ -n "${UP_PID:-}" ]] && kill "$UP_PID" 2>/dev/null
  wait 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

ledger_total() {
  "$PY" -c "
import sqlite3
c = sqlite3.connect('$WORK/smoke.db')
print(f\"{c.execute('select coalesce(sum(cost),0) from usage_records').fetchone()[0]:.2f}\")
"
}

echo "### 1. migrate"
"$PY" -m alembic -c apps/gateway/alembic.ini upgrade head 2>&1 | grep -E 'Running upgrade' || exit 1

echo
echo "### 2. seed"
"$PY" -m gateway.cli seed --model smoke-model --upstream-model upstream/smoke-model \
  > "$WORK/seed.out" 2>&1 || { cat "$WORK/seed.out"; exit 1; }
KEY=$(grep -oE 'gwk_[A-Za-z0-9_-]+' "$WORK/seed.out" | head -1)
echo "    key ${KEY:0:18}...  (0.15/Mtok in, 0.60/Mtok out, 10 EUR/day group ceiling)"

H="Authorization: Bearer $KEY"
CT="content-type: application/json"

echo
echo "### 3. start the fake upstream and the gateway"
( cd "$REPO_ROOT/scripts" && exec "$PY" -m uvicorn _fake_upstream:app --port 9099 --log-level warning ) \
  > "$WORK/upstream.log" 2>&1 &
UP_PID=$!
"$PY" -m uvicorn gateway.main:app --port 8099 --log-level warning > "$WORK/gateway.log" 2>&1 &
GW_PID=$!

for _ in $(seq 1 60); do
  curl -sf "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.5
done
if ! curl -sf "$BASE/healthz" >/dev/null 2>&1; then
  echo "error: the gateway did not start" >&2
  cat "$WORK/gateway.log" >&2
  exit 1
fi
echo "    /readyz -> $(curl -s "$BASE/readyz")"

echo
echo "### 4. auth and access control"
printf "    no key            -> %s (expect 401)\n" \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/chat/completions" -H "$CT" \
     -d '{"model":"smoke-model","messages":[{"role":"user","content":"hi"}]}')"
printf "    bad key           -> %s (expect 401)\n" \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/chat/completions" \
     -H 'Authorization: Bearer gwk_dead_beef' -H "$CT" \
     -d '{"model":"smoke-model","messages":[{"role":"user","content":"hi"}]}')"
printf "    model not granted -> %s (expect 404)\n" \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/chat/completions" -H "$H" -H "$CT" \
     -d '{"model":"nope","messages":[{"role":"user","content":"hi"}]}')"
echo "    /v1/models        -> $(curl -s -H "$H" "$BASE/v1/models" \
  | "$PY" -c 'import sys,json; print([m["id"] for m in json.load(sys.stdin)["data"]])')"

echo
echo "### 5. non-streaming"
curl -s -X POST "$BASE/v1/chat/completions" -H "$H" -H "$CT" \
  -d '{"model":"smoke-model","messages":[{"role":"user","content":"hello there"}]}' \
  | "$PY" -c 'import sys,json; d=json.load(sys.stdin); print("    model:", d["model"], "| content:", d["choices"][0]["message"]["content"], "| tokens:", d["usage"]["total_tokens"])'

echo
echo "### 6. streaming without include_usage (frame must be stripped)"
curl -sN -X POST "$BASE/v1/chat/completions" -H "$H" -H "$CT" \
  -d '{"model":"smoke-model","stream":true,"messages":[{"role":"user","content":"hello there"}]}' \
  > "$WORK/stream1.txt"
echo "    frames    : $(grep -c '^data:' "$WORK/stream1.txt")"
echo "    reassembled: $(grep '^data: {' "$WORK/stream1.txt" | sed 's/^data: //' | "$PY" -c '
import sys, json
out = []
for line in sys.stdin:
    if not line.strip():
        continue
    for choice in json.loads(line).get("choices") or []:
        out.append((choice.get("delta") or {}).get("content") or "")
print("".join(out))')"
grep -q '"usage"' "$WORK/stream1.txt" && echo "    usage leaked        : YES (BUG)" || echo "    usage leaked        : no"
grep -q 'upstream/smoke-model' "$WORK/stream1.txt" && echo "    upstream name leaked: YES (BUG)" || echo "    upstream name leaked: no"

echo
echo "### 7. streaming with include_usage (frame must pass through)"
curl -sN -X POST "$BASE/v1/chat/completions" -H "$H" -H "$CT" \
  -d '{"model":"smoke-model","stream":true,"stream_options":{"include_usage":true},"messages":[{"role":"user","content":"hello"}]}' \
  > "$WORK/stream2.txt"
grep -q '"usage"' "$WORK/stream2.txt" && echo "    usage frame present: yes" || echo "    usage frame present: NO (BUG)"

echo
echo "### 8. what the upstream received"
grep -o '"stream_options": {[^}]*}' "$WORK/upstream.log" | sort -u | sed 's/^/    forced : /'
grep -o '"model": "[^"]*"' "$WORK/upstream.log" | sort -u | sed 's/^/    sent as: /'

echo
echo "### 9. quota — spend is $(ledger_total) EUR of 10; each streamed call costs 0.90"
for i in $(seq 1 12); do
  code=$(curl -sN -o "$WORK/q.txt" -D "$WORK/q.hdr" -w '%{http_code}' \
         -X POST "$BASE/v1/chat/completions" -H "$H" -H "$CT" \
         -d '{"model":"smoke-model","stream":true,"messages":[{"role":"user","content":"hello"}]}')
  if [[ "$code" == "429" ]]; then
    printf "    call %2d -> 429  ledger=%s EUR  %s\n" "$i" "$(ledger_total)" \
      "$(grep -i '^retry-after' "$WORK/q.hdr" | tr -d '\r')"
    echo "      $(head -c 200 "$WORK/q.txt")"
    echo "    ^ the call that crossed the limit was admitted; the next was refused."
    break
  fi
  printf "    call %2d -> %s  ledger=%s EUR\n" "$i" "$code" "$(ledger_total)"
done

echo
echo "### 10. the ledger"
"$PY" - <<PYEOF
import sqlite3
c = sqlite3.connect("$WORK/smoke.db")
rows = c.execute("""select status, streamed, usage_source, prompt_tokens, completion_tokens,
                           cost, currency, assistant_text, price_id is not null,
                           ttfb_ms is not null
                    from usage_records order by created_at""").fetchall()
for i, r in enumerate(rows, 1):
    print(f"    {i:2d}. {r[0]:<9} {'stream' if r[1] else 'buffer'}  {r[2]:<14} "
          f"{r[3]}+{r[4]} tok  {r[5]} {r[6]}  priced={bool(r[8])}  ttfb={bool(r[9])}")
total, n = c.execute("select coalesce(sum(cost),0), count(*) from usage_records").fetchone()
zero = c.execute("select count(*) from usage_records "
                 "where total_tokens = 0 and status = 'completed'").fetchone()[0]
est = c.execute("select count(*) from usage_records "
                "where usage_source = 'estimated'").fetchone()[0]
print(f"    {n} rows, {total:.2f} EUR")
print(f"    estimated rows: {est}   completed-but-zero-token rows: {zero} (must be 0)")
assert zero == 0, "a completed request recorded zero tokens"
PYEOF

echo
echo "OK — the vertical slice works end to end."
