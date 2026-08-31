"""Persistent outbound WebSocket client to the BotsChat cloud (ConnectionDO).

Faithful port of packages/plugin/src/ws-client.ts to Python asyncio:

- Connects to ``{ws|wss}://<cloudUrl>/api/gateway/<accountId>?token=<pairingToken>``
- Sends ``auth`` on open; treats ``auth.ok`` as the connected signal and derives
  the E2E key right after (PBKDF2 is slow, ~1-2 s — done off the hot path)
- App-level keepalive: ``status`` heartbeat every 25 s, ``ping`` -> ``pong``
- Exponential backoff 1 s -> 30 s with +/-25 % jitter; close code 4009
  ("replaced by a newer connection") and 4001 (auth failed) do NOT reconnect
- HTTP-level 429/503 rejections extend the backoff (Retry-After honored)
"""

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable, Optional
from urllib.parse import quote

import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, InvalidStatus

from .e2e import derive_key
from .protocol import Auth, AuthOk, AuthFail, CloudMessage, Pong, Status

logger = logging.getLogger("botschat")

MIN_BACKOFF_MS = 1_000
MAX_BACKOFF_MS = 30_000
BACKOFF_RESET_MS = 10_000
KEEPALIVE_S = 25

# Custom close codes used by the ConnectionDO.
CLOSE_REPLACED = 4009  # server replaced this connection with a newer one
CLOSE_AUTH_FAILED = 4001


def _is_open(ws) -> bool:
    """True when the websockets connection is in the OPEN state.

    websockets >= 13 dropped the ``open`` property in favor of ``state``
    (a ``State`` enum); older versions only have ``open``.
    """
    state = getattr(ws, "state", None)
    if state is not None:
        try:
            from websockets.protocol import State

            return state is State.OPEN
        except ImportError:
            pass
        return bool(state)
    return bool(getattr(ws, "open", False))

OnMessage = Callable[[CloudMessage], Awaitable[None]]
OnStatusChange = Callable[[bool], None]


