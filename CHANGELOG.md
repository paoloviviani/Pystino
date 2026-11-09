# Changelog

Notable changes to Pystino for operators of the gateway, newest first. Each release is a git tag `vX.Y.Z`; "Paired with Cerea" names the Cerea release it was tested with. Versions before 0.24.0 were reconstructed from the project history.

## Unreleased

- **Classifiers use only local models by default.** A privacy classifier ("classified as") may now use only a chat model that runs inside the deployment (the built-in provider, the notion the readers already use). A new deployment setting **Allow remote classifiers** (Privacy, Classifiers tab; migration 0086; off by default) lets an administrator choose a model of an outside provider too, with the existing warning that the text is sent there. While it is off, the classifier model list shows only local models, a remote model is refused with 400 `remote_classifier_not_allowed`, and a classifier already stored with a remote model stays but fails closed (undecidable, never forwarded) and is flagged `unavailable_now` in the pipeline check. `GET /classifiers` gains `allow_remote` and a `local` flag per model; `PUT /settings` gains `allow_remote_classifiers` and leaves an omitted `user_entity_types` unchanged. Upgrading turns it off, so a classifier on a remote model stops answering until the setting is switched on.
- **Redaction timeout fix.** `GATEWAY_REDACTION__TIMEOUT_SECONDS` now defaults to **30** (was 5): NER screening of a large tool result, such as an Overleaf LaTeX file, took longer and the request was refused with a log line that read `detection service unavailable ()`. The refusal now says what happened: the log gives `screening timed out after 30 s (N characters, M items, engine X)` (never the text), the 502 says the service timed out, and the refused request's row carries a stable `error_code`, `redaction_timeout` or `redaction_unreachable`. The 502's wire error code changes accordingly: it was `redaction_unavailable`, it is now one of those two (Cerea does not read it). The request log API gains `error_code` (the code only, never the message) and the console shows the reason under the status. Fail-open is unchanged apart from the log message. See docs/redaction.md.
- **The extractor's engines are OCR models.** The built-in extractor's engines are now separate models in the catalogue, under the built-in provider: `markitdown` (the plain reader: no OCR), `markitdown-ocr` (markitdown plus RapidOCR) and `docling`, so a client that lists OCR models (Cerea) sees what the deployment can read with. The extractor reports the engines it can run in `/healthz` (`extractor_engines`); a model is offered (in `/v1/models?include=ocr` and the privacy reader pickers) only when its engine is reported, a call to one that is not is refused with `extractor_engine_unavailable`, and a rule naming one is refused as a read that cannot be done. The extractor treats an explicit `X-Extractor-Engine: markitdown` as OCR off even on the OCR and Docling images. **Privacy pipelines**: "read with" picks among these models and any remote OCR model; the separate Engine choice is removed (the `engine` field of a rule's action, the `engines` list of the options, `engine` and `engine_by` of the Test result, and the console's Engine select), as is the `builtin` reader. **Migration `0085`** creates `markitdown-ocr` and `docling` (copying `markitdown`'s active and public flags and its grants; unpriced) and rewrites stored rules: `builtin` with engine `markitdown`, `ocr`, `docling` reads with `markitdown`, `markitdown-ocr`, `docling`; `builtin` with no engine reads with `markitdown`; every change is logged (`extractor engine models 0085`). A rule is only pointed at a model on the extractor provider: if a model of that name exists elsewhere (a remote service, an alias) the rule reads with `markitdown` and the log says so loudly; a missing `markitdown` row is recreated (0041's defaults) and logged. The built-in readers (every model on the extractor provider, and the provider) can no longer be deleted: 409 from the API, Delete disabled in the console; deactivating and grants still work. See docs/extractor-engines.md and docs/privacy-routing.md. **Upgrading a stack with a locally built extractor** (the OCR or Docling image is not published): rebuild it for this release from the kit folder, `./dev/build.sh --ocr` (or `--docling`), then `docker compose up -d --wait extractor gateway`; until then the old image reports no engines and only `markitdown` is offered.
- **Detectors tab and a privacy master switch.** The Redaction service tab is now **Detectors** (`/admin/privacy/detectors`; the old route redirects): one card per detector the redaction service reports, the patterns, spaCy and GLiNER, each with an On/Off switch, its capabilities and "used by N policies". Switching off a detector an active policy uses is refused with a 409 naming the policies, and a policy can only choose detectors that are on (the editor's engine picker, formats and entities follow). Per-engine switches are stored on the append-only engine rows (**migration `0084`**: `spacy_enabled`, `gliner_enabled`; null means the older `presidio_ner` applies, so existing rows behave as before). The engine list is gone from the console: **Privacy: On / Off** at the top of the Pipelines tab is the old `noop` engine under a clear name (inline confirmation to turn it off, a warning banner across the Privacy area while it is off). `GET /api/admin/privacy/engines` reports `enabled` and `used_by` per engine and `patterns_enabled` and `patterns_used_by`. **The switches are enforced on the request path**: an ask that needs a switched-off engine or detector family is refused with a 503 naming the policy (never fail-open, whatever `fail_open` says), save and activation check a policy against what is on now (including a policy that names no engine, which reads with the service's default), the in-use refusal also counts users' own policies and uses the service's real default engine, every engine or master-switch change is audited with a reason (a default one when none is given), and turning privacy on restores the engine that was running. **Migration `0084`** also switches off any AGE, ID or EMAIL format a policy still acted on (kept under formats, with a log line): nothing ever detected them. The fold of confidence thresholds, including a user's own tightening, no longer lets another policy's higher threshold loosen an entry that left the default. Also: the Capabilities cards on the settings page are two lines at every width.
- **Two redaction fixes.** A type's confidence threshold is now the lowest any acting entry sets (entities, formats, custom entities), so a stricter pattern or keyword list of the same name no longer puts the type back on the deployment default. A user's own pattern may not take the name of an administrator's pattern, keyword list or custom entity (as it already could not take a format or entity name); the name is refused rather than the fold changed.
- **Redaction policy editor fixes.** (1) Every row of the editor, built-in formats, entities, keyword lists and custom entities as well as custom patterns, now carries the **placeholder style** select (tagged or same length); it appears when the action writes a token (the two Anonymise actions; redact writes the bare label and block refuses). (2) **AGE, ID and EMAIL are no longer advertised as pattern formats.** They came from Presidio's spaCy recogniser, whose label map lists them although the stock models never emit them; the redaction service now partitions `pattern_entities` and `model_entities` by the recognisers actually loaded (a deterministic recogniser is a pattern, a model-backed one is NER, an unloaded one advertises nothing). Policies that listed them under formats never matched anything (nothing could produce them); such a policy is kept: the console moves it to the entities when the engine finds it, otherwise keeps it **turned off with a warning**, and the API accepts an unlisted format only while it is `off`. (3) **Entities need an NER engine.** When the redaction service has none, the console hides the Entities section and says "No NER engine is installed in the redaction service; entities need one." with a link to the docs; the engine picker offers only available engines, custom entities only appear with GLiNER; the API refuses an entity item with the same message, and My privacy offers no model-backed types and refuses them (`GET /api/me/privacy/options` gains `ner_available` and `ner_note`). **New policy from template** (administrators, and users for their own policies): six built-in examples (secrets and credentials, personal and banking data, government IDs, contact details, network and infrastructure, health identifiers), offered and never seeded, that add only what the service reports and name what they skipped. See docs/redaction.md.
- **A user's own pattern no longer breaks the test box.** A custom pattern in a user's policy was also listed as an entity type, so it was asked of the redaction service (a warning on every request, and a 503 from `POST /api/me/privacy/test`). Patterns and keyword lists are evaluated in the gateway and are never asked of the service. **Compose default:** `GATEWAY_OIDC__ACCEPTED_CLIENTS` now defaults the machine client to `opencode-enrollment`, like `GATEWAY_OIDC__MACHINE_CLIENT_ID`; with `OIDC_MACHINE_CLIENT_ID` **unset or empty** (the `:-` default covers both), device-flow tokens from that client are accepted instead of refused with 401; to turn the machine client off, set `OIDC_ACCEPTED_CLIENTS` explicitly. A user's pattern can no longer be named like an entity type (`PERSON`, `EMAIL_ADDRESS`, a type an administrator's policy uses): labels and types share one name space, and such a pattern would have stood in for the type. A pattern or keyword list never removes a type from what the detection service is asked for.
- **Privacy: policies and pipelines (ADR 0113, phases 0 and 1, revised).** Redaction and routing become two objects, one job each. A **redaction policy** is *what to find, and hide* (items in two groups, patterns and entities, a mode for each, and a NER engine) and has **no scope**. A **pipeline** is *when and how*: a kind (read: `/v1/ocr`; embed: `/v1/embeddings` and `/v1/rerank`; chat: every chat surface), a scope (who: all, users, groups or keys; target: all, only or all except, over providers and models), ordered rules (conditions → action, the **first match wins inside the pipeline**, observe-only rules are logged and the search goes on) and a fixed last rule, "everything else". **Every pipeline in scope runs and the results combine**: where a request is handled is a **ladder** and the highest level wins (read: default reader < read with a chosen reader < refuse; embed and chat: pass < refuse); the policies each pipeline chose **add up** and the strictest mode wins per entity type (the fold of ADR 0038, unchanged); an administrator pipeline's reader and engine hold over a user's; the 403 and the log name the rule that set the winning level (the administrator's on a tie); the trace lists every pipeline in scope. Reading: *read with* names any configured `/v1/ocr` model (the built-in extractor with an optional engine, or another OCR provider such as a facility reader), overrides the model the caller named, fails closed for a link document read by the built-in extractor or a reader the caller is not granted, and after a reroute the pipelines that guard the new reader also run (bounded). Conditions: file type, "is a scan" and file-name globs (read), "**\<policy\> finds at least N items**" (read, embed, chat) and "classified as \<classifier\>"; a file-name-only protection warns that a rename defeats it. Any non-refuse action may add one **redact with** policy: on the returned text (read), the input (embed) or messages and tool results (chat). **Whole files leave whole**: a file sent to a remote reader is not redacted before it goes, only the text that returns. **There is no "local"**: `providers.is_local`, its checks and the embed `local` action are gone, and there is no reroute between models for embed or chat. **User pipelines**: a user writes pipelines for themselves only (their who is fixed, enforced by the server) whose actions can only tighten (read with a reader they are granted, refuse, redact with an administrator's policy or their own, an engine); their own policies use custom patterns and the entity types an administrator makes available, and they may name only the administrators' policies marked shared with users; the combination is monotone, so a user's pipeline can never loosen anything. `/api/admin/privacy/{pipelines,policies,classifiers,engines,settings,options,test,status}` (audited) and `/api/me/privacy/{pipelines,policies,options,in-force,test}`; new `GET /v1/pystino/privacy` lists, for the calling user and per embedding and chat model, what the pipelines in scope do in short words ("refuses text where Codenames finds an item", "redacts with Default"), for a client to show beside the embedder choice. The old `/api/admin/redaction/rules`, `/api/me/redaction/{rules,in-force,test}` and `/api/admin/privacy/rules` are replaced. Works with any client: no Cerea headers; every embedding or chat input is judged separately and in full; text too large to check, a file of unknown name or type, an unavailable detector, and a read that cannot be done all fail closed; a worker that never loaded the pipelines answers 503. **Console**: Admin, Privacy has five tabs, **Pipelines**, **Redaction policies**, **Classifiers**, **Redaction service** and **Test** (the real combination with every pipeline in scope and its trace, the reroute hops, the ladder winner, who chose the reader and engine, and the policy table with the strictest mode and its source), with one "How it works" overlay and the explanations in tooltips; users get **Privacy pipelines** (their own pipelines and policies, the administrator's read-only, a test box locked to them). `/admin/redaction`, `/admin/privacy/hidden` and the old rule links redirect (a redaction rule kept its id as a policy). **Migration `0082`**: every scoped redaction rule becomes a policy (same id) plus an administrator pipeline per kind with the rule's scope, a user's own rules (ADR 0112) become a user policy and three user pipelines, routing rules from the first build become rules in per-kind pipelines (a `local` action becomes `refuse`), and `providers.is_local` is dropped; a contract test proves byte-identical redaction on chat, responses, embeddings, rerank and both readers for representative configurations, and that a deployment with no rules behaves exactly as before. A condition that cannot be decided (detector down, input too large) matches only on a protective rule with a non-negated condition; otherwise the request is refused with a 503 (`privacy_undecidable`). A user's pipeline is folded after every administrator's and can never decide the allow-list or replace the environment's policy.  Reroute hops are fail-closed (the pipeline guarding the current reader decides the next hop; running out of hops refuses). Deactivating a policy a pipeline applies is refused. The migration's downgrade writes policies back as scoped rules (or refuses unless `PYSTINO_ALLOW_LOSSY_DOWNGRADE=1`). See docs/privacy.md, docs/privacy-routing.md and docs/redaction.md.
- **Privacy: one detection vocabulary (ADR 0113, section 0c).** Redaction policies are the **only** object that finds things, and a pipeline condition counts what a policy finds. A policy's items come in two groups that are never mixed, in the API and in the one editor: **Patterns** (fast, deterministic: the redaction service's built-in formats, regular expressions evaluated in the gateway under RE2, and new **keyword lists**: a name, comma-separated words and phrases, whole-word match, a *case sensitive* checkbox, accents folded unless case sensitive, evaluated in linear time with no regular expression built from the list) and **Entities** (NER, probabilistic: the types the policy's **engine** exposes, each with a mode and a threshold, and custom GLiNER types, an UPPER_SNAKE name plus a natural-language label). A policy chooses its **NER engine**, spaCy or GLiNER, among those the redaction service reports available (none: the deployment default); a request whose policy needs an engine the service no longer has is refused with a 503 naming the policy (`redaction_engine_unavailable`), and when policies of different engines combine the service is called once per engine and the spans merged with the strictest mode. New mode **`detect only`** (found and counted, never hidden; just above `off`; the strictest-mode fold and the tighten-only user rules are unchanged). New condition "**\<policy\> finds at least N items**" (is or is not; read after a local read, embed, chat) counts distinct items in any mode but `off`; the Test trace lists each item with its group and, for entities, engine and score. The patterns part never fails; if the NER part cannot run the existing fail-closed rule applies (a protective rule acts, otherwise a 503 naming the rule), `fail_open` notwithstanding. "**Classified as \<classifier\>**": a classifier is a decision model, a chat model plus a topic in plain words, shipped as scaffolding (no model bundled, none seeded; classifying sends the text to that model's provider, which the console says). **Reading decides from the file**: `finds` and `classified` are for embed and chat only; the API refuses them on a reading rule with a 400, and the scan probe reads a file only to tell whether it is a scan. A fold takes the strictest mode over every table that names a type, in every policy (a `detect only` entity never lowers another policy's keyword list). A condition is undecided, not "found nothing", when the engine cannot serve a requested type, and an undecided rule stands in for a match only if no later enabled rule of its pipeline is stricter. Wire limits are aligned (a custom label 80 characters, 30 custom types per policy and per engine across active policies; a named 503, never a 500). The migration's downgrade writes the rows back. The redaction service bounds its GLiNER queue (503 `engine_busy`) and no longer asks the model for pattern type names. **Removed**: the hidden quick check and the health, IDs and language conditions, the "detector profiles" and their "take effect later" refusal. Nothing in Pystino is specific to any topic. A **Try it** box in the policy editor runs the real detection with no side effects (`POST /api/admin/privacy/policies/try`). New `GET /api/admin/privacy/engines`, `/api/admin/privacy/classifiers`, and `shared_with_users` on policies (users may name an administrator's policy, to apply or to count, only if it is shared). The redaction service gains `ner_engines` in `/healthz`, `ner_engine` and `custom_labels` on `/detect`, 409 `engine_unavailable`, and `score` and `engine` on spans (built on its own branch). **Migration `0083`** (only unreleased #52 data exists): policies are rewritten in the two-group shape with the engine null (byte-identical redaction, the contract test, extended for keyword lists and the engine field); a condition on IDs becomes "finds at least 1 item" of a policy "Identifiers (migrated)" listing the formats the quick check knew as `detect only`; a rule that used health or language has **no equivalent** and is **kept, switched off, with the condition removed and a note** shown as a warning (a protective rule can then only apply to more requests when turned on; a rule that only passed requests is also made observe-only), and every conversion is named in the migration log; policies users already apply are marked shared so nothing stops working; `detector_profiles` and the `profile_id` columns are dropped. See docs/privacy-routing.md and docs/redaction.md.
- **What Pystino saw and what handled it, on every request.** The redaction log entry and the opt-in `pystino_redaction` summary gain a `routing` object: kind (read, embed, chat), the file type and whether it is a scan (the file name only in the per-response summary, never in the log), the target, the number of items each counted policy found (and what each classifier said) when a pipeline asked, and what handled it (extractor, fallback reason, reader and engine, the rule that set the winning level with its pipeline, the policies applied) plus every pipeline that was in scope and the rule that fired in each. Once a pipeline does routing, every request is logged, including those where nothing was hidden; a deployment with only redaction policies logs as it always did. `/v1/ocr` and `/v1/embeddings` now return the summary too when asked. The user's Redactions page shows it. Compatible: clients that ignore unknown fields need no change, and without pipelines and without the header nothing changes.
- **Console sign-in returns to the console.** A console page opened without a session (for example from a link in a phone app) signed in and then landed on the origin root, which on a kit is the chat app. It now returns to the console page that was opened, and to `/console` when there is nothing better; a `next` of `/` is no longer honoured.
- **Extractor engines: Docling, and OCR for scans.** The local document reader gains `EXTRACTOR_ENGINE=docling` (layout, reading order, tables as Markdown, RapidOCR for scanned pages; Word, PowerPoint and Excel through Docling, with markitdown and antiword as fallbacks) and RapidOCR for scanned pages and pictures on the default engine (`EXTRACTOR_OCR=rapidocr`). Both are image variants built locally (`--build-arg EXTRACTOR=ocr|docling`, tags `-ocr` and `-docling`); the default `-pattern` image is unchanged. Weights are baked in at pinned revisions with SHA-256 checks, nothing is downloaded at runtime, and threads, pages and time per document are bounded (`EXTRACTOR_THREADS`, `EXTRACTOR_MAX_PAGES`, `EXTRACTOR_TIMEOUT_SECONDS`). An `X-Extractor-Engine` request header picks the engine for one request. The API and response shape are unchanged. The Docling table-structure weights are under CDLA-Permissive-2.0 (permissive, redistributable, not OSI-approved; accepted by the owner), so the image carries a `NOTICE.md`, the agreement text and licence labels, and may be published later; for now it is built locally. See docs/extractor-engines.md for licences and measurements.

## v0.31.0 — 2026-11-08

Paired with Cerea 0.46.0.

- **Redaction visibility for users.** Users get a **Redactions** page next to Your usage: the administrators' rules in plain words (read-only), a log of requests where something was hidden or refused (what kind of value, how many, which rule decided; never the values), their own rules and a box to try text against all of them. A user's own rules fold in after the administrators' and can only tighten: a custom pattern (same RE2 engine and limits as an administrator's) and a stricter mode per entity type; no `off`, thresholds or exceptions, and a rule that is not stricter than the administrators' is saved and inert. `/api/me/redaction/{rules,in-force,test,events}`, and `GET /api/admin/redaction/events` for administrators (own-key requests left out). Migration `0080` adds `user_redaction_rules` and `usage_records.redaction_detail` (the log entry, on the usage row, so no extra write; retention is the request log's).
- **Per-response redaction summary.** A request with `x-pystino-redaction-summary: 1` gets a top-level `pystino_redaction` object on `/v1/chat/completions` (in the body, or on the final stream chunk): whether redaction applied, per type the count, mode, deciding rule and where the value was. Absent when redaction is off; without the header responses are unchanged. With `x-pystino-redaction-values: 1` and the user's own sign-in token (an external IdP token or the bundled IdP's session token; never a standing API key) it also carries the substituted originals, in that response only and never stored. Not sent: a cost (settled after the response).

