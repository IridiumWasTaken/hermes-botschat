# BotsChat Internals — Technical Report

**Goal of this report:** precise enough to reimplement the *agent-side* protocol against a Hermes plugin, so a Hermes agent can be driven through the same BotsChat server + web UI that today drives OpenClaw.

**Source:** `github.com/botschat-app/botsChat`, commit examined at clone time (shallow). All paths relative to repo root. Repo version `0.1.24`, Apache-2.0.

---

## 1. Repository layout

```
packages/
  api/            Cloudflare Worker (Hono) + ConnectionDO Durable Object + all REST routes
    src/index.ts            Hono app + WebSocket upgrade routes + plugin-upload
    src/env.ts              Env bindings (DB, MEDIA, CONNECTION_DO, JWT_SECRET, FCM/APNS)
    src/do/connection-do.ts The Durable Object relay (core of the system)
    src/routes/*.ts         auth, agents, channels, tasks, jobs, models, pairing,
                            sessions, upload, push, setup, demo, dev-auth
    src/utils/*.ts          auth (JWT/PBKDF2/HMAC), id, uuid, fcm, apns, firebase, ...
  plugin/         OpenClaw channel plugin (@botschat/botschat) — THE AGENT SIDE
    index.ts                plugin entry (register() → registerChannel)
    src/channel.ts          entire plugin: gateway, outbound, incoming handler, tasks, A2UI
    src/types.ts            **the wire protocol types (CloudInbound / CloudOutbound)**
    src/ws-client.ts        outbound WSS client (reconnect, auth, ping, E2E key)
    src/e2e-crypto.ts       isomorphic AES-256-CTR + PBKDF2 (same code as packages/e2e-crypto)
    src/accounts.ts         config resolution from openclaw.json
    src/runtime.ts          holds OpenClaw PluginRuntime / API refs
    bin/botschat-setup.mjs  interactive pairing setup (writes openclaw config)
    bin/botschat-cli.mjs    skill/CLI shim
    openclaw.plugin.json    plugin manifest
    SKILL.md                OpenClaw skill (CLI usage)
  web/            React SPA (Vite + Tailwind + Capacitor)
    src/ws.ts               browser WSS client (JWT auth, E2E decrypt)
    src/e2e.ts              browser E2E key service (localStorage)
    src/store.ts            app state + message reducer
    src/components/MessageContent.tsx   A2UI renderer + ```action parser
    src/components/ChatWindow.tsx / ThreadPanel.tsx  composer / thread composer
    src/App.tsx             WS message dispatch
    src/api.ts              REST client
  cli/            Headless CLI (`botschat`) — talks to the server as a *browser* client
    src/index.ts            commander program
    src/commands/*.ts       login, logout, whoami, setup, channels, sessions, tokens,
                            models, status, chat, tasks, jobs, messages, config
    src/lib/*.ts            config, api-client, ws-client, e2e, output
  e2e-crypto/     Shared isomorphic crypto library + test vectors
android/ ios/ macos/        Capacitor shells (iOS/Android/macOS native apps)
migrations/       D1 SQL migrations (0001..0012) — full schema
docs/             screenshots + e2e-encryption-plan.md
scripts/          dev.sh, e2e verifiers, mock-openclaw.mjs
tests/            media/api/cli e2e harnesses
wrangler.toml     Worker config (D1 `botschat-db`, R2 `botschat-media`, DO `ConnectionDO`)
```

Monorepo workspaces: `packages/*`. The plugin and the `e2e-crypto` package are published; `e2e-crypto` is a dependency of both `api`-adjacent code and the web/plugin (the plugin vendors its own copy at `src/e2e-crypto.ts`).

---

## 2. WebSocket message protocol (exact)

Defined in **`packages/plugin/src/types.ts`** — the single source of truth. The envelope is a **flat JSON object**; the `type` string field is the discriminator. There is no separate `{type, payload}` wrapper. Correlation is done with `sessionKey` (the conversation/session identifier) and `messageId` (per-message UUID, also the E2E nonce context). `runId` correlates streaming events.

### 2.1 Connection endpoints

| Role | URL | Auth |
|---|---|---|
| Agent plugin (OpenClaw/Hermes) | `wss://<host>/api/gateway/<accountId|userId>?token=<pairingToken>` | pairing token (`bc_pat_…`) |
| Browser / CLI | `wss://<host>/api/ws/<userId>/<sessionId>` | JWT sent as first `{type:"auth", token}` |

The plugin connects with `accountId` (usually `"default"`); the API worker resolves it to a real `u_…` userId via the pairing token before routing into the DO (see §9).

### 2.2 Plugin → Cloud (`CloudOutbound`)

```ts
// Handshake
| { type: "auth"; token: string; agentId?: string; agentType?: string; agents?: string[]; model?: string }
| { type: "status"; connected: boolean; agents: string[]; model?: string }   // heartbeat every 25s
| { type: "pong" }                                                            // reply to ping

// Agent replies (non-streaming)
| { type: "agent.text";
    agentId?: string; sessionKey: string; text: string;
    replyToId?: string; threadId?: string; encrypted?: boolean;
    messageId?: string; notifyPreview?: string }
| { type: "agent.media";
    sessionKey: string; mediaUrl: string; caption?: string;
    replyToId?: string; threadId?: string; encrypted?: boolean;
    mediaEncrypted?: boolean; messageId?: string; notifyPreview?: string }

// Streaming
| { type: "agent.stream.start"; sessionKey: string; runId: string }
| { type: "agent.stream.chunk"; sessionKey: string; runId: string; text: string;
    encrypted?: boolean; chunkId?: string }
| { type: "agent.stream.end";   sessionKey: string; runId: string }

// Activity (reasoning / tool calls) — shown as collapsible activity rows
| { type: "agent.activity"; sessionKey: string; runId: string;
    kind: "reasoning" | "tool_start" | "tool_end";
    text?: string; toolName?: string; durationMs?: number;
    encrypted?: boolean; activityId?: string }

// A2UI (interactive UI) — supported by server/web, see §5
| { type: "agent.a2ui"; sessionKey: string; jsonl: string;
    replyToId?: string; threadId?: string; encrypted?: boolean }

// Background task (cron) sync
| { type: "task.scan.result"; tasks: Array<{
      cronJobId: string; name: string; schedule: string; agentId: string;
      enabled: boolean; instructions: string; model?: string;
      lastRun?: { status: string; ts: number; summary?: string };
      encrypted?: boolean; iv?: string }> }
| { type: "task.schedule.ack"; cronJobId: string; taskId?: string; ok: boolean; error?: string }
| { type: "job.update"; cronJobId: string; jobId: string; sessionKey: string;
    status: "running" | "ok" | "error" | "skipped";
    summary?: string; startedAt: number; finishedAt?: number;
    durationMs?: number; encrypted?: boolean }
| { type: "job.output"; cronJobId: string; jobId: string; text: string }   // streamed run output

// Models
| { type: "models.list"; models: Array<{ id: string; name: string; provider: string }> }
| { type: "model.changed"; model: string; sessionKey: string }            // /model command ack
| { type: "defaultModel.updated"; model: string }
```

### 2.3 Cloud → Plugin (`CloudInbound`)

```ts
| { type: "auth.ok"; userId?: string; agentId?: string;
    availableAgents?: Array<{ id: string; name: string; type: string;
      role: string; capabilities: string[]; status: string }> }
| { type: "auth.fail"; reason: string }

// User traffic
| { type: "user.message"; sessionKey: string; text: string; userId: string;
    messageId: string; targetAgentId?: string; mediaUrl?: string;
    // parent-message fields attached by DO for thread messages:
    parentMessageId?: string; parentText?: string; parentSender?: string;
    parentEncrypted?: number }   // 0 = plaintext, 1 = encrypted
| { type: "user.media"; sessionKey: string; mediaUrl: string; userId: string }
| { type: "user.action"; sessionKey: string; action: string; params: Record<string, unknown> }
| { type: "user.command"; sessionKey: string; command: string; args?: string }
| { type: "config.request"; method: string; params: unknown }
| { type: "ping" }

// Task management
| { type: "task.schedule"; taskId?: string; name?: string; cronJobId: string;
    agentId: string; schedule: string; instructions: string; enabled: boolean; model?: string }
| { type: "task.delete"; cronJobId: string }
| { type: "task.run"; cronJobId: string; agentId: string; instructions: string; model?: string }
| { type: "task.scan.request" }

// Models / settings
| { type: "models.request" }
| { type: "settings.defaultModel"; defaultModel: string }
| { type: "settings.notifyPreview"; enabled: boolean }
```

`CloudMessage = CloudOutbound | CloudInbound`. (Field names as authored in the repo, including the stray double `}` typos.)

### 2.4 Browser → server (extra message types, for completeness)

From `packages/api/src/do/connection-do.ts` (`handleBrowserMessage`) and `packages/web/src/ws.ts`:

- Browser sends `{type:"auth", token:<JWT>}`, `user.message`, `user.media`, `user.command`, `user.action`, plus presence frames `foreground.enter` / `foreground.leave` / `focus.update` (each carrying `sessionKey`).
- Server → browser: `auth.ok` / `auth.fail`, `connection.status {openclawConnected, defaultModel, models}`, `openclaw.disconnected`, `error`, and everything the plugin emits (relayed verbatim minus `notifyPreview`), plus `agent.stream.*`, `agent.activity`, `task.scan.result`, `job.update`.

### 2.5 Handshake sequence (plugin)

1. Plugin opens `wss://host/api/gateway/<id>?token=bc_pat_…`.
2. On open, plugin sends `{type:"auth", token, agents:[…], model?}`.
3. DO validates token (pre-verified by the API worker via `?verified=1`, else D1 lookup) and replies `{type:"auth.ok", userId}` — else `auth.fail` and closes with code `4001`.
4. Immediately after `auth.ok`, the DO sends to the plugin, in order: `task.scan.request`, `models.request`, `settings.notifyPreview`.
5. Plugin derives the E2E key *after* `auth.ok` (needs `userId` as salt).
6. Plugin heartbeats every 25s with `{type:"status", connected:true, agents, model}` and answers `ping` with `pong`.

`packages/plugin/src/ws-client.ts` details: exponential backoff (1s→30s, ±25% jitter), close code `4009` = "replaced by a newer connection, do NOT reconnect", `4001` = auth failed. HTTP `429`/`503` are honored via `Retry-After`.

---

## 3. How the OpenClaw plugin drives the agent

All in **`packages/plugin/src/channel.ts`** (~2160 lines). The plugin is a standard OpenClaw *channel plugin* (`registerChannel({ plugin })` in `index.ts`); it injects OpenClaw's `PluginRuntime` and `api` (with `registerHook`) through `src/runtime.ts`.

### 3.1 Incoming `user.message` → agent call (`handleCloudMessage`, line 649)

1. Decrypt `msg.text` if `msg.encrypted` && key present (contextId = `msg.messageId`).
2. Extract `threadId` from `sessionKey` regex `/:thread:(.+)$/`.
3. If threaded and `parentText` attached, decrypt parent (contextId = `parentMessageId`) and build a `GroupSystemPrompt` injection for thread context.
4. Download any inbound `mediaUrl` to `~/.openclaw/media/inbound/…` (decrypting with contextId `` `${messageId}:media` `` if encrypted) and set `__resolvedMedia` so OpenClaw's vision pipeline can attach it.
5. Build OpenClaw's `MsgContext`:

```ts
const msgCtx = {
  Body: text, RawBody: text, CommandBody: text, BodyForCommands: text,
  From: `botschat:${msg.userId}`,
  To: msg.sessionKey, SessionKey: msg.sessionKey,
  AccountId: ctx.accountId, MessageSid: msg.messageId,
  ChatType: threadId ? "thread" : "direct",
  Channel: "botschat", MessageChannel: "botschat", Provider: "botschat", Surface: "botschat",
  CommandAuthorized: true,
  ...(parentContext ? { GroupSystemPrompt: parentContext } : {}),
  ...(threadId ? { MessageThreadId: threadId, ReplyToId: threadId } : {}),
  ...((msg).__resolvedMedia || {}),
};
```

6. `runtime.channel.reply.finalizeInboundContext(msgCtx)` normalizes and routes the agent.
7. `runtime.channel.session.recordInboundSession(...)` records the session and sets `lastChannel = "botschat"` (so cron delivery can resolve the target).
8. Calls `runtime.channel.reply.dispatchReplyFromConfig({ ctx, cfg, dispatcher, replyOptions })`.

### 3.2 Reply delivery (the `deliver` callback)

`deliver(payload)` is the reply sink. It:
- Encrypts text (contextId = fresh `messageId`) if E2E key present, else plaintext.
- For media: uploads the file to `POST /api/plugin-upload` (header `X-Pairing-Token: <pairingToken>`) — encrypting bytes with contextId `` `${messageId}:media` `` first — then sends `agent.media` with the returned R2 signed URL. If upload fails, sends `agent.text` instead.
- If `client.notifyPreview` is on and text is encrypted, includes a truncated plaintext `notifyPreview` (≤100 chars).
- Detects model-change confirmations via regex `/Model (?:set to|reset to default)\b…/` and emits `model.changed`.

### 3.3 Streaming (partial replies)

```ts
const runId = `run_${Date.now()}_${rand}`;
const onPartialReply = async ({ text }) => {
  if (!streamStarted) { streamStarted = true; send({ type:"agent.stream.start", sessionKey, runId }); }
  const enc = await encryptForStream(client, text);          // fresh UUID contextId per chunk
  send({ type:"agent.stream.chunk", sessionKey, runId, text: enc.text,
         ...(enc.encrypted ? { encrypted:true, chunkId: enc.id } : {}) });
};
const onReasoningStream = async ({ text }) => send({ type:"agent.activity", sessionKey, runId,
   kind:"reasoning", text: enc.text, ...(enc.encrypted ? { encrypted:true, activityId: enc.id } : {}) });
```

passed via `replyOptions = { ...replyOptions, onPartialReply, onReasoningStream, allowPartialStream: true }`. After dispatch returns, if streaming started, it sends `agent.stream.end` with the same `runId`.

### 3.4 Tool-activity hooks (`registerActivityHooks`, line 129)

The plugin registers OpenClaw hooks `before_tool_call` / `after_tool_call`. They emit `agent.activity` with `kind: "tool_start"` (toolName) and `kind: "tool_end"` (toolName, durationMs, truncated result text ≤500 chars, E2E-encrypted with `activityId` context). `runId` is `""` for these.

### 3.5 Background tasks (cron)

- **`task.schedule`** → `handleTaskSchedule`: if `cronJobExists(cronJobId)` (reads `~/.openclaw/cron/jobs.json`), run `openclaw cron edit <id> …`; else `openclaw cron add --name … --every/--at/--cron … --message <instructions> --session isolated --agent <id> --model <m> [--disabled] --json` and parse the JSON to get the new `cronJobId`. Replied with `task.schedule.ack` (note: ack carries the *OpenClaw-generated* cronJobId, which the DO writes back to D1).
- **`task.delete`** → `openclaw cron rm <cronJobId>`.
- **`task.run`** → `handleTaskRun`: sends `job.update {status:"running"}` immediately, then runs the job via `runtime.cron.runJobNow` / `triggerJob` (falling back to dispatching the instructions through the agent pipeline), accumulating output as `job.output` (throttled 200ms), and finally sends `job.update {status:"ok"|"error", summary}`. Each run uses a fresh session key `agent:<agentId>:cron:<cronJobId>:run:<ts>`.
- **`task.scan.request`** → `handleTaskScanRequest`: reads `~/.openclaw/cron/jobs.json`, converts schedules to human strings (`every 30m`, `at 09:00`, `cron …`), reads last-run output via a 3-layer strategy (run log `~/.openclaw/cron/runs/<id>.jsonl` → `openclaw cron runs` CLI → session JSONL `~/.openclaw/agents/<a>/sessions/<id>.jsonl`), and replies `task.scan.result`. If E2E key present, `schedule` + `instructions` are encrypted with a random UUID contextId that is returned as the `iv` field.
- **`models.request`** → reads `~/.openclaw/openclaw.json` model keys, dedupes, splits on `/` into provider/model, replies `models.list`.
- **`settings.defaultModel`** → `openclaw config set agents.defaults.model.primary <model>`, then `defaultModel.updated`.

### 3.6 Commands and A2UI actions

- `user.command` → re-dispatched as `user.message` with text `/<command> <args>`.
- `user.action` → converted to `user.message` with text `[Action: kind=<kind>] User selected: "<label>"`.
- `user.media` → re-dispatched as `user.message` with `mediaUrl` and empty text.

---

## 4. E2E encryption (exact)

Source: **`packages/e2e-crypto/e2e-crypto.ts`** (identical copy in `packages/plugin/src/e2e-crypto.ts`).

| Parameter | Value |
|---|---|
| Key derivation | PBKDF2-SHA256, **310,000** iterations, 32-byte key |
| Salt | `"botschat-e2e:" + userId` (deterministic, domain-prefixed) |
| Cipher | AES-256-CTR (zero overhead — no tag/MAC, no padding) |
| Nonce / IV | **derived**, not random: HKDF-SHA256 *expand-only*, single HMAC round |
| Nonce derivation | `nonce = HMAC-SHA256(key, "nonce-" + contextId + 0x01)[0..16]` |
| contextId | a globally-unique string used **once** per key — the nonce source |

`contextId` semantics per field:
- Message text/caption → the `messageId` (UUID generated by sender).
- Media bytes → `` `${messageId}:media` ``.
- Stream chunk → `chunkId` (fresh UUID per chunk).
- Activity/reasoning → `activityId`.
- Job summary → `jobId`.
- Task scan `schedule`/`instructions` → a random UUID returned as the `iv` field.

Public API: `deriveKey(pwd, userId)`, `encryptText/decryptText`, `encryptBytes/decryptBytes`, `toBase64/fromBase64`. Works in both Web Crypto (browser/Workers) and Node (`node:crypto`). Base64 is standard (the docstring says "URL-safe, no padding" but the Node impl is plain `base64`; the browser fallback uses `btoa`).

**Encrypted fields / where encryption happens:**
- **Browser** encrypts `user.message.text` before sending (contextId = existing `messageId`); sets `encrypted:true`.
- **Plugin** encrypts `agent.text.text` / `agent.media.caption`, media bytes, stream chunks, activities, job summary, task-scan schedule/instructions.
- **Server (DO)** never encrypts/decrypts — it stores ciphertext base64 in D1 and relays opaque bytes. It *strips* `notifyPreview` from anything forwarded to browsers (plaintext must not reach browser WS; browsers decrypt locally).
- **Encrypted flag bitmask** in D1 `messages.encrypted` / `jobs.encrypted`: **bit 0 = text encrypted, bit 1 = media encrypted** (`(msg.encrypted?1:0) | (msg.mediaEncrypted?2:0)`).

Key management: browser caches password + derived key in `localStorage` (`botschat_e2e_pwd_cache`, `botschat_e2e_key_cache`). Plugin stores `e2ePassword` in `openclaw.json` (`channels.botschat.e2ePassword`) and derives the key at runtime from `auth.ok.userId`. CLI stores `e2ePassword`/`e2eKeyBase64` in `~/.botschat/config.json`.

Test vectors (`packages/e2e-crypto/e2e-crypto.test.ts`): deterministic (same key+contextId ⇒ same ciphertext), zero expansion (ciphertext length == UTF-8 plaintext length), wrong key/contextId ⇒ garbage (no authentication). **Caveat for reimplementation:** no authentication tag means wrong-key decryption silently produces garbage — callers must handle gracefully (they set `decryptionError` flags).

---

## 5. A2UI wire format

Two complementary mechanisms coexist:

### 5.1 `` ```action `` markdown fenced blocks (the mechanism OpenClaw actually uses)

The plugin does **not** emit `agent.a2ui`; instead it injects **prompt hints** into OpenClaw's message-tool system prompt (`A2UI_MESSAGE_TOOL_HINTS`, `channel.ts` line 72):

```ts
"- This channel renders ```action fenced code blocks as interactive clickable widgets. …
   Action block format: ```action\n{\"kind\":\"buttons\",\"prompt\":\"What next?\",\"items\":[{\"label\":\"Do X\",\"value\":\"x\",\"style\":\"primary\"},{\"label\":\"Do Y\",\"value\":\"y\"}]}\n```
   — kinds: buttons, confirm, select, input. Styles: \"primary\", \"danger\", or omit."
```

So the agent emits a fenced block tagged `` ```action `` whose body is a single-line JSON:

```json
{ "kind": "buttons" | "confirm" | "select" | "input",
  "prompt": "…",
  "items":  [ { "label": "…", "value": "…", "style": "primary"|"secondary"|"danger" } ],
  "placeholder": "…" }
```

The web client's markdown renderer (`MessageContent.tsx`) intercepts `language-action` fences, parses the JSON, and renders an `ActionCard`. On click it calls `onResolve(value,label)` → which (in `ChatWindow`/`ThreadPanel`) sends a plain `user.message` whose text is the selected `value`/`label` (NOT `user.action`). `preprocessActionBlocks` hides incomplete `` ```action `` blocks while streaming.

### 5.2 Real A2UI v0.8 JSONL protocol (`agent.a2ui` message)

The full A2UI v0.8 surface protocol is supported by the server (persists `messages.a2ui`) and rendered by the web (`A2UIRenderer` in `MessageContent.tsx`). `agent.a2ui` carries a `jsonl` string — one JSON object per line:

```jsonl
{"surfaceUpdate":{"surfaceId":"s1","components":[{"id":"c1","component":{"Text":{"text":{"literalString":"Hello"},"usageHint":"h1"}}},{"id":"c2","component":{"Button":{"label":{"literalString":"Go"},"action":{"sendMessage":"go"},"style":"primary"}}}]}}
{"beginRendering":{"surfaceId":"s1","root":"c1"}}
{"dataModelUpdate":{"surfaceId":"s1","updates":[{"path":"/x","value":{"literalString":"v"}}]}}
```

Component types rendered (subset of the spec): `Text` (usageHint h1..h5/caption/body), `Button` (action.sendMessage), `Column`, `Row`, `Card`, `List`, `Image`, `Divider`, `Icon`. `A2UIValue = string | {literalString} | {dataPath}`. Button clicks route through `onAction(action.sendMessage, action)`. The OpenClaw plugin currently only uses the `` ```action `` path; the JSONL path is a supported-but-unused-by-OpenClaw message type that a Hermes adapter *could* emit directly.

---

## 6. Server architecture

### 6.1 Stack

Cloudflare Workers + **Hono** (`packages/api/src/index.ts`). Bindings (`env.ts`, `wrangler.toml`):

- `DB` — **D1** (`botschat-db`).
- `MEDIA` — **R2** bucket (`botschat-media`), keys `media/<userId>/<filename>`.
- `CONNECTION_DO` — **Durable Object** `ConnectionDO` (SQLite-backed DO, class declared in the `[[migrations]]` block).
- Secrets: `JWT_SECRET`, `FCM_SERVICE_ACCOUNT_JSON`, `APNS_AUTH_KEY`/`KEY_ID`/`TEAM_ID`; vars `ENVIRONMENT`, `FIREBASE_PROJECT_ID` (`botschat-130ff`), `GOOGLE_*_CLIENT_ID`, `PUBLIC_URL`.

### 6.2 ConnectionDO (`packages/api/src/do/connection-do.ts`)

**One DO instance per user** — `CONNECTION_DO.idFromName(userId)`. Responsibilities:

- Holds the persistent WSS from the agent ("openclaw" tag) and any number of browser WSS ("browser:<sessionId>" tags).
- **Bidirectional relay**: browser `user.*` messages → agent socket; agent `agent.*`/`job.*`/`task.*` messages → all authenticated browser sockets (`broadcastToBrowsers`).
- Uses the **WebSocket Hibernation API** (accepts sockets, tags via `serializeAttachment`, zero compute when idle).
- **Auth**: agent socket auth = pairing-token validation (fast-tracked when the API worker pre-verified via `?verified=1`); browser socket auth = JWT verification, with `payload.sub` matched against the DO's stored `userId`.
- **Persistence** (D1): agent messages (`agent.text`/`agent.media`/`agent.a2ui`) and user messages via `persistMessage`; `task.schedule.ack` → `UPDATE tasks SET openclaw_cron_job_id`; `task.scan.result` → sync/auto-create `tasks` + persist last-run `jobs`; `models.list` → cached in DO storage; `job.update` → `INSERT OR REPLACE jobs`.
- **Push**: on agent message, if no foreground browser socket (grace windows: 15s background, 30s disconnect), sends FCM (web/android) and/or APNs (iOS) notifications. `notifyPreview` is the only plaintext allowed in push payloads.
- **Media caching**: for `agent.media` with an external URL (and not already `mediaEncrypted`), downloads it (SSRF-guarded: https-only, blocks private/reserved ranges, ≤20MB, blocks SVG/scripts) into R2 and re-signs the URL.
- **Connection replacement**: only the newest authenticated "openclaw" socket survives; older ones closed with `4009`.
- **Demo mode**: demo users (`u_demo_…` via `isDemoUserId`) get a built-in mock agent (`handleDemoMockReply`) instead of an OpenClaw socket.

### 6.3 REST surface (Hono)

- Public: `/api/health`, `/api/auth` (login/refresh), `/api/dev-auth`, `/api/demo`, `/api/setup` (`/init`, `/cloud-url`, `/status`), `/api/media/:userId/:filename` (signed URL or Bearer), `/api/plugin-upload` (pairing-token auth), `/api/messages/:userId` (proxied to DO).
- Protected (Bearer JWT): `/api/me`, `/api/agents`, `/api/channels` (+ nested `/channels/:id/tasks`, `/tasks/:id/jobs`, `/channels/:id/sessions`), `/api/models`, `/api/pairing-tokens`, `/api/push-tokens`, `/api/upload`, `/api/tasks`, `/api/task-scan`, `/api/connection/:userId/status`.
- WS upgrade: `/api/gateway/:connId` (agent) and `/api/ws/:userId/:sessionId` (browser). Rate limiting via the Cache API (10s cooldown) to protect DO wake-ups.

### 6.4 D1 schema (migrations/0001…0012)

Final shape (net of all migrations):

```
users(id PK, email UNIQUE, password_hash, display_name, settings_json,
      auth_provider DEFAULT 'email', firebase_uid UNIQUE, created_at, updated_at)
pairing_tokens(id PK, user_id FK, token UNIQUE /*bc_pat_*/, label,
      last_connected_at, last_ip, connection_count, revoked_at, created_at)
