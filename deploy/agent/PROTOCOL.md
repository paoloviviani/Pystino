# Thin machine agent: architecture and wire protocol (v1)

Status: design for P0, 2026-09-24. Supersedes paseo in the `/code` data path (ADR 0086 draft to follow).
Source of truth for both implementations: the Go agent (Pystino `deploy/agent/`) and Cerea's machine link (`src/lib/server/code/machine*.ts`).

## 1. Shape

```
 browser ──HTTPS (existing /api/v2/code/* REST + SSE)──► Cerea server
                                                          ▲
                                   WSS, dialled OUT by the machine, bearer = enrollment access token
                                                          │
 user's machine:  pystino-agent run
                   ├─ link      : WSS client to Cerea, reconnect w/ backoff, token renewal frames
                   ├─ sessions  : backend-agnostic materializer (epoch/seq, ring buffer, auto-accept, policy)
                   ├─ backend   : Backend interface; first impl = opencode (supervises `opencode serve`)
                   └─ shim      : the existing /v1 proxy (injects the gateway bearer + x-bill-to), now with a local secret + Host allowlist
                                   opencode ──HTTP──► shim ──HTTPS──► Pystino /v1
```

- **One binary** (`pystino-agent`, grown from `pystino-enroll`), no Node/npm/Python/paseo on the machine besides opencode itself.
- **One credential root**: the enrollment's OIDC refresh token. The same access token authenticates the WSS to Cerea and the `/v1` calls. Revoking at the IdP kills both within one access-token lifetime.
- **Nothing capability-bearing is stored in Cerea.** Cerea holds only a live socket that the machine opened. A DB dump yields device names and ids, nothing that can reach a machine.
- LLM traffic never crosses Cerea (unchanged two-path rule).

## 2. Why this backend interface, and not ACP as the interface

We checked `opencode serve` (1.18.31, OpenAPI at `GET /doc`) against ACP (Agent Client Protocol).

- `opencode serve` gives, over HTTP + one SSE stream (`GET /global/event`, all directories): sessions (`POST/GET/PATCH/DELETE /session`), async prompt (`POST /session/:id/prompt_async`, `parts[]` with text and file parts, per-prompt `agent` = mode and `model`), abort, permissions (`permission.asked` event, `POST /permission/:id/reply {reply: once|always|reject}`), questions, modes (`GET /agent`, `mode: primary`), models (`GET /config/providers`), per-session diffs (`GET /session/:id/diff`, `session.diff` event), children (subagents, `GET /session/:id/children`), todos, usage (tokens and cost on assistant messages / step-finish parts), compaction (`POST /session/:id/summarize`), revert, worktrees (`/experimental/worktree`), basic auth (`OPENCODE_SERVER_PASSWORD`). Sessions persist across restarts.
- ACP (JSON-RPC over stdio) covers the core: `session/new|load|prompt|cancel|set_mode|set_model`, `session/update` notifications (message/thought chunks, tool_call(+update), plan, mode updates), `session/request_permission` with allow/reject once/always options, image/resource content blocks. It lacks (or has only unstable drafts for) session listing, usage/context, diffs, subagent trees and multi-client attach; the client owns the process.

**Decision:** our own backend interface, *shaped like ACP* (same nouns: session, prompt, update stream, permission request with once/always/reject, cancel, mode, model) plus optional capabilities for what ACP lacks (usage, diff, children, compact, worktrees). opencode is implemented over `opencode serve` (richer than `opencode acp`, survives restarts, multi-directory). The generic **ACP adapter** is the natural *second* backend (covers Gemini CLI, Claude Code and Codex via their ACP adapters, Pi via `pi-acp`); it simply reports fewer capabilities. Capabilities are advertised per backend in `hello`, and Cerea hides affordances a backend lacks.

