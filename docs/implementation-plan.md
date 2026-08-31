# BotChats ↔ Hermes Integration — Implementation Plan

Ordered milestones. Each ends with a verifiable artifact. Everything is testable against a **local
BotsChat server + Mock AI** (see M0) — no Cloudflare account, no real API keys.

---

## M0 — Spike: protocol + crypto parity (½ day)

**Goal:** lock down the two things that must be byte-exact before any integration: the message types
and the E2E cipher.

1. Create `protocol.py` — dataclasses for `CloudInbound` / `CloudOutbound`, mirroring `types.ts`
   exactly. Use `typing`/`dataclasses`; add `to_json()`/`from_json()` that round-trip cleanly.
2. Create `e2e.py`:
   - `derive_key(password, user_id)` → `hashlib.pbkdf2_hmac("sha256", password.encode(), f"botschat-e2e:{user_id}".encode(), 310_000, 32)`.
   - `_nonce(key, context_id)` → `hmac.new(key, f"nonce-{context_id}\x01".encode(), hashlib.sha256).digest()[:16]`.
   - `encrypt(key, plaintext: str|bytes, context_id)` / `decrypt(...)` via AES-256-CTR (`cryptography.hazmat.primitives.ciphers.Cipher`).
   - `b64e`/`b64d` helpers.
3. `tests/test_e2e.py`: **port every case from `packages/e2e-crypto/e2e-crypto.test.ts`** (known
   plaintext → ciphertext → plaintext round-trips, cross-impl vectors). Also assert a fixed
   password/userId/text produces a *stable* key and nonce (determinism).
4. `tests/test_protocol.py`: serialize/deserialize every message type; assert field names match
   `types.ts` exactly (casing: `sessionKey`, `runId`, `cronJobId`, `agentId` — camelCase).

**Exit:** `pytest tests/test_e2e.py tests/test_protocol.py` green; crypto matches the TS test vectors.

**Spike environment:** `git clone --depth 1 https://github.com/botschat-app/botsChat`, run its
`npm run dev:full`-style command to get server + Mock AI + auto-login browser (see README "Option B").

---

## M1 — Chat loop (1–1.5 days)

**Goal:** type a message in the BotsChat web UI, get a Hermes agent reply (whole-message, no E2E, no
streaming).

1. `ws_client.py` — `BotsChatCloudClient`:
   - `__init__(cloud_url, account_id, pairing_token, e2e_password, get_model, on_message, on_status)`.
   - `connect()`: build `{ws|wss}://<host>/api/gateway/<accountId>?token=…`; on open send `auth`.
   - `handle_message()`: switch on type; `auth.ok` → connected + start 25 s `status` keepalive;
     `auth.fail` → close 4001; `ping` → `pong`; else → `on_message`.
   - **Immediately after `auth.ok` the server sends `task.scan.request`, `models.request`,
     `settings.notifyPreview`** — the client must tolerate receiving these before any user traffic
     (stub them in M1, implement in M4).
   - `schedule_reconnect()`: 1 s→30 s backoff, ±25 % jitter; `4009` → no reconnect.
   - `send(msg)`, `disconnect()`.
2. `adapter.py` — `BotsChatAdapter(BasePlatformAdapter)`:
   - `connect()` → build client, `.connect()`, wire `on_message=self._on_cloud`.
   - `_on_cloud(msg)`: `user.message` → decrypt-if-needed (stub for M1) → build
     `MessageEvent(text, source=build_source(chat_id=sessionKey, user_id, user_name, chat_type="dm"))`
     → `await self.handle_message(event)`. Also map `user.command`/`user.action`/`user.media` → message
     (per spec §4.1).
   - `send(chat_id, content, …)` → `client.send({type:"agent.text", sessionKey: chat_id, text: content, messageId: uuid4()})`.
3. `__init__.py` — `register(ctx)`: `ctx.register_platform(name="botschat", label="BotsChat",
   adapter_factory=…, check_fn=…, validate_config=…, required_env=[…], platform_hint=A2UI_HINTS,
   emoji="🤖")`.
4. `plugin.yaml` — manifest from spec §1.

**Exit:** `hermes status` shows `botschat (plugin)`; web UI message → agent reply round-trips. Verify
with the local BotsChat Mock AI endpoint pointed at a live Hermes gateway
(`BOTSCHAT_CLOUD_URL=http://localhost:8787`).

**Pitfall to avoid:** don't assume Hermes can *parse* the BotsChat session key — it's opaque. Store it
verbatim on the event source and echo it back. Hermes' own session key is internal.

---

## M2 — E2E encryption on chat (½–1 day)

1. In `_on_cloud` `user.message`: if `encrypted` and key present, `decrypt_text(base64→bytes, messageId)`.
2. In `send`: if key present, `encrypt_text(text, messageId)` → base64 + `encrypted: true`; set
   `notifyPreview` (first 100 chars) when `notifyPreview` setting is on.
3. Key derivation: lazily after `auth.ok` (PBKDF2 ~1–2 s) — store on the client, never block the loop.
4. Parent-thread context: decrypt `parentText` with `parentMessageId` and inject as thread context
   (mirror `channel.ts` lines ~689–708).