## v0.30.0 — 2026-11-08

Paired with Cerea 0.45.0.

- **Redaction image: medium spaCy models.** `SPACY_MODELS` now accepts `en_core_web_md` and `it_core_news_md` (3.8.0, pinned), about 40 MB each and roughly twice as fast as the `lg` models, somewhat less accurate on names; suited to small boxes. Also fixes an Italian-only NER image (for example `SPACY_MODELS="it_core_news_md"`) failing at startup with "supported languages have to be consistent".
- **A provider without a model list tests as reachable.** Servers such as text-embeddings-inference answer `/models` with 404 but serve embeddings; the connection test now checks `/health` and `/info` beside the version path and says "reachable, add its models by hand" instead of "not reachable". A base URL missing `/v1` still gets the hint.
- **Security: `/docs` and `/openapi.json` are closed by default.** Unauthenticated callers no longer get the API schema. `GATEWAY_DOCS_ENABLED=true` serves them again, for development; `scripts/export_openapi.py` and the console's generated types are unaffected.
- **Security: bring-your-own-key provider replies are bounded.** A private provider's reply is cut off at 64 MiB (`GATEWAY_UPSTREAM__BYOK_MAX_RESPONSE_BYTES`) and the gateway waits at most 300 s between reads (`GATEWAY_UPSTREAM__BYOK_READ_TIMEOUT_SECONDS`), so one member's host cannot hold a worker.
- **Security: callers cannot set a router's routing fields.** `models`, `provider` and `route` in a request body are dropped before it goes to a router-type provider such as OpenRouter.
- **Docs: two known limits written down.** Redaction does not cover tool definitions, `response_format` schemas, `metadata`, `user` or image URLs; an abandoned stream is billed from an estimate that omits reasoning the provider never streamed.
- **Valkey down at startup no longer means silent per-process counters.** With `GATEWAY_VALKEY_URL` set and Valkey unreachable at boot, the gateway logs `COUNTER STORE DOWN`, `/readyz` reports `degraded`, and quotas are answered from the ledger (or refused, per `GATEWAY_QUOTA__FALLBACK_TO_DATABASE` and `FAIL_OPEN`) until a restart with Valkey up. An empty `GATEWAY_VALKEY_URL` still means single-process.
- **Security: usage exports neutralise spreadsheet formulas.** In the usage report and request log CSVs every text cell (names, labels, tags, models) that starts with `=`, `+`, `-`, `@`, a tab or a carriage return is written as text; numbers stay numbers.
- **Security: an abandoned stream is estimated from everything the caller sent and received.** The estimate behind a stream the client dropped before the usage frame now counts image parts, tool definitions and streamed reasoning. Estimates are still billed from our price table, as before.
- **Security: oversized request bodies are refused before parsing.** `/v1` bodies over 64 MB and `/api`, `/auth` and `/scim` bodies over 4 MB get a 413 before authentication or parsing (`GATEWAY_REQUEST_LIMITS__MAX_V1_BODY_BYTES`, `GATEWAY_REQUEST_LIMITS__MAX_API_BODY_BYTES`).
- **Security: `no-store` and HSTS headers.** `/api`, `/auth`, `/scim` and `/v1/me` responses carry `Cache-Control: no-store` unless the route sets its own, and responses over HTTPS carry `Strict-Transport-Security`.
- **An over-long or malformed `x-request-id` is replaced, not a 500.** A caller's id longer than 64 characters or with non-printable characters is swapped for a generated one.
- **Docs: current screenshots.** The README screenshot (light and dark) and the docs pages for providers, models, usage, the request log and quotas show the current console, taken on a test deployment with fictional users and illustrative figures.

## v0.29.2 — 2026-11-07

Paired with Cerea 0.44.0.

- **Console tables stack on a phone.** Providers, quotas, quota usage, users and the audit log draw each row as a labelled card under 640 px instead of clipping the action column, the usage bar or the dates.
- **Developer detail is folded away.** The console build hash, `.env` and `./configure` notes and the search backend's endpoint (with its ADR number) moved into collapsed **About this server** and **How it works** disclosures on Overview, Settings and Search; the card descriptions read in plain words.
- **More developer detail folded away.** "Join mode set in .env" and "Required groups set in .env" on the Identity page, and the `POST /v1/search` endpoint named under the search policy, moved into the folded **About this server** and **How it works** sections.

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