channels(id PK, user_id FK, name, description, openclaw_agent_id, system_prompt,
      created_at, updated_at)                                  -- was "projects" pre-0002
tasks(id PK, channel_id FK, name, kind CHECK('background'|'adhoc'),
      openclaw_cron_job_id, session_key, enabled, created_at, updated_at)
      -- schedule/instructions/model REMOVED in 0008: they live in OpenClaw's jobs.json
sessions(id PK, channel_id FK, user_id FK, name, session_key UNIQUE,
      created_at, updated_at)
messages(id PK, user_id, session_key, thread_id, sender CHECK('user'|'agent'),
      text BLOB, media_url, a2ui BLOB, encrypted INTEGER, created_at)
jobs(id PK, task_id, user_id, session_key, status CHECK('running'|'ok'|'error'|'skipped'),
      started_at, finished_at, duration_ms, summary BLOB, encrypted, created_at)
threads(id PK, task_id FK, parent_message_id, thread_session_key, created_at)
deleted_cron_jobs(cron_job_id PK, user_id, deleted_at)
push_tokens(id PK, user_id FK, token, platform CHECK('web'|'ios'|'android'),
      created_at, updated_at, UNIQUE(user_id, token))
skill_usage, voice_keywords, chat_search (FTS5)   -- legacy/vestigial
```

Key insight: **D1 stores only structural metadata.** Schedule, instructions, and model for background tasks are **owned by OpenClaw** (its `~/.openclaw/cron/jobs.json`), delivered to the frontend live via `task.scan.result`. The default model is stored in DO durable storage, not D1. This keeps the server agent-agnostic.

---

## 7. Concept mapping

| BotsChat concept | Server model | Agent (OpenClaw) primitive |
|---|---|---|
| **Channel** | `channels` row | an OpenClaw **agent** (`openclaw_agent_id`, e.g. `main`); `system_prompt` is a channel-level override |
| **Session** | `sessions` row with `session_key` | OpenClaw **session key**. Format `agent:<agentId>:botschat:<userId>:adhoc` (direct chat) or `agent:<agentId>:botschat:<userId>:ses:<sesId>`. The `session_key` is passed verbatim as the OpenClaw `SessionKey`. |
| **Thread** | messages with `thread_id`; `sessionKey` gains suffix `:thread:<messageId>` | OpenClaw thread (via `MessageThreadId`/`ReplyToId`); parent message text is injected as `GroupSystemPrompt`. |
| **Background Task** | `tasks` row, kind=`background`, `openclaw_cron_job_id` | OpenClaw **CronJob** (jobs.json entry). |
| **Job** | `jobs` row (one execution) | one CronJob run; produced by `runtime.cron.runJobNow` or the fallback dispatch; result is the run summary. |
| **Ad-hoc chat task** | `tasks` row kind=`adhoc` (created alongside each channel) | the direct-message conversation (its `session_key`). |

How web+server model them: the web sends `user.message {sessionKey}` and the DO routes by `sessionKey` (thread detection is purely the `:thread:` suffix). Sessions/channels/tasks are CRUD via REST; live state (models, connection, task scan, job output, streams) flows over the WS.

---

## 8. CLI (`packages/cli`)

Binary `botschat` (commander). Config `~/.botschat/config.json` (0600): `{url, token, refreshToken, userId, e2ePassword, e2eKeyBase64, defaultChannel, defaultSession}`.

Commands: `login`, `logout`, `whoami`, `setup`, `channels`, `sessions`, `tokens` (pairing tokens), `models`, `status`, `chat`, `tasks`, `jobs`, `messages`, `config` (`config e2e --password …` sets the E2E password). Global flags: `--json`, `--url`, `--config`.

**Authentication** (`commands/login.ts`): OAuth browser flow — CLI starts a localhost HTTP server on a random port, opens `/start` → 302 to `https://console.botschat.app/?cli_port=…&cli_state=…`, the web app redirects back to `http://127.0.0.1:<port>/callback?token=…&refreshToken=…&userId=…`. Also supports `--email/--password` (dev only) and `--dev --secret` (dev-auth bypass).

