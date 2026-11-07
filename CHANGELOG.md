# Changelog

Notable changes to Pystino for operators of the gateway, newest first. Each release is a git tag `vX.Y.Z`; "Paired with Cerea" names the Cerea release it was tested with. Versions before 0.24.0 were reconstructed from the project history.

## Unreleased

- **Console tables stack on a phone.** Providers, quotas, quota usage, users and the audit log draw each row as a labelled card under 640 px instead of clipping the action column, the usage bar or the dates.
- **Developer detail is folded away.** The console build hash, `.env` and `./configure` notes and the search backend's endpoint (with its ADR number) moved into collapsed **About this server** and **How it works** disclosures on Overview, Settings and Search; the card descriptions read in plain words.

## v0.29.1 — 2026-11-06

Paired with Cerea 0.44.0.

- **The same upstream id at two providers imports.** A model is identified upstream by (provider, upstream id), but the import's "already in the catalogue" check was deployment-wide, so importing `clef-flash`, `qwen3-embedding-8b` or `whisper-large-v3` from Melious was refused when Cortecs already had it, even with a prefix. The check is now per provider (this provider already has that id); the name check stays global. Discovery, drift ("no longer offered"), prices and routing already key on the provider or the model row, and no database constraint spans providers, so no migration.

## v0.29.0 — 2026-11-06

Paired with Cerea 0.43.0.

- **Reranking models as a model kind.** A new kind **rerank** and `POST /v1/rerank` in the Cohere and Jina shape (`query`, `documents` as strings or `{"text"}`, `top_n`, `return_documents`; `results` with `index` and `relevance_score`, best first; `usage`). Metered per input token like an embedding, priced at the input rate, redacted (query and documents) and routed, granted and quota'd like every other kind; `GET /v1/models` reports `kind: "rerank"`. Provider differences are normalised: documents always go upstream as strings (Cortecs refuses objects), `return_documents` is applied by the gateway, and usage is read from Cortecs's `usage` or Melious's `meta.billed_units`. Limits: `GATEWAY_RERANK__MAX_DOCUMENTS` (1000) and `GATEWAY_RERANK__MAX_TOTAL_BYTES` (2 MiB), 400 beyond. Discovery maps Cortecs's `Rerank` models and Melious's reranker (`qwen3-reranker-4b`) to the kind; the console model pages offer it. No migration. Details: [Reranking](https://paoloviviani.github.io/Pystino/gateway/#reranking-v1rerank).

- **Decision dialects, translated both ways.** A decision provider declares the dialects it speaks natively, each with its path, as data (`DecisionSupport.paths`: Cortecs `decisions` and `systemone`, Melious `systemone` only), and the gateway translates between them as it does between chat completions and Messages (`gateway.translate.decisions`). `/v1/decisions` (the `input` form) and `/v1/systemone` (the `state` form) now work against **any** decision model: the request and the answer are translated when the provider speaks the other dialect and forwarded untouched when it speaks the caller's. predicate is noul, a choice's `choices` are `criteria` keys, a score's `levels` an ordered `criteria` array; `preference` and any question type without a counterpart are a 400 naming them. **Behaviour change:** an `input`-form request carrying `preference` to a Melious model is a 400 (it used to be dropped). Nothing else changes for Cortecs, which still gets both forms as sent. `GET /v1/models` lists `decision_forms` (both) on each decision model. The Melious plugin's own translation moved into the shared module.
- **Melious as a provider.** A built-in catalogue entry (`https://api.melious.ai/v1`, region EU, no training, no content logging) and a small `melious` plugin on the OpenAI-compatible protocol. Discovery reads kinds, limits and EUR prices from its `/v1/models?include_meta=true` (image and speech-to-text models are priced per image and per minute; the reranker is kind rerank). Its decision models (`clef`, `clef-flash`) exist only on System One, so `/v1/decisions` with the `input` form is translated to `/v1/systemone` and the answer back (predicate, choice and score), and a client written for the Cortecs shape works unchanged. `scripts/check_melious_live.py` re-checks each kind against a real key.

## v0.28.0 — 2026-11-06

Paired with Cerea 0.42.2.

- **Label requests, read back your own usage by label.** A `/v1` request (chat completions, responses, messages) may carry an `x-pystino-tag` header (printable ASCII, at most 128 characters; an invalid one is ignored); the usage row stores it (migration 0079). `GET /v1/pystino/usage/tags?tag=…` returns the caller's own totals per tag, optionally between `since` and `until`. Only the caller's own requests are ever read; bring-your-own-key and in-flight requests are left out. Cerea's coding agent uses it to show a session's and a turn's cost as metered by the gateway.

