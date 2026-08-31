# BotChats ↔ Hermes Integration — Specification Sheet

**Plugin id:** `botschat` · **kind:** `platform` · **location:** `plugins/platforms/botschat/`
**Target:** Hermes Agent (any recent version with `ctx.register_platform()`)
**Protocol source of truth:** `packages/plugin/src/types.ts` @ botschat-app/botsChat (pinned)

---

## 1. Config schema

`plugin.yaml` env vars (auto-surfaced in `hermes config`):

| Env var | Required | Secret | Purpose |
|---|---|---|---|
| `BOTSCHAT_CLOUD_URL` | yes | no | Server URL, e.g. `https://console.botschat.app` or `http://localhost:8787` |
| `BOTSCHAT_PAIRING_TOKEN` | yes | yes | `bc_pat_…` from the dashboard |
| `BOTSCHAT_E2E_PASSWORD` | no | yes | Optional E2E password; must equal the web UI's |
| `BOTSCHAT_ALLOWED_USERS` | no | no | Comma-separated BotsChat user ids allowed to talk to the agent (empty = pairing-token trust) |

Config path: `gateway.platforms.botschat.{enabled: true, extra: {…}}`.
Enable: `plugins.enabled: [botschat]` (user-installed platform plugins are opt-in).

## 2. Connection & auth

- Endpoint: `{ws|wss}://<cloudUrl>/api/gateway/<accountId>?token=<pairingToken>` (ws for `http://`, wss otherwise). `accountId` defaults to `"default"`.
- On open → send `auth { token, agentType: "hermes", agents: ["hermes"], model }`.
- Expect `auth.ok { userId, availableAgents? }` (or `auth.fail { reason }`).
- After `auth.ok`: mark connected, begin 25 s keep-alive `status { connected, agents, model }`, then lazily derive E2E key.
- Reconnect: exponential backoff 1 s → 30 s, ±25 % jitter. Close code `4009` = replaced → do not reconnect. `auth.fail` → close 4001, no reconnect.
- Ping/pong: reply `pong` to `ping`.

## 3. E2E encryption (must match upstream byte-for-byte)

| Parameter | Value |
|---|---|
| KDF | PBKDF2-HMAC-SHA256, **310,000** iterations |
| Salt | UTF-8 `"botschat-e2e:" + userId` |
| Key | 32 bytes (AES-256) |
| Cipher | AES-256-CTR |
| Nonce | 16 bytes = HKDF-SHA256 expand-only: `HMAC(key, b"nonce-"+contextId+b"\x01")[:16]` |
| contextId | the message `messageId` (per-message, globally unique) |
| Media contextId | `"<messageId>:media"` |
| Overhead | none (no tag, no padding) |
| Transport | base64 of ciphertext in the `text`/`summary`/`a2ui` field + `encrypted: true` |

Failure handling: decryption errors → emit `"[Decryption Failed]"`, log, continue (CTR has no auth).

## 4. WebSocket message contract

### 4.1 Cloud → Plugin (inbound)

| Type | Key fields | Handler |
|---|---|---|
| `auth.ok` | `userId`, `agentId?`, `availableAgents?` | mark connected, derive key |
| `auth.fail` | `reason` | close, no reconnect |
| `user.message` | `sessionKey`, `text`, `userId`, `messageId`, `targetAgentId?`, `mediaUrl?`, `parentMessageId?`, `parentText?`, `parentSender?`, `parentEncrypted?` | decrypt → `MessageEvent` → `handle_message` |
| `user.media` | `sessionKey`, `mediaUrl`, `userId` | → user.message w/ mediaUrl |
| `user.action` | `sessionKey`, `action`, `params` | → `[Action: kind=…] User selected: "…"` message |
| `user.command` | `sessionKey`, `command`, `args?` | → `/<command> <args>` message |
| `ping` | — | reply `pong` |
| `task.schedule` | `taskId?`, `name?`, `cronJobId`, `agentId`, `schedule`, `instructions`, `enabled`, `model?` | create/update Hermes cron |
| `task.delete` | `cronJobId` | remove Hermes cron |
| `task.run` | `cronJobId`, `agentId`, `instructions`, `model?` | run now |
| `task.scan.request` | — | emit `task.scan.result` |
| `models.request` | — | emit `models.list` |
| `settings.defaultModel` | `defaultModel` | set Hermes default model |
| `settings.notifyPreview` | `enabled` | toggle plaintext push previews |

### 4.2 Plugin → Cloud (outbound)