**Chat** (`commands/chat.ts`) connects as a **browser client** to `/api/ws/<userId>/<randomSessionId>` with JWT auth, then sends `user.message {sessionKey, text, messageId}` (with optional `targetAgentId`). It reads `agent.stream.start/chunk/end`, `agent.text`, `agent.activity`. Modes: single-shot, `--async`, `--pipe`, interactive REPL (`/quit`, `/exit`). So the CLI is *not* an agent-side client — it is a headless *user* client.

Note the plugin package also ships `botschat-setup` (`bin/botschat-setup.mjs`): interactive pairing — takes a `bc_pat_…` token or email/password (`POST /api/setup/init`), then writes `openclaw config set channels.botschat.{cloudUrl,pairingToken,enabled}` and restarts the gateway.

---

## 9. Pairing / auth

### 9.1 Users & sessions

- **Sign-up/sign-in**: email+password (password hashed with PBKDF2-SHA256, **600,000** iterations, random 16-byte salt, format `pbkdf2:<iter>:<saltHex>:<hashHex>`, legacy SHA-256 supported w/ auto-rehash) — email login disabled outside development; or **Firebase OAuth** (Google/GitHub/Apple) via `idToken` verification, users keyed by `firebase_uid`.
- **Sessions**: HS256 JWT (`jose` library) — access token (30 min, `{sub, type:"access"}`) + refresh token (7 days, `{type:"refresh"}`). Issuer `"botschat"`. Secret = `JWT_SECRET` (dev fallback `botschat-dev-secret-local-only`).

