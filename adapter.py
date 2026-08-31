"""BotsChat gateway channel adapter for Hermes Agent.

Registers a ``kind: platform`` plugin so Hermes agents can be driven through
the existing BotsChat server + web UI / mobile apps / CLI — the agent-side
counterpart of BotsChat's reference plugin (packages/plugin/src/channel.ts).

The server (ConnectionDO relay) is agent-agnostic: it relays flat JSON frames
and stores ciphertext. This adapter implements the agent side of that protocol
on top of Hermes' BasePlatformAdapter:

    inbound:  user.message/action/command/media -> MessageEvent -> handle_message()
    outbound: agent.text (E2E-encrypted when a key is set) -> WSS -> UI
"""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
import uuid
from typing import Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

from .e2e import decrypt_bytes, decrypt_text, encrypt_bytes, encrypt_text, from_base64, to_base64
from .media import EXT_BY_MIME, fetch_bytes, guess_content_type, resolve_url, upload_to_r2
from .protocol import (
    AgentActivity,
    AgentMedia,
    AgentStreamChunk,
    AgentStreamEnd,
    AgentStreamStart,
    AgentText,
    CloudMessage,
    DefaultModelUpdated,
    JobUpdate,
    ModelChanged,
    ModelInfo,
    ModelsList,
    ModelsRequest,
    SettingsDefaultModel,
    SettingsNotifyPreview,
    TaskDelete,
    TaskRun,
    TaskSchedule,
    TaskScanRequest,
    TaskScanResult,
    UserAction,
    UserCommand,
    UserMedia,
    UserMessage,
)
from .tasks import TaskBridge
from .ws_client import BotsChatCloudClient

logger = logging.getLogger("botschat")

# Active profile and plugin context, captured at register() time.
_PROFILE_NAME: Optional[str] = None
_CTX = None
# Connected clients, for hooks that run outside the adapter (cron session end).
_ACTIVE_CLIENTS: list = []
# Live adapter instances, for stream/tool hooks that run on worker threads.
_ADAPTERS: list = []

A2UI_PLATFORM_HINT = (
    "You are chatting via BotsChat, a Slack-like chat UI. The UI renders "
    "```action fenced code blocks as interactive clickable widgets. When your "
    "reply offers choices, next steps, or confirmations, you MUST wrap a "
    "single-line JSON in an ```action fence instead of using plain-text option "
    "lists. Action block format: ```action\\n{\"kind\":\"buttons\",\"prompt\":"
    "\"What next?\",\"items\":[{\"label\":\"Do X\",\"value\":\"x\",\"style\":"
    "\"primary\"},{\"label\":\"Do Y\",\"value\":\"y\"}]}\\n``` — kinds: buttons, "
    "confirm, select, input. Styles: \"primary\", \"danger\", or omit. NEVER "
    "present selectable options as plain-text lists with bullets, numbers, or "
    "emojis — they are not clickable. Skip action blocks only for purely "
    "informational replies."
)

# Durable, cache-safe system-prompt section (port of the reference plugin's
# A2UI_MESSAGE_TOOL_HINTS) — registered via ctx.register_system_prompt_section
# so the instruction is frozen into every new session prompt.
A2UI_SECTION_ID = "botschat.a2ui"
A2UI_SECTION = """\
This channel renders ```action fenced code blocks as interactive clickable widgets. When your reply offers choices, next steps, or confirmations, you MUST wrap a single-line JSON in an ```action fence instead of using plain-text option lists.
Action block format: ```action\\n{"kind":"buttons","prompt":"What next?","items":[{"label":"Do X","value":"x","style":"primary"},{"label":"Do Y","value":"y"}]}\\n``` — kinds: buttons, confirm, select, input. Styles: "primary", "danger", or omit.
NEVER present selectable options as plain-text lists with bullets, numbers, or emojis (✅ • - 🔧 etc.) — they are NOT clickable. Always use an ```action block for choices. Skip action blocks only for purely informational replies."""


