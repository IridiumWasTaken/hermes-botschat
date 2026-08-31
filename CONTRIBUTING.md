# Contributing to hermes-botschat

Thanks for considering a contribution! This plugin is a port of BotsChat's
agent-side reference implementation (`packages/plugin` in
[botschat-app/botsChat](https://github.com/botschat-app/botsChat)) onto
Hermes' platform-adapter API. Two constraints are non-negotiable:

1. **The protocol must stay in lock-step with upstream.** Every message type,
   field name, and casing comes from `packages/plugin/src/types.ts`. Change
   `protocol.py` only when upstream does.
2. **The E2E cipher must stay byte-exact.** PBKDF2-SHA256 (310,000 iterations)
   + AES-256-CTR + HKDF-derived nonce, no tag, no padding. Any drift breaks
   interoperability with the web UI and mobile apps. The vectors in
   `tests/test_e2e.py` are ported from
   `packages/e2e-crypto/e2e-crypto.test.ts` and must keep passing.

## Quick Start

Get from clone to green tests in a few commands:

```bash
git clone git@github.com:iridiumwastaken/hermes-botschat.git
cd hermes-botschat
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m pytest tests/ -q
```

Notes:

- `pip install -e .` installs the runtime dependencies (`websockets`,
  `cryptography`) and makes the `botschat` package importable through the
  wheel layout in `pyproject.toml`.
- The full suite (including the adapter tests) needs Hermes' `gateway.*` /
  `hermes_cli.*` importable. Easiest: run the tests with the Python
  interpreter from a Hermes Agent install — its venv already has hermes-agent
  available. Otherwise `tests/conftest.py` resolves a source tree from the
  `HERMES_SOURCE` env var or the `hermes` executable on PATH.
- No Hermes install at hand? The protocol/crypto/ws-client tests run standalone:
  `python -m pytest tests/test_e2e.py tests/test_protocol.py tests/test_ws_client.py`

## Development setup

- **Python ≥ 3.10** — the plugin's floor (`pyproject.toml` sets
  `requires-python = ">=3.10"`; the code uses `X | None` union syntax). In
  practice the plugin runs inside Hermes Agent, which requires 3.11–3.13, so
  develop and test on 3.11+ (the suite here runs on 3.11).
- A Hermes Agent source tree must be importable for the adapter tests (they
  import `gateway.*` / `hermes_cli.*`). `tests/conftest.py` resolves it
  automatically: an installed hermes-agent, else the `HERMES_SOURCE` env var,
  else the `hermes` executable on PATH. No hardcoded paths.
- Dependencies: `websockets>=13`, `cryptography>=41` (see `pyproject.toml`).
  The plugin is a flat directory plugin — `plugin.yaml` + modules at the root.

## Project structure

The repo is a **flat directory plugin** — Hermes' loader imports the plugin
directory as a package, so all modules sit at the root next to `plugin.yaml`:

```
plugin.yaml      manifest: name, kind: platform, required/optional env vars
__init__.py      entry point — re-exports register() from adapter.py
adapter.py       BotsChatAdapter(BasePlatformAdapter) + register(ctx) wiring
ws_client.py     BotsChatCloudClient — WSS, auth, keepalive, backoff, E2E key
protocol.py      CloudInbound/CloudOutbound dataclasses (types.ts port)
e2e.py           byte-exact PBKDF2 + AES-256-CTR + HKDF crypto port
tasks.py         cron <-> task.schedule/scan/run/delete bridge
media.py         inbound download/decrypt, outbound R2 upload
scripts/         live-server harnesses (interop_smoke, roundtrip_live, …)
tests/           pytest suite; conftest fabricates the botschat package
docs/            analysis, architecture, spec, implementation plan
research/        background research notes
pyproject.toml   pip packaging (wheel re-maps the flat files into a package)
```

How it runs: the gateway loads the plugin and calls `register(ctx)` (in
`adapter.py`), which registers the platform adapter, the A2UI system-prompt
section, and the stream/tool-activity hooks. See *What to change where* below
for the ownership map.

## Running the tests

```bash
python -m pytest tests/ -q
```

The suite currently has 167 tests. Add tests for anything you change — for
protocol/crypto work the upstream TS tests are the authoritative oracle.

## What to change where

| File | Owns |
|---|---|
| `protocol.py` | `CloudInbound`/`CloudOutbound` dataclasses — mirror `types.ts` exactly |
| `e2e.py` | PBKDF2 / AES-CTR / HKDF port — byte-exact |
| `ws_client.py` | WSS connection, auth handshake, keepalive, backoff/reconnect |
| `adapter.py` | `MessageEvent` mapping, send/encrypt, A2UI, slash commands, hooks |
| `tasks.py` | cron ↔ `task.schedule`/`scan`/`run`/`delete` bridge |
| `media.py` | inbound download/decrypt, outbound R2 upload |
| `plugin.yaml` | manifest + required/optional env declarations |

## Live testing

Unit tests can't cover the real server round-trip. For anything behavioral,
run against a local BotsChat server:

1. Clone `botschat-app/botsChat` and start it (see the README's
   *Interop smoke test* section for the exact `wrangler dev` recipe).
2. Plaintext + E2E protocol checks:
   ```bash
   python scripts/interop_smoke.py --url http://127.0.0.1:8787 --secret devsecret
   python scripts/interop_smoke.py --url http://127.0.0.1:8787 --secret devsecret --e2e-password x
   ```
3. Full agent round-trip (needs a running Hermes gateway):
   ```bash
   python scripts/roundtrip_live.py --url http://127.0.0.1:8787 --secret devsecret
   ```

## Conventions & pitfalls

Read the [Pitfalls learned](#pitfalls-learned) section before touching the adapter — it
collects hard-won facts (hooks are invoked synchronously, `replyToId` files
replies into phantom threads, A2UI instructions are frozen per session, cron
session ids embed underscores, …).

- **Relative imports only** (`from .e2e import ...`) — Hermes loads plugins as
  packages.
- **Protocol fields stay camelCase** (`sessionKey`, `runId`, `cronJobId`) —
  never snake_case them.
- **Never forward `replyToId` on `agent.text`** — only `threadId` from the
  `:thread:` session-key suffix.
- **Secrets stay out of logs** — pairing tokens and E2E passwords are never
  logged.
- Env-var behavior is read at gateway start: after changing `BOTSCHAT_*`
  values, restart the gateway before assuming the new value took effect.

## Pull requests

1. **Fork** this repository.
2. **Branch** from an up-to-date `main`:
3. **Make your change** — keep it focused; add or adjust tests.
4. **Push to your fork** and open the pull request against upstream.

**PR checklist:**

- [ ] Full suite green: `python -m pytest tests/ -q`
- [ ] Change is focused (one logical unit per PR)
- [ ] Tests added or adjusted for the change
- [ ] Status table in the README updated if a milestone moved forward
- [ ] For protocol/crypto changes: the upstream commit/PR you're tracking is
      cited in the PR description

## Known open work (good first contributions)

The M7 items in the README's Status table are implemented but not live-verified:

- **Token streaming** — `agent.stream.*` forwarding is wired and unit-tested,
  but the provider call doesn't stream in this environment (replies arrive
  whole). Verifying against a streaming-capable provider would close it.
- **Live media round-trip** — inbound vision + outbound R2 upload are
  implemented in `media.py`; a live test needs a vision-capable profile.

## Pitfalls learned

- **websockets >= 13 has no `ws.open` property** — use `ws.state is State.OPEN`
  (see `_is_open` in `ws_client.py`).
- **Hermes loads plugins as packages** — intra-plugin imports must be
  **relative** (`from .e2e import ...`), and `request.path` in websockets 15
  includes the query string.
- **`BotsChatCloudClient.send` is async** — always `await` it; an un-awaited
  call silently drops the frame.
- **`decrypt_text` must be lenient UTF-8** (`errors="replace"`) to match the
  TS `TextDecoder`/`Buffer.toString` behavior (wrong keys yield U+FFFD
  garbage, never an exception).
- BotsChat's `messages.encrypted` is a **bitmask**: bit 0 = text, bit 1 =
  media (`(encrypted?1:0) | (mediaEncrypted?2:0)`).
- **Plugin hooks are invoked synchronously** (`invoke_hook` never awaits) —
  async hook callbacks are silently discarded ("coroutine was never awaited").
  Hooks run on **worker threads with no event loop**; marshal sends onto the
  client's loop (`client.loop.call_soon_threadsafe(...)`, loop captured in
  `BotsChatCloudClient.start()`).
- **`ctx.dispatch_tool("cronjob", ...)` fails outside an agent session** with
  "Unknown tool" — the tool's `check_fn` (session env flags) is TTL-cached at
  startup. Call `tools.cronjob_tools._cronjob_handler(args)` directly.
- **Cron session ids are `cron_<job_id>_<YYYYmmdd>_<HHMMSS>`** — the timestamp
  contains an underscore; extract with `rsplit("_", 2)[0]`
  (`cron_job_id_from_session`), not `rsplit("_", 1)`.
- **Never forward `replyToId` on `agent.text`** — the DO maps
  `(threadId ?? replyToId)` into `messages.thread_id`, filing replies into
  phantom threads. Only `threadId` from the `:thread:` session-key suffix.
- **A2UI instructions are frozen per session** — existing sessions keep their
  old system prompt; test with a fresh session key.


## License

Apache-2.0 — by contributing you agree that your work is licensed accordingly.
