# BotChats ↔ Hermes Integration — Feasibility Analysis

## Verdict

**Yes — fully feasible, and architecturally clean.** BotChats can be integrated with Hermes as a
first-class Hermes **platform plugin** (`~/.hermes/plugins/platforms/botschat/`), with **zero
changes to the BotChats server or web UI**.

The decisive fact: **the BotChats server is agent-agnostic.** The Cloudflare worker (Hono) +
`ConnectionDO` Durable Object + D1/R2 stack only relays opaque JSON frames over WebSocket and
stores ciphertext BLOBs it cannot decrypt. Every piece of OpenClaw-specific logic lives in the
**OpenClaw plugin** (`packages/plugin/src/`), not the server. The server's only "knowledge" of
OpenClaw is a cosmetic `tag: "openclaw"` string on the socket attachment and some column names in
early D1 migrations (which migration `0008_remove_openclaw_fields.sql` is actively deleting).

Hermes has a first-class extension point that is the *exact analog* of an OpenClaw channel plugin:
`ctx.register_platform(name, label, adapter_factory, ...)` → a `BasePlatformAdapter` subclass that
opens a persistent connection, receives inbound `MessageEvent`s via `self.handle_message(event)`,
and sends responses via `self.send(chat_id, content)`. This is the same surface used by the bundled
Discord/Telegram/IRC/Teams/WeCom/Google Chat/LINE adapters.

In short: **write one Hermes plugin that speaks the BotsChat WebSocket protocol; reuse BotsChat's
existing server, Slack-like web UI, iOS/Android/macOS apps, CLI, and E2E encryption as-is.**

---

## What each side provides

### Hermes side (the agent runtime we're plugging into)

| Capability | Mechanism |
|---|---|
| Add a new messaging channel, no core edits | `ctx.register_platform()` in `plugins/platforms/<name>/` |
| Adapter lifecycle | `BasePlatformAdapter.connect()` / `disconnect()` / `send()` / `send_typing()` / `get_chat_info()` |
| Inbound routing | `await self.handle_message(MessageEvent(text=…, source=…, message_id=…))` → GatewayRunner → AIAgent |
| Config | `gateway.platforms.botschat.enabled` + `extra` dict, or env vars via `requires_env` / `env_enablement_fn` |
| Streaming | Gateway-wide `streaming.enabled` + per-platform `display.platforms.<name>.streaming` (edit-based); observer hooks `on_stream_start` / `on_stream_delta` / `on_stream_end` |
| Scheduling | Built-in cron (scheduler + `cronjob` tool + `hermes cron`); platform `cron_deliver_env_var` / `standalone_sender_fn` |
| Auth / consent | `plugins.enabled` allow-list; `requires_env`; capability consent; `allowed_users_env` / `allow_all_env` |
| Skills/slash commands | `ctx.register_command()`, `ctx.register_skill()` — map cleanly to BotsChat's `/model`, `/help`, `/skills` command bar |

### BotChats side (the front-end + relay we're reusing)