class BotsChatAdapter(BasePlatformAdapter):
    """Connects a Hermes agent to one BotsChat server account via outbound WSS."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("botschat"))
        extra = config.extra or {}
        self.cloud_url = os.getenv("BOTSCHAT_CLOUD_URL") or extra.get("cloudUrl", "")
        self.pairing_token = os.getenv("BOTSCHAT_PAIRING_TOKEN") or extra.get("pairingToken", "")
        self.e2e_password = os.getenv("BOTSCHAT_E2E_PASSWORD") or extra.get("e2ePassword")
        # Optional distinct agent identity: lets one BotsChat account host
        # several Hermes profiles as separate agents (own channels/sessions).
        self.agent_id = os.getenv("BOTSCHAT_AGENT_ID") or extra.get("agentId")
        # Optional multi-agent list for hub mode: one connection declaring all
        # agents (BOTSCHAT_AGENTS="main,private"). When set it wins over
        # agent_id for the auth frame's agents array.
        self.agent_ids = self._parse_agents(extra)
        # Hub-mode inbound routing: agent id (from the session key) -> Hermes
        # profile. Stamped onto source.profile so the multiplexer activates
        # that profile's agent run (BOTSCHAT_AGENT_PROFILES="main:default,private:private").
        self.agent_profiles = self._parse_agent_profiles(extra)
        self._client: Optional[BotsChatCloudClient] = None
        self._default_model: Optional[str] = None
        self._default_provider: Optional[str] = None
        self._bridge: Optional[TaskBridge] = None
        # Scoped-lock identity (sha256 of the pairing token) while this
        # adapter owns the token; None when not locked or status unavailable.
        self._lock_key: Optional[str] = None
        # sessionKey -> model requested via /model, emitted as model.changed
        # after the next reply to that session.
        self._pending_model_changes: dict = {}
        # sessionKey -> last known userId (user.command frames carry no userId,
        # so we attribute them to the session's known sender for authorization).
        self._session_users: dict = {}
        # Streaming state: BotsChat sessionKeys seen, and Hermes session_id ->
        # (botschat sessionKey, runId) for in-flight streams.
        self._known_sessions: set = set()
        self._stream_runs: dict = {}
        self._last_session: Optional[str] = None
        self._media_dir: Optional[str] = None

    @staticmethod
    def _parse_agents(extra: dict) -> Optional[list]:
        """Resolve the auth-frame agents list for hub mode.

        Priority: BOTSCHAT_AGENTS env (comma-separated) > extra.agents
        (list or comma-separated string). Returns None when unset — the
        client then falls back to ``[agent_id or "hermes"]``.
        """
        raw = os.getenv("BOTSCHAT_AGENTS") or extra.get("agents")
        if not raw:
            return None
        if isinstance(raw, list):
            items = raw
        else:
            items = [p.strip() for p in str(raw).split(",")]
        agents = [p for p in items if p]
        return agents or None

    @staticmethod
    def _parse_agent_profiles(extra: dict) -> dict:
        """Resolve the hub-mode agent->profile routing table.

        Priority: BOTSCHAT_AGENT_PROFILES env ("agent:profile,agent:profile")
        > extra.agentProfiles (dict, or comma-separated string). Returns {} when
        unset — no profile stamping (single-profile behavior).
        """
        raw = os.getenv("BOTSCHAT_AGENT_PROFILES") or extra.get("agentProfiles")
        if not raw:
            return {}
        if isinstance(raw, dict):
            return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip()}
        result = {}
        for pair in str(raw).split(","):
            if ":" not in pair:
                continue
            agent, _, profile = pair.partition(":")
            agent, profile = agent.strip(), profile.strip()
            if agent and profile:
                result[agent] = profile
        return result

    def _profile_for_session(self, session_key: str) -> Optional[str]:
        """Map a BotsChat sessionKey's agent segment to a Hermes profile.

        Session keys are ``agent:<agentId>:botschat:<userId>:...`` — the
        second segment is the target agent. Returns None when hub mode is
        off or the agent has no mapping (stays on the owning profile).
        """
        if not self.agent_profiles:
            return None
        try:
            agent = session_key.split(":", 2)[1]
        except IndexError:
            return None
        return self.agent_profiles.get(agent)

    # ------------------------------------------------------------- connection

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not (self.cloud_url and self.pairing_token):
            logger.error("[botschat] Missing BOTSCHAT_CLOUD_URL or BOTSCHAT_PAIRING_TOKEN")
            return False

        # Prevent two profiles from binding the same pairing token (duplicate
        # replies, split session state). Mirror the LINE adapter: lock on a
        # hash of the token so the secret never lands in the lock file. The
        # lock is re-acquired on every connect() — same-PID reconnects
        # self-reacquire, so this is idempotent.
        try:
            from gateway.status import acquire_scoped_lock

            tok_hash = hashlib.sha256(self.pairing_token.encode()).hexdigest()[:16]
            ok, _ = acquire_scoped_lock("botschat", tok_hash)
            if not ok:
                logger.error("[botschat] pairing token already in use by another profile")
                self._set_fatal_error(
                    "lock_conflict",
                    "BotsChat pairing token already in use by another profile",
                    retryable=False,
                )
                return False
            self._lock_key = tok_hash
        except ImportError:
            self._lock_key = None  # status module not available (e.g. tests)

        # Read the default model once at connect (auth frame + status keepalive
        # + models.list all report it).
        self._default_model = _hermes_config_get("model.default")
        self._default_provider = _hermes_config_get("model.provider")

        client = BotsChatCloudClient(
            cloud_url=self.cloud_url,
            pairing_token=self.pairing_token,
            account_id="default",
            e2e_password=self.e2e_password,
            agent_ids=self.agent_ids or ([self.agent_id] if self.agent_id else ["hermes"]),
            agent_id=self.agent_id,
            get_model=lambda: self._default_model,
            on_message=self._on_cloud,
            on_status_change=self._on_connection_status,
        )
        self._client = client
        self._bridge = TaskBridge(dispatch=self._dispatch_cron, send=self._client_send)
        _ACTIVE_CLIENTS.append(client)
        _ADAPTERS.append(self)
        if self._media_dir is None:
            self._media_dir = tempfile.mkdtemp(prefix="botschat-media-")
        client.start()
        # The gateway considers the platform running; connected state flips on
        # auth.ok via _on_connection_status(True).
        return True

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                _ACTIVE_CLIENTS.remove(self._client)
            except ValueError:
                pass
            await self._client.stop()
            self._client = None
        # Release the scoped lock so another profile can use this token.
        if self._lock_key:
            try:
                from gateway.status import release_scoped_lock

                release_scoped_lock("botschat", self._lock_key)
            except Exception:
                pass
            self._lock_key = None
        try:
            _ADAPTERS.remove(self)
        except ValueError:
            pass
        self._mark_disconnected()

    def _dispatch_cron(self, args: dict) -> str:
        """Run the cronjob tool handler directly (sanctioned programmatic path).

        ctx.dispatch_tool would be cleaner, but the cronjob tool's check_fn
        (session env flags) is TTL-cached at startup, so from the adapter's
        non-session context it resolves to "Unknown tool". Calling the
        registered handler directly is deterministic and runs the exact same
        code the registry dispatches.
        """
        try:
            from tools.cronjob_tools import _cronjob_handler

            return _cronjob_handler(args)
        except Exception as exc:
            logger.error(f"[botschat] cronjob dispatch failed: {exc}")
            return json.dumps({"success": False, "error": f"cronjob dispatch failed: {exc}"})

    def _on_connection_status(self, connected: bool) -> None:
        if connected:
            self._mark_connected()
        else:
            self._mark_disconnected()

    # ------------------------------------------------------------- outbound

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> SendResult:
        client = self._client
        if client is None or not client.connected:
            return SendResult(success=False, error="Not connected to BotsChat cloud")
        if not content:
            return SendResult(success=True, message_id=None)

        message_id = str(uuid.uuid4())
        text = content
        encrypted = False
        if client.e2e_key:
            try:
                text = to_base64(encrypt_text(client.e2e_key, content, message_id))
                encrypted = True
            except Exception as exc:
                return SendResult(success=False, error=f"Encryption failed: {exc}")

        notify_preview = None
        if encrypted and client.notify_preview and content:
            notify_preview = content[:100] + ("…" if len(content) > 100 else "")

        # Only threadId from the session key's ":thread:" suffix — never
        # replyToId: the DO maps (threadId ?? replyToId) into messages.thread_id,
        # so forwarding reply_to would file every reply into a phantom thread
        # that disappears from the main session readback.
        await client.send(
            AgentText(
                sessionKey=chat_id,
                text=text,
                threadId=thread_id_from_session_key(chat_id),
                messageId=message_id,
                encrypted=encrypted,
                notifyPreview=notify_preview,
            )
        )
        # A /model switch just happened in this session — tell the UI the
        # session model changed (the user-requested model, like the reference
        # plugin's model.changed regex but deterministic).
        pending = self._pending_model_changes.pop(chat_id, None)
        if pending:
            await client.send(ModelChanged(model=pending, sessionKey=chat_id))
        return SendResult(success=True, message_id=message_id)

    async def get_chat_info(self, chat_id: str) -> dict:
        return {"name": chat_id, "type": "dm"}

    # ------------------------------------------------------------- inbound

    async def _on_cloud(self, msg: CloudMessage) -> None:
        if isinstance(msg, UserMessage):
            await self._handle_user_message(msg)
        elif isinstance(msg, UserCommand):
            text = f"/{msg.command}"
            if msg.args:
                text += f" {msg.args}"
                if msg.command == "model":
                    # Track the requested model so the next reply to this
                    # session emits model.changed (the reply card from /model
                    # contains no model id, unlike the reference plugin's reply format).
                    requested = msg.args.strip().split()[0]
                    if not requested.startswith("--"):
                        self._pending_model_changes[msg.sessionKey] = requested
            await self._dispatch(
                text=text,
                session_key=msg.sessionKey,
                user_id=self._session_users.get(msg.sessionKey, "command"),
                message_id=f"cmd-{uuid.uuid4()}",
            )
        elif isinstance(msg, UserAction):
            params = msg.params or {}
            kind = params.get("kind") or msg.action or "action"
            value = params.get("value") or params.get("selected") or ""
            label = params.get("label") or value
            await self._dispatch(
                text=f'[Action: kind={kind}] User selected: "{label}"',
                session_key=msg.sessionKey,
                user_id=params.get("userId") or "action",
                message_id=f"action-{uuid.uuid4()}",
            )
        elif isinstance(msg, UserMedia):
            # Media download/decrypt lands in M5; dispatch the attachment now.
            await self._dispatch(
                text="",
                session_key=msg.sessionKey,
                user_id=msg.userId,
                message_id=f"media-{uuid.uuid4()}",
                media_url=msg.mediaUrl,
            )
        elif isinstance(msg, TaskSchedule):
            if self._bridge is not None:
                ack = await self._bridge.schedule(msg)
                await self._client_send(ack)
        elif isinstance(msg, TaskDelete):
            if self._bridge is not None:
                await self._bridge.delete(msg)
        elif isinstance(msg, TaskScanRequest):
            if self._bridge is not None:
                await self._client_send(await self._bridge.scan())
        elif isinstance(msg, TaskRun):
            if self._bridge is not None:
                await self._bridge.run(msg)
        elif isinstance(msg, ModelsRequest):
            models = []
            if self._default_model:
                models.append(
                    ModelInfo(
                        id=self._default_model,
                        name=self._default_model,
                        provider=self._default_provider or "hermes",
                    )
                )
            await self._client_send(ModelsList(models=models))
        elif isinstance(msg, SettingsNotifyPreview):
            if self._client is not None:
                self._client.notify_preview = msg.enabled
        elif isinstance(msg, SettingsDefaultModel):
            await self._apply_default_model(msg.defaultModel)
        else:
            logger.debug(f"[botschat] unhandled inbound message type: {msg.type}")

    async def _apply_default_model(self, model: str) -> None:
        """Apply the web UI's default-model choice and confirm to the cloud."""
        model = (model or "").strip()
        if not model:
            return
        ok = await asyncio.to_thread(_hermes_config_set, "model.default", model)
        if ok:
            self._default_model = model
            await self._client_send(DefaultModelUpdated(model=model))
        else:
            logger.error(f"[botschat] failed to apply default model {model}")

    async def _handle_user_message(self, msg: UserMessage) -> None:
        client = self._client
        self._session_users[msg.sessionKey] = msg.userId
        text = msg.text

        if msg.encrypted and client is not None and client.e2e_key and msg.messageId:
            try:
                text = decrypt_text(client.e2e_key, from_base64(msg.text), msg.messageId)
            except Exception as exc:
                logger.error(f"[botschat] Decryption failed for message {msg.messageId}: {exc}")
                text = "[Decryption Failed]"

        # Some clients send /model as plain text rather than user.command.
        if text.startswith("/model ") and " " in text:
            requested = text.split(maxsplit=2)[1]
            if requested and not requested.startswith("--"):
                self._pending_model_changes[msg.sessionKey] = requested

        # Thread context: the ConnectionDO attaches the parent message; decrypt
        # and hand it to Hermes as channel_context (prepended before the
        # trigger message, like the reference plugin's GroupSystemPrompt).
        channel_context = None
        thread_id = thread_id_from_session_key(msg.sessionKey)
        if thread_id and msg.parentText:
            parent = msg.parentText
            if msg.parentEncrypted and client is not None and client.e2e_key and msg.parentMessageId:
                try:
                    parent = decrypt_text(client.e2e_key, from_base64(msg.parentText), msg.parentMessageId)
                except Exception as exc:
                    logger.error(f"[botschat] Failed to decrypt parent message: {exc}")
                    parent = "[Decryption Failed]"
            sender = msg.parentSender or "unknown"
            channel_context = (
                "[Thread context — this conversation is a thread reply to the "
                f"following {sender} message]\n{parent}"
            )

        await self._dispatch(
            text=text,
            session_key=msg.sessionKey,
            user_id=msg.userId,
            message_id=msg.messageId,
            media_url=msg.mediaUrl,
            thread_id=thread_id,
            channel_context=channel_context,
            media_encrypted=msg.encrypted,
        )

    async def _dispatch(
        self,
        text: str,
        session_key: str,
        user_id: str,
        message_id: str,
        media_url: Optional[str] = None,
        thread_id: Optional[str] = None,
        channel_context: Optional[str] = None,
        media_encrypted: Optional[int] = None,
    ) -> None:
        """Build a Hermes MessageEvent and route it into the gateway runner."""
        self._known_sessions.add(session_key)
        self._last_session = session_key
        source = self.build_source(
            chat_id=session_key,
            chat_name=session_key,
            chat_type="thread" if thread_id else "dm",
            user_id=user_id,
            user_name=user_id,
            thread_id=thread_id,
        )
        # Hub mode: route to the profile owning the session key's agent.
        # The runner honors a pre-stamped source.profile (skips route
        # resolution) and activates the profile-scoped agent run.
        hub_profile = self._profile_for_session(session_key)
        if hub_profile:
            source.profile = hub_profile
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            user_id=user_id,
            source=source,
            message_id=message_id,
            channel_context=channel_context,
            metadata={"botschat_thread": thread_id} if thread_id else None,
        )
        if media_url:
            local = await self._download_inbound_media(
                media_url, message_id, encrypted=media_encrypted
            )
            if local:
                event.media_urls = [local]
            else:
                event.media_urls = [media_url]
        await self.handle_message(event)

    # ------------------------------------------------------------- media

    async def _download_inbound_media(
        self, media_url: str, message_id: str, encrypted: Optional[int] = None
    ) -> Optional[str]:
        """Download inbound media to a local file (decrypting when E2E)."""
        resolved = resolve_url(self.cloud_url, media_url)
        fetched = await asyncio.to_thread(fetch_bytes, resolved)
        if fetched is None:
            return None
        data, content_type = fetched
        if encrypted and self._client is not None and self._client.e2e_key:
            try:
                data = decrypt_bytes(
                    self._client.e2e_key, data, f"{message_id}:media"
                )
            except Exception as exc:
                logger.warning(f"[botschat] inbound media decrypt failed: {exc}")
                return None
        ext = EXT_BY_MIME.get(content_type, "bin")
        path = os.path.join(self._media_dir or "/tmp", f"inbound-{uuid.uuid4().hex}.{ext}")
        with open(path, "wb") as fh:
            fh.write(data)
        logger.info(f"[botschat] inbound media -> {path} ({len(data)} bytes, {content_type})")
        return path

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> SendResult:
        """Upload an agent image to R2 and emit agent.media."""
        return await self._send_media(chat_id, image_url, caption or "", reply_to)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media(chat_id, file_path, caption or "", reply_to)

    async def _send_media(
        self, chat_id: str, media_url: str, caption: str, reply_to: Optional[str]
    ) -> SendResult:
        """Fetch, optionally E2E-encrypt, upload to R2, emit agent.media."""
        client = self._client
        if client is None or not client.connected:
            return SendResult(success=False, error="Not connected to BotsChat cloud")
        message_id = str(uuid.uuid4())

        fetched = await asyncio.to_thread(self._read_media, media_url)
        if fetched is None:
            # Fall back to sending the reference as text (base behavior).
            text = f"{caption}\n{media_url}" if caption else media_url
            return await self.send(chat_id, text, reply_to)
        data, content_type = fetched

        media_encrypted = False
        if client.e2e_key:
            data = encrypt_bytes(client.e2e_key, data, f"{message_id}:media")
            media_encrypted = True

        filename = f"{'encrypted' if media_encrypted else 'media'}-{uuid.uuid4().hex[:8]}"
        url = await asyncio.to_thread(
            upload_to_r2, self.cloud_url, self.pairing_token, data, content_type, filename
        )
        if not url:
            text = f"{caption}\n{media_url}" if caption else media_url
            return await self.send(chat_id, text, reply_to)

        caption_enc, encrypted = caption, False
        if client.e2e_key and caption:
            caption_enc = to_base64(encrypt_text(client.e2e_key, caption, message_id))
            encrypted = True
        await client.send(
            AgentMedia(
                sessionKey=chat_id,
                mediaUrl=url,
                caption=caption_enc or None,
                threadId=thread_id_from_session_key(chat_id),
                messageId=message_id,
                encrypted=encrypted,
                mediaEncrypted=media_encrypted,
            )
        )
        return SendResult(success=True, message_id=message_id)

    @staticmethod
    def _read_media(media_url: str) -> Optional[tuple]:
        """Read media bytes from a local path or remote URL."""
        if media_url.startswith(("http://", "https://")):
            return fetch_bytes(media_url)
        try:
            with open(media_url, "rb") as fh:
                return fh.read(), guess_content_type(media_url)
        except Exception as exc:
            logger.warning(f"[botschat] cannot read media {media_url}: {exc}")
            return None

    async def _client_send(self, msg: CloudMessage) -> None:
        if self._client is not None:
            await self._client.send(msg)


