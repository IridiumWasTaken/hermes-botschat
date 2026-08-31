"""BotsChat background-task bridge: task.* messages <-> Hermes cron jobs.

Mirrors packages/plugin/src/channel.ts's task handlers (lines ~1156-2159) but
maps onto Hermes' cron system:

- task.schedule  -> cronjob create/update (via ctx.dispatch_tool) + ack
- task.delete    -> cronjob remove
- task.scan.request -> cronjob list (in-process cron.jobs read) -> task.scan.result
- task.run       -> cronjob run (background) + job.update/job.output lifecycle

Job record reads use the in-process ``cron.jobs`` module (the gateway's own
store); writes go through the sanctioned ``cronjob`` tool dispatch.
"""

import asyncio
import json
import logging
import re
import time

from .protocol import (
    JobOutput,
    JobUpdate,
    TaskDelete,
    TaskInfo,
    TaskLastRun,
    TaskRun,
    TaskSchedule,
    TaskScheduleAck,
    TaskScanResult,
)

logger = logging.getLogger("botschat")

# BotsChat schedule forms -> Hermes cronjob schedule strings.
_AT_RE = re.compile(r"^at\s+(\d{1,2}):(\d{2})$", re.IGNORECASE)


def normalize_schedule(schedule: str) -> str:
    """Map a BotsChat schedule string to a Hermes cronjob schedule value.

    "every 30m"/"every 2h" pass through (Hermes accepts them natively);
    "at 09:00" becomes a cron expression; "cron <expr>" loses its prefix.
    """
    s = (schedule or "").strip()
    if not s:
        return "every 24h"
    if s.lower().startswith("cron "):
        return s[5:].strip()
    if s.lower().startswith("every "):
        return s
    m = _AT_RE.match(s)
    if m:
        return f"{int(m.group(2))} {int(m.group(1))} * * *"
    return s


def _extract_job_id(dispatch_json: str) -> str:
    """Pull the job id out of a cronjob tool JSON response, defensively."""
    try:
        data = json.loads(dispatch_json or "{}")
    except (ValueError, TypeError):
        raise ValueError(f"cronjob tool returned unparseable JSON: {dispatch_json!r}")
    for key in ("job_id", "id"):
        if isinstance(data, dict) and data.get(key):
            return str(data[key])
    job = data.get("job") if isinstance(data, dict) else None
    if isinstance(job, dict):
        for key in ("job_id", "id"):
            if job.get(key):
                return str(job[key])
    raise ValueError(f"cronjob tool response has no job id: {str(data)[:200]}")