### 9.2 Pairing tokens (agent auth)

- Generated by the server: `generatePairingToken()` = `"bc_pat_" + 32 chars` (CSPRNG, rejection-sampled alphabet `a-z0-9`).
- Stored in `pairing_tokens` with `revoked_at` (soft-delete), `last_ip`, `connection_count`. Max **10 active** per user. Full token returned only at creation; list shows `bc_pat_…<last8>`.
- REST: `GET/POST/DELETE /api/pairing-tokens` (Bearer auth).
- **Plugin auth** (agent side): the plugin connects to `/api/gateway/:accountId?token=<pairingToken>`. The API worker looks up `pairing_tokens` → `user_id`, then forwards to the DO with `?verified=1`; the DO fast-tracks the plugin's `auth` frame (otherwise it re-validates locally with a 30s cache). The plugin *also* re-sends the token in the `auth` frame body (used only when not pre-verified).
- Token reuse in setup: `POST /api/setup/init` reuses a `label='CLI setup'` token created in the last 5 minutes.
- `POST /api/plugin-upload` authenticates with the `X-Pairing-Token` header.

### 9.3 Media URLs

R2 objects are served at `/api/media/:userId/:filename` behind an **HMAC-SHA256 signed URL** (`?expires=<ts>&sig=<url-safe b64>` over `${userId}/${filename}:${expires}`) with 1h expiry, or a Bearer token. Signatures are re-signed on every history load (`refreshMediaUrl`).