def thread_id_from_session_key(session_key: str) -> Optional[str]:
    """Extract the thread id from a BotsChat session key (``...:thread:<id>``)."""
    if ":thread:" in session_key:
        return session_key.rsplit(":thread:", 1)[-1]
    return None


def match_botschat_session(session_id: str, known: set) -> Optional[str]:
    """Map a Hermes session id to the BotsChat sessionKey it embeds.

    Gateway session ids are ``agent:<profile>:botschat:<chat_type>:<sessionKey>``
    (the BotsChat key itself contains colons and starts with "agent:..."), so
    the BotsChat key is a SUFFIX. Return the longest known key that matches.
    """
    if not session_id:
        return None
    if session_id in known:
        return session_id
    best = None
    for sk in known:
        if sk and session_id.endswith(sk) and (best is None or len(sk) > len(best)):
            best = sk
    return best


# ---------------------------------------------------------------------------
# Stream / tool-activity hooks — invoked synchronously on worker threads;
# sends are marshaled onto the client's event loop (see _on_cron_session_end).
# ---------------------------------------------------------------------------


def _active_adapter():
    for a in list(_ADAPTERS):
        if a._client is not None and getattr(a._client, "connected", False):
            return a
    return None


def _send_via_loop(adapter, msg: CloudMessage) -> None:
    client = adapter._client
    loop = getattr(client, "loop", None)
    if loop is None or loop.is_closed():
        return
    loop.call_soon_threadsafe(lambda: loop.create_task(client.send(msg)))