`pystino-agent run --backend acp --acp-command "<cmd…>"` (default `opencode acp`) runs the generic adapter (`internal/backend/acp`) instead of the default `opencode` backend; `--backend opencode` (or omitting the flag) keeps the default. The adapter spawns and supervises `<cmd…>` as a child process (restart with backoff, process group + `Pdeathsig` on Linux, same discipline as the opencode backend's own supervisor) and speaks JSON-RPC 2.0 newline-delimited over its stdio: `initialize` (protocol 1, no fs/terminal client capabilities — this client owns none of that) seeds `Capabilities()` (Images from `promptCapabilities.image`; Diff/Children/Usage/Compact/Worktrees always false — ACP has no wire message for any of them; AutoAccept always true, since auto-accept is the materializer's job, not a backend capability) and `ID()`/`Version()` (`acp:<agentInfo.name>`, e.g. `acp:OpenCode`, and `agentInfo.version`). `session/new`/`session/load` answer with either the ACP draft's `modes.availableModes`/`models.availableModels` or — verified live against opencode 1.18.31 — a bespoke `configOptions` list; both are read. Prompts map to `session/prompt`'s content blocks (text, plus images as `{type:"image",mimeType,data}`) and return once sent, per the async contract; the turn's own completion arrives later as a `status` event once `session/prompt`'s response carries `stopReason` (`cancelled` → idle, no error; `refusal` or a JSON-RPC error → an `error` event). `session/update` notifications (`agent_message_chunk`/`agent_thought_chunk`/`tool_call(_update)`/`plan`/`current_mode_update`) become the normalized event stream; ids are derived from a local turn counter (not the agent's own per-chunk message ids) so "one message per turn per role" holds regardless of how a given ACP agent chunks its own output. `session/request_permission` becomes `permission.asked`; `ReplyPermission` answers with the `optionId` of kind `allow_once`/`allow_always`/`reject_once` (falling back to `allow_once`/`reject_always` if the agent didn't offer the exact kind); a `Cancel` with a permission still pending answers it `{outcome:{outcome:"cancelled"}}` before sending `session/cancel` (a notification, not a request — verified live). `session.rename`/`session.delete` are best effort (an in-memory title overlay; `session/close` if `sessionCapabilities.close` was advertised); `session.setModel` reports the underlying error cleanly if the agent doesn't implement the (still-unstable) `session/set_model`.

## 3. Transport and authentication