| Type | Key fields | When |
|---|---|---|
| `auth` | `token`, `agentId?`, `agentType?`, `agents?`, `model?` | on connect |
| `status` | `connected`, `agents`, `model?` | 25 s keep-alive |
| `pong` | — | on `ping` |
| `agent.text` | `sessionKey`, `text`, `replyToId?`, `threadId?`, `encrypted?`, `messageId?`, `notifyPreview?` | final reply |
| `agent.media` | `sessionKey`, `mediaUrl`, `caption?`, `replyToId?`, `threadId?`, `encrypted?`, `mediaEncrypted?`, `messageId?` | media reply |
| `agent.stream.start` | `sessionKey`, `runId` | first token (v2) |
| `agent.stream.chunk` | `sessionKey`, `runId`, `text`, `encrypted?`, `chunkId?` | per delta (v2) |
| `agent.stream.end` | `sessionKey`, `runId` | stream done (v2) |
| `agent.activity` | `sessionKey`, `runId`, `kind` (`reasoning`|`tool_start`|`tool_end`), `text?`, `toolName?`, `durationMs?` | activity (v2) |
| `agent.a2ui` | `sessionKey`, `jsonl`, `replyToId?`, `threadId?`, `encrypted?` | reserved |
| `task.scan.result` | `tasks[]` (`cronJobId`, `name`, `schedule`, `agentId`, `enabled`, `instructions`, `model?`, `lastRun?`) | on scan request |
| `task.schedule.ack` | `cronJobId`, `taskId?`, `ok`, `error?` | after schedule applied |
| `job.update` | `cronJobId`, `jobId`, `sessionKey`, `status` (`running`|`ok`|`error`|`skipped`), `summary?`, `startedAt`, `finishedAt?`, `durationMs?`, `encrypted?` | job lifecycle |
| `job.output` | `cronJobId`, `jobId`, `text` | streaming job output |
| `models.list` | `models[]` (`id`, `name`, `provider`) | on `models.request` |
| `model.changed` | `model`, `sessionKey` | after `/model` |
| `defaultModel.updated` | `model` | after applying default |

## 5. Concept mapping

| BotsChat | Hermes primitive |
|---|---|
| Channel (workspace per agent) | Hermes profile / agent instance |
| Session | gateway session (`sessionKey`) |
| Thread | thread id embedded in `sessionKey` (`:thread:<id>`) |
| Background Task | Hermes cron job |
| Job (task execution) | cron run log entry |
| Skill command (`/skills`) | `ctx.register_skill()` namespaced as `plugin:botschat:*` + slash commands |
| Model switch (`/model`) | `hermes model` / config default |
| A2UI widget | ` ```action ` fenced block in the assistant reply |

## 6. Adapter contract (`BasePlatformAdapter`)

- `connect(*, is_reconnect) -> bool`
- `disconnect() -> None`
- `send(chat_id, content, reply_to=None, metadata=None) -> SendResult`
- `send_typing()` (no-op; BotChats UI derives its own activity from `agent.activity`/stream)
- `get_chat_info(chat_id) -> {name, type}` (type `dm` or `thread`)
- Inbound: `await self.handle_message(MessageEvent(text, message_type=TEXT, source=build_source(chat_id=sessionKey, user_id, user_name, chat_type), message_id))`

`register_platform` extras: `check_fn` (WebSocket lib + crypto deps present), `validate_config`,
`env_enablement_fn`, `required_env`, `platform_hint` (A2UI hints), `cron_deliver_env_var`,
`standalone_sender_fn`, `max_message_length=4096`, `emoji="🤖"`.

## 7. Dependencies

- `websockets` (or `aiohttp`) for the WSS client.
- `cryptography` (or `pycryptodome`) for AES-CTR — OR pure `hashlib`+`hmac` for KDF/nonce + a CTR impl.
- Prefer minimal: `hashlib`/`hmac` cover KDF + nonce; add `cryptography` only for AES-CTR.
- No server-side changes; no BotsChat repo fork required.

## 8. Security & trust

- Plugin is in-process Python (not sandboxed) — document this; recommend pinning install via
  `hermes plugins install owner/repo --ref <sha>`.
- Pairing token + E2E password live in env (`.env`) / `requires_env`, never logged.
- Inbound `user.*` payloads are untrusted data: validate/coerce types, never `eval`; A2UI `params`
  are user-controlled.
- Authorization: `BOTSCHAT_ALLOWED_USERS` allowlist (else pairing-token trust = anyone with the token).
- E2E is confidentiality-only (CTR, no integrity) — matches upstream design; do not advertise as
  tamper-proof.

## 9. Acceptance criteria

1. Fresh Hermes install + `plugins install` + env vars → `hermes status` shows `botschat (plugin)`.
2. Connecting to a local BotsChat server logs "Authenticated with BotsChat cloud" + "Task scan complete".
3. Text message from web UI → correct Hermes agent reply appears in the UI (round-trip < a few seconds).
4. With E2E enabled on both sides: messages encrypt/decrypt correctly; D1 stores `encrypted=1` ciphertext; server-side dump shows no plaintext.
5. ````action```` blocks render as clickable widgets; clicking sends `user.action` and the agent continues.
6. `/model`, `/help`, `/skills` work from the command bar.
7. Create/enable/disable/delete a background task in the UI → mirrored in `hermes cron` and vice-versa; job runs produce `job.update` + `job.output` in the UI.
8. Streaming (v2): reply appears progressively, not as one block.
9. Kill the network → adapter reconnects with backoff; a second instance replaces the first (4009) without a loop.
10. Media: image sent from UI reaches the agent's vision pipeline; an agent-emitted file reaches the UI.
