"""Live M4 test: drive the full BotsChat background-task flow via REST.

The web UI's task CRUD goes through these exact endpoints; the API worker
pushes task.schedule / task.run / task.delete frames to the plugin over the
ConnectionDO, and the plugin mirrors them onto Hermes cron jobs.

Requires the local BotsChat dev server + the test gateway running with the
botschat plugin enabled (see scripts/roundtrip_live.py header).

    python scripts/tasks_live.py --url http://127.0.0.1:8787 --secret devsecret
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request

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


def hermes_cron_list(profile):
    out = subprocess.run(
        ["hermes", "-p", profile, "cron", "list"],
        capture_output=True, text=True, timeout=30,
    )
    return out.stdout


def wait_until(fn, timeout=30.0, interval=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    ap.add_argument("--secret", default="devsecret")
    ap.add_argument("--profile", default="botschat-test")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    auth = http_json("POST", f"{base}/api/dev-auth/login", {"secret": args.secret})
    jwt = auth["token"]

    # 1. channel (workspace per agent)
    ch = http_json("POST", f"{base}/api/channels", {"name": "M4 Hermes Channel"}, token=jwt)
    channel_id = ch.get("id") or ch.get("channel", {}).get("id")
    assert channel_id, f"no channel id in {ch}"
    print(f"{PASS} created channel {channel_id}")

    # 2. background task -> task.schedule -> plugin creates a Hermes cron job
    task = http_json(
        "POST", f"{base}/api/channels/{channel_id}/tasks",
        {"name": "M4 Test Task", "kind": "background", "schedule": "every 1m",
         "instructions": "Reply with the current date and time in one short sentence."},
        token=jwt,
    )
    task_id = task.get("id") or task.get("task", {}).get("id")
    assert task_id, f"no task id in {task}"
    print(f"{PASS} created background task {task_id} (schedule 'every 1m')")

    # 3. wait for the plugin's task.schedule.ack to land in D1. The server's
    # task rows expose the write-back under the legacy schema column name
    # "openclawCronJobId" (server-side D1 schema, not plugin code) — accept
    # both spellings the API has used.
    def cron_id_set():
        try:
            tasks = http_json("GET", f"{base}/api/channels/{channel_id}/tasks", token=jwt)
            rows = tasks if isinstance(tasks, list) else tasks.get("tasks", [])
            return any((t.get("openclawCronJobId") or t.get("openclaw_cron_job_id")) for t in rows)
        except Exception:
            return False

    assert wait_until(cron_id_set, timeout=30), "task.schedule.ack never reached D1"
    print(f"{PASS} task.schedule.ack persisted (Hermes cron job id stored in D1)")
    print("--- hermes cron list ---")
    print(hermes_cron_list(args.profile)[:600])

    # 4. task.run -> job.update running -> ok (poller)
    http_json("POST", f"{base}/api/channels/{channel_id}/tasks/{task_id}/run", {}, token=jwt)
    print(f"{PASS} triggered task.run")

    def job_finished():
        try:
            jobs = http_json("GET", f"{base}/api/channels/{channel_id}/tasks/{task_id}/jobs", token=jwt)
            rows = jobs if isinstance(jobs, list) else jobs.get("jobs", [])
            return any(r.get("status") in ("ok", "error") for r in rows)
        except Exception:
            return False

    if wait_until(job_finished, timeout=120, interval=3):
        jobs = http_json("GET", f"{base}/api/channels/{channel_id}/tasks/{task_id}/jobs", token=jwt)
        rows = jobs if isinstance(jobs, list) else jobs.get("jobs", [])
        latest = rows[-1]
        print(f"{PASS} job finished: status={latest.get('status')} summary={str(latest.get('summary'))[:80]!r}")
    else:
        print(f"{FAIL} job never reached a terminal status")

    # 5. scheduled run (every 1m): wait for the cron ticker + on_session_end -> job.update
    print("waiting for one scheduled run (up to 90s)...")
    before = len(rows) if job_finished else 0

    def scheduled_run_appeared():
        try:
            jobs = http_json("GET", f"{base}/api/channels/{channel_id}/tasks/{task_id}/jobs", token=jwt)
            rows = jobs if isinstance(jobs, list) else jobs.get("jobs", [])
            return len(rows) > before
        except Exception:
            return False

    if wait_until(scheduled_run_appeared, timeout=90, interval=5):
        print(f"{PASS} scheduled run reported as a job row (on_session_end -> job.update)")
    else:
        print(f"{FAIL} no scheduled run row appeared (ticker/hook check)")

    # 6. task.delete -> plugin removes the Hermes cron job
    http_json("DELETE", f"{base}/api/channels/{channel_id}/tasks/{task_id}", token=jwt)
    time.sleep(3)
    listing = hermes_cron_list(args.profile)
    gone = "M4 Test Task" not in listing
    print(f"{PASS} task.delete removed the cron job" if gone else f"{FAIL} cron job still present")
    print("--- hermes cron list after delete ---")
    print(listing[:400])


if __name__ == "__main__":
    main()