---

## 10. OpenClaw-specific vs. generic (what a Hermes adapter must implement)

### 10.1 Generic / server-owned (do **not** reimplement — the server already abstracts these)

- The entire **wire protocol** (§2): the DO relay is agent-agnostic. The socket is tagged `"openclaw"` purely for bookkeeping; nothing in the protocol payloads is OpenClaw-specific.
- **ConnectionDO**, D1/R2, REST routes, web UI, CLI, demo mock, push notifications, media caching/signing, pairing tokens, JWT.
- **E2E crypto** (§4) — fully specified and language-independent.
- **A2UI** (§5) — rendered by the web; an agent just needs to emit `` ```action `` JSON or `agent.a2ui` JSONL.
- The `session_key` string conventions (`agent:<agentId>:botschat:<userId>:adhoc`, `:thread:`, etc.) are *server-side* conventions the agent must honor, but they are not OpenClaw concepts.

### 10.2 OpenClaw-specific (what a Hermes adapter replaces)

Everything in `packages/plugin/src/channel.ts` that touches OpenClaw internals:

1. **Runtime plumbing**: `registerChannel`, `PluginRuntime`, `runtime.channel.reply.finalizeInboundContext / dispatchReplyFromConfig / createReplyDispatcherWithTyping`, `runtime.channel.session.recordInboundSession / resolveStorePath`, `api.registerHook("before_tool_call"/"after_tool_call")`, `runtime.cron.runJobNow / triggerJob`. A Hermes adapter maps these to Hermes's own agent-run/session/hook equivalents.
2. **The `MsgContext` shape** (`Body`, `From`, `To`, `SessionKey`, `MessageSid`, `ChatType`, `Channel`, `CommandAuthorized`, `GroupSystemPrompt`, `MediaPath`, `MessageThreadId`) — OpenClaw's internal inbound-context contract.
3. **Shelling out to the `openclaw` CLI** for `cron add/edit/rm`, `config set agents.defaults.model.primary`, `cron runs`.
4. **Reading OpenClaw's on-disk state**: `~/.openclaw/cron/jobs.json` (cron jobs + schedules), `~/.openclaw/cron/runs/<id>.jsonl` (run logs), `~/.openclaw/agents/<a>/sessions/<id>.jsonl` + `sessions.json` (session transcripts), `~/.openclaw/openclaw.json` (models), `~/.openclaw/media/inbound/` (inbound media staging).
5. **Config storage location**: `channels.botschat.{cloudUrl, pairingToken, e2ePassword, enabled}` in `openclaw.json` — a Hermes plugin would store the equivalent (cloud URL, pairing token, E2E password) in Hermes config instead.
6. **Model introspection**: `readAgentModel()` / model enumeration read OpenClaw's `agents.defaults.model.primary` and `agents.defaults.models`.

### 10.3 Minimum a Hermes adapter must implement (agent-side checklist)

1. Open `wss://<cloudUrl>/api/gateway/<accountId>?token=<pairingToken>` and send `{type:"auth", token, agents:[…], model?}` on open.
2. Handle `auth.ok` (capture `userId`, derive E2E key via PBKDF2(310k, salt `botschat-e2e:`+userId)), `auth.fail`, `ping`→`pong`.
3. Answer `task.scan.request` → `task.scan.result` (or send empty `{tasks:[]}`), `models.request` → `models.list`, honor `settings.notifyPreview` / `settings.defaultModel`.
4. On `user.message` / `user.command` / `user.action` / `user.media`: decrypt (contextId=`messageId`), route to the agent keyed by `sessionKey`, and emit `agent.stream.start` → `agent.stream.chunk`* → `agent.stream.end` (optionally `agent.activity` for reasoning/tools), falling back to a single `agent.text`.
5. On `task.schedule` / `task.delete` / `task.run`: manage the equivalent of cron jobs, reply `task.schedule.ack` (with the *Hermes-side* job id when creating new), emit `job.update` + `job.output`.
6. Send a `status` heartbeat every ~25s and reconnect with exponential backoff, treating close code `4009` as "do not reconnect" and `4001` as auth failure.
7. E2E: encrypt all outgoing text/media/stream/activity/job/task-scan fields with the specified contextId rules (§4), and upload media to `POST /api/plugin-upload` (header `X-Pairing-Token`) before sending `agent.media`.

