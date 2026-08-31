# hermes-botschat

BotsChat gateway channel adapter for Hermes Agent — chat with your Hermes agents from the [BotsChat](https://github.com/botschat-app/botsChat) web UI, mobile apps, and CLI. The plugin speaks BotsChat's WebSocket protocol from a `BasePlatformAdapter`, so the existing BotsChat server + front-ends work unchanged; the server remains zero-knowledge (E2E encryption included).

## Why BotsChat?

I wanted a simple & straightforward plug-and-play solution to chat with my agent(s) from my phone without giving up any privacy.

Having tried different messaging platforms for Hermes Agent, I was not satisfied with any of them. Most of them don't offer real E2E encryption or require manual tinkering by installing additional daemons. I tried Matrix but is has issues with the olm library on Mac OS (see [here](https://github.com/NousResearch/hermes-agent/pull/95411)). 
Finally, I stumbled upon [BotsChat](https://github.com/botschat-app/botsChat), designed for OpenClaw, and decided to develop a compatible plugin for Hermes.

## Key Features

hermes-botschat is not fully feature-complete yet. Namely, token-streaming and live media compatibility has not been implemented yet. Feel free to add it!
For all other features, see [BotsChat](https://github.com/botschat-app/botsChat).

## Status

| Milestone | State |
|---|---|
| M0 protocol + E2E crypto (TS-verified vectors) | ✅ 122 unit tests green |
| M1 WebSocket client + adapter (mock ConnectionDO) | ✅ 122 unit tests green |
| M1 real-server interop (plaintext + E2E) | ✅ `scripts/interop_smoke.py` |
| M1 full gateway round-trip (web UI → Hermes agent) | ✅ `scripts/roundtrip_live.py` (test profile `botschat-test`) |
| M2 E2E on the live chat path (ciphertext-only at rest) | ✅ verified live; D1 holds ciphertext, bitmask set |
| M3 A2UI + slash commands | ✅ ```action blocks live; /help /skills /model work; model.changed + models.list wired |
| M4 background tasks | ✅ schedule/run/delete live via REST; scheduled-run job.update via on_session_end hook |
| M5 streaming + activity + media | ✅ tool activity live (agent.activity); streaming + media implemented, live verification deferred to M7 (provider stream gate not found in this env) |
| M6 hardening, packaging | ✅ reconnect verified (4009/auth.fail no-reconnect, 429/503 backoff, jitter, reset); multi-profile token lock; pyproject entry point (wheel + git install E2E verified); README (E2E, trust model, troubleshooting); port-parity audit clean |
| M7 nice-to-have (parked) | ⏳ token streaming (provider must stream), live media round-trip — may implement in the future |

## Deployment

For running your own or a hosted version of hermes-botschat, see the official BotsChat documentation. 
This repo only contains the Hermes plugin.

## Installation

Two ways to install — pick the one that matches how you run Hermes:

- **[Option A — Desktop app](#option-a--desktop-app)** — install from the app's Plugins settings page.
- **[Option B — TUI / command line](#option-b--tui--command-line)** — install with `hermes plugins install`.

Both end in the same result: a `botschat` entry in `hermes plugins list` and a
live connection from Hermes to your BotsChat server.

### Prerequisites

- **Hermes Agent** — the desktop app or the `hermes` CLI, any recent version.
- **A BotsChat account** (see [Deployment](#deployment)) — the hosted service
  at <https://console.botschat.app>, or your own BotsChat server running
  locally (e.g. `http://localhost:8787`).
- **A pairing token** — the bot credential for your BotsChat account. Create
  one in the BotsChat app (Settings → pairing tokens / dashboard); it looks
  like `bc_pat_…`. It is a password for the bot: anyone holding it can drive
  your agent, so keep it secret.

### Choose a configuration style

The plugin needs three settings, which you can provide **either** as
environment variables **or** in `config.yaml`. Env vars take precedence when
both are set, so pick one place per setting:

| Setting | Env var | `config.yaml` key |
|---|---|---|
| BotsChat server URL | `BOTSCHAT_CLOUD_URL` | `gateway.platforms.botschat.extra.cloudUrl` |
| Pairing token | `BOTSCHAT_PAIRING_TOKEN` | `gateway.platforms.botschat.extra.pairingToken` |
| E2E password *(optional)* | `BOTSCHAT_E2E_PASSWORD` | `gateway.platforms.botschat.extra.e2ePassword` |

If you configure via `config.yaml` only, you must also set
`gateway.platforms.botschat.enabled: true` explicitly — the env-var path
enables the platform automatically at gateway start, the config path does not.

All four settings can also be edited from the desktop app: **Settings →
Messaging → BotsChat** card (it shows every field, including the optional E2E
password, on Hermes builds with plugin optional-env support).

### Option A — Desktop app

1. **Install the plugin.** Open **Settings → Plugins** and click **Install
   plugin**. In the dialog:
   - **Repo** — enter the plugin source: `iridiumwastaken/hermes-botschat`
     (or a full git URL).
   - **Target** — choose **Agent plugin**. This installs into the Hermes
     backend (`~/.hermes/plugins/`).
     (The *Desktop* target is for UI packages and will not install this
     adapter.)
   - Optionally tick **Enable agent plugin after install**.
   - Confirm — the app runs the same backend install as the CLI.
2. **Configure the settings** — either in the app or on disk:

   **Settings → Messaging** *(recommended)* — find the **BotsChat** card and
   fill in its fields: server URL and pairing token, plus the optional E2E
   password (see [E2E encryption](#e2e-encryption)). On Hermes builds that
   surface plugin optional env vars the card shows all four fields; on older
   builds the E2E field is absent — set `BOTSCHAT_E2E_PASSWORD` in the
   profile's `config.yaml` instead.

   **Or on disk** — the same values live in the profile's `config.yaml`.
3. **Enable the channel** (unless you ticked *Enable after install*):
   ```bash
   hermes config set gateway.platforms.botschat.enabled true
   ```
4. **Restart the gateway** from the app (or `hermes gateway restart`). On
   start, the plugin authenticates with your pairing token.
5. **Verify.** Open the BotsChat web UI — the agent should appear online. Send
   a message; you should get a reply.

### Option B — TUI / command line

1. **Install the plugin** (clones the repo into `~/.hermes/plugins/`):
   ```bash
   hermes plugins install iridiumwastaken/hermes-botschat --enable
   ```
   Hermes scans plugins for security on install; this plugin talks to the
   network, so expect a *caution* verdict. Review the findings and confirm —
   or re-run with `--force` — if you trust the source.
2. **Configure the settings** (see Step 0) — choose one:

   **Environment variables** — put these in your profile's `.env` so they
   survive reboots (`export` in a shell only lasts for that session):
   ```bash
   export BOTSCHAT_CLOUD_URL=https://console.botschat.app
   export BOTSCHAT_PAIRING_TOKEN=bc_pat_...
   export BOTSCHAT_E2E_PASSWORD=...   # optional; must match the web UI
   ```

   **Or `config.yaml`** (`~/.hermes/profiles/<profile>/config.yaml`):
   ```yaml
   gateway:
     platforms:
       botschat:
         enabled: true
         extra:
           cloudUrl: https://console.botschat.app
           pairingToken: bc_pat_...
           e2ePassword: ...   # optional
   ```

   **Or the CLI** (same effect as editing the file):
   ```bash
   hermes config set gateway.platforms.botschat.extra.cloudUrl https://console.botschat.app
   hermes config set gateway.platforms.botschat.extra.pairingToken bc_pat_...
   hermes config set gateway.platforms.botschat.enabled true
   ```
3. **Enable the channel** (env-var setups are auto-enabled, but setting it
   explicitly never hurts):
   ```bash
   hermes config set gateway.platforms.botschat.enabled true
   ```
4. **Restart the gateway** so the plugin loads and connects:
   ```bash
   hermes gateway restart
   ```
5. **Verify:**
   ```bash
   hermes plugins list    # botschat present, source: git
   ```
   then open the BotsChat web UI and send a message — the agent should reply.

## E2E encryption

- Set `BOTSCHAT_E2E_PASSWORD` **identically** in the plugin and the BotsChat
  web UI (chat settings). Plugin side: the profile's `.env`, `config.yaml`,
  or the desktop app's **Settings → Messaging → BotsChat** card. The web UI
  and the plugin derive the same key.
- Cipher (byte-exact port of `packages/e2e-crypto`): PBKDF2-SHA256 (310,000
  iterations) → AES-256-CTR; per-message nonce derived from the message id.
  **No tag, no padding — confidentiality only, not tamper-proof.** Don't
  advertise it as authenticated encryption.
- With E2E on, the server stores ciphertext only (`messages.encrypted` bitmask)
  — the server stays zero-knowledge.
- Wrong or mismatched password → U+FFFD garbage (or `[Decryption Failed]`).
  The gateway reads `BOTSCHAT_E2E_PASSWORD` at process start: after changing
  it, restart with `hermes -p <profile> gateway restart`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Replies arrive whole, never token-by-token | Provider doesn't stream in this env. `agent.stream.*` forwarding has not yet been implemented |
| U+FFFD garbage both directions | E2E password mismatch between plugin and web UI; restart the gateway after fixing |
| `[Decryption Failed]` in chat | Key derivable but a message failed to decrypt — usually a stale key after a password change |
| `pairing token already in use by another profile` | Another gateway profile holds the token lock; stop it (stale locks are reaped on gateway start) |
| Enabled but never connects | `BOTSCHAT_CLOUD_URL` / `BOTSCHAT_PAIRING_TOKEN` unset — add them in the profile `.env` or in Settings → Messaging → BotsChat; gateway restart needed |
| `hermes plugins install` blocked | Caution verdict (network plugin) — review findings, `--force` if trusted |
| Cron jobs don't reach chat | Set `BOTSCHAT_HOME_CHANNEL` to a sessionKey and use `deliver=botschat` |
| `/model` seems ignored | Applies on the next reply to that session (`model.changed` emission) |

# Plugin reference
## Layout

```
plugin.yaml      manifest (kind: platform)
__init__.py      register(ctx) -> ctx.register_platform("botschat", ...)
adapter.py       BotsChatAdapter(BasePlatformAdapter)
ws_client.py     outbound WSS client (auth, backoff, keepalive, E2E key)
protocol.py      CloudInbound/CloudOutbound dataclasses (types.ts port)
e2e.py           PBKDF2(310k) + AES-256-CTR + HKDF nonce (byte-exact port)
media.py         inbound download/decrypt, outbound R2 upload
tasks.py         cron <-> task.schedule/scan/run/delete bridge
pyproject.toml   pip packaging (entry point hermes_agent.plugins -> botschat)
scripts/interop_smoke.py   real-server interop harness
tests/           pytest suite (conftest mirrors Hermes' plugin loader)
```
## Profiles & multi-agent setups

The plugin is an ordinary channel adapter — it runs in your normal Hermes
profile; **no dedicated profile is required**. (The `botschat-test` profile
used during development is a testing artifact, not a requirement.)

Running the plugin in several profiles at once works, with one rule: **each
live connection needs its own pairing token** (a BotsChat account can issue
several).

- **Same token, same machine** — the second profile fails to connect with
  `pairing token already in use by another profile`. This is a deliberate
  machine-local scoped lock (M6): it prevents two agents double-answering the
  same chat.
- **Same token, different machines** — the BotsChat server itself replaces the
  older connection (close code 4009, no reconnect), so one token effectively
  supports one live bot no matter where it runs.
- **Different tokens (or different servers)** — fully independent
  connections; each profile is its own agent relaying into BotsChat. This is
  the supported multi-profile mode.

Configuration is per-profile: each profile has its own `.env` / `config.yaml`,
and the desktop app's Settings → Messaging card has an **"Applies to"** scope
selector to edit each profile's fields from the same page.

> **Multiplexer note:** when several profiles are served by a single gateway
> process (multiplexed profiles), the plugin reads its `BOTSCHAT_*` settings
> from the gateway's *process* environment, which is shared by all served
> profiles — per-profile env values don't separate in that mode. Use separate
> gateways when profiles need genuinely different connections. (In-process,
> tool-activity attribution is also best-effort: the activity hooks attach to
> the first connected adapter.)

## Tests

```bash
# Any interpreter with hermes-agent importable (the venv that ships with
# your Hermes install works). Adapter tests need the Hermes source tree:
# installed hermes-agent resolves automatically, otherwise point
# HERMES_SOURCE at a checkout (or have `hermes` on PATH).
python -m pytest tests/ -q
```

## Interop smoke test (real BotsChat server)

```bash
# in a checkout of botschat-app/botsChat:
npm install && npm install-scripts approve esbuild workerd && npm rebuild esbuild workerd
npm run db:migrate
npx wrangler dev --config wrangler.toml \
    --var ENVIRONMENT:development --var DEV_AUTH_SECRET:devsecret \
    --var JWT_SECRET:test-jwt-secret --port 8787

# then, from this repo:
python scripts/interop_smoke.py --url http://127.0.0.1:8787 --secret devsecret            # plaintext
python scripts/interop_smoke.py --url http://127.0.0.1:8787 --secret devsecret --e2e-password x  # E2E
python scripts/roundtrip_live.py --url http://127.0.0.1:8787 --secret devsecret          # live agent (needs gateway)
python scripts/roundtrip_live.py --url http://127.0.0.1:8787 --secret devsecret --e2e-password x  # live E2E
```

## Trust model & security

- **In-process Python, not sandboxed.** The plugin runs with full Hermes
  permissions. Install only from sources you trust; pin installs with
  `--ref <sha>`.
- **Secrets**: pairing token + E2E password live in the profile `.env`
  (`requires_env`), never logged.
- **The pairing token is the trust anchor** — anyone holding it can drive the
  agent. Restrict inbound senders with `BOTSCHAT_ALLOWED_USERS` (comma-separated
  BotsChat user ids; empty = pairing-token trust).
- **Multi-profile safety**: each gateway takes a machine-local scoped lock on a
  hash of the token. A second profile trying to bind the same token fails with
  `lock_conflict` instead of producing duplicate replies.
- **Install-time scan**: `hermes plugins install` runs a security scan; a
  network-capable plugin like this one gets a caution verdict. Review the
  findings and `--force` if you trust the source.

# Development

See [CONTRIBUTING.md](./CONTRIBUTING.md) for full development guide.

# Author
Fully made with AI (Hermes Agent, Deepseek v4 Flash & Deepseek v4 Pro) under supervision of iridiumwastaken.

# License
Apache-2.0

© 2026 iridiumwastaken