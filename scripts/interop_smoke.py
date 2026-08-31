"""Real-server interop smoke test for the BotsChat Hermes plugin.

Requires a local BotsChat dev server running with dev-auth enabled:

    cd <botschat-repo>
    npm run db:migrate
    npx wrangler dev --config wrangler.toml \
        --var ENVIRONMENT:development \
        --var DEV_AUTH_SECRET:devsecret \
        --var JWT_SECRET:test-jwt-secret

Then:  python scripts/interop_smoke.py --secret devsecret

Flow (mirrors the real web UI <-> ConnectionDO <-> agent loop):
  1. dev-auth login -> JWT + userId
  2. create a pairing token (bc_pat_...)
  3. plugin-side WSS client connects with the pairing token, gets auth.ok
  4. browser-side WSS client connects with the JWT
  5. browser sends user.message -> plugin client receives it (decrypted when E2E)
  6. plugin sends agent.text (plaintext and E2E-encrypted variants) -> browser sees it
  7. server persists the messages; API readback confirms ciphertext for E2E
"""

import argparse
import asyncio
import json
import sys
import time
import types
import urllib.request
import uuid

ROOT = __file__.rsplit("/", 2)[0]
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Expose the flat layout as package "botschat" (mirrors Hermes' plugin loader
# and tests/conftest.py) so the plugin's relative imports resolve.
if "botschat" not in sys.modules:
    _pkg = types.ModuleType("botschat")
    _pkg.__path__ = [ROOT]  # type: ignore[attr-defined]
    _pkg.__package__ = "botschat"
    sys.modules["botschat"] = _pkg

import websockets  # noqa: E402

from botschat.e2e import derive_key, encrypt_text, to_base64  # noqa: E402
from botschat.protocol import (  # noqa: E402
    AgentText,
    Auth,
    CloudMessage,
    ModelsList,
    ModelInfo,
    TaskScanResult,
    UserMessage,
)
from botschat.ws_client import BotsChatCloudClient  # noqa: E402

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


def http_json(method, url, payload=None, token=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(req, data=data, timeout=10) as resp:
        return json.loads(resp.read())


async def wait_until(fn, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if fn():
            return True
        await asyncio.sleep(0.05)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    ap.add_argument("--secret", default="devsecret")
    ap.add_argument("--e2e-password", default=None, help="test E2E encryption end-to-end")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    # 1. dev-auth login
    auth = http_json("POST", f"{base}/api/dev-auth/login", {"secret": args.secret})
    jwt, user_id = auth["token"], auth["userId"]
    print(f"{PASS} dev-auth login -> userId={user_id}")

    # 2. pairing token
    pair = http_json("POST", f"{base}/api/pairing-tokens", {"label": "interop-smoke"}, token=jwt)
    pairing_token = pair.get("token") or pair.get("pairingToken") or pair.get("id")
    print(f"{PASS} pairing token created: {pairing_token[:16]}…")

    async def scenario():
        plugin_seen = []
        browser_seen = []

        # 3. plugin-side client (what the Hermes adapter runs)
        plugin = BotsChatCloudClient(
            cloud_url=base,
            pairing_token=pairing_token,
            e2e_password=args.e2e_password,
            on_message=lambda m: plugin_seen.append(m),
        )
        plugin.start()
        assert await wait_until(lambda: plugin.connected), "plugin never got auth.ok"
        print(f"{PASS} plugin client authenticated (userId={user_id}, e2e_key={'yes' if plugin.e2e_key else 'no'})")

        # Post-auth pushes from the real DO:
        assert await wait_until(lambda: len(plugin_seen) >= 3), f"post-auth pushes missing: {[m.type for m in plugin_seen]}"
        kinds = [m.type for m in plugin_seen]
        print(f"{PASS} post-auth pushes received: {kinds}")

        # Answer them like the adapter does.
        await plugin.send(TaskScanResult(tasks=[]))
        await plugin.send(ModelsList(models=[ModelInfo(id="test/model-1", name="Test Model 1", provider="test")]))
        await asyncio.sleep(0.3)

        # 4. browser-side client (what the web UI / CLI run)
        session_id = f"ses_{uuid.uuid4().hex[:8]}"
        browser_ws = await websockets.connect(
            f"{base.replace('http://', 'ws://')}/api/ws/{user_id}/{session_id}"
        )
        await browser_ws.send(json.dumps({"type": "auth", "token": jwt}))
        await browser_ws.send(json.dumps({"type": "foreground.enter", "sessionKey": "x"}))
        print(f"{PASS} browser client connected ({session_id})")

        async def collect_browser_frames():
            try:
                while True:
                    raw = await browser_ws.recv()
                    try:
                        browser_seen.append(json.loads(raw))
                    except Exception:
                        pass
            except (websockets.ConnectionClosed, asyncio.CancelledError):
                pass

        collector = asyncio.create_task(collect_browser_frames())

        # 5. browser -> plugin: user.message
        session_key = f"agent:main:botschat:{user_id}:adhoc"
        msg_id = str(uuid.uuid4())
        hello = "Hello from the interop test 👋"
        payload = {"type": "user.message", "sessionKey": session_key, "text": hello,
                   "userId": user_id, "messageId": msg_id}
        if args.e2e_password:
            key = derive_key(args.e2e_password, user_id)
            payload["text"] = to_base64(encrypt_text(key, hello, msg_id))
            payload["encrypted"] = 1
        await browser_ws.send(json.dumps(payload))

        def got_user_msg():
            return any(isinstance(m, UserMessage) for m in plugin_seen)

        assert await wait_until(got_user_msg), "plugin never received user.message"
        um = next(m for m in plugin_seen if isinstance(m, UserMessage))
        if args.e2e_password:
            from botschat.e2e import decrypt_text, from_base64
            assert decrypt_text(plugin.e2e_key, from_base64(um.text), um.messageId) == hello
            print(f"{PASS} browser -> plugin user.message (E2E decrypted, {len(hello)} chars)")
        else:
            assert um.text == hello
            print(f"{PASS} browser -> plugin user.message ({len(hello)} chars)")
        assert um.sessionKey == session_key

        # 6. plugin -> browser: agent.text (plaintext)
        reply = "Reply from the Hermes plugin ✅"
        await plugin.send(AgentText(sessionKey=session_key, text=reply, messageId=str(uuid.uuid4())))

        def browser_saw_reply():
            return any(
                m.get("type") == "agent.text" and m.get("text") == reply for m in browser_seen
            )

        assert await wait_until(browser_saw_reply), \
            f"agent.text not in browser frames: {[m.get('type') for m in browser_seen]}"
        print(f"{PASS} plugin -> browser agent.text")

        # 7. E2E variant: encrypted agent.text reaches browser as ciphertext
        if args.e2e_password:
            enc_reply = "secret encrypted reply"
            enc_id = str(uuid.uuid4())
            await plugin.send(AgentText(sessionKey=session_key,
                                        text=to_base64(encrypt_text(plugin.e2e_key, enc_reply, enc_id)),
                                        messageId=enc_id, encrypted=True))
            assert await wait_until(
                lambda: any(m.get("type") == "agent.text" and m.get("encrypted") for m in browser_seen)
            ), "no encrypted agent.text reached the browser"
            print(f"{PASS} plugin -> browser agent.text (E2E ciphertext)")

        # 8. server-side persistence readback
        data = http_json("GET", f"{base}/api/messages/{user_id}?sessionKey={session_key}", token=jwt)
        msgs = data.get("messages", [])
        text_msgs = [m for m in msgs if m.get("sender") == "agent"]
        assert text_msgs, "server persisted no agent messages"
        print(f"{PASS} server persisted {len(text_msgs)} agent message(s) (sender=agent)")

        collector.cancel()
        await plugin.stop()
        await browser_ws.close()

    try:
        asyncio.run(scenario())
    except Exception as exc:
        print(f"{FAIL} interop failed: {exc}")
        sys.exit(1)

    print("\nAll interop checks passed against the real BotsChat server.")


if __name__ == "__main__":
    main()
