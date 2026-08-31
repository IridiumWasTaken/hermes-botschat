"""Tests for the BotsChat WSS client against a local mock ConnectionDO.

The mock speaks the same handshake as packages/api/src/do/connection-do.ts:
auth frame -> auth.ok (+ the three post-auth pushes) -> relay loop.
"""

import asyncio
import random
from types import SimpleNamespace

import pytest
import websockets
from websockets.exceptions import InvalidStatus

from botschat import ws_client as ws_mod
from botschat.e2e import derive_key
from botschat.protocol import AgentText, Auth, CloudMessage, UserMessage
from botschat.ws_client import BotsChatCloudClient

USER_ID = "u_test_user_123"


class MockConnectionDO:
    """Minimal ConnectionDO stand-in: records frames, drives handshakes."""

    def __init__(self):
        self.received = []  # CloudMessage frames from the client, in order
        self.connection_count = 0
        self.path = None
        self.query_token = None
        self.expect_auth_fail = False
        self.close_on_auth = None  # close code to send right after auth.ok
        self.close_once_after_auth = False  # close only on the FIRST connection
        self.pong_seen = False
        self._ws = None  # the live server-side connection (for pushing frames)

    async def handler(self, ws):
        self.connection_count += 1
        self._ws = ws
        path = ws.request.path if hasattr(ws.request, "path") else None
        self.path = path
        # websockets 15 keeps the query in request.path; parse the token out.
        self.query_token = None
        if path and "?token=" in path:
            self.query_token = path.split("?token=", 1)[1]

        async for raw in ws:
            try:
                msg = CloudMessage.from_json(raw)
            except Exception:
                continue
            self.received.append(msg)

            if isinstance(msg, Auth):
                if self.expect_auth_fail:
                    await ws.send('{"type":"auth.fail","reason":"Invalid pairing token"}')
                    await ws.close(code=4001, reason="auth failed")
                    return
                await ws.send(f'{{"type":"auth.ok","userId":"{USER_ID}"}}')
                if self.close_on_auth is not None and (
                    not self.close_once_after_auth or self.connection_count == 1
                ):
                    await ws.close(code=self.close_on_auth, reason="replaced")
                    return
                # The real DO pushes these three immediately after auth.ok.
                await ws.send('{"type":"task.scan.request"}')
                await ws.send('{"type":"models.request"}')
                await ws.send('{"type":"settings.notifyPreview","enabled":false}')
            elif msg.type == "pong":
                self.pong_seen = True


