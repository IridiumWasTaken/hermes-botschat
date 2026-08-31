"""Task-bridge tests: schedule normalization + job-record mapping.

The bridge's cron writes go through ``ctx.dispatch_tool``, which is not
available under pytest; these tests exercise the pure mapping logic and the
handlers against a fake dispatch/send.
"""

import pytest

from botschat.protocol import (
    JobOutput,
    JobUpdate,
    TaskDelete,
    TaskInfo,
    TaskRun,
    TaskSchedule,
    TaskScanResult,
    TaskScheduleAck,
)
from botschat.tasks import TaskBridge, _extract_job_id, normalize_schedule


class FakeBridge:
    """TaskBridge with recorded dispatch/send calls and scripted job store."""

    def __init__(self, jobs=None):
        self.jobs = jobs or []
        self.dispatched = []
        self.sent = []
        # What dispatch() returns for create (a canned job id).
        self.create_response = '{"success": true, "job": {"job_id": "new-job-1"}}'

    def load(self):
        return self.jobs

    def find(self, job_id):
        return next((j for j in self.jobs if j.get("id") == job_id), None)

    def dispatch(self, args):
        self.dispatched.append(args)
        if args.get("action") == "create":
            return self.create_response
        return '{"success": true}'


@pytest.fixture()
def bridge(monkeypatch):
    fake = FakeBridge()

    async def send(msg):
        fake.sent.append(msg)

    b = TaskBridge(dispatch=fake.dispatch, send=send)
    monkeypatch.setattr(b, "_load_jobs", fake.load)
    monkeypatch.setattr(b, "_find_job", fake.find)
    b._fake = fake
    return b


# ------------------------------------------------------------- normalize_schedule


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("every 30m", "every 30m"),
        ("every 2h", "every 2h"),
        ("every 10s", "every 10s"),
        ("at 09:00", "0 9 * * *"),
        ("at 23:05", "5 23 * * *"),
        ("cron 0 9 * * 1", "0 9 * * 1"),
        ("", "every 24h"),
        ("   ", "every 24h"),
        ("weird schedule", "weird schedule"),
    ],
)
def test_normalize_schedule(raw, expected):
    assert normalize_schedule(raw) == expected


# ------------------------------------------------------------- _extract_job_id


def test_extract_job_id_forms():
    assert _extract_job_id('{"success": true, "job": {"job_id": "abc123"}}') == "abc123"
    assert _extract_job_id('{"success": true, "job": {"id": "abc123"}}') == "abc123"
    assert _extract_job_id('{"job_id": "abc123"}') == "abc123"
    assert _extract_job_id('{"id": "abc123"}') == "abc123"


def test_extract_job_id_missing_raises():
    with pytest.raises(ValueError):
        _extract_job_id('{"success": false, "error": "nope"}')


# ------------------------------------------------------------- handlers


def test_schedule_creates_new_job(bridge):
    bridge._fake.jobs = []
    ack = None

    async def go():
        nonlocal ack
        ack = await bridge.schedule(
            TaskSchedule(cronJobId="", agentId="hermes", schedule="every 30m",
                         instructions="Summarize the news", enabled=True)
        )

    import asyncio
    asyncio.run(go())
    assert ack.ok is True
    assert bridge._fake.dispatched[0]["action"] == "create"
    assert bridge._fake.dispatched[0]["schedule"] == "every 30m"
    assert bridge._fake.dispatched[0]["prompt"] == "Summarize the news"


def test_schedule_updates_existing_job(bridge):
    bridge._fake.jobs = [{"id": "job-1", "name": "Daily", "enabled": True}]
    ack = None

    async def go():
        nonlocal ack
        ack = await bridge.schedule(
            TaskSchedule(cronJobId="job-1", agentId="hermes", schedule="at 09:00",
                         instructions="New prompt", enabled=False)
        )

    import asyncio
    asyncio.run(go())
    assert ack.ok is True
    assert ack.cronJobId == "job-1"
    assert bridge._fake.dispatched[0]["action"] == "update"
    assert bridge._fake.dispatched[0]["schedule"] == "0 9 * * *"
    # disabled -> paused
    assert bridge._fake.dispatched[-1] == {"action": "pause", "job_id": "job-1"}


def test_scan_maps_jobs(bridge):
    bridge._fake.jobs = [
        {
            "id": "job-1", "name": "Daily", "prompt": "Do the thing",
            "schedule_display": "every 30m", "enabled": True,
            "last_status": "ok", "last_run_at": 1788100000,
        }
    ]
    result = None

    async def go():
        nonlocal result
        result = await bridge.scan()

    import asyncio
    asyncio.run(go())
    assert isinstance(result, TaskScanResult)
    assert len(result.tasks) == 1
    t = result.tasks[0]
    assert t.cronJobId == "job-1"
    assert t.name == "Daily"
    assert t.schedule == "every 30m"
    assert t.instructions == "Do the thing"
    assert t.lastRun.status == "ok"
    assert t.lastRun.ts == 1788100000


def test_scan_empty(bridge):
    bridge._fake.jobs = []
    result = None

    async def go():
        nonlocal result
        result = await bridge.scan()

    import asyncio
    asyncio.run(go())
    assert result.tasks == []


def test_delete_removes_job(bridge):
    bridge._fake.jobs = [{"id": "job-1"}]

    async def go():
        await bridge.delete(TaskDelete(cronJobId="job-1"))

    import asyncio
    asyncio.run(go())
    assert bridge._fake.dispatched == [{"action": "remove", "job_id": "job-1"}]


def test_delete_unknown_job_noop(bridge):
    bridge._fake.jobs = []

    async def go():
        await bridge.delete(TaskDelete(cronJobId="missing"))

    import asyncio
    asyncio.run(go())
    assert bridge._fake.dispatched == []


def test_run_emits_running_then_error_on_bad_dispatch(bridge):
    bridge._fake.jobs = [{"id": "job-1", "enabled": True, "last_run_at": None}]

    def bad_dispatch(args):
        raise RuntimeError("dispatch boom")

    bridge.dispatch = bad_dispatch

    async def go():
        await bridge.run(TaskRun(cronJobId="job-1", agentId="hermes", instructions="x"))

    import asyncio
    asyncio.run(go())
    assert len(bridge._fake.sent) == 2
    first, second = bridge._fake.sent
    assert isinstance(first, JobUpdate) and first.status == "running"
    assert isinstance(second, JobUpdate) and second.status == "error"
    assert "dispatch boom" in second.summary