class BotsChatCloudClient:
    """One persistent outbound WSS connection to a BotsChat server account."""

    def __init__(
        self,
        cloud_url: str,
        pairing_token: str,
        account_id: str = "default",
        e2e_password: Optional[str] = None,
        agent_ids: Optional[list] = None,
        agent_id: Optional[str] = None,
        get_model: Optional[Callable[[], Optional[str]]] = None,
        on_message: Optional[OnMessage] = None,
        on_status_change: Optional[OnStatusChange] = None,
        log: Optional[logging.Logger] = None,
    ):
        self.cloud_url = cloud_url
        self.account_id = account_id
        self.pairing_token = pairing_token
        self.e2e_password = e2e_password
        self.agent_ids = agent_ids or ["hermes"]
        # Optional distinct agent identity (BOTSCHAT_AGENT_ID). When unset the
        # server assigns the default agent; setting it lets one BotsChat
        # account host several Hermes profiles as separate agents.
        self.agent_id = agent_id
        self.get_model = get_model or (lambda: None)
        self.on_message = on_message
        self.on_status_change = on_status_change

        self.log = log or logger
        self.e2e_key: Optional[bytes] = None
        self.notify_preview = False
        self.connected = False

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._intentional_close = False
        self._backoff_ms = MIN_BACKOFF_MS
        self._run_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._backoff_reset_task: Optional[asyncio.Task] = None
        # The event loop this client runs on (the gateway's main loop) —
        # captured in start(); used by out-of-adapter hooks to marshal sends.
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------ public

    def start(self) -> asyncio.Task:
        """Start the connection+reconnect loop as a background task."""
        self._intentional_close = False
        self.loop = asyncio.get_running_loop()
        self._run_task = asyncio.create_task(self._run_loop())
        return self._run_task

    async def stop(self) -> None:
        """Gracefully close and cancel the reconnect loop."""
        self._intentional_close = True
        for task in (self._keepalive_task, self._backoff_reset_task):
            if task and not task.done():
                task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close(code=1000, reason="shutdown")
            except Exception:
                pass
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._run_task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self._set_connected(False)

    async def send(self, msg: CloudMessage) -> None:
        """Send one CloudMessage (no-op when the socket is not open)."""
        ws = self._ws
        if ws is None or not _is_open(ws):
            self.log.warning("[botschat] Cannot send — WebSocket not open")
            return
        try:
            await ws.send(msg.to_json())
        except ConnectionClosed:
            self.log.warning("[botschat] send failed — connection closed")

    # ------------------------------------------------------------------ internals

    def _build_url(self) -> str:
        host = self.cloud_url.replace("https://", "").replace("http://", "")
        scheme = "ws" if self.cloud_url.startswith("http://") else "wss"
        return (
            f"{scheme}://{host}/api/gateway/{self.account_id}"
            f"?token={quote(self.pairing_token)}"
        )

    async def _run_loop(self) -> None:
        """Connect -> serve -> on drop, back off and retry (unless intentional)."""
        while not self._intentional_close:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                code = getattr(getattr(exc, "rcvd", None), "code", None) or getattr(
                    getattr(exc, "sent", None), "code", None
                )
                if code == CLOSE_REPLACED:
                    self.log.info("[botschat] Connection replaced by server — not reconnecting")
                    self._intentional_close = True
                    break
                if self._intentional_close:
                    break  # auth.fail closed us with 4001 — no reconnect
                self.log.warning(f"[botschat] WebSocket closed (code={code}) — reconnecting")
            except InvalidStatus as exc:
                status = exc.response.status_code if exc.response is not None else 0
                retry_after = 0
                if exc.response is not None:
                    try:
                        retry_after = int(exc.response.headers.get("Retry-After", "0"))
                    except (TypeError, ValueError):
                        retry_after = 0
                if status == 429 and retry_after > 0:
                    self._backoff_ms = min(max(self._backoff_ms, retry_after * 1000), MAX_BACKOFF_MS)
                    self.log.warning(f"[botschat] Rate-limited (429), backing off {retry_after}s")
                elif status == 503:
                    secs = retry_after or 300
                    self._backoff_ms = min(max(self._backoff_ms, secs * 1000), MAX_BACKOFF_MS)
                    self.log.warning(f"[botschat] Service unavailable (503), backing off {secs}s")
                else:
                    self.log.warning(f"[botschat] Connect rejected (HTTP {status})")
            except Exception as exc:
                self.log.warning(f"[botschat] Connection error: {exc}")

            if self._intentional_close:
                break
            await self._sleep_backoff()

        self._set_connected(False)

    async def _connect_once(self) -> None:
        try:
            ws = await websockets.connect(
                self._build_url(),
                open_timeout=30,
                close_timeout=5,
                ping_interval=None,  # app-level keepalive only (matches upstream)
                max_size=4 * 1024 * 1024,
            )
        except InvalidStatus:
            raise  # handled by _run_loop (429/503 backoff)
        except OSError as exc:
            raise ConnectionError(f"cannot reach {self.cloud_url}: {exc}") from exc

        self._ws = ws
        await ws.send(
            Auth(
                token=self.pairing_token,
                agentId=self.agent_id,
                agentType="hermes",
                agents=self.agent_ids,
                model=self.get_model(),
            ).to_json()
        )

        async for raw in ws:
            await self._handle_raw(raw)

    async def _handle_raw(self, raw: str) -> None:
        try:
            msg = CloudMessage.from_json(raw)
        except Exception:
            self.log.error("[botschat] Failed to parse message", exc_info=True)
            return

        if isinstance(msg, AuthOk):
            self.log.info(f"[botschat] Authenticated with BotsChat cloud (userId={msg.userId})")
            self._schedule_backoff_reset()
            self._set_connected(True)
            self._start_keepalive()
            # Derive the E2E key AFTER marking connected (PBKDF2 is slow ~1-2 s).
            if msg.userId and self.e2e_password:
                self.log.info("[botschat] Deriving E2E key")
                try:
                    self.e2e_key = await asyncio.to_thread(derive_key, self.e2e_password, msg.userId)
                    self.log.info("[botschat] E2E key derived successfully")
                except Exception as exc:
                    self.log.error(f"[botschat] E2E key derivation failed: {exc}")
        elif isinstance(msg, AuthFail):
            self.log.error(f"[botschat] Authentication failed: {msg.reason}")
            self._intentional_close = True  # don't reconnect on auth failure
            if self._ws is not None:
                try:
                    await self._ws.close(code=CLOSE_AUTH_FAILED, reason="auth failed")
                except Exception:
                    pass
        elif isinstance(msg, Pong):
            pass  # outbound-only; ignore unexpected pongs
        else:
            # Everything else (user.*, task.*, settings.*, ping) -> adapter handler.
            if msg.type == "ping":
                await self.send(Pong())
                return
            handler = self.on_message
            if handler is not None:
                result = handler(msg)
                if asyncio.iscoroutine(result):
                    await result

    # ------------------------------------------------------------------ keepalive / backoff / state

    def _start_keepalive(self) -> None:
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return

        async def _beat():
            try:
                while True:
                    await asyncio.sleep(KEEPALIVE_S)
                    await self.send(
                        Status(
                            connected=True,
                            agents=self.agent_ids,
                            model=self.get_model(),
                        )
                    )
            except asyncio.CancelledError:
                pass

        self._keepalive_task = asyncio.create_task(_beat())

    def _schedule_backoff_reset(self) -> None:
        if self._backoff_reset_task is not None and not self._backoff_reset_task.done():
            return

        async def _reset():
            try:
                await asyncio.sleep(BACKOFF_RESET_MS / 1000)
                self._backoff_ms = MIN_BACKOFF_MS
            except asyncio.CancelledError:
                pass

        self._backoff_reset_task = asyncio.create_task(_reset())

    async def _sleep_backoff(self) -> None:
        jitter = 0.75 + random.random() * 0.5
        delay = self._backoff_ms * jitter / 1000
        self._backoff_ms = min(self._backoff_ms * 2, MAX_BACKOFF_MS)
        self.log.info(f"[botschat] Reconnecting in {delay * 1000:.0f}ms")
        await asyncio.sleep(delay)

    def _set_connected(self, value: bool) -> None:
        if self.connected != value:
            self.connected = value
            if self.on_status_change is not None:
                self.on_status_change(value)