**Exit:** with `BOTSCHAT_E2E_PASSWORD` set in both web UI and plugin, `SELECT` on D1 `messages` shows
`encrypted=1` and no plaintext; UI still shows correct text both directions.

---

## M3 — A2UI + slash commands (½–1 day)

1. Inject the A2UI prompt hints via **`ctx.register_system_prompt_section(id, fn, max_chars=4000)`**
   (durable, cache-safe — preferred) and/or `platform_hint` in `register_platform`. The section tells
   the agent to wrap choices in ```` ```action {json} ```` blocks (`buttons`, `confirm`, `select`,
   `input`; styles `primary`/`danger`) and never use bullet lists for selectable options. (Optionally
   also bundle a `ctx.register_skill("botschat:a2ui", …)` for on-demand loading.)
2. `ctx.register_command("model", …)`, `("help", …)`, `("skills", …)` so `/model` etc. resolve in the
   gateway session. Map the model-switch path to `model.changed` emission.
3. `user.action` handler: build `[Action: kind=<kind>] User selected: "<label>"` and dispatch as a
   user message (spec §4.1).

**Exit:** an agent reply with choices renders as buttons in the web UI; clicking drives a follow-up
turn; `/skills` lists `plugin:botschat:*`.

---

## M4 — Background tasks (1–1.5 days)

1. `tasks.py`:
   - `handle_task_schedule(msg)`: translate `schedule` (`every Ns|m|h`, `at HH:MM`, `cron expr`) to a
     Hermes cron job via **`ctx.dispatch_tool("cronjob", {"action": "create"|"update", …})`** (the
     sanctioned programmatic path — runs through the registry with agent context, no `hermes cron`
     shell-out). Persist the Hermes job id in `ctx.state` (survives updates, not tied to the install
     dir). Reply `task.schedule.ack { cronJobId: <hermes-job-id>, ok }` — the DO writes that id back to
     D1, so it must be the **Hermes-side** id for new jobs.
   - `handle_task_scan_request()`: enumerate Hermes cron jobs → `task.scan.result { tasks: [...] }`.
   - `handle_task_run(msg)`: run job now; emit `job.update` (running→ok/error) + `job.output`.
   - `handle_task_delete(msg)`: remove the Hermes cron job.
   - `handle_models_request()`: emit `models.list` from Hermes' providers.
2. `standalone_sender_fn` so `deliver=botschat` cron jobs route via the adapter (out-of-process cron).
3. Thread `job.update`/`job.output` through the gateway's job/cron hooks (`on_session_*`,
   `post_tool_call` where relevant) so background runs report to the UI.

**Exit:** create a background task in the UI → appears in `hermes cron`; run it → `job.update` +
`job.output` visible in the UI job log; delete in UI → gone in Hermes.

**Pitfall:** Hermes cron semantics (prompt/schedule/delivery) differ from OpenClaw's. Normalize the
schedule strings explicitly; don't assume `every 6h` is accepted verbatim.

---

## M5 — Streaming + activity + media (1–2 days)

1. Streaming (native, v2):
   - `ctx.register_hook("on_stream_start", …)`, `("on_stream_delta", …)`, `("on_stream_end", …)`.
   - Maintain `session_id → (sessionKey, runId)` map (populated in `_on_cloud` from the last
     `handle_message` per session).
   - `on_stream_start` → `agent.stream.start`; `on_stream_delta` (text) → `agent.stream.chunk`
     (encrypt with `chunkId` context); `on_stream_end` → `agent.stream.end` + final `agent.text`.
   - Reasoning deltas → `agent.activity { kind: "reasoning" }`; tool start/end (via `pre_tool_call`/
     `post_tool_call` hooks) → `agent.activity { kind: "tool_start"/"tool_end", toolName, durationMs }`.
   - Guard against observer-queue backpressure: drop-oldest is fine; never block the token path.
2. Media:
   - Inbound: `media.py` download + E2E-decrypt (`"<messageId>:media"`); attach local path to the event.
   - Outbound: `send_media` — read file, optional encrypt, `POST /api/plugin-upload`
     (`X-Pairing-Token`), emit `agent.media` with R2 URL.

**Exit:** replies appear progressively; tool activity visible; image both directions with E2E on.

---

## M6 — Hardening, packaging, docs (1 day)

1. Reconnect: verify `4009` replacement, `auth.fail` no-reconnect, 429/503 backoff, jitter.
2. Multi-profile token lock: `acquire_scoped_lock("botschat", token)` in `connect()`.
3. `pyproject.toml` entry-point; README (setup, E2E, trust model, troubleshooting).
4. `hermes plugins install owner/repo --enable` end-to-end; `hermes plugins capabilities`.
5. Port parity audit (`search_files "openclaw"` in the plugin dir → should be zero).

**Exit:** `pytest` full suite green; install via one command; README walkthrough reproduces a chat
from scratch.

---

## M7 — Nice-to-have (parked, may implement in the future)

Items deferred from M5/M6 — not blockers; revisit when the environment supports them.

1. **Token streaming** (`agent.stream.*` end-to-end): forwarding is implemented and unit-tested, but
   the provider call never streams in this env (gate in the agent's LLM-call path not found). Net
   effect today: replies arrive whole. Revisit when the streaming gate is resolved — the plugin side
   is done; this is a verification task, not an implementation task.
2. **Live media test** (inbound vision + outbound R2 upload): implemented in `media.py`, but a live
   round-trip needs a vision-capable Hermes profile and a BotsChat media-capable client. Test with
   `scripts/roundtrip_live.py` once such a profile exists.

## M8 — Multi-agent hub: several Hermes profiles, one BotsChat account (2–3 days)

**Goal.** Chat with *multiple* Hermes profiles from one BotsChat account, each in its own channel —
without the account-connection fight documented in the README.

**Server constraint (verified in `botschat-app/botsChat` `connection-do.ts`).** The ConnectionDO keeps
exactly ONE agent socket per account: the auth handler closes every other socket tagged `"openclaw"`
with 4009 (`getWebSockets("openclaw")`, code at line ~275), and the relay picks a single socket
(`sockets.find(s => getTag(s) === "openclaw")`, line ~635). The tag is hardcoded (`serializeAttachment
({ tag: "openclaw" })`, line ~226). Consequences:

- Two Hermes profiles each opening a connection on the same account ⇒ replacement war: newest wins,
  older dies permanently (`Connection replaced by server — not reconnecting`). `BOTSCHAT_AGENT_ID` is
  identity metadata only — it does not create a second socket.
- The multi-agent protocol fields (`auth.agents: [...]`, `user.message.targetAgentId`, channel
  `openclaw_agent_id`) exist for ONE connection hosting several agents internally — that is the model
  to implement.

**Design: hub mode.** One profile (the hub, default) owns the single connection and declares all
agents; the multiplexer routes each message to the right profile's agent run.

1. **Single connection, all agents.** The hub profile's adapter connects once with
   `agents: ["main", "private", ...]` (one entry per participating profile, configured via
   `BOTSCHAT_AGENTS` / `gateway.platforms.botschat.extra.agents`). Secondary profiles do NOT open a
   connection (plugin not enabled there, or hub-mode flag suppresses connect).
2. **Inbound routing via existing multiplexer machinery.** `gateway.profile_routes` matches
   (platform, chat_id) → profile and stamps `source.profile`; under `multiplex_profiles` this
   activates the profile-scoped agent run (run.py `_profile_name_for_source`). The BotsChat session
   key is the chat_id (`agent:<agentId>:botschat:<userId>:adhoc`), so a route per agent-id prefix
   maps 1:1 to profiles, e.g.:
   ```yaml
   gateway:
     multiplex_profiles: true
     profile_routes:
       - platform: botschat
         chat_id: "agent:private:"   # prefix match
         profile: private
   ```
3. **Reply path (the open question).** Replies from a secondary profile's agent run must flow back
   through the hub's single connection. The session store already namespaces keys by profile
   (`agent:<profile>:botschat:dm:<sessionKey>`, via `set_owner_profile`), and replies target the
   source that received the message — so the gateway should route them to the botschat adapter; the
   adapter must then send over the shared socket with the original sessionKey. **Spike first**: run
   hub mode against the hosted console with two profiles and verify a `private` channel message is
   answered by the private profile's agent via the one socket.
4. **Docs.** README Profiles section: hub mode as the supported multi-agent setup (one account, one
   connection, `profile_routes`); keep the single-socket limitation note.

**Exit criteria.** Hub-mode unit tests (agents list from config; suppressed secondary connections);
live E2E: two channels (`main`, `private`) on ONE account, each answered by its own profile, no 4009
replacement, replies decrypt (E2E) in both.

---

## Testing strategy

- **Unit:** crypto parity (M0), protocol round-trip (M0), cron mapping (M4).
- **Integration (primary):** local BotsChat server + Mock AI + a real Hermes gateway on the same host.
  Drive it via the web UI and the `npx botschat` CLI.
- **E2E crypto:** cross-check against the reference `e2e-crypto.test.ts` vectors, plus a live
  browser↔plugin round-trip with the D1 `encrypted` flag asserted.
- **Failure injection:** drop WSS mid-turn (reconnect), kill the gateway mid-job (job marks `error`),
  wrong E2E password (garbled text, `[Decryption Failed]`, no crash).

## Key files to consult during implementation

- `packages/plugin/src/types.ts` — protocol contract (authoritative).
- `packages/plugin/src/ws-client.ts` — connection/auth/reconnect reference.
- `packages/plugin/src/channel.ts` — inbound dispatch, streaming, task, media, A2UI reference.
- `packages/e2e-crypto/e2e-crypto.ts` + `.test.ts` — crypto spec + vectors.
- `packages/api/src/do/connection-do.ts` — relay/auth server behavior (what the server expects).
- `migrations/*.sql` — D1 schema (what's persisted).
- Hermes: `gateway/platforms/base.py` (`BasePlatformAdapter`), `plugins/platforms/irc/` (stdlib
  reference adapter), docs `adding-platform-adapters.md`.
