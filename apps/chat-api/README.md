# chat-api

The chat application's backend: conversations, messages, and — as the
milestones land — assistants, knowledge bases and the tool loop.

## What it is, and what it deliberately is not

It is a **client of the gateway**. Every model call is an ordinary metered
`/v1` request, made with the access token of the person whose message it is
([ADR 0040](../../docs/adr/0040-bearer-tokens-on-v1.md)). So quotas, model
access, redaction and the ledger apply to a chat turn exactly as they apply to
an API call, because it is one.

It is **not** a second path into providers, and it holds no API key. It does not
import from `gateway`; the two share a PostgreSQL server and a Keycloak realm
and nothing else. That boundary is the whole reason this is a separate service —
see [docs/phase-3-plan.md](../../docs/phase-3-plan.md).

Account management — spend, API keys, quotas, providers — is the console's, and
the chat UI links to it rather than reimplementing it.

## Running it

Part of the compose stack:

```bash
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.keycloak.yml \
  -f deploy/compose/docker-compose.chat.yml up -d --build
```

The gateway must have `GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE` set, and the realm's
`llm-chat` client must carry the audience mapper naming it — without both, every
call from here is refused with the same message a bad API key gets.
`scripts/test_bearer_tokens_live.py` checks exactly that.