- Endpoint: `GET wss://<cerea-origin>/api/v2/code/machine` (WebSocket upgrade), subprotocol `pystino-machine.v1`.
- Headers: `Authorization: Bearer <access token>`, `X-Pystino-Machine-Id: <uuid>` (generated once, persisted in the agent state dir; a re-enroll mints a new one), `X-Pystino-Machine-Name: <display name>`.
- Cerea validates the JWT **locally** (JWKS from the issuer's discovery doc, cached): `iss` == configured issuer (`OPENID_PROVIDER_URL`, exact, trailing-slash-normalized); `aud` contains `CODE_MACHINE_AUDIENCE` (default `pystino-api`); `azp` (or `client_id`) == `CODE_MACHINE_CLIENT_ID` (default `opencode-enrollment`); `exp` in the future; `typ` header, if present, is not an ID token. Userinfo is never used. This is review C1 done properly for the one thing a machine presents.
- The token's `sub` must map to an existing Cerea user (same mapping the OIDC login uses). No user → close `4403 "sign in to Cerea once before connecting a machine"`.
- Handshake rejections happen **before** the upgrade completes when possible (HTTP 401/403), otherwise as close codes:
  `4401` token invalid/expired · `4403` forbidden (revoked device, unknown user) · `4409` a newer connection for the same machine replaced this one · `4000` protocol error.
- **Token renewal:** the agent refreshes its access token at ~70% of lifetime and sends `{"type":"auth","token":"…"}`. Cerea re-validates (same `sub`, same checks) and moves the connection's deadline. Cerea closes with `4401` when `exp + 60s` passes without renewal. That makes IdP-side revocation effective within one access-token lifetime.
- Keepalive: WebSocket ping every 20 s from Cerea; the agent treats 60 s of silence as dead and reconnects. Agent reconnect: exponential backoff 1 s → 30 s with jitter, never gives up except on `4403`.
- **Deadlines everywhere (R1):** the dial has a 10 s timeout; every Cerea→machine request has a deadline (default 15 s, `session.sync` 20 s). An offline machine answers "offline" instantly from the registry, never hangs.

## 4. Pairing and authorization (per user, per device, revocable)

- A machine connecting with a valid token and an unknown `machineId` creates a `codeDevices` row `{userId, machineId, name, status: "pending", sub, iss, createdAt}` (unique index `(userId, machineId)`). The socket stays open but **no operations are forwarded** until the owner confirms.
- The `/code` panel lists pending machines with **Confirm / Reject**. Confirm → `status: "paired"` and Cerea sends `{"type":"status","status":"paired"}` down the socket. This click is the fresh human approval review C2 asks for; a phished device-code approval alone never reaches the panel's operations.
- Revoke (DELETE device) → row becomes `status: "revoked"` (tombstone keyed by machineId), the live socket is closed with `4403`, and further connects with that machineId are refused. Re-enrolling mints a new machineId → a new pending row.
- Every browser request is authorized as: `locals.user` present (C6) → device row with `userId == locals.user._id` and `status == "paired"` → the live connection for that row is authenticated as the same `sub`. A device can never be driven by a user other than the one whose credential the machine holds (C10 closed by construction; there is no paste flow).
- **Machine-side policy (the machine's veto, C4):** `pystino-agent` has a local policy file (`policy.json` in its config dir, set by `enroll` flags, never writable over the link):
  - `autoAccept`: `"allowed" | "denied"` (default `denied`). When denied, the agent refuses `session.setAutoAccept` and never auto-replies to permissions whatever Cerea sends.
  - `workspaceRoots`: absolute paths; if non-empty, `workspace.create` outside them is refused.
  - `allowFreeModels`: bool (default false). When false the agent lists and accepts only gateway (`pystino/*`) models; Cerea filters as well (defence in depth).
  - The policy is reported in `hello` so the UI can explain refusals.

What this achieves against review C4: no capability at rest in Cerea; per-device, per-user authority that ends on revoke (socket closed, machineId tombstoned) or on IdP revocation (≤ one token lifetime); the machine can veto auto-accept and confine workspaces. What it does **not** achieve: while a machine is connected and paired, a compromised Cerea process can still send prompts to it within the machine's policy (the panel is, by design, a remote control). Mitigations beyond that (per-prompt signing by the browser) are out of scope for v1.

## 5. Framing

JSON text frames, one object per frame, `type` discriminates.

| Direction | Frame |
| --- | --- |
| M→C (first frame) | `{"type":"hello","protocol":1,"agent":{"version":"0.1.0","os":"linux","arch":"amd64","hostname":"…"},"backends":[Backend],"policy":Policy,"credential":{"state":"ok"}}` |
| C→M | `{"type":"welcome","deviceId":"<ObjectId hex>","status":"pending"\|"paired"}` |
| C→M | `{"type":"status","status":"paired"}` (after the browser confirms) |
| C→M | `{"type":"req","id":"<string>","op":"<op>","args":{…}}` |
| M→C | `{"type":"res","id":"<same>","ok":true,"result":{…}}` or `{"type":"res","id":"…","ok":false,"error":{"code":"not_found"\|"invalid"\|"forbidden"\|"unavailable"\|"backend"\|"unsupported","message":"…"}}` |
| M→C | `{"type":"event","sessionId":"…","epoch":"…","seq":123,"event":Event}` — pushed for **every** session of the machine; Cerea fans out to watchers and drops the rest |
| M→C | `{"type":"credential","state":"ok"\|"expiring"\|"expired","detail":"…"}` — the gateway/enrollment credential's health (replaces regex-matching `lastError`, review C9). `expired` = refresh got `invalid_grant`; the machine sends it before its socket dies so Cerea can record it on the device row and show the re-enroll card |
| M→C | `{"type":"auth","token":"…"}` (renewal, §3) |

Unknown frame types and unknown event kinds are ignored by both sides (forward compatibility); unknown ops answer `unsupported`.

`Backend = {"id":"opencode","version":"1.18.31","capabilities":{"diff":true,"children":true,"usage":true,"compact":true,"images":true,"files":true,"worktrees":false,"autoAccept":true}}`
`Policy = {"autoAccept":"denied","workspaceRoots":[],"allowFreeModels":false}`

## 6. Operations (C→M `req.op`)

All ids are opaque strings. `workspaceId` is the agent's own registry id; a workspace is a directory.

| op | args | result |
| --- | --- | --- |
| `workspace.list` | `{}` | `{workspaces: Workspace[]}` |
| `workspace.create` | `{path, title?}` (path must exist, be a dir, satisfy `workspaceRoots`) | `{workspace}` |
| `workspace.rename` | `{workspaceId, title}` | `{workspace}` |
| `workspace.archive` | `{workspaceId}` | `{}` |
| `session.list` | `{workspaceId?}` | `{sessions: Session[]}` (archived excluded) |
| `session.get` | `{sessionId}` | `{session}` |
| `session.create` | `{workspaceId, backend?:"opencode", title?, modeId?, modelId?}` | `{session}` |
| `session.prompt` | `{sessionId, text, clientMessageId?, attachments?: Attachment[]}` | `{}` once accepted (the turn streams as events) |
| `session.cancel` | `{sessionId}` | `{}` |
| `session.rename` | `{sessionId, title}` | `{session}` |
| `session.archive` / `session.delete` | `{sessionId}` | `{}` |
| `session.setMode` | `{sessionId, modeId}` | `{session}` |
| `session.setModel` | `{sessionId, modelId}` (`"<provider>/<model>"`) | `{session}` |
| `session.setAutoAccept` | `{sessionId, enabled}` | `{session}` or `forbidden` by policy |
| `permission.reply` | `{sessionId, requestId, decision: "once"\|"always"\|"reject", message?}` | `{}` |
| `session.sync` | `{sessionId, epoch?, afterSeq?}` | `{epoch, seq, events: Envelope[]}` if `epoch` matches and the ring buffer still holds `afterSeq+1…seq`; else `{epoch, seq, snapshot: Transcript}` |
| `session.diff` | `{sessionId}` | `{files: FileDiff[]}` (capability `diff`) |
| `session.children` | `{sessionId}` | `{sessions: Session[]}` (capability `children`) |
| `session.compact` | `{sessionId}` | `{}` once accepted (capability `compact`; `unsupported` if the backend has none — opencode: `POST /session/:id/summarize`) |
| `backend.modes` | `{backend?, workspaceId?}` | `{modes: Mode[]}` |
| `backend.models` | `{backend?, workspaceId?}` | `{models: Model[], hidden: number}` (already filtered by `allowFreeModels`; `hidden` counts what the filter removed) |

Types:
```
Workspace  = {id, name, path, createdAt}
Session    = {id, workspaceId, backend, title, status: "idle"|"busy"|"retry"|"error", pendingPermissions: number,
              modeId|null, modelId|null, autoAccept: bool, parentId|null, createdAt, updatedAt, usage: Usage|null}
Mode       = {id, label, description?}
Model      = {id: "<provider>/<model>", label, providerId, isDefault?, contextWindow?, images?: bool, reasoning?: bool}
Usage      = {input, output, reasoning, cacheRead, cacheWrite, cost, contextUsed, contextMax|null}
Attachment = {type:"file", mime, filename, url}   // url = data: URL for P0; later a Cerea attachment-store URL
FileDiff   = {path, status: "added"|"modified"|"deleted", before, after, additions, deletions}
Transcript = {messages: [{message: Message, parts: Part[]}], permissions: PermissionRequest[], status, usage|null, todos: Todo[]}
```
Times are RFC 3339 strings; everything is plain JSON.

## 7. Events (the normalized stream)

Envelope: `{sessionId, epoch, seq, event}`. `epoch` is a random id minted when the agent process starts (its in-memory buffers are lost on restart, so a new epoch means "resync from snapshot"). `seq` is per session, starts at 1 in each epoch, strictly increasing, no gaps. Identity-based dedup is on `(epoch, seq)` only, never on content (R3/R4).

| `event.kind` | fields | notes |
| --- | --- | --- |
| `message` | `message: Message` | upsert. `Message = {id, role:"user"|"assistant", parentId?, createdAt, modeId?, modelId?, completedAt?, error?, clientMessageId?}` — `clientMessageId` echoes `session.prompt`'s on the user message it created (it keys Cerea's attachment store) |
| `part` | `part: Part` | upsert of the full part. **Text contract:** for `text` and `reasoning` parts, the first `part` event for a part id carries the text so far; later growth arrives only as `delta`; a later `part` event for the same id never carries text that contradicts `text_so_far + deltas` (the agent converts a backend's full-text re-send into a suffix `delta`, or drops it if nothing is new). So `concat(first part.text, deltas…)` is always the current text. |
| `delta` | `messageId, partId, role, field:"text", delta` | append |
| `part.removed` | `messageId, partId` | |
| `status` | `status: "idle"|"busy"|"retry", detail?` | turn boundary signal: busy = running, idle = finished |
| `permission.asked` | `request: PermissionRequest` | `{id, sessionId, tool, title, patterns[], metadata{}, callId?, messageId?, always[]}` |
| `permission.replied` | `requestId, decision, by: "user"|"auto"` | |
| `usage` | `usage: Usage` | after each step/turn |
| `session` | `session: Session` | metadata changed (title, mode, model, autoAccept, status) |
| `error` | `message, code?` | a turn-level failure (provider error, credential dead) |
| `todo` | `todos: [{id, content, status:"pending"|"in_progress"|"completed"|"cancelled", priority?}]` | full list |

`Part = {id, messageId, role, type, …}` by `type`:
- `text` `{text, synthetic?: bool}` (synthetic parts are backend-injected; Cerea does not show them as user text)
- `reasoning` `{text}`
- `tool` `{callId, tool, status:"pending"|"running"|"completed"|"error", title?, input{}, output?, error?}`
- `file` `{mime, filename?, url?}`
- `subtask` `{sessionId?, description?, agent?}` (a subagent spawn)
- `compaction` `{auto: bool}` — a marker part on the assistant message that summarized the session (opencode: `CompactionPart`, verified against 1.18.31's `GET /doc`; carried as an ordinary part on `message.part.updated`/the transcript, not a separate event kind). `auto` distinguishes opencode's own context-overflow trigger from a person's "Compact now". opencode's OpenAPI also has richer `session.next.compaction.{started,delta,ended}`/`session.compacted` broadcast events (progress text, `reason: auto|manual`) that this backend does not map — the `compaction` part is the single signal Cerea needs, per §8.

The agent is subscribed to its backend from process start, so it has seen every event of every session touched since; for sessions untouched since start, the backend's persisted transcript is exact. The snapshot therefore equals "persisted transcript + everything applied since", with no gap.

**Usage on `Session` (`session.get`/`session.list`).** opencode's own session object carries no usage field — only assistant messages do — so the agent caches each session's latest `Usage` as it is observed on the event stream (or when `Transcript` is fetched) and answers `session.get`/`session.list` from that cache. A session this process has not touched since it started (no event, no `Transcript` call) answers with `usage: null`; the caller's `session.sync` snapshot (which does read the persisted transcript) is what backfills that case, per the no-gap guarantee above.

## 8. Cerea side (what maps to what)

- The browser-facing API stays (`/api/v2/code/v1/...?device=` + `/api/v2/code/agents/[id]/stream`), so the UI and its specs keep working; only the server behind it changes: `codeDaemon.ts` (paseo) → `machineLink` (typed ops over the socket). `@getpaseo/*` is removed.
- SSE bridge: register a fan-out listener (buffering), `session.sync` with the browser's `Last-Event-ID` (`<epoch>:<seq>`), emit (snapshot → chat frames, or the missing events), then drain buffered live events with `seq >` the sync's `seq`. SSE `id` = `<epoch>:<seq>` of the last envelope a frame came from. Epoch change → a `reset` event that makes the client re-fold from scratch. No per-frame info logs (R6).
- Mapping normalized → chat `AgentStreamUpdate` (replaces `codeTimeline.ts`): user `text` part (non-synthetic) → `user`; assistant `text` part/delta → `Stream`; `tool` part → Tool call / result / error (uuid = callId); `permission.asked/replied` → Elicitation request/resolved; `status busy` → TurnState running, `idle` → done (failed if the last assistant message carries `error`); `error` → TurnState failed; `todo` → Plan; `usage` → a side-channel frame (M3).