| Capability | What it gives us for free |
|---|---|
| Web UI | Slack-like Channel → Session → Thread hierarchy, command bar, model selector, debug log |
| Native apps | macOS (signed/notarized DMG), iOS, Android (Capacitor shells over the same web UI) |
| WebSocket relay | `ConnectionDO` per user; browser ↔ agent message routing; reconnect/backoff semantics |
| Persistence | D1 (users, channels, tasks, sessions, messages, jobs) + R2 (media) |
| E2E encryption | AES-256-CTR + PBKDF2(310k) — spec is fixed and isomorphic (Web Crypto + Node) |
| Scheduled tasks | Background-task model with job history, surfaced in the UI |
| CLI | `npx botschat` login/chat/management with full E2E support |
| A2UI | ` ```action ` fenced blocks rendered as clickable buttons/radios/cards in the web UI |

---

## Biggest hurdles and how to overcome them

### H1. Reimplementing the agent side from scratch (OpenClaw → Hermes internals)

**The hurdle.** The OpenClaw plugin is tightly coupled to OpenClaw's runtime
(`runtime.channel.reply.dispatchReplyFromConfig`, `finalizeInboundContext`, `recordInboundSession`,
`createReplyDispatcherWithTyping`). None of those APIs exist in Hermes. This is a **reimplementation,
not a port**.

**Why it's manageable.** The actual protocol is tiny — ~25 JSON message types (`types.ts`) — and
Hermes' `BasePlatformAdapter` + `handle_message()` + `send()` already encapsulate everything
`dispatchReplyFromConfig` does. The OpenClaw plugin is ~2,100 lines; the Hermes equivalent will be
significantly smaller because the gateway does the heavy lifting.

**Mitigation.** Port only the *protocol* (types, encryption, framing), not the OpenClaw runtime glue.
Build a single `BotsChatAdapter(BasePlatformAdapter)` + a `BotsChatCloudClient` (WebSocket client,
~260 lines in OpenClaw, trivially portable to Python `websockets`/`aiohttp`).

### H2. Streaming correlation (Hermes stream → BotsChat `agent.stream.*`)

**The hurdle.** BotsChat has a native stream protocol (`agent.stream.start/chunk/end` keyed by
`runId`). Hermes' built-in gateway streaming is *edit-based* (send-then-edit) and its native token
stream is exposed via **global** observer hooks (`on_stream_delta`) keyed by `session_id`/`turn_id`,
not by an adapter's chat_id.

**Mitigations (pick per milestone):**
- **v1 (simplest):** whole-message delivery only — `send()` → single `agent.text` frame. Zero streaming
  complexity; the UI shows a complete reply. Ship this first.
- **v2:** register `on_stream_start` / `on_stream_delta` / `on_stream_end` observer hooks in the
  plugin, maintain a `session_id → (sessionKey, runId)` map (populated from `handle_message` /
  `on_session_start`), and forward deltas as `agent.stream.chunk`. The observer queue is host-owned and
  off the token path, so it won't stall the agent loop.
- Declare `supports_streaming` so the gateway doesn't try its edit-based path against a protocol that
  already streams natively.

### H3. Session-key and identity mapping

**The hurdle.** BotsChat session keys look like `agent:botschat:botschat:u_xxx:ses:ses_xxx` (and
threads carry `:thread:<id>`); Hermes builds its own `agent:<profile>:<platform>:<chat>:<thread>` keys.
The web UI treats the `sessionKey` as opaque, but the plugin must echo back the *same* key the cloud
assigned.

**Mitigation.** The adapter stores the inbound `msg.sessionKey` verbatim on the `MessageEvent` source
and echoes it on every outbound frame. Hermes never needs to *understand* the key — it just treats it
as the chat_id. Threads: parse `:thread:<id>` from the key (as OpenClaw does) and pass `threadId`
through on outbound frames.

### H4. Background-task (cron) mapping

**The hurdle.** BotsChat background tasks are modeled as OpenClaw cron jobs; the plugin must create,
edit, delete, run, and scan them via the agent's cron store, and report `task.scan.result` /
`job.update` / `job.output` back to the cloud. Hermes has its own cron system with a different CLI
and storage.

**Mitigation.** Map 1:1:
- `task.schedule` → create/update a Hermes cron job (`cronjob` tool / `hermes cron` / cron store).
- `task.scan.request` → list Hermes cron jobs, emit `task.scan.result` with `cronJobId`, `name`,
  `schedule`, `instructions`, `enabled`, `lastRun`.
- `task.run` → trigger the job immediately, emit `job.update` + `job.output`.
- `task.delete` → remove the Hermes cron job.
- Store the `cronJobId` (Hermes job id) so edits/removes correlate. Use `cron_deliver_env_var` +
  `standalone_sender_fn` so `deliver=botschat` cron jobs route back through the adapter.

### H5. E2E encryption parity (must match byte-for-byte)

**The hurdle.** The cipher is fully specified but unforgiving: PBKDF2-SHA256, **310,000 iterations**,
`salt = "botschat-e2e:" + userId`, 32-byte key; AES-256-CTR with a 16-byte nonce derived via
HKDF-SHA256 expand-only (`info = "nonce-" + contextId`, `T(1) = HMAC(PRK, info || 0x01)`), where
`contextId = messageId`. No MAC/tag, no padding. Any mismatch → garbled text.

**Mitigation.** Reimplement in Python using `hashlib.pbkdf2_hmac`, `hmac`, and a CTR cipher
(`cryptography` or `Crypto.Cipher.AES`). Port the test vectors from
`packages/e2e-crypto/e2e-crypto.test.ts` verbatim as pytest cases. Derive the key *after* `auth.ok`
returns `userId` (PBKDF2 ~1–2 s; do it lazily, off the hot path — exactly as OpenClaw does).

### H6. A2UI (interactive widgets)

**The hurdle.** BotsChat renders ` ```action ` fenced code blocks (single-line JSON: `buttons`,
`confirm`, `select`, `input`) as clickable widgets; clicks come back as `user.action`. Hermes' agent
won't emit these by default.

