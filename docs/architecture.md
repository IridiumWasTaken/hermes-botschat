# BotChats ↔ Hermes Integration — Architecture

## 1. High-level topology

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  BotChats (reused as-is, unchanged)                                         │
│                                                                             │
│   Web UI (React)  macOS/iOS/Android apps (Capacitor)   CLI (npx botschat)   │
│            │                        │                          │           │
│            └──────────────┬─────────┴──────────────────────────┘           │
│                           │  WSS (browser auth via JWT)                    │
│                           ▼                                                  │
│   Cloudflare Worker (Hono API) ──► ConnectionDO (per-user WSS relay)        │
│                           │        + D1 (users/channels/sessions/messages/  │
│                           │          jobs/tasks) + R2 (media)               │
└───────────────────────────┼─────────────────────────────────────────────────┘
                            │  outbound WSS (pairing token)
                            │  /api/gateway/<accountId>?token=bc_pat_…
                            ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Hermes (this is the part we build)                                         │
│                                                                             │
│   ~/.hermes/plugins/platforms/botschat/                                     │
│   ├── plugin.yaml        (manifest: kind=platform, requires_env)            │
│   ├── adapter.py         BotsChatAdapter(BasePlatformAdapter)               │
│   ├── ws_client.py       BotsChatCloudClient (WSS + reconnect + auth)       │
│   ├── protocol.py        CloudInbound/CloudOutbound dataclasses (typed)     │
│   ├── e2e.py             PBKDF2 + AES-256-CTR + HKDF (byte-exact)           │
│   ├── tasks.py           cron ↔ task.schedule/scan/run/delete mapping       │
│   ├── media.py           inbound download/decrypt, outbound R2 upload       │
│   └── __init__.py        register(ctx) entry point                          │
│                                                                             │
│   GatewayRunner ──► AIAgent (the model + tools + skills + memory)           │
│   Cron scheduler ──► cron store (jobs)                                      │
└─────────────────────────────────────────────────────────────────────────────┘
```

The plugin is the **agent-side counterpart** of BotChats' existing OpenClaw plugin. Everything above
the dashed line is off-the-shelf.

## 2. Component responsibilities

### 2.1 `BotsChatCloudClient` (ws_client.py)

A faithful port of BotChats' `BotsChatCloudClient` (`packages/plugin/src/ws-client.ts`, ~260 lines)
to Python `websockets` (or `aiohttp`):

- Open `wss://<cloudUrl>/api/gateway/<accountId>?token=<pairingToken>`.
- On open, send `auth { token, agentType:"hermes", agents:["hermes"], model }`.
- Wait for `auth.ok` (carries `userId`) → mark connected, start 25 s `status` keep-alive, then lazily
  derive the E2E key (`PBKDF2(password, userId)`).
- `auth.fail` → close 4001, do **not** reconnect.
- Exponential backoff (1 s → 30 s, ±25 % jitter) on drop; honor `4009` ("replaced") as intentional close.
- Honor HTTP 429 `Retry-After` / 503 backoff from `unexpected-response`.
- Dispatch parsed `CloudInbound` messages to the adapter's handler.

### 2.2 `BotsChatAdapter` (adapter.py)

Subclass of `gateway.platforms.base.BasePlatformAdapter`, registered via `ctx.register_platform()`:

- `connect()` — construct `BotsChatCloudClient`, connect, and bind its `on_message` callback.
- `disconnect()` — graceful close.
- `send(chat_id, content, reply_to=None, metadata=None)` — encrypt if E2E enabled, emit
  `agent.text` (or `agent.media` when `metadata` carries a media URL).
- `send_typing()` — no-op (BotsChat has no typing concept; the web UI shows its own spinners driven by
  `agent.stream.*` / `agent.activity`).
- Inbound: each `user.message` → build `MessageEvent(text=…, source=build_source(chat_id=sessionKey, user_id=…))`
  → `await self.handle_message(event)`.

**Streaming** (v2): register `on_stream_start` / `on_stream_delta` / `on_stream_end` observer hooks;
correlate `session_id` → `(sessionKey, runId)` via a map the adapter maintains; emit
`agent.stream.start/chunk/end`. Tool/reasoning activity → `agent.activity`
(`tool_start`/`tool_end`/`reasoning`).

### 2.3 `protocol.py`

Typed dataclasses mirroring `packages/plugin/src/types.ts` `CloudOutbound` / `CloudInbound` exactly.
This file is the contract — keep it in lock-step with upstream `types.ts`.

### 2.4 `e2e.py`

Byte-exact port of `packages/e2e-crypto/e2e-crypto.ts`:

- `derive_key(password, user_id)`: `hashlib.pbkdf2_hmac("sha256", password, b"botschat-e2e:"+user_id, 310_000, 32)`.
- `nonce(key, context_id)`: HKDF-expand single step — `hmac.new(key, b"nonce-"+context_id+b"\x01", sha256).digest()[:16]`.
- `encrypt(key, plaintext, context_id)` / `decrypt(...)`: AES-256-CTR with that nonce.
- `b64encode` / `b64decode` for JSON transport.