class TaskBridge:
    """Maps BotsChat task management to Hermes cron jobs."""

    def __init__(self, dispatch, send, log=None):
        self.dispatch = dispatch  # callable(args: dict) -> str (cronjob tool JSON)
        self.send = send  # async callable(CloudMessage) -> None
        self.log = log or logger

    # ------------------------------------------------------------- reads

    def _load_jobs(self):
        try:
            from cron.jobs import load_jobs

            return load_jobs()
        except Exception as exc:
            self.log.warning(f"[botschat] cron.jobs.load_jobs failed: {exc}")
            return []

    def _find_job(self, cron_job_id: str):
        if not cron_job_id:
            return None
        try:
            from cron.jobs import get_job

            job = get_job(cron_job_id)
            if job:
                return job
        except Exception:
            pass
        for job in self._load_jobs():
            if job.get("id") == cron_job_id:
                return job
        return None

    def _to_task_info(self, job) -> TaskInfo:
        status = job.get("last_status")
        last_run_at = job.get("last_run_at")
        last_run = None
        if last_run_at:
            try:
                ts = int(last_run_at)
            except (TypeError, ValueError):
                ts = int(time.time())
            last_run = TaskLastRun(
                status=status or "ok",
                ts=ts,
                summary=job.get("last_fire_error") or None,
            )
        return TaskInfo(
            cronJobId=str(job.get("id") or ""),
            name=str(job.get("name") or job.get("id") or "cron job"),
            schedule=str(job.get("schedule_display") or "?"),
            agentId="hermes",
            enabled=bool(job.get("enabled", True)),
            instructions=str(job.get("prompt") or ""),
            model=job.get("model"),
            lastRun=last_run,
        )

    # ------------------------------------------------------------- handlers

    async def schedule(self, msg: TaskSchedule) -> TaskScheduleAck:
        """Create or update a Hermes cron job; returns the ack to send."""
        schedule = normalize_schedule(msg.schedule)
        existing = self._find_job(msg.cronJobId) if msg.cronJobId else None
        try:
            if existing:
                payload = {
                    "action": "update",
                    "job_id": existing["id"],
                    "prompt": msg.instructions,
                    "schedule": schedule,
                    "deliver": "local",
                }
                if msg.model:
                    payload["model"] = msg.model
                self.dispatch(payload)
                job_id = existing["id"]
                if msg.enabled:
                    self.dispatch({"action": "resume", "job_id": job_id})
                else:
                    self.dispatch({"action": "pause", "job_id": job_id})
            else:
                payload = {
                    "action": "create",
                    "name": msg.name or "BotsChat Task",
                    "prompt": msg.instructions,
                    "schedule": schedule,
                    "deliver": "local",
                }
                if msg.model:
                    payload["model"] = msg.model
                resp = self.dispatch(payload)
                job_id = _extract_job_id(resp)
                if not msg.enabled:
                    self.dispatch({"action": "pause", "job_id": job_id})
            self.log.info(f"[botschat] task.schedule -> cron job {job_id} ({schedule})")
            return TaskScheduleAck(cronJobId=job_id, taskId=msg.taskId, ok=True)
        except Exception as exc:
            self.log.error(f"[botschat] task.schedule failed: {exc}")
            return TaskScheduleAck(
                cronJobId=msg.cronJobId, taskId=msg.taskId, ok=False, error=str(exc)
            )

    async def delete(self, msg: TaskDelete) -> None:
        try:
            job = self._find_job(msg.cronJobId)
            if job is not None:
                self.dispatch({"action": "remove", "job_id": job["id"]})
                self.log.info(f"[botschat] task.delete -> removed cron job {job['id']}")
            else:
                self.log.warning(f"[botschat] task.delete: unknown job {msg.cronJobId}")
        except Exception as exc:
            self.log.error(f"[botschat] task.delete failed: {exc}")

    async def scan(self) -> TaskScanResult:
        jobs = self._load_jobs()
        return TaskScanResult(tasks=[self._to_task_info(j) for j in jobs])

    async def run(self, msg: TaskRun) -> None:
        """Trigger a job now; report running -> ok/error via job.update/output."""
        job = self._find_job(msg.cronJobId)
        if job is None:
            self.log.warning(f"[botschat] task.run: unknown job {msg.cronJobId}")
            return
        job_id = job["id"]
        run_suffix = f"run_{int(time.time() * 1000)}"
        out_job_id = f"{job_id}_{run_suffix}"
        session_key = f"agent:hermes:cron:{job_id}:{run_suffix}"
        started = int(time.time() * 1000)

        await self.send(
            JobUpdate(
                cronJobId=job_id,
                jobId=out_job_id,
                sessionKey=session_key,
                status="running",
                startedAt=started,
            )
        )
        try:
            self.dispatch({"action": "run", "job_id": job_id})
        except Exception as exc:
            self.log.error(f"[botschat] task.run dispatch failed: {exc}")
            await self.send(
                JobUpdate(
                    cronJobId=job_id,
                    jobId=out_job_id,
                    sessionKey=session_key,
                    status="error",
                    summary=str(exc),
                    startedAt=started,
                    finishedAt=int(time.time() * 1000),
                )
            )
            return

        # The cronjob tool returns immediately (background dispatch); poll the
        # job record until last_run_at advances past the pre-run value.
        pre_run_last = job.get("last_run_at")
        asyncio.create_task(
            self._poll_run(job_id, out_job_id, session_key, started, pre_run_last)
        )

    async def _poll_run(self, job_id, out_job_id, session_key, started, pre_run_last, timeout_s=600):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            await asyncio.sleep(4)
            job = self._find_job(job_id)
            if job is None:
                return
            status = job.get("last_status")
            last_run = job.get("last_run_at")
            if last_run != pre_run_last and status in ("ok", "error"):
                finished = int(time.time() * 1000)
                summary = job.get("last_fire_error") or f"Run finished ({status})"
                await self.send(
                    JobOutput(cronJobId=job_id, jobId=out_job_id, text=summary)
                )
                await self.send(
                    JobUpdate(
                        cronJobId=job_id,
                        jobId=out_job_id,
                        sessionKey=session_key,
                        status=status,
                        summary=summary,
                        startedAt=started,
                        finishedAt=finished,
                        durationMs=finished - started,
                    )
                )
                return
        # Timed out: the run may still be executing; leave the last "running"
        # update in place (the next task.scan.request reports the truth).
        self.log.warning(f"[botschat] poll for job {job_id} timed out after {timeout_s}s")