def _encrypt_payload(adapter, text: str) -> tuple:
    """Return (text, encrypted, context_id) — E2E-encrypting when a key is set."""
    client = adapter._client
    if client is not None and client.e2e_key and text:
        context_id = str(uuid.uuid4())
        try:
            return to_base64(encrypt_text(client.e2e_key, text, context_id)), True, context_id
        except Exception:
            pass
    return text, False, None


def _on_stream_start(session_id=None, **kwargs):
    adapter = _active_adapter()
    if adapter is None:
        return
    sk = match_botschat_session(session_id or "", adapter._known_sessions)
    if sk is None:
        return
    run_id = f"run_{int(time.time() * 1000)}"
    adapter._stream_runs[session_id] = (sk, run_id)
    logger.info(f"[botschat] stream start -> {sk} (run {run_id})")
    _send_via_loop(adapter, AgentStreamStart(sessionKey=sk, runId=run_id))


def _on_stream_delta(session_id=None, delta=None, kind="text", **kwargs):
    adapter = _active_adapter()
    if adapter is None or not delta:
        return
    run = adapter._stream_runs.get(session_id)
    if run is None:
        return
    sk, run_id = run
    if kind == "reasoning":
        text, encrypted, aid = _encrypt_payload(adapter, delta)
        _send_via_loop(
            adapter,
            AgentActivity(
                sessionKey=sk, runId=run_id, kind="reasoning", text=text,
                encrypted=encrypted or None, activityId=aid,
            ),
        )
        return
    text, encrypted, cid = _encrypt_payload(adapter, delta)
    _send_via_loop(
        adapter,
        AgentStreamChunk(
            sessionKey=sk, runId=run_id, text=text,
            encrypted=encrypted or None, chunkId=cid,
        ),
    )


