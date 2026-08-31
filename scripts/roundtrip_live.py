"""Live round-trip: browser WSS -> BotsChat server -> Hermes gateway -> AIAgent -> back.

Requires: local BotsChat dev server (see scripts/interop_smoke.py header) AND a
Hermes gateway running with the botschat plugin enabled and connected.

    hermes -p botschat-test gateway run
    python scripts/roundtrip_live.py --url http://127.0.0.1:8787 --secret devsecret
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
if "botschat" not in sys.modules:
    _pkg = types.ModuleType("botschat")
    _pkg.__path__ = [ROOT]  # type: ignore[attr-defined]
    _pkg.__package__ = "botschat"
    sys.modules["botschat"] = _pkg

import websockets  # noqa: E402

from botschat.e2e import decrypt_text, derive_key, encrypt_text, from_base64, to_base64  # noqa: E402

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


def http_json(method, url, payload=None, token=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(req, data=data, timeout=15) as resp:
        return json.loads(resp.read())


async def wait_until(fn, timeout):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if fn():
            return True
        await asyncio.sleep(0.25)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    ap.add_argument("--secret", default="devsecret")
    ap.add_argument("--prompt", default="Reply with exactly one short sentence confirming you are a Hermes agent.")
    ap.add_argument("--timeout", type=float, default=150.0)
    ap.add_argument("--e2e-password", default=None,
                    help="encrypt outbound / decrypt inbound with this E2E password (must match the plugin's)")
    ap.add_argument("--session-key", default=None,
                    help="BotsChat sessionKey to talk into (default: agent:main:botschat:<userId>:adhoc). "
                         "Use a fresh one to get a new Hermes session with the current system prompt.")
    ap.add_argument("--command", default=None,
                    help="send a user.command frame instead of user.message (e.g. --command model --args deepseek-v4-flash)")
    ap.add_argument("--args", default=None, help="args for --command")
    ap.add_argument("--user-id", default=None,
                    help="dev-auth login as this userId instead of the default dev-test-user")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    auth = http_json("POST", f"{base}/api/dev-auth/login",
                     {"secret": args.secret, **({"userId": args.user_id} if args.user_id else {})})
    jwt, user_id = auth["token"], auth["userId"]
    session_key = args.session_key or f"agent:main:botschat:{user_id}:adhoc"
    e2e_key = derive_key(args.e2e_password, user_id) if args.e2e_password else None

    async def scenario():
        session_id = f"ses_{uuid.uuid4().hex[:8]}"
        frames = []

        async def collect():
            try:
                while True:
                    raw = await browser_ws.recv()
                    try:
                        frames.append(json.loads(raw))
                    except Exception:
                        pass
            except (websockets.ConnectionClosed, asyncio.CancelledError):
                pass

        browser_ws = await websockets.connect(f"{base.replace('http://', 'ws://')}/api/ws/{user_id}/{session_id}")
        await browser_ws.send(json.dumps({"type": "auth", "token": jwt}))
        await browser_ws.send(json.dumps({"type": "foreground.enter", "sessionKey": session_key}))
        collector = asyncio.create_task(collect())
        await asyncio.sleep(0.5)

        msg_id = str(uuid.uuid4())
        if args.command:
            payload = {"type": "user.command", "sessionKey": session_key,
                       "command": args.command, "args": args.args}
            print(f"{PASS} sent command /{args.command} {args.args or ''}")
        else:
            payload = {
                "type": "user.message", "sessionKey": session_key,
                "text": args.prompt, "userId": user_id, "messageId": msg_id,
            }
            if e2e_key:
                payload["text"] = to_base64(encrypt_text(e2e_key, args.prompt, msg_id))
                payload["encrypted"] = 1
                print(f"{PASS} sent to Hermes agent (E2E-encrypted, {len(args.prompt)} chars): {args.prompt!r}")
            else:
                print(f"{PASS} sent to Hermes agent: {args.prompt!r}")
        await browser_ws.send(json.dumps(payload))
        t0 = time.time()

        def done():
            return any(m.get("type") == "agent.text" and m.get("sessionKey") == session_key for m in frames)

        ok = await wait_until(done, args.timeout)
        elapsed = time.time() - t0
        if not ok:
            print(f"{FAIL} no agent.text after {elapsed:.0f}s. Frames: {[m.get('type') for m in frames]}")
            sys.exit(1)
        reply_msg = next(m for m in frames if m.get("type") == "agent.text" and m.get("sessionKey") == session_key)
        if e2e_key:
            assert reply_msg.get("encrypted"), "agent reply was not E2E-encrypted"
            reply = decrypt_text(e2e_key, from_base64(reply_msg["text"]), reply_msg["messageId"])
            print(f"{PASS} agent replied in {elapsed:.1f}s (E2E-decrypted, {len(reply)} chars):")
        else:
            reply = reply_msg["text"]
            print(f"{PASS} agent replied in {elapsed:.1f}s ({len(reply)} chars):")
        print(f"\n    {reply}\n")

        # At-rest verification: the server must hold ciphertext + the encrypted flag.
        if e2e_key:
            data = http_json("GET", f"{base}/api/messages/{user_id}?sessionKey={session_key}", token=jwt)
            stored = data.get("messages", [])
            user_rows = [m for m in stored if m.get("sender") == "user"]
            agent_rows = [m for m in stored if m.get("sender") == "agent"]
            latest_agent = agent_rows[-1]
            if user_rows:
                latest_user = user_rows[-1]
                assert latest_user.get("encrypted", 0) & 1, "stored user message lacks encrypted flag"
                assert latest_user.get("text") != args.prompt, "user plaintext leaked to D1"
            assert latest_agent.get("encrypted", 0) & 1, "stored agent reply lacks encrypted flag"
            assert latest_agent.get("text") != reply, "agent plaintext leaked to D1"
            assert from_base64(latest_agent["text"]) is not None
            print(f"{PASS} at-rest: D1 holds ciphertext only (agent rows, encrypted bitmask set)")

        # Streaming/activity observation (informational):
        streamed = [m for m in frames if m.get("type") in ("agent.stream.start", "agent.stream.chunk", "agent.stream.end")]
        activities = [m for m in frames if m.get("type") == "agent.activity"]
        if streamed:
            print(f"{PASS} streaming frames observed: {len(streamed)}")
        if activities:
            print(f"{PASS} activity frames observed: {len(activities)} ({', '.join(a.get('kind','') for a in activities)})")

        collector.cancel()
        await browser_ws.close()

    try:
        asyncio.run(scenario())
    except Exception as exc:
        print(f"{FAIL} round-trip failed: {exc}")
        sys.exit(1)
    print("\nLive round-trip OK: web-protocol user.message -> Hermes agent reply.")


if __name__ == "__main__":
    main()