**Mitigation.** This is prompt-level, not code-level. The plugin injects the A2UI message-tool hints
(via the platform's `platform_hint` in `register_platform`, or a bundled skill) so the agent wraps
choices in ` ```action ` blocks. `user.action` inbound is converted to a plain
`[Action: kind=…] User selected: "…"` message (exactly as OpenClaw does) and fed back to the agent.

### H7. Cosmetic "OpenClaw" branding + agent identity

**The hurdle.** The server stamps plugin sockets `tag: "openclaw"` and the web UI may hardcode
"OpenClaw" in its agent-type label / connection status.

**Mitigation.** The `auth` frame already carries `agentType`/`agents`/`agentId`, which the server
relays. A Hermes plugin sends `agentType: "hermes"`, `agents: ["hermes"]`. If the web UI hardcodes
the label, this is a one-line cosmetic patch in `packages/web` (or a PR upstream); it does **not**
affect protocol compatibility. Not a blocker.

### H8. Media (inbound vision, outbound files)

**The hurdle.** Inbound `mediaUrl` must be downloaded (and E2E-decrypted with `contextId =
"<messageId>:media"`) to a local file before the agent can see it; outbound media must be uploaded to
R2 via `POST /api/plugin-upload` with `X-Pairing-Token`, optionally E2E-encrypted.

**Mitigation.** Direct port of the OpenClaw `readMedia` / upload logic (~100 lines). Hermes'
`MessageEvent` carries media metadata; the agent's vision pipeline reads the local file path.

### H9. Plugin consent / trust model

**The hurdle.** User-installed platform plugins are opt-in (`plugins.enabled`) and in-process Python
(not sandboxed). A BotsChat plugin holds a pairing token + E2E password.

**Mitigation.** Declare secrets via `requires_env` (prompted at install), keep the E2E password out of
logs, and document the trust model. This matches how every other channel adapter works — nothing new.

---

## Risk summary

| Risk | Severity | Likelihood | Notes |
|---|---|---|---|
| OpenClaw→Hermes reimplementation scope | Medium | Certain | Protocol small; mitigated by porting protocol only |
| Streaming correlation complexity | Medium | Certain | v1 ships whole-message; v2 adds native stream |
| E2E crypto mismatch | High | Low–Medium | Fixed spec; port test vectors; verify round-trip |
| Cron semantics drift (Hermes vs OpenClaw cron) | Medium | Medium | Map to Hermes cron; accept minor feature delta |
| Web UI "OpenClaw" hardcoding | Low | Medium | Cosmetic; optional PR |
| BotsChat server drift (upstream changes protocol) | Low | Low | Pin a protocol version; track upstream |

**Net: green light.** Proceed to architecture and spec.
