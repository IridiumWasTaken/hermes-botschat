# Hermes Agent Plugin System — Building a Messaging Channel Adapter

**Scope:** how to build a Hermes plugin that acts as a live messaging **CHANNEL adapter** — an external chat front-end drives Hermes agents over a persistent connection (inbound messages in, agent responses streamed out), plus scheduled-task support.

**Primary sources (authoritative):** the Hermes docs (https://hermes-agent.nousresearch.com/docs) and the Hermes source repo `https://github.com/NousResearch/hermes-agent`. File paths like `gateway/platforms/base.py`, `cron/scheduler.py`, `agent/plugin_llm.py` refer to the repo tree.

---

## TL;DR — the answer to "how do I add a whole channel?"

Hermes **does** support adding a full inbound/outbound gateway channel as a plugin — no core code changes. You do **not** have to fall back to gateway event hooks. The supported mechanism is:

1. A `kind: platform` plugin directory (e.g. `~/.hermes/plugins/myplatform/`) whose `register(ctx)` calls **`ctx.register_platform(...)`**.
2. An adapter class subclassing **`BasePlatformAdapter`** (`gateway/platforms/base.py`) implementing `connect()`, `send()`, `disconnect()`.
3. Inbound messages are normalized into a `MessageEvent` and forwarded with `await self.handle_message(event)` — the base class routes them to the gateway runner, which runs an `AIAgent` turn and calls your adapter's `send()` with the response.

This is the exact analog of OpenClaw's "channel" concept: the adapter is the persistent connection, `handle_message()` is the inbound leg, `send()` is the outbound leg. Streaming of intermediate tokens to an external front-end is not automatic on the adapter `send()` path (the adapter receives the final response while `_keep_typing()` shows a typing heartbeat); for token-level streaming you subscribe to the `on_stream_delta` plugin hook (or use one of the three external protocols in §5).

---

## 1. Python plugin structure, manifest, lifecycle

### Directory layout

Two equivalent "doors" into the general plugin system, plus specialized sub-categories:

```text
~/.hermes/plugins/<id>/          # user plugin (default home)
    ├── plugin.yaml              # manifest
    ├── __init__.py              # register(ctx) — wiring
    ├── schemas.py               # tool schemas (what the LLM sees)
    └── tools.py                 # tool handlers (what runs)
```

Discovery sources, in priority order (later overrides earlier on name collision) — from `user-guide/features/plugins.md` "Plugin discovery":

| Source | Path | Notes |
|---|---|---|
| Bundled | `<repo>/plugins/` | ships with Hermes |
| User | `~/.hermes/plugins/` | personal |
| Project | `.hermes/plugins/` | requires `HERMES_ENABLE_PROJECT_PLUGINS=true` |
| pip | `hermes_agent.plugins` entry points | distributed packages |
| Nix | `extraPlugins` / `extraPythonPackages` | NixOS |

Specialized sub-directories route to different discovery systems (`user-guide/features/plugins.md` "Plugin sub-categories"):

- `plugins/` (root) → general plugins (`PluginManager`, `kind: standalone` or `backend`)
- **`plugins/platforms/<name>/`** → gateway channel adapters (`kind: platform`, one level deeper) — **this is the channel hook**
- `plugins/image_gen/<name>/` → image backends (`kind: backend`)
- `plugins/memory/<name>/` → memory providers (own loader, `kind: exclusive`, single-select)
- `plugins/context_engine/<name>/` → context compressors (own loader, single-select)
- `plugins/model-providers/<name>/` → LLM provider profiles (own lazy loader)
- `plugins/cron_providers/<name>/` → cron *trigger* providers (§6)

`~/.hermes/plugins/<plugin-name>/plugin.yaml` may be flat or one category level deep; anything deeper is ignored. `hermes plugins doctor [path]` runs the same discovery/manifest/import/`register()` pipeline Hermes itself uses (plus a `--ci` flag and a temporary `HERMES_HOME`).

### Manifest (`plugin.yaml`)

Minimal (`developer-guide/plugins.md` Step 2):

```yaml
name: calculator
version: 1.0.0
description: Math calculator
provides_tools: [calculate, unit_convert]
provides_hooks: [post_tool_call]
```

Full field set (v1 + additive v2 schema):

- `name` (required), `version`, `description`, `author`
- `provides_tools` / `provides_hooks` — declared lists of what `register()` registers (doctor checks drift)
- `kind` — `standalone`, `backend`, or **`platform`** (gateway adapters *must* use `kind: platform`)
- `label`, `emoji` — display metadata (platform adapters)
- `requires_env` — env-var gate; plugin disabled with a clear message if missing; prompted during `hermes plugins install`. Simple form `- NAME` or rich form `{name, description, url, secret}`.
- `optional_env` — surfaced in `hermes config` setup UI (platform adapters use this heavily)
- `capabilities` — privileged host surfaces requesting user consent (§9)
- v2 fields (`manifest_version`, `api_version`, `requires_plugins`, `python_dependencies`, `config_schema`, `license`, `homepage`, `tags`) — all optional; unknown fields are ignored (forward-compat).

### Lifecycle

**There is no `on_enable` / `on_disable` / `on_startup` / `on_shutdown` plugin callback.** This is the single most important lifecycle fact, and it is documented implicitly rather than named explicitly. The plugin lifecycle is:

1. **Discovery** — `PluginManager` scans the sources, parses `plugin.yaml`.
2. **Namespaced import** — the plugin package is imported.
3. **`register(ctx)`** — called **exactly once at startup**. Everything a plugin does happens here: `register_tool`, `register_hook`, `register_platform`, `register_command`, etc. If `register()` raises, the plugin is disabled and Hermes continues.
4. **Enable/disable is a load-time config gate**, not a runtime callback: a plugin only loads if its name is in `plugins.enabled` in `config.yaml` (and not in `plugins.disabled`). There is no callback fired when a user toggles it — the plugin simply is or isn't imported next session.

The `register(ctx)` entry point is described in `developer-guide/plugins.md` Step 5 ("Called exactly once at startup … If this function crashes, the plugin is disabled but Hermes continues fine"). The opt-in gate is `user-guide/features/plugins.md` "Plugins are opt-in". A native plugin must contain both `plugin.yaml` and `__init__.py` with a `register(ctx)` function.

"Startup" as an *event* (rather than a plugin method) is observable two ways: the gateway event hook `gateway:startup` (§4, gateway process only) and the plugin hook `on_session_start` (§4, per-session). Neither is a plugin-method lifecycle callback.

### Config and state

- `ctx.get_config(key, default=...)` / `ctx.set_config(key, value)` — read/write **only this plugin's** namespace, resolved under `plugins.entries.<plugin-id>.settings`; global/cross-plugin/traversal paths are rejected.
- `ctx.state.get(...)` / `ctx.state.set(...)` — profile-scoped, atomically-replaced plugin-owned runtime data (≤10 MiB/plugin), stored under `<HERMES_HOME>/plugin-data/` (via `plugins.plugin_storage.plugin_data_dir()` / `plugin_db()`), **not** in the install directory (which `hermes plugins update`/`remove` may delete).
- `ctx.profile_name` — active profile name (works in gateway, CLI, and kanban-worker processes; no `_cli_ref` dependency).
- `manifest_version`/`api_version` in the manifest are metadata only; the compatibility contract is behavioral (see "Native plugin compatibility contract" in `developer-guide/plugins.md`): documented `PluginContext` methods are never removed/renamed; hook payloads are keyword-only and additive; callbacks must accept `**kwargs`.

---

## 2. Registering custom tools and toolset wiring

Tools are registered in `register(ctx)` and become visible to the agent immediately (`developer-guide/plugins.md` Steps 3–5):

```python
def register(ctx):
    ctx.register_tool(
        name="calculate",          # unique tool name
        toolset="calculator",      # toolset namespace (becomes a toolset key)
        schema=CALCULATE,          # JSON schema: {name, description, parameters}
        handler=tools.calculate,   # callable(args: dict, **kwargs) -> str
        check_fn=lambda: _has_lib(),  # optional; False hides tool from the model
    )
```

Handler contract (`developer-guide/plugins.md` "Key rules for handlers"):
- Signature `def handler(args: dict, **kwargs) -> str`; accept `**kwargs` for forward-compat.
- Always return a **JSON string** (success and error alike); never raise.
- `schema["description"]` is the model-facing text — write it specifically (when to call, what it does).

Toolset wiring: registering a tool under `toolset="X"` makes `X` a toolset key the user enables like any other (`hermes tools enable X --platform cli`, or `platform_toolsets` in `config.yaml`). Contrast with **core** tools (`developer-guide/adding-tools.md`), which require `tools/<name>.py` + an entry in `toolsets.py` (`_HERMES_CORE_TOOLS` etc.) — plugins avoid all of that.

Related tool-registration APIs in the same `ctx`:

- `ctx.register_tool(..., override=True)` — replace a built-in tool. Requires the `tools.override` capability (§9) plus, for non-bundled plugins, `plugins.entries.<id>.allow_tool_override: true`; otherwise raises `PluginToolOverrideError`. Without `override=True`, the registry rejects any registration that shadows an existing tool from a different toolset.
- `ctx.register_tool(..., description=...)` — optional `ToolEntry` registry metadata (defaults to schema description; the model sees the schema value).
- `ctx.dispatch_tool(name, args, *, parent_agent=None) -> str` — invoke any tool (built-in or plugin) with parent-agent context (approvals, redaction, budget, credentials) wired automatically; the sanctioned way for slash-command/hook handlers to orchestrate tools.
- `ctx.register_skill(name, path)` — bundle read-only skills, namespaced `plugin:<skill>`, loaded via `skill_view("plugin:skill")`.
- `ctx.register_command(name, handler, description="", args_hint="")` — in-session slash command (`/name`), works in CLI and gateway; handler receives the raw args string, may be async.
- `ctx.register_cli_command(name, help, setup_fn, handler_fn)` — `hermes <name> <subcommand>` argparse tree.
- `ctx.register_middleware(kind, fn)` — request/execution middleware (`tool_request`, `llm_request`, `tool_execution`, `llm_execution`; `VALID_MIDDLEWARE` in `hermes_cli/middleware.py`).
- `ctx.register_system_prompt_section(id, fn, position="after_memory", max_chars=4000)` — durable, cache-safe system-prompt section.
- Lazy optional deps: `tools.lazy_deps.ensure("my-plugin.backend")` inside a handler, gated by `security.allow_lazy_installs` and an in-tree `LAZY_DEPS` allowlist.

---

## 3. Registering a platform/channel adapter (the gateway channel hook)

**This is the supported, first-class path — a plugin CAN add a whole inbound/outbound channel.** It is documented at `developer-guide/adding-platform-adapters.md` ("Plugin Path (Recommended)") and summarized in `user-guide/features/plugins.md` (`ctx.register_platform(...)`). The bundled IRC, Teams, Google Chat, and LINE adapters (`plugins/platforms/{irc,teams,google_chat,line}/adapter.py`) are the reference implementations.

### The three required files

```text
~/.hermes/plugins/my-platform/
    ├── plugin.yaml     # kind: platform
    └── adapter.py      # BasePlatformAdapter subclass + register(ctx)
```

`plugin.yaml` (note `kind: platform`, and `requires_env`/`optional_env` auto-populate `hermes config`):

```yaml
name: my-platform
label: My Platform
kind: platform
version: 1.0.0
description: My custom messaging platform adapter
requires_env:
  - MY_PLATFORM_TOKEN
  - name: MY_PLATFORM_CHANNEL
    description: "Channel to join"
    password: false
optional_env:
  - name: MY_PLATFORM_HOME_CHANNEL
    description: "Default channel for cron delivery"
```

`adapter.py` — the adapter and registration (`developer-guide/adding-platform-adapters.md` "adapter.py"):

```python
import os
from gateway.platforms.base import (
    BasePlatformAdapter, SendResult, MessageEvent, MessageType,
)
from gateway.config import Platform, PlatformConfig

class MyPlatformAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("my_platform"))
        extra = config.extra or {}
        self.token = os.getenv("MY_PLATFORM_TOKEN") or extra.get("token", "")

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # open the persistent connection (WebSocket / long-poll / HTTP server)
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        # outbound: send the agent response via the platform API
        return SendResult(success=True, message_id="...")

    async def get_chat_info(self, chat_id):   # optional
        return {"name": chat_id, "type": "dm"}

def register(ctx):
    ctx.register_platform(
        name="my_platform",
        label="My Platform",
        adapter_factory=lambda cfg: MyPlatformAdapter(cfg),
        check_fn=check_requirements,          # passive probe; NEVER pip-installs
        validate_config=validate_config,
        required_env=["MY_PLATFORM_TOKEN"],
        install_hint="pip install my-platform-sdk",
        env_enablement_fn=_env_enablement,    # seed PlatformConfig.extra from env
        cron_deliver_env_var="MY_PLATFORM_HOME_CHANNEL",  # cron delivery (§6)
        allowed_users_env="MY_PLATFORM_ALLOWED_USERS",
        allow_all_env="MY_PLATFORM_ALLOW_ALL_USERS",
        max_message_length=4000,              # smart chunking; 0 = no limit
        platform_hint="You are chatting via My Platform…",  # injected into system prompt
        emoji="💬",
    )
```

### The inbound leg

Build a `MessageEvent` and hand it to the base class (`developer-guide/adding-platform-adapters.md` "Step-by-Step Checklist — Built-in Path", which shows the identical call):

```python
source = self.build_source(
    chat_id=chat_id, chat_name=name, chat_type="dm",  # or "group"
    user_id=user_id, user_name=user_name,
)
event = MessageEvent(
    text=content, message_type=MessageType.TEXT,
    source=source, message_id=msg_id,
)
await self.handle_message(event)   # routes to GatewayRunner → AIAgent → your send()
```

`handle_message()` is inherited from `BasePlatformAdapter` (`gateway/platforms/base.py`) and routes the event to the gateway runner. The runner resolves a session key (`agent:main:{platform}:{chat_type}:{chat_id}` — never construct manually; use `build_session_key()` in `gateway/session.py`), checks authorization, dispatches slash commands, runs an `AIAgent` turn, and calls the adapter's `send()` with the final response (`developer-guide/gateway-internals.md` "Message Flow").

### What `register_platform()` wires up for free

From the table in `developer-guide/adding-platform-adapters.md` "What the Plugin System Handles Automatically": gateway adapter creation (registry checked before the built-in if/elif chain), config parsing, connected-platform validation, per-user authorization, env-only auto-enable, YAML→env config bridge, cron delivery, `hermes config` UI entries, the send engine (`tools/send_message_tool.py`), webhook cross-platform delivery, `/update` access, channel directory, system-prompt hints, message chunking, PII redaction, `hermes status`/`gateway setup`/`tools`/`skills` surfaces, and token-lock (multi-profile) support.

Full `ctx.register_platform(...)` keyword surface (all optional except `name`, `label`, `adapter_factory`):

`name, label, adapter_factory, check_fn, ensure_deps_fn, validate_config, required_env, optional_env, install_hint, env_enablement_fn, apply_yaml_config_fn, cron_deliver_env_var, allowed_users_env, allow_all_env, allow_update_command, pii_safe, max_message_length, platform_hint, emoji, parse_target_ref_fn, validate_target_ref_fn, send_message_handler, standalone_sender_fn, platform_toolsets (via provides_tools), …`

### Platform events in (native SDK access)

To receive platform events the core adapter doesn't route (extra update types, native button callbacks, reaction/member events, extra webhook routes), plugins register a handler factory invoked at connect time — works on **every** gateway platform (`developer-guide/plugins.md` "Register native platform handlers"):

```python
ctx.register_platform_handler("discord", lambda native, adapter: ...)
# native = discord.ext.commands.Bot; telegram → PTB Application; slack → slack_bolt.AsyncApp;
# matrix → client; teams → App; line/api_server/msgraph_webhook → aiohttp web.Application;
# everything else (whatsapp, signal, irc, email, sms, …) → None (work through the adapter handle)
```

Also: `ctx.register_slack_action_handler(action_id, callback)` (Block Kit clicks) and `ctx.platform_actions` (capability-gated verbs `add_reaction`, `set_thread_title` — off by default, §9).

### What does NOT exist / needs core support

- **There is no generic `BasePlatformAdapter` token-streaming `send()` hook.** The adapter's `send()` receives the completed response; during generation the base class runs a `_keep_typing()` heartbeat (override it to layer platform-specific "still thinking" UX). Token deltas reach plugins only through the `on_stream_delta` observer hook (§4) or via the external protocols (§5). This is explicit in the docs (`developer-guide/adding-platform-adapters.md` "Platform-Specific Slow-LLM UX"; `user-guide/features/hooks.md` "Streaming output hooks").
- **The built-in path** (adding a platform to core) touches 20+ files across `gateway/config.py`, `gateway/run.py`, `cron/scheduler.py`, `hermes_cli/*`, `tools/*`, `toolsets.py`, `agent/prompt_builder.py` — the plugin path exists precisely to avoid all of that (`developer-guide/adding-platform-adapters.md` "Step-by-Step Checklist (Built-in Path)").
- For **outbound-only** host-driven delivery (cron `deliver=`, `hermes send`), a plugin registers `parse_target_ref_fn`, `validate_target_ref_fn`, `send_message_handler`, and `standalone_sender_fn` on the same `ctx.register_platform()` call. `send_message` is deliberately **not** an agent-callable model tool.

---

## 4. Lifecycle / event hooks and shell hooks

There are **four** hook systems (`user-guide/features/hooks.md`):

| System | Registration | Runs in |
|---|---|---|
| Gateway event hooks | `HOOK.yaml` + `handler.py` in `~/.hermes/hooks/<name>/` | gateway only |
| **Plugin hooks** | `ctx.register_hook()` | CLI **and** gateway |
| Shell hooks | `hooks:` block in `config.yaml` | CLI and gateway |
| Outbound webhooks | `hooks.outbound:` list in `config.yaml` | CLI and gateway |

### Plugin hooks (the general surface)

Registered in `register(ctx)`. The canonical catalog is `user-guide/features/hooks.md` "Shipped plugin-hook catalog"; validity comes from `hermes_cli.plugins.VALID_HOOKS`. Categories:

- **Directive/control:** `pre_tool_call` (may return `{"action":"block"|"approve"|"modify", ...}` — first valid directive wins; `modify` shallow-merges new args), `pre_llm_call` (return `{"context": "..."}` or a string to inject into the user message), `pre_verify`, `pre_gateway_dispatch`.
- **Transform:** `transform_tool_result`, `transform_terminal_output`, `transform_llm_output`, `pre_transcription` (first non-empty string replaces the content).
- **Observer** (return ignored): `post_tool_call`, `post_llm_call`, `pre_api_request`, `post_api_request`, `api_request_error`, `transform_api_error_classification`, **`on_stream_start` / `on_stream_delta` / `on_stream_end` / `on_interim_message`** (streaming — §5), `on_session_start`, `on_session_end`, `on_session_finalize`, `on_session_reset`, `on_skill_lifecycle`, `subagent_start`, `subagent_stop`, `pre_approval_request`, `post_approval_response`, `pre_command`, `gateway_platform_event`, and the `kanban_*` family.

Key signatures (from the catalog):

- `pre_tool_call(tool_name, args, task_id, **kwargs)` — fires in `model_tools.py` inside `handle_function_call()` before the handler runs.
- `post_tool_call(tool_name, args, result, task_id, duration_ms, **kwargs)`.
- `pre_llm_call(session_id, user_message, conversation_history, is_first_turn, model, platform, **kwargs)` — fires once per `run_conversation()` call (once per turn), before the tool loop; injected context is appended to the **user message** (never the system prompt, to preserve the prompt cache).
- `on_session_start(session_id, model, platform)` / `on_session_end(session_id, ...)`.

General rules: callbacks receive **keyword** args and must accept `**kwargs`; exceptions are logged and skipped; hot-path callbacks are bounded by `plugins.hook_callback_timeout` (default 30s; timed-out or still-running `pre_tool_call` callbacks **fail closed** — the tool is blocked). `pre_llm_call` context is capped at 10,000 chars with spill-to-file (`hooks.output_spill`).

### Streaming output hooks (relevant to a channel adapter)

`on_stream_start`, `on_stream_delta`, `on_stream_end`, `on_interim_message` are observer-only hooks that expose normalized LLM streaming deltas **off the token path** via host-owned bounded queues (one worker per callback; a stalled callback drops only its own events). `on_stream_delta(delta, kind, turn_id, iteration, session_id, model, provider, surface)` — `kind` is `"text"` or `"reasoning"` (reasoning requires the `plugins.stream_reasoning_deltas` opt-in). **This is the plugin-side hook a channel adapter uses to forward token deltas out to an external front-end.**

### Gateway event hooks

Drop-in directories (`~/.hermes/hooks/<name>/HOOK.yaml` + `handler.py` with an `async def handle(event_type, context)` function). Events: `gateway:startup`, `session:start`, `session:end`, `session:reset`, `session:compress`, `agent:start`, `agent:step`, `agent:end`, `reaction:added`/`reaction:removed`, and wildcard `command:*`. Discovered by `HookRegistry.discover_and_load()`; fired via `hooks.emit()`; errors never crash the agent. Gateway hooks only fire in the gateway process (not the CLI).

### Shell hooks

Config-driven, no Python (`user-guide/features/hooks.md` "Shell Hooks"):

```yaml
hooks:
  - event: post_tool_call
    command: "notify-send 'Tool ran: {tool_name}'"
    when:
      tools: [terminal, patch, write_file]
```

Supports the same events as plugin hooks plus `pre_gateway_dispatch`, and structured JSON output for `pre_tool_call` blocking decisions.

---

## 5. Programmatic send + stream (the critical path)

There are **five** ways to drive the agent programmatically. Four are documented protocols (no plugin needed); the fifth is in-process embedding. The platform-adapter path (§3) is the *channel* mechanism; these are the *send-and-receive* mechanisms.

### 5a. Platform adapter (bidirectional channel — the intended pattern)

Inbound → `handle_message(MessageEvent)`; outbound → `send(chat_id, content, ...)`. The gateway runner manages sessions, authorization, slash commands, and the agent turn. Streaming: `_keep_typing()` heartbeat during generation + final `send()`; token deltas via the `on_stream_delta` hook if the adapter forwards them. This is the OpenClaw-channel equivalent. File: `gateway/platforms/base.py`; docs `developer-guide/adding-platform-adapters.md`.

### 5b. TUI Gateway JSON-RPC (`tui_gateway/server.py`, `tui_gateway/ws.py`)

The Ink TUI and dashboard PTY bridge speak this protocol; any external host can too, over **stdio or WebSocket** (`developer-guide/programmatic-integration.md`). This is the richest external channel: it exposes sessions, slash commands, approvals, clarify, multi-agent, and **streaming events**.

- Methods (selected): `prompt.submit`, `prompt.background`, `session.steer`, `session.create`, `session.list`, `session.active_list`, `session.activate`, `session.close`, `session.interrupt`, `session.history`, `session.compress`, `session.branch`, `session.title`, `session.usage`, `session.status`, `clarify.respond`, `sudo.respond`, `secret.respond`, `approval.respond`, `config.set`/`config.get`, `commands.catalog`, `command.resolve`, `command.dispatch`, `cli.exec`, `reload.mcp`, `reload.env`, `process.stop`, `delegation.status`, `subagent.interrupt`, `subagent.steer`, `spawn_tree.save/list/load`, `terminal.resize`, `clipboard.paste`, `image.attach`.
- Events streamed back: **`message.delta`, `message.complete`, `tool.start`, `tool.progress`, `tool.complete`**, `approval.request`, `clarify.request`, `sudo.request`/`sudo.expire`, `secret.request`/`secret.expire`, `gateway.ready`, plus session lifecycle/error events.

`prompt.submit` accepts rewind/truncation parameters (`truncate_before_user_ordinal`, `truncate_before_row_id`, `confirm_truncate`, `confirm_empty_truncate`) for edit/regenerate semantics.

### 5c. OpenAI-compatible API server (`gateway/platforms/api_server.py`)

HTTP + SSE for OpenAI-compatible frontends and language-agnostic clients (`developer-guide/programmatic-integration.md`; user guide `user-guide/features/api-server.md`):

```
POST /v1/chat/completions        # streaming via SSE
POST /v1/responses               # stateful Responses API
POST /v1/runs                    # returns run_id (202)
GET  /v1/runs/{id}               # status
GET  /v1/runs/{id}/events        # SSE lifecycle event stream
POST /v1/runs/{id}/approval | /steer | /stop
GET  /v1/capabilities | /v1/models | /api/model/options
GET  /health, /health/detailed
```

Session identity via `X-Hermes-Session-Id` / `X-Hermes-Session-Key` headers.

### 5d. ACP (Agent Client Protocol) — JSON-RPC over stdio (`acp_adapter/`)

`hermes acp` serves a stdio JSON-RPC ACP server for IDE clients (VS Code, Zed, JetBrains). Exposes session creation, prompt submission, streaming message chunks, tool-call events, permission requests, fork, cancel, auth.

### 5e. In-process `AIAgent` (`run_agent.py`)

`from run_agent import AIAgent; agent = AIAgent(model=..., quiet_mode=True); agent.chat(msg)` → final string, or `agent.run_conversation(user_message, task_id=...)` → `{"final_response": ..., "messages": [...]}`. Multi-turn by passing `conversation_history=result["messages"]`. **Non-streaming** (returns the final response). Not thread-safe to share — one instance per thread/task. Docs: `guides/python-library.md`. (A gateway hook example that spawns a one-shot `AIAgent` with `_resolve_gateway_model()` / `_resolve_runtime_agent_kwargs()` is in `user-guide/features/hooks.md` "BOOT.md tutorial".)

### 5f. Plugin-side programmatic entry points

- **`ctx.inject_message(content, role="user", *, session_key=None) -> bool`** (`user-guide/features/plugins.md` "Injecting Messages") — inject a message into an active CLI conversation or a known gateway session. In CLI mode: queues as next input (or interrupts a mid-turn agent). In gateway mode: `session_key` (the stable routing key, e.g. `agent:main:telegram:dm:123456789`) is required; the route is re-authorized before dispatch; **gated by `plugins.entries.<id>.allow_gateway_injection: true`** (off by default); returns `True` on accepted async dispatch, not on completion. This is the plugin-side inbound feed for a messaging-bridge/webhook-receiver pattern.
- **`ctx.llm.complete(...)` / `complete_structured(...)` / `acomplete(...)` / `acomplete_structured(...)`** (`developer-guide/plugin-llm-access.md`) — one-shot host-owned LLM calls for out-of-band work. **Bounded: no streaming, no tool loops.** `ctx.llm` is `agent.plugin_llm.PluginLlm`; fails closed (no provider/model override without operator opt-in). Useful for a channel adapter that pre-classifies inbound messages or translates before queueing.
- **`ctx.dispatch_tool(name, args)`** — run any tool with agent context.

---

## 6. Cron / scheduled tasks

### How scheduled jobs work (`developer-guide/cron-internals.md`)

- Jobs persist in `~/.hermes/cron/jobs.json` (atomic write). One model-facing tool, **`cronjob`**, with action-style operations `create`, `list`, `update`, `pause`, `resume`, `run`, `remove`. Job record: `{id, name, prompt, schedule{kind, expr, display}, skills[], deliver, repeat, state, enabled, next_run_at, …}`.
- Four schedule kinds: relative delay (`30m`), interval (`every 2h`), cron expression (`0 9 * * *`), ISO timestamp.
- Scheduler ticks every 60s (`cron/scheduler.py`): acquire cross-process `flock` lock → load jobs → run due jobs in a **fresh `AIAgent` session** (no conversation history; `cronjob` toolset disabled as a recursion guard) → deliver to the `deliver:` target. `scheduler.run_job()` / `_deliver_result()`.
- **Plug-able trigger**: `cron.provider` config selects a `CronScheduler` discovered from `plugins/cron_providers/<name>/` (or `$HERMES_HOME/plugins/<name>/`) — e.g. the `chronos` managed-cron provider for scale-to-zero. A provider controls only the *trigger*, never execution. (This is a provider plugin, not a per-job registration API.)
- Delivery targets include `telegram:<chat_id>`, `discord:#channel`, `local`, `origin`, `bot-chat` / `bot-chat:<profile>`, and any plugin platform via `platform:<target>`. `[SILENT]` prefix suppresses delivery.

### Can a plugin schedule/trigger cron jobs programmatically?

**There is no documented `ctx.register_cron_job()` / `ctx.schedule_cron()` API.** The documented ways a plugin drives cron are:

1. **`ctx.dispatch_tool("cronjob", {"action": "create", ...})`** — dispatch the `cronjob` tool through the registry, exactly as the model would, with agent context wired. This is the sanctioned programmatic path (it is a real tool invocation through the normal pipeline).
2. **Write `~/.hermes/cron/jobs.json` directly** (or via `cron/jobs.py`), respecting the atomic-write and lock semantics — brittle, not a public plugin API, but used by external tooling.
3. **`cron_deliver_env_var` + `standalone_sender_fn`** — this is the *outbound* cron hook for a channel plugin: declare `cron_deliver_env_var="MY_PLATFORM_HOME_CHANNEL"` in `ctx.register_platform()` so `deliver=my_platform` jobs resolve a home channel, and register `standalone_sender_fn` so delivery works when cron runs in a separate process from the gateway (otherwise it fails with `No live adapter for platform '<name>'`). Reference impls: `plugins/platforms/{irc,teams,google_chat}/adapter.py`.
4. **`on_session_start`/`gateway:startup` hooks + `ctx.dispatch_tool("terminal", {"command": "hermes cron ..."})`** — shell out to the `hermes cron` CLI.
5. **Script-backed jobs**: a cron job may carry a `script` field (a Python script whose stdout is injected into the prompt) — the *job author* attaches it; this is not a plugin registration API.

So: **scheduled execution is fully supported for a channel plugin (inbound agent runs on a schedule + outbound delivery to your channel), but there is no first-class `schedule()` API on the plugin context** — you go through the `cronjob` tool, the cron files, or a `CronScheduler` provider plugin.

---

## 7. Desktop Plugin SDK (one-paragraph orientation)

The native desktop app (`hermes desktop`) is extended by a single ESM file — `$HERMES_HOME/desktop-plugins/<id>/plugin.js`, or the `desktop/plugin.js` half of a unified agent plugin under `~/.hermes/plugins/<id>/` — importing only `@hermes/plugin-sdk` (plus the app's own `react`/`react/jsx-runtime`); no build step, hot-reload on save (`developer-guide/desktop-plugin-sdk.md`). It registers panes (`ctx.register({ area: 'panes', ... })`), full pages/routes (`ROUTES_AREA` + `SIDEBAR_NAV_AREA`), status-bar chips, ⌘K palette commands (`PALETTE_AREA`), keybinds, and themes; reads live state via `host.state.*` atoms, drives the gateway with `host.request` (JSON-RPC) and `host.onEvent` (gateway event stream), and reaches its **own backend namespace** through `ctx.rest('/path', {method, body, timeoutMs})` and its live twin `ctx.socket('/events', onMessage)` — both scoped to `/api/plugins/<id>` by construction (traversal rejected; `ctx.socket` is a no-op on OAuth remotes). The backend is a Python file `dashboard/plugin_api.py` exporting `router = APIRouter()`, declared by `dashboard/manifest.json` `{"name": "<id>", "api": "plugin_api.py"}`; routes mount under `/api/plugins/<id>/` and run **inside the gateway process** (can import `hermes_state`, `hermes_cli.config`, …), and the Python half is imported only when the plugin is in `plugins.enabled` (a security boundary — GHSA-mcfc-hp25-cjv7). The **web dashboard** (`hermes dashboard`) has an unrelated plugin system (`window.__HERMES_PLUGIN_SDK__` + `manifest.json` + pre-built JS bundle, tabs/shell-slots/page-slots/backend routes) documented at `user-guide/features/extending-the-dashboard.md`; the two share only the `plugin_api.py` `/api/plugins/<id>` backend. For a chat-channel adapter the desktop SDK is a secondary concern — the channel lives in the gateway/platform layer, and the desktop pane would be an optional control surface.

---

## 8. Distribution / packaging

- **Directory plugin** — drop `~/.hermes/plugins/<id>/` and enable it. The standard distribution: `hermes plugins install owner/repo` (Git clone, pinned to an immutable 40-char commit SHA; `--enable`/`--no-enable`; `--ref <sha>`; `--force`), `hermes plugins update`, `hermes plugins remove`. Install-time static security scanning (safe/caution/dangerous verdicts; `plugins.scan_on_install`).
- **pip entry point** — for a pip package, declare the `hermes_agent.plugins` group (`developer-guide/plugins.md` "Distribute via pip"):

  ```toml
  [project.entry-points."hermes_agent.plugins"]
  my-plugin = "my_plugin_package"          # module with register(ctx)
  ```

  Auto-discovered on next `hermes` startup. **Pip-distributed plugins have no `plugin.yaml` directory**, so capabilities are declared via the companion `hermes_agent.plugin_capabilities` entry-point group (each `<plugin-id>.<capability-id>` = same object). Optional deps go in `[project.optional-dependencies]` extras.
- **Standalone repo** — third-party product plugins ship as standalone repos (never merged into `NousResearch/hermes-agent`); promote in the Discord `#plugins-skills-and-skins` channel. Indexed via the community plugin index (`hermes plugins search`; submit a PR to `NousResearch/hermes-plugin-index`).
- **Community index** — `https://raw.githubusercontent.com/NousResearch/hermes-plugin-index/main/index.json` (bundled seed for offline; entries pin `owner/repo` + 40-char commit SHA).
- **NixOS** — `services.hermes-agent.extraPlugins` / `extraPythonPackages` + `settings.plugins.enabled`.
- **Portable "Agent Plugins v1" packages** — directory with `plugin.json` + `skills/*/SKILL.md` + root `mcp.json` (compatibility adapter; `PLUGIN_ROOT` / `PLUGIN_DATA` env vars).
- **Plugin packs** — declarative `hermes-pack.yaml` pinning a set of plugins (consent never bulk-granted; secrets never travel in packs).
- **One-click install** — `hermes://plugin/install?repo=owner/repo&enable=1` deep links (never auto-install; show a confirmation dialog).

---

## 9. Security model

**Core posture: plugins are opt-in, in-process, and NOT sandboxed.** Consent + audit, not isolation.

- **Enable/disable gate** — general plugins and user-installed backends load nothing until their name is in `plugins.enabled` (`config.yaml`), with an optional `plugins.disabled` deny-list (always wins). Three states: `enabled` / `disabled` / `not enabled`. Bundled platform/backend plugins are auto-loaded infrastructure (the *channel* turns on via `gateway.platforms.<name>.enabled`); **user-installed platforms (third-party gateway adapters) are opt-in**. Grandfathering applied only to pre-existing user plugins at the opt-in migration (schema v21+).
- **Capabilities + consent** (`user-guide/features/plugins.md` "Plugin capabilities and consent"; `hermes_cli/plugin_capabilities.py`) — privileged host surfaces are declared in `plugin.yaml` under `capabilities:` (`tools.override`, `llm.provider_override`, `llm.model_override`, `llm.agent_id_override`, `llm.profile_override`, `llm.task_override`, `gateway.platform_actions`). At install/enable, the user consents once; the grant is recorded (`plugins.entries.<id>.granted_capabilities` + consent hash + timestamp); updates that add capabilities re-prompt; non-interactive sessions fail closed. Undeclared/unconsented capabilities are simply off — plugins probe with `ctx.has_capability(...)` and degrade. **Capabilities are consent + audit, not a sandbox** — a malicious plugin can ignore every gate.
- **Per-feature host gates** (independent, default-off, per-plugin in `plugins.entries.<id>`): `allow_gateway_injection` (§5f), `allow_tool_override` (§2), `mcp_allowlist` (per-server, no wildcards — `ctx.call_mcp`), `allow_platform_actions` (`ctx.platform_actions`), `llm.allow_{provider,model,agent_id,profile}_override` + `allowed_providers`/`allowed_models` (§5f `ctx.llm` trust gate).
- **Install-time security scanning** — static scan over the plugin tree (exfil of credential stores, reverse shells, destructive commands, persistence, obfuscation, prompt injection in docs) with safe/caution/dangerous verdicts; dangerous is blocked (not `--force`-able).
- **Runtime isolation of failures** — `register()` exceptions disable just that plugin; hook/middleware exceptions are logged and skipped; hot-path hooks are timeout-bounded (`plugins.hook_callback_timeout`, fail-closed for `pre_tool_call`).
- **Secret handling** — credentials go in `~/.hermes/.env` (never `plugin.yaml` `config`/`mcp.json`); `requires_env` prompts on install and saves to `.env`; `ctx.llm` gives the plugin no token visibility; the `run_secret_cli` helper (secret-source plugins) enforces minimal allowlisted child env; `strip_env_keys` on terminal-backend providers strips vendor tokens from spawned shells.
- **Trust boundary for a channel adapter specifically** — a `kind: platform` plugin is a full-trust in-process component that can read/write anything the agent can. Its outbound `send()` and inbound `handle_message()` run as the agent; per-user authorization (`allowed_users_env`/`allow_all_env`) is enforced by the gateway, and the plugin's own auth is re-validated before dispatch. The docs are explicit: capabilities and gates are consent, not a code audit — "Only install plugins from sources you trust."

---

## Appendix — key source files to read

| Concern | Repo file |
|---|---|
| Platform adapter base (lifecycle, `handle_message`, `_keep_typing`) | `gateway/platforms/base.py` |
| Adapter registry + deferred loaders | `gateway/platform_registry.py` |
| Gateway runner, session keys, authorization, dispatch | `gateway/run.py`, `gateway/session.py`, `gateway/config.py` |
| Outbound delivery | `gateway/delivery.py` |
| Reference adapters (stdlib-only IRC, Teams, Google Chat, LINE) | `plugins/platforms/{irc,teams,google_chat,line}/adapter.py` |
| Cron model + scheduler | `cron/jobs.py`, `cron/scheduler.py`, `cron/scheduler_provider.py` |
| Model-facing cron tool | `tools/cronjob_tools.py` |
| Tool registry / dispatch | `tools/registry.py`, `model_tools.py` |
| Plugin manager, valid hooks, `PluginContext` | `hermes_cli/plugins.py` (and `hermes_cli/plugin_capabilities.py`, `hermes_cli/middleware.py`) |
| Plugin LLM surface | `agent/plugin_llm.py` |
| Agent core (in-process embedding) | `run_agent.py` (`AIAgent`) |
| External protocols | `tui_gateway/server.py`, `tui_gateway/ws.py`, `gateway/platforms/api_server.py`, `acp_adapter/` |
| Plugin storage/state helpers | `plugins/plugin_storage.py`, `plugins/plugin_utils.py` |
| Lazy deps | `tools/lazy_deps.py` |