## v0.27.0 — 2026-11-05

Paired with Cerea 0.41.0.

- **Model discovery always reads the provider's whole catalogue.** The console's "Catalogue tag" box is gone, and so are `GET /api/admin/models/tags`, `GET /api/me/models/tags` and the `tag` parameter of `models/discover` and `models/import`. A provider whose plugin names an "everything" spelling (Cortecs: `tag=all`) is fetched with it, so the listing covers every category and models served here under another Cortecs tag (decision, audio, image, embedding, OCR) are no longer reported as "no longer offered upstream".

## v0.26.0 — 2026-11-05

Paired with Cerea 0.40.1.

- **Group capability: console access.** A new `console` capability, per group (console **Groups** and **Settings**, `ACCESS_CONSOLE`), lets you give users the chat and nothing else: without it a user's console visit is redirected to the chat and `/api/me/*` answers 403 `console_not_allowed`, while their `/v1` access is untouched. **On for every existing group and by default for new ones** (migration 0078), so nothing changes until you turn it off; administrators always have the console. Bring your own key needs it: `capabilities.byok` reads false without it. `/v1/me` and `/api/me` report `capabilities.console`. Opening the console without it redirects to the chat's usage page (`GATEWAY_CHAT__URL`, which the Cerea kit sets to `<PUBLIC_ORIGIN>/chat`); with no chat URL a plain "managed through the chat" page is shown instead. Details: [Identity](https://paoloviviani.github.io/Pystino/identity/#console-access).
- **Decision models: discovery and live check.** Importing from Cortecs with the tag `Decision` lists `clef-flash` and `laya` as kind **Decision**, priced from the catalogue. `scripts/test_decisions_live.py` checks the routes, the reported micro-EUR cost, quota settlement and the refusals against a real Cortecs key. (ADR 0100)

## v0.25.0 — 2026-11-04

Paired with Cerea 0.39.0.

- Console: **Your providers and models** now opens by naming the feature ("Bring your own key").

## v0.24.0 — 2026-11-03

Pairs with Cerea 0.39.0, whose deploy kit pins it (tested with the released Cerea 0.38.1). Images: `ghcr.io/paoloviviani/pystino-gateway:0.24.0`, `ghcr.io/paoloviviani/pystino-redaction:0.24.0-pattern`.

**To update:** with Cerea's kit, update to Cerea 0.39.0 (`cd ~/cerea/kit && git pull && docker compose pull && docker compose up -d`). With Pystino's own `deploy/`: `git pull && docker compose pull && docker compose up -d`. A database migration (0077) runs on start.

### Security

- **Redaction could let a re-sent text through unredacted after a policy change.** The detection cache did not tell "nothing enabled" apart from "everything", so a text first seen while no redaction rule applied stayed cached as clean after an administrator added a catch-all rule, and conversation history re-sent on later turns went upstream unredacted until the gateway restarted. Fixed; present since 0.1.0. If you rely on redaction, update. (#30)
- **CVE-2026-98432 (Linux kernel, critical):** the bundled Caddy proxy in `deploy/` no longer accepts Multipath TCP, the public exploit's path. This only narrows the exposure: patch the host kernel (Ubuntu 24.04: `linux` 6.8.0-149 or later), and until then drop IPv4 TCP packets carrying IP options on the host (rule in Cerea's `kit/CHANGELOG.md`, 0.39.0). (#29)

### Bring your own key

- **Users in a group with the new `byok` capability can add their own providers (with their own API key) and models**, and use them privately as `my/<name>` on every `/v1` surface. Nobody else sees or uses them. Off by default: an administrator turns it on per group (console **Groups**), or sets the default for new groups (**Settings**, or `ACCESS_BYOK`). (#25)
- **Usage on your own key is shown, not charged:** it is recorded for the user's information (in its own currency, never summed with the deployment's), never counted against quotas or billed to a group, and kept out of reports, CSVs and the request log. (#25)
- **Network guard:** a private provider may only reach public addresses, checked at connect time (no DNS rebinding window). The administrator switch `byok_private_networks` allows private networks; even then, loopback, link-local/metadata addresses, the stack's own service names and the gateway's own subnets stay refused. (#25, #26)
- **Console:** users get **Your providers and models** (the same screens administrators use) and see own-key usage apart; administrators get the capability on Groups and Settings, and a **Private providers** page to see every user's providers and models (never their keys) and disable or re-enable them, audit-logged. (#27)
- Details: [Bring your own key](https://paoloviviani.github.io/Pystino/byok/).

### For maintainers

- `scripts/test_byok_live.py`: a live check of BYOK end to end against a running stack. (#28)

## v0.23.0 — 2026-11-02

Paired with Cerea 0.37.2.

- **Anthropic providers serve chat completions.** Requests to `/v1/chat/completions` for an Anthropic provider are translated to Anthropic's native Messages API, with prompt caching, thinking, model discovery and a built-in price table (including one-hour cache writes). A defaulted `max_tokens` is capped at 32k. (#23)
- **Prices:** Haiku 5.5 is priced at its higher tier.
- **Console:** one Provider selector in the provider form, which now reads each plugin's protocol from the gateway; icons on the administration cards; quota resets in one place, on Quota usage. (#24)

## v0.22.1 — 2026-11-01

Paired with Cerea 0.35.1.

- A free image model shows €0.00 per image in pricing, not a dash. (#22)

## v0.22.0 — 2026-11-01

Paired with Cerea 0.35.1.

- **Image models import priced.** Cortecs image models are imported with a price per image and a per-extra-megapixel rate; billing uses the measured image size, and the basis used is recorded on the usage row. Reports flag image prices.

## v0.21.0 — 2026-10-31

Paired with Cerea 0.34.2.

- **Transcription is a model kind.** `/v1/audio/transcriptions` (and translations) are served, metered per minute of audio and routed like other kinds. Uploads are counted and parsing is bounded. Live-checked against Cortecs, whose transcription behaviour is documented.

## v0.20.0 — 2026-10-30

Paired with Cerea 0.32.6.

- **Redaction placeholders are restored everywhere.** They are put back in reasoning, in tool-call arguments (including streamed ones) and in every copy of the answer. The Responses `instructions` field is redacted too.
- **Echoed fields are screened on the way back in:** what a follow-up request repeats from an earlier answer is checked again.
- **The model is told what a placeholder is** when a request was rewritten, in a system note that follows the policy and stays cache-stable. An entity can use same-length placeholders.

## v0.19.0 — 2026-10-28

Paired with Cerea 0.32.2.

- **Reset one user's consumption of a per-user quota,** and a **Quota usage** page in the console per user. (migration 0074)
- **Images in tool results:** a new `tool_images` model feature, on by default for image-input models and switchable per model in the console.

## v0.18.1 — 2026-10-26

Paired with Cerea 0.28.0.

- **Fix:** document extraction gets its own timeout (`GATEWAY_EXTRACTOR__TIMEOUT_SECONDS`, default 180 s), so a large document may take minutes to read instead of failing at 10 seconds.
- Console: one shell width on every page, so the sidebar never moves; usage, summary and overview pages use the wide layout.

## v0.18.0 — 2026-10-26

Paired with Cerea 0.27.0.

- **A price per request for web-search backends.** Searches through a backend of the gateway's own (`POST /v1/search`, `POST /v1/search/{backend}`) were recorded at a cost of 0. An administrator now sets a price per request per backend on the Web search screen (optional per-tier override, in any currency the ECB publishes a rate for). The price is effective-dated like a model price, so a change never rewrites what an earlier search was billed. A price of 0 means "billed out of band"; a backend with no price is "unpriced". Upgrading bills nothing until a price is set, and earlier searches are not repriced. Reports show unpriced searches, and the usage CSV gains a trailing `unpriced_own_searches` column. Cost quotas count search cost. (migration 0072, additive; new endpoints `GET` and `POST /api/admin/search-prices`)
- **Console:** the redaction rule page reads top-down with a live summary and a test box, and previews an unsaved policy; the read-only provider catalogue list is gone from the Providers page (the catalogue itself stays).

## v0.17.2 — 2026-10-24

Paired with Cerea 0.23.1.

- **Security:** the console build stage uses Node 24.22.1 (Brotli decoder fix, CVE-2026-111497).
- Console: long values (redirect URIs, ids, tokens) wrap on phones; Presidio-only redaction settings look inactive when Presidio is not the engine in force.

## v0.17.1 — 2026-10-23

Paired with Cerea 0.23.0.

- **Provider facts are stated from dated primary sources:** built-in providers show region, retention and sources; routers say "Various". The console shows them consistently.
- **Endpoints:** Nebius on Token Factory and Mistral on its EU endpoint; the console flags a provider whose endpoint differs from its catalogue entry.
- Console: one type scale, named by role, on every page.

## v0.17.0 — 2026-10-23

Paired with Cerea 0.22.2.

### Changes

- **Local connectors capability.** A new group capability, `local_connectors`, beside `code_agents`; both are offered only when `GATEWAY_CHAT__ENABLED` is on.
- **Keycloak is an ordinary external OIDC provider.** It is no longer a bundled kind or a sync adapter; a group source fed by the Keycloak mirror moves to none when the adapter is withdrawn.
- `pystino idp check` accepts GitLab's device grant.
- Console: wider list pages, row actions behind one overflow menu, denser tables.

## v0.16.0 — 2026-10-22

Paired with Cerea 0.22.1.

- **Decision models:** `/v1/decisions` and `/v1/systemone` are served as a metered model kind, routed like OCR. (ADR 0100)
- **Fix:** workers starting together no longer race on the bundled-directory startup migration.
- Console: capability defaults are a "New groups" select with two matching bulk buttons.

## v0.15.0 — 2026-10-22

Paired with Cerea 0.21.1.

- **Provider catalogue in YAML,** with an operator override directory.
- **Reports by serving provider,** a daily report split by a second key, and usage charts in the console (amounts to the cent).

## v0.14.0 — 2026-10-22

Paired with Cerea 0.21.0.

- **Routing policies:** several targets per alias, a selection rule (the cheapest rule weighs input against output prices), stickiness and fallback. An upstream 401, 402 or account-shaped 403 falls back to the next target. A route editor in the Routing tab; the request log shows the attempt number.
- **Per-user quotas:** a global quota rule may apply to each user, and a user rule may override the each-user ceiling for that user. Console checkbox and "each user" option.
- Fix: an unknown "unavailable" reason no longer blanks the Routing tab.

## v0.13.0 — 2026-10-21

Paired with Cerea 0.16.2.

- **Admin audit log:** administrators' configuration changes are recorded, with a console page.
- **Request log:** one line per request, never any content, as an API and a console page.
- **Images are priced by size.**
- Fix: quota admission reads the clock after its rule query, not before. The usage report no longer blames a provider for a client disconnect, and deleting a user blanks their stored reply text.
- The opencode install command pins opencode 1.19.3.

## v0.12.0 — 2026-10-20

Paired with Cerea 0.16.1.

- Console: the navigation is a collapsible sidebar, grouped like the administration landing page.

## v0.11.1 — 2026-10-20

Paired with Cerea 0.16.0.

- Docs: one page for the billing-group rule (**Billing groups**), linked from everywhere it applies. Routing appears on the administration landing page.

## v0.11.0 — 2026-10-19

Paired with Cerea 0.15.13.

- **`/v1/models` lists models by the user's billing group,** the same rule admission already used.

## v0.10.0 — 2026-10-19

Paired with Cerea 0.15.11.

- **Model aliases.** An alias resolves to one target model and the request is billed to the target; reports say "via alias", and `/v1/models` shows an alias card with the target's capabilities. The grant endpoints take an alias id. A **Routing** tab in the console manages them. (ADR 0101)

## v0.9.6 — 2026-10-18

Paired with Cerea 0.15.9.

- A price typed by hand at import can be in any convertible currency.

## v0.9.5 — 2026-10-18

Paired with Cerea 0.15.8.

- A generic catalogue's token limits reach the model, priced or not.

## v0.9.4 — 2026-10-18

Paired with Cerea 0.15.7.

- A price typed at import can carry cache rates, and Discover offers cache rates on embedding rows too.

## v0.9.3 — 2026-10-18

Paired with Cerea 0.15.6.

- **The console speaks from a message catalogue,** so it can be translated; the docs say how to add a language and how to find what is not translated.
- **Discover:** a model without a listed price can be imported with a price typed on its row; a manual price must name a listed model.

## v0.9.2 — 2026-10-17

Paired with Cerea 0.15.4.

- A model's display name is settable and shown wherever a name is read.

## v0.9.1 — 2026-10-16

Paired with Cerea 0.15.3.

- Fix: a claimed invitation no longer reads as "Invited".

## v0.9.0 — 2026-10-15

Paired with Cerea 0.13.2.

- **Group quota rules can cap each member** instead of the group's total. (ADR 0098)

## v0.8.2 — 2026-10-15

Paired with Cerea 0.13.2.

- Usage notes in the console collapse to one line.

## v0.8.1 — 2026-10-14

Paired with Cerea 0.12.0.

- Fix: a new default group gets the deployment's capability defaults when it is created.

## v0.8.0 — 2026-10-14

Paired with Cerea 0.12.0.

- **Capabilities are per-group flags,** and a setting supplies the defaults for new groups. (ADR 0096 amendment; migration 0060)

## v0.7.1 — 2026-10-14

Paired with Cerea 0.11.2.

- The gateway refuses to start only for a `code_agents` restriction that can never fire, and warns when the named machine client looks misnamed.

## v0.7.0 — 2026-10-14

Paired with Cerea 0.11.2.

- **Group capabilities:** chat-only users, and per-group `code_agents` and `api_keys` capabilities, with settings, console toggles and a badge. Keys and machine clients are refused when the capability is missing, and `/v1/me` reports both. (ADR 0096)
- A restriction that can never fire makes the gateway refuse to start.

## v0.6.0 — 2026-10-13

Paired with Cerea 0.11.0.

- **IdP groups reach local groups only through a mapping.** One **Map** dialog in the console maps a seen identity-provider name to a group, and the seen list shows every unmapped name. Migration 0058 turns the group claims already feeding groups into mappings, without touching a mapping the operator already made. `pystino idp check` says to map full paths.

## v0.5.0 — 2026-10-13

Paired with Cerea 0.10.17.

- **Join modes** for identity providers, with pre-registration (any provider, claimed by verified email), invitations, an approval queue and an access-group gate. The gate answers on every path, keys included (`/v1` refuses with 403), with per-mode refusal texts. Existing policy is folded into the new modes (migration 0057). Console: join modes, invite dialog, approval queue. (ADR 0095)
- The Authentik outbound-SCIM dialect is supported, and an invite-mail failure fails safe.
- `pystino idp check` fails loudly without the device flow and names the join mode.

## v0.4.2 — 2026-10-12

Paired with Cerea 0.10.12.

- GitLab machine tokens are accepted through Doorkeeper's token info.

## v0.4.1 — 2026-10-10

Paired with Cerea 0.10.1.

- Fix: the Identity page's provisioning hint describes the state the box is in.

## v0.4.0 — 2026-10-08

Paired with Cerea 0.7.2.

- **Opaque OIDC access tokens are accepted** by introspection (GitLab), and every bearer rejection logs its reason.
- **IdP groups are imported by hand by default,** and there is a default group everyone joins.
- Fix: migrations 0032, 0035 and 0040 run on SQLite again.
- Maintainers: docs checked against the code; a Development guide; ruff format enforced in CI.

## v0.3.2 — 2026-10-06

Paired with Cerea 0.4.5.

- Every administrator-supplied reason is optional. The console's text is a step larger, in the Inter font.

## v0.3.1 — 2026-10-06

Paired with Cerea 0.4.4.

- **Fix:** quota rebuilds replace counters with the ledger instead of adding to them, and cold windows are detected from the counters. Quota health and reconcile are in the admin API and console.
- Redaction image is built in CI for the deploy kit's document reader.

## v0.3.0 — 2026-10-06

Paired with Cerea 0.4.0.

- `POST /v1/search` accepts an optional backend the caller is granted.

## v0.2.6 — 2026-10-05

Paired with Cerea 0.3.6.

- Scanned PDF pages can be returned as images on request, page by page, by the local extractor.
- Stored reply text is cleared after a retention window (24 hours by default).
- Legacy Word `.doc` files are read whatever their name, and the extractor's reason reaches the caller.
- A disabled previous identity provider that nobody signed in through can be removed from the console.

## v0.2.5 — 2026-10-04

No operator-facing changes.

## v0.2.4 — 2026-10-04

Paired with Cerea 0.3.3.

- The release manifest's upstream images are pinned by digest, and the console's "How it works" links to the published docs.

## v0.2.3 — 2026-10-04

- The console and the docs landing page link to the opencode setup one-liner.

## v0.2.2 — 2026-10-04

- **opencode setup script:** a public one-liner, served by the gateway, that points opencode at the gateway with an API key; it merges into the global config.

## v0.2.1 — 2026-10-04

- Console: dark faint text reaches a 4.5:1 contrast ratio.

## v0.2.0 — 2026-09-29

First published release of the code: the gateway (accounting, quotas, per-caller model access, redaction), the console and the identity section. Provider Test probes work per plugin, and the local extractor is listed for OCR and cannot be deleted. Docs say who acts on each operator page, and deployment covers serving satellite chats.

## v0.1.0 — 2026-09-29

First tagged version, the same code as v0.2.0 before publication. It includes the identity stack: one environment-owned identity provider, bundled Authelia accounts with a default `users` group, linking by email, admin merge, delete with chat erasure, `pystino break-glass` and `pystino idp check`, and the console Users page.