async def wait_until(fn, timeout=3.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if fn():
            return True
        await asyncio.sleep(0.01)
    return False


def make_client(port, e2e_password=None, on_message=None, agent_id=None, agent_ids=None):
    return BotsChatCloudClient(
        cloud_url=f"http://127.0.0.1:{port}",
        pairing_token="bc_pat_test_token",
        e2e_password=e2e_password,
        agent_ids=agent_ids or ["hermes"],
        agent_id=agent_id,
        on_message=on_message,
    )


async def run_server(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def test_auth_handshake_and_post_auth_push():
    async def scenario():
        mock = MockConnectionDO()
        server, port = await run_server(mock.handler)
        received = []
        client = make_client(port, on_message=received.append)
        client.start()
        try:
            assert await wait_until(lambda: client.connected), "auth.ok never arrived"
            assert await wait_until(lambda: len(received) >= 3), "post-auth pushes missing"
        finally:
            await client.stop()
            server.close()
            await server.wait_closed()

        assert mock.connection_count == 1
        # websockets 15 includes the query string in request.path.
        assert (mock.path or "").startswith("/api/gateway/default")
        assert mock.query_token == "bc_pat_test_token"
        auth = mock.received[0]
        assert isinstance(auth, Auth)
        assert auth.agentType == "hermes"
        assert auth.agents == ["hermes"]
        # Post-auth push order matches the real DO: scan, models, notifyPreview.
        assert [m.type for m in received] == [
            "task.scan.request", "models.request", "settings.notifyPreview",
        ]

    asyncio.run(scenario())


def test_auth_carries_distinct_agent_id():
    """BOTSCHAT_AGENT_ID flows into the auth frame (multi-agent accounts)."""

    async def scenario():
        mock = MockConnectionDO()
        server, port = await run_server(mock.handler)
        client = make_client(port, agent_id="hermes-2", agent_ids=["hermes-2"])
        client.start()
        try:
            assert await wait_until(lambda: client.connected), "auth.ok never arrived"
        finally:
            await client.stop()
            server.close()
            await server.wait_closed()

        auth = mock.received[0]
        assert isinstance(auth, Auth)
        assert auth.agentId == "hermes-2"
        assert auth.agents == ["hermes-2"]

    asyncio.run(scenario())


def test_user_message_routed_to_on_message():
    async def scenario():
        mock = MockConnectionDO()
        server, port = await run_server(mock.handler)
        received = []
        client = make_client(port, on_message=received.append)
        client.start()
        try:
            assert await wait_until(lambda: client.connected)
            assert await wait_until(lambda: len(received) == 3)  # post-auth pushes
            # The DO pushes a user.message to the client:
            await mock._ws.send(
                '{"type":"user.message","sessionKey":"agent:main:botschat:u_1:adhoc",'
                '"text":"hello","userId":"u_1","messageId":"m1"}'
            )
            assert await wait_until(
                lambda: any(isinstance(m, UserMessage) for m in received)
            ), "user.message never reached on_message"
            msg = next(m for m in received if isinstance(m, UserMessage))
            assert msg.sessionKey == "agent:main:botschat:u_1:adhoc"
            assert msg.text == "hello"
            assert msg.userId == "u_1"
            assert msg.messageId == "m1"
        finally:
            await client.stop()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_send_delivers_agent_text():
    async def scenario():
        mock = MockConnectionDO()
        server, port = await run_server(mock.handler)
        client = make_client(port)
        client.start()
        try:
            assert await wait_until(lambda: client.connected)
            await client.send(AgentText(sessionKey="sk", text="Hi there", messageId="mid-1"))
            assert await wait_until(lambda: len(mock.received) == 2)
            out = mock.received[1]
            assert isinstance(out, AgentText)
            assert out.sessionKey == "sk"
            assert out.text == "Hi there"
            assert out.messageId == "mid-1"
        finally:
            await client.stop()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_e2e_key_derived_after_auth_ok():
    async def scenario():
        mock = MockConnectionDO()
        server, port = await run_server(mock.handler)
        client = make_client(port, e2e_password="test-password")
        client.start()
        try:
            assert await wait_until(lambda: client.e2e_key is not None), "E2E key never derived"
            assert client.e2e_key == derive_key("test-password", USER_ID)
        finally:
            await client.stop()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_ping_gets_pong():
    async def scenario():
        mock = MockConnectionDO()
        server, port = await run_server(mock.handler)
        client = make_client(port)
        client.start()
        try:
            assert await wait_until(lambda: client.connected)
            await mock._ws.send('{"type":"ping"}')  # server sends an app-level ping
            assert await wait_until(lambda: mock.pong_seen), "client never answered ping"
        finally:
            await client.stop()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_auth_fail_does_not_reconnect():
    async def scenario():
        mock = MockConnectionDO()
        mock.expect_auth_fail = True
        server, port = await run_server(mock.handler)
        client = make_client(port)
        client.start()
        try:
            await asyncio.sleep(1.6)  # longer than the min backoff
            assert not client.connected
        finally:
            await client.stop()
            server.close()
            await server.wait_closed()

        assert mock.connection_count == 1, "client must not reconnect after auth.fail"

    asyncio.run(scenario())


def test_close_4009_does_not_reconnect():
    async def scenario():
        mock = MockConnectionDO()
        mock.close_on_auth = 4009
        server, port = await run_server(mock.handler)
        client = make_client(port)
        client.start()
        try:
            await asyncio.sleep(1.6)
            assert not client.connected
        finally:
            await client.stop()
            server.close()
            await server.wait_closed()

        assert mock.connection_count == 1, "client must not reconnect after 4009"

    asyncio.run(scenario())


def test_reconnect_after_clean_drop():
    """A normal close (code 1000) triggers backoff + reconnect; auth re-runs."""

    async def scenario():
        mock = MockConnectionDO()
        mock.close_on_auth = 1000
        mock.close_once_after_auth = True
        server, port = await run_server(mock.handler)
        client = make_client(port)
        client.start()
        try:
            assert await wait_until(lambda: client.connected), "first connect failed"
            # Server dropped us; the client must back off and reconnect.
            assert await wait_until(lambda: mock.connection_count >= 2), "no reconnect attempt"
            assert await wait_until(lambda: client.connected), "did not recover after reconnect"
            # Backoff grew after the drop (jitter 0.75–1.25x of min).
            assert ws_mod.MIN_BACKOFF_MS <= client._backoff_ms <= ws_mod.MAX_BACKOFF_MS
        finally:
            await client.stop()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_429_rate_limit_backoff(monkeypatch):
    """HTTP 429 with Retry-After extends the backoff beyond the minimum."""
    fake_resp = SimpleNamespace(status_code=429, headers={"Retry-After": "5"})

    def _reject(*args, **kwargs):
        raise InvalidStatus(fake_resp)

    monkeypatch.setattr(websockets, "connect", _reject)

    async def scenario():
        client = make_client(1)
        client.start()
        try:
            assert await wait_until(
                lambda: client._backoff_ms >= 5000, timeout=2
            ), "429 Retry-After never applied to backoff"
            assert not client.connected
        finally:
            await client.stop()

    asyncio.run(scenario())


def test_503_service_unavailable_backoff(monkeypatch):
    """HTTP 503 without Retry-After backs off to the MAX (capped)."""
    fake_resp = SimpleNamespace(status_code=503, headers={})

    def _reject(*args, **kwargs):
        raise InvalidStatus(fake_resp)

    monkeypatch.setattr(websockets, "connect", _reject)

    async def scenario():
        client = make_client(1)
        client.start()
        try:
            assert await wait_until(
                lambda: client._backoff_ms == ws_mod.MAX_BACKOFF_MS, timeout=2
            ), "503 backoff not clamped to MAX_BACKOFF_MS"
            assert not client.connected
        finally:
            await client.stop()

    asyncio.run(scenario())


def test_backoff_jitter_doubles_and_caps(monkeypatch):
    """_sleep_backoff sleeps backoff*jitter, doubles, and caps at MAX."""
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    jitter = {"value": 1.0}
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(random, "random", lambda: jitter["value"])

    async def scenario():
        client = make_client(1)
        client._backoff_ms = 1000
        jitter["value"] = 1.0  # jitter = 0.75 + 1.0*0.5 = 1.25 (upper bound)
        await client._sleep_backoff()
        assert sleeps == [pytest.approx(1.25)]
        assert client._backoff_ms == 2000  # doubled

        jitter["value"] = 0.0  # jitter = 0.75 (lower bound)
        await client._sleep_backoff()
        assert sleeps[-1] == pytest.approx(1.5)
        assert client._backoff_ms == 4000

        client._backoff_ms = ws_mod.MAX_BACKOFF_MS
        jitter["value"] = 1.0
        await client._sleep_backoff()
        assert sleeps[-1] == pytest.approx(37.5)  # 30000ms * 1.25 jitter
        assert client._backoff_ms == ws_mod.MAX_BACKOFF_MS  # capped, not 60s

    asyncio.run(scenario())


def test_backoff_resets_after_sustained_connection(monkeypatch):
    """A stable connection resets the backoff to MIN after BACKOFF_RESET_MS."""
    monkeypatch.setattr(ws_mod, "BACKOFF_RESET_MS", 50)

    async def scenario():
        client = make_client(1)
        client._backoff_ms = 20000
        client._schedule_backoff_reset()
        client._schedule_backoff_reset()  # guard: must not schedule a second task
        assert await wait_until(
            lambda: client._backoff_ms == ws_mod.MIN_BACKOFF_MS, timeout=2
        ), "backoff never reset after sustained connection"
        # No duplicate reset task may fire and clobber a later bump.
        client._backoff_ms = 12345
        await asyncio.sleep(0.15)
        assert client._backoff_ms == 12345

    asyncio.run(scenario())