---

## Appendix A — Key file-by-path index

| Concern | File |
|---|---|
| Wire protocol types | `packages/plugin/src/types.ts` |
| Plugin WS client / auth / reconnect | `packages/plugin/src/ws-client.ts` |
| Incoming message → agent dispatch + streaming | `packages/plugin/src/channel.ts` (`handleCloudMessage`, ~line 649) |
| Cron/task/models handlers | `packages/plugin/src/channel.ts` (lines 1156–2159) |
| A2UI prompt hints | `packages/plugin/src/channel.ts` (`A2UI_MESSAGE_TOOL_HINTS`, line 72) |
| E2E crypto (shared) | `packages/e2e-crypto/e2e-crypto.ts` |
| Browser WS client + decrypt | `packages/web/src/ws.ts` |
| Browser E2E key service | `packages/web/src/e2e.ts` |
| A2UI renderer + ```action parser | `packages/web/src/components/MessageContent.tsx` |
| DO relay (all routing) | `packages/api/src/do/connection-do.ts` |
| Hono app + WS upgrade + plugin-upload | `packages/api/src/index.ts` |
| D1 schema | `migrations/0001..0012_*.sql` |
| JWT / PBKDF2 / HMAC media signing | `packages/api/src/utils/auth.ts` |
| Pairing token routes | `packages/api/src/routes/pairing.ts` |
| Setup/onboarding + token issuance | `packages/api/src/routes/setup.ts` |
| Channel/agent mapping | `packages/api/src/routes/channels.ts` |
| Task scheduling push | `packages/api/src/routes/tasks.ts` |
| Upload routes | `packages/api/src/routes/upload.ts` |
| ID / pairing-token generation | `packages/api/src/utils/id.ts` |
| CLI program | `packages/cli/src/index.ts` |
| CLI login (OAuth) | `packages/cli/src/commands/login.ts` |
| CLI chat | `packages/cli/src/commands/chat.ts` |
| Plugin pairing setup | `packages/plugin/bin/botschat-setup.mjs` |