def _on_stream_end(session_id=None, **kwargs):
    adapter = _active_adapter()
    if adapter is None:
        return
    run = adapter._stream_runs.pop(session_id, None)
    if run is None:
        return
    sk, run_id = run
    _send_via_loop(adapter, AgentStreamEnd(sessionKey=sk, runId=run_id))


def _on_pre_tool_call(tool_name=None, session_id=None, **kwargs):
    adapter = _active_adapter()
    if adapter is None or not tool_name:
        return None
    sk = match_botschat_session(session_id or "", adapter._known_sessions)
    if sk is None:
        sk = adapter._last_session
    if sk is None:
        return None
    _send_via_loop(
        adapter, AgentActivity(sessionKey=sk, runId="", kind="tool_start", toolName=tool_name)
    )
    return None  # no directive


def _on_post_tool_call(tool_name=None, session_id=None, duration_ms=None,
                       status=None, error_message=None, **kwargs):
    adapter = _active_adapter()
    if adapter is None or not tool_name:
        return
    sk = match_botschat_session(session_id or "", adapter._known_sessions)
    if sk is None:
        sk = adapter._last_session
    if sk is None:
        return
    msg = AgentActivity(
        sessionKey=sk, runId="", kind="tool_end",
        toolName=tool_name, durationMs=int(duration_ms or 0),
    )
    detail = error_message or (f"status={status}" if status and status != "ok" else None)
    if detail:
        text, encrypted, aid = _encrypt_payload(adapter, detail[:500])
        msg.text = text
        msg.encrypted = encrypted or None
        msg.activityId = aid
    _send_via_loop(adapter, msg)


