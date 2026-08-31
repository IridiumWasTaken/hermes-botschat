#!/usr/bin/env python3
"""Configure BotsChat per-profile (private + botschat-test), clear shared env.

Reads the existing BOTSCHAT_* values from the default profile .env, writes
per-profile config.yaml `extra` blocks (env-free, multiplexer-safe), then
removes the BOTSCHAT_* vars from the shared .env (backed up first).
Secrets never printed.
"""
import json
import os
import shutil
import subprocess
import sys

ENV_PATH = "/Users/aaron/.hermes/.env"
HERMES = "/Users/aaron/.hermes/hermes-agent/venv/bin/hermes"


def parse_dotenv(path):
    vals = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.replace("export ", "").strip()
        v = v.strip().strip('"').strip("'")
        vals[k] = v
    return vals


def cfg_set(profile, key, value):
    # List-form subprocess: no shell involved, so quoting is a non-issue.
    r = subprocess.run(
        [HERMES, "-p", profile, "config", "set", key, value],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        print(f"FAILED {profile} {key}: {r.stderr.strip()[:200]}", file=sys.stderr)
        sys.exit(1)
    print(f"ok {profile}: {key}")


def main():
    env = parse_dotenv(ENV_PATH)
    cloud = env.get("BOTSCHAT_CLOUD_URL", "")
    old_token = env.get("BOTSCHAT_PAIRING_TOKEN", "")
    e2e = env.get("BOTSCHAT_E2E_PASSWORD", "")
    if not (cloud and old_token and e2e):
        print("missing BOTSCHAT_ values in env", file=sys.stderr)
        sys.exit(1)

    # --- private profile: new token (placeholder), distinct agent id, E2E on
    cfg_set("private", "gateway.platforms.botschat.enabled", "true")
    cfg_set("private", "gateway.platforms.botschat.extra.cloudUrl", cloud)
    cfg_set("private", "gateway.platforms.botschat.extra.pairingToken", "token_abc")
    cfg_set("private", "gateway.platforms.botschat.extra.agentId", "private")
    cfg_set("private", "gateway.platforms.botschat.extra.e2ePassword", e2e)

    # --- botschat-test profile: keep its token, move config out of env
    cfg_set("botschat-test", "gateway.platforms.botschat.extra.cloudUrl", cloud)
    cfg_set("botschat-test", "gateway.platforms.botschat.extra.pairingToken", old_token)
    cfg_set("botschat-test", "gateway.platforms.botschat.extra.e2ePassword", e2e)

    # --- strip BOTSCHAT_* from the shared env (backup first)
    shutil.copy2(ENV_PATH, ENV_PATH + ".bak")
    kept = []
    removed = []
    for line in open(ENV_PATH, encoding="utf-8"):
        key = line.strip().split("=", 1)[0].replace("export ", "").strip()
        if key.startswith("BOTSCHAT_"):
            removed.append(key)
        else:
            kept.append(line)
    with open(ENV_PATH, "w", encoding="utf-8") as fh:
        fh.writelines(kept)
    print("removed from shared env:", sorted(removed))
    print("backup: " + ENV_PATH + ".bak")
    print("DONE")


if __name__ == "__main__":
    main()