### 2.5 `tasks.py`

Implements the cron bridge:

| Cloud message | Action |
|---|---|
| `task.schedule` | create/update a Hermes cron job; reply `task.schedule.ack` |
| `task.delete` | remove the Hermes cron job |
| `task.run` | run now; emit `job.update` (running→ok/error) + `job.output` |
| `task.scan.request` | list Hermes cron jobs; emit `task.scan.result` |
| `models.request` | emit `models.list` from Hermes' configured providers |
| `settings.defaultModel` | set Hermes default model; emit `defaultModel.updated` |

### 2.6 `media.py`

Inbound: download `mediaUrl` (resolve relative → cloud base), E2E-decrypt with `contextId =
"<messageId>:media"`, write to a temp dir, attach `MediaPath` to the `MessageEvent`.
Outbound: read local file/URL, optional E2E-encrypt, `POST /api/plugin-upload` with
`X-Pairing-Token`, emit `agent.media` with the returned R2 URL.

## 3. Key data flows

### 3.1 Inbound chat turn

```
Browser ──WSS──► ConnectionDO ──user.message──► BotsChatAdapter.on_message
  └─ (maybe E2E-encrypted text)                        │ decrypt (if encrypted)
                                                       ▼
                                        MessageEvent(sessionKey, text, …)
                                                       │
                                                       ▼
                                        handle_message() ──► GatewayRunner ──► AIAgent
                                                       │
                            (v1) send() ──agent.text──► ConnectionDO ──► Browser
                            (v2) on_stream_delta ──agent.stream.chunk──► Browser
```

### 3.2 Outbound cron delivery (background task)

```
Hermes cron scheduler fires ──► job output ──► job.update / job.output ──► ConnectionDO ──► UI job log
       (deliver=botschat via standalone_sender_fn ──► send() ──► agent.text ──► UI channel)
```

### 3.3 A2UI round-trip

```
AIAgent emits ```action{json}``` (prompt-injected) ──► agent.text ──► web UI renders buttons
   └─ user clicks ──► user.action ──► adapter converts to "[Action: …] User selected: …" ──► agent
```

## 4. Configuration surface

`plugin.yaml` (`kind: platform`) declares:

```yaml
name: botschat
label: BotsChat
kind: platform
version: 0.1.0
requires_env:
  - name: BOTSCHAT_CLOUD_URL
    description: "BotsChat server URL (e.g. https://console.botschat.app or http://localhost:8787)"
    prompt: "BotsChat server URL"
    url: "https://console.botschat.app"
  - name: BOTSCHAT_PAIRING_TOKEN
    description: "Pairing token from the BotsChat dashboard (bc_pat_…)"
    prompt: "BotsChat pairing token"
    password: true
optional_env:
  - name: BOTSCHAT_E2E_PASSWORD
    description: "Optional E2E encryption password (must match the web UI)"
    prompt: "E2E password (or empty)"
    password: true
```

Runtime config lands under `gateway.platforms.botschat.{enabled, extra}` and is enabled via
`plugins.enabled: [botschat]`.

## 5. Repo / packaging layout

Standalone plugin repo (per Hermes' "someone else's product" guidance) — **not** merged into core:

```
hermes-botschat/
├── plugin.yaml
├── __init__.py            # register(ctx)
├── adapter.py
├── ws_client.py
├── protocol.py
├── e2e.py
├── tasks.py
├── media.py
├── pyproject.toml         # [project.entry-points."hermes_agent.plugins"] hermes_botschat = "..."
├── tests/
│   ├── test_e2e.py        # ported vectors from e2e-crypto.test.ts
│   ├── test_protocol.py   # frame (de)serialization round-trips
│   └── test_tasks.py      # cron mapping
└── README.md
```

Install: `hermes plugins install owner/hermes-botschat --enable` (or drop into
`~/.hermes/plugins/platforms/botschat/`). Pip entry-point and Nix paths also work.

## 6. Milestone sequencing (see implementation plan for detail)

1. **M0 — Spike:** static port of `types.ts` + `e2e.ts`; unit-test crypto parity against upstream vectors.
2. **M1 — Chat loop:** `ws_client` + `adapter` + `handle_message`/`send`; whole-message text chat works end-to-end against a local BotsChat server + Mock AI.
3. **M2 — E2E encryption** on the chat path.
4. **M3 — A2UI + slash commands** (`/model`, `/help`, `/skills`) via `platform_hint` + `register_command`.
5. **M4 — Background tasks** (`task.schedule/scan/run/delete`, `job.update/output`).
6. **M5 — Streaming** (`agent.stream.*` + `agent.activity`) and media.
7. **M6 — Hardening:** reconnect, rate-limit, auth-fail, multi-profile token lock, docs, pack.