# ---------------------------------------------------------------------------
# Registration helpers
# ---------------------------------------------------------------------------


def check_requirements() -> bool:
    """Passive dependency probe: are the runtime deps importable?

    Deliberately does NOT check credentials: the registry calls check_fn
    without a config, and validate_config() is the credential gate (env OR
    config extra) — so config-only setups (e.g. per-profile ``extra`` under
    the multiplexer) can create adapters too.
    """
    for _mod in ("websockets", "cryptography"):
        try:
            __import__(_mod)
        except ImportError:
            return False
    return True


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    url = os.getenv("BOTSCHAT_CLOUD_URL") or extra.get("cloudUrl")
    token = os.getenv("BOTSCHAT_PAIRING_TOKEN") or extra.get("pairingToken")
    return bool(url and token)


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env vars (env-only setups auto-enable)."""
    url = os.getenv("BOTSCHAT_CLOUD_URL", "").strip()
    token = os.getenv("BOTSCHAT_PAIRING_TOKEN", "").strip()
    if not (url and token):
        return None
    seed = {"cloudUrl": url, "pairingToken": token}
    e2e = os.getenv("BOTSCHAT_E2E_PASSWORD", "").strip()
    if e2e:
        seed["e2ePassword"] = e2e
    agent_id = os.getenv("BOTSCHAT_AGENT_ID", "").strip()
    if agent_id:
        seed["agentId"] = agent_id
    home = os.getenv("BOTSCHAT_HOME_CHANNEL", "").strip()
    if home:
        # Cron delivery + cross-platform message target (a BotsChat sessionKey).
        seed["home_channel"] = {"chat_id": home, "name": "BotsChat home"}
    return seed


def _hermes_config_get(key: str) -> Optional[str]:
    """Read a config value via the hermes CLI (targets the active profile)."""
    try:
        cmd = ["hermes"]
        if _PROFILE_NAME:
            cmd += ["-p", _PROFILE_NAME]
        cmd += ["config", "get", key]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            val = out.stdout.strip()
            return val or None
    except Exception as exc:
        logger.debug(f"[botschat] `hermes config get {key}` failed: {exc}")
    return None


def _hermes_config_set(key: str, value: str) -> bool:
    """Set a config value via the hermes CLI (targets the active profile)."""
    try:
        cmd = ["hermes"]
        if _PROFILE_NAME:
            cmd += ["-p", _PROFILE_NAME]
        cmd += ["config", "set", key, value]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return out.returncode == 0
    except Exception as exc:
        logger.debug(f"[botschat] `hermes config set {key}` failed: {exc}")
    return False


def cron_job_id_from_session(session_id: str) -> Optional[str]:
    """Extract the Hermes cron job id from a cron session id.

    Cron sessions are keyed ``cron_<job_id>_<YYYYmmdd>_<HHMMSS>``
    (cron/scheduler.py line ~5719) — the timestamp itself contains an
    underscore, so strip the last TWO underscore segments.
    """
    if not session_id or not session_id.startswith("cron_"):
        return None
    rest = session_id[len("cron_"):]
    parts = rest.rsplit("_", 2)
    job_id = parts[0] if parts else None
    return job_id or None


def _on_cron_session_end(session_id=None, platform=None, **kwargs):
    """Report a finished scheduled cron run as a job.update (best effort).

    This hook is invoked SYNCHRONOUSLY by the gateway (invoke_hook does not
    await callbacks), so the actual send is scheduled on the client's event
    loop. The job record may lag the hook, so this is informational;
    task.scan.request remains authoritative.
    """
    if platform != "cron" or not session_id or not session_id.startswith("cron_"):
        return
    job_id = cron_job_id_from_session(session_id)
    if not job_id:
        return
    logger.info(f"[botschat] cron session ended: {session_id} (job {job_id})")
    status, summary = "ok", "Scheduled run finished"
    try:
        from cron.jobs import get_job

        job = get_job(job_id)
        if job is not None and job.get("last_status") in ("ok", "error"):
            status = job["last_status"]
            summary = job.get("last_fire_error") or f"Run finished ({status})"
    except Exception as exc:
        logger.debug(f"[botschat] cron session-end lookup failed: {exc}")
    ts = int(time.time() * 1000)
    update = JobUpdate(
        cronJobId=job_id,
        jobId=session_id,
        sessionKey=session_id,
        status=status,
        summary=summary,
        startedAt=ts,
        finishedAt=ts,
    )
    # Hooks run on worker threads with no loop of their own — marshal the
    # send onto the gateway's main loop (the client's own loop).
    client = next(
        (c for c in list(_ACTIVE_CLIENTS) if getattr(c, "connected", False)), None
    )
    loop = getattr(client, "loop", None) if client is not None else None
    if loop is None or loop.is_closed():
        logger.warning("[botschat] no event loop for cron session-end update")
        return

    def _send():
        loop.create_task(client.send(update))

    loop.call_soon_threadsafe(_send)


def register(ctx):
    """Plugin entry point — called once at Hermes startup."""
    global _PROFILE_NAME, _CTX
    _PROFILE_NAME = getattr(ctx, "profile_name", None)
    _CTX = ctx

    ctx.register_platform(
        name="botschat",
        label="BotsChat",
        adapter_factory=lambda cfg: BotsChatAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        # The enablement sweep's credential gate: "would this platform be
        # configured with this (probe) config?" — env or extra creds. Without
        # it, the deps-only check_fn would auto-enable botschat in profiles
        # that have no config at all.
        is_connected=validate_config,
        required_env=["BOTSCHAT_CLOUD_URL", "BOTSCHAT_PAIRING_TOKEN"],
        env_enablement_fn=_env_enablement,
        platform_hint=A2UI_PLATFORM_HINT,
        emoji="🤖",
        max_message_length=4000,
    )
    # Durable A2UI instructions, frozen into every new session prompt
    # (cache-safe: does not mutate the user message like pre_llm_call would).
    ctx.register_system_prompt_section(A2UI_SECTION_ID, A2UI_SECTION)
    # Report scheduled cron runs as job.update (best effort).
    ctx.register_hook("on_session_end", _on_cron_session_end)
    # Streaming: forward token deltas as agent.stream.* (native BotsChat streaming).
    ctx.register_hook("on_stream_start", _on_stream_start)
    ctx.register_hook("on_stream_delta", _on_stream_delta)
    ctx.register_hook("on_stream_end", _on_stream_end)
    # Tool activity: agent.activity tool_start/tool_end.
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
