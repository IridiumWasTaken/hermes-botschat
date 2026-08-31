"""Wire-protocol fidelity tests.

For every message type in types.ts: an instance with ALL fields set must
serialize to exactly the types.ts field set (camelCase), and a
to_dict -> from_dict round-trip must reconstruct an equal object. Nested
shapes (TaskInfo/ModelInfo/AgentInfo) must coerce back into dataclasses.
"""

import pytest

from botschat.protocol import (  # noqa: E402
    AgentA2ui,
    AgentActivity,
    AgentInfo,
    AgentMedia,
    AgentStreamChunk,
    AgentStreamEnd,
    AgentStreamStart,
    AgentText,
    Auth,
    AuthFail,
    AuthOk,
    CloudMessage,
    ConfigRequest,
    DefaultModelUpdated,
    JobOutput,
    JobUpdate,
    ModelChanged,
    ModelInfo,
    ModelsList,
    ModelsRequest,
    Ping,
    Pong,
    SettingsDefaultModel,
    SettingsNotifyPreview,
    Status,
    TaskDelete,
    TaskInfo,
    TaskLastRun,
    TaskRun,
    TaskScanRequest,
    TaskScanResult,
    TaskSchedule,
    TaskScheduleAck,
    UserAction,
    UserCommand,
    UserMedia,
    UserMessage,
)

# (class, kwargs-with-every-field-set, expected sorted wire keys)
ALL_FIELDS = [
    # --- inbound ---
    (AuthOk, dict(userId="u_1", agentId="a", availableAgents=[AgentInfo(id="x", name="n", type="t", role="r", status="ok")]),
     ["availableAgents", "agentId", "type", "userId"]),
    (AuthFail, dict(reason="bad"), ["reason", "type"]),
    (UserMessage, dict(sessionKey="sk", text="hi", userId="u", messageId="m",
                       targetAgentId="a", mediaUrl="/api/media/x.png",
                       parentMessageId="pm", parentText="pt", parentSender="user",
                       parentEncrypted=1, encrypted=1),
     ["encrypted", "mediaUrl", "messageId", "parentEncrypted", "parentMessageId",
      "parentSender", "parentText", "sessionKey", "targetAgentId", "text", "type", "userId"]),
    (UserMedia, dict(sessionKey="sk", mediaUrl="/m.png", userId="u"),
     ["mediaUrl", "sessionKey", "type", "userId"]),
    (UserAction, dict(sessionKey="sk", action="click", params={"kind": "buttons", "value": "x"}),
     ["action", "params", "sessionKey", "type"]),
    (UserCommand, dict(sessionKey="sk", command="model", args="claude-opus-4-6"),
     ["args", "command", "sessionKey", "type"]),
    (ConfigRequest, dict(method="get", params={"k": "v"}), ["method", "params", "type"]),
    (Ping, dict(), ["type"]),
    (TaskSchedule, dict(taskId="t", name="Daily", cronJobId="cj", agentId="a",
                        schedule="every 30m", instructions="do it", enabled=False, model="m"),
     ["agentId", "cronJobId", "enabled", "instructions", "model", "name", "schedule", "taskId", "type"]),
    (TaskDelete, dict(cronJobId="cj"), ["cronJobId", "type"]),
    (TaskRun, dict(cronJobId="cj", agentId="a", instructions="run", model="m"),
     ["agentId", "cronJobId", "instructions", "model", "type"]),
    (TaskScanRequest, dict(), ["type"]),
    (ModelsRequest, dict(), ["type"]),
    (SettingsDefaultModel, dict(defaultModel="m"), ["defaultModel", "type"]),
    (SettingsNotifyPreview, dict(enabled=True), ["enabled", "type"]),
    # --- outbound ---
    (Auth, dict(token="bc_pat_x", agentId="a", agentType="hermes", agents=["hermes"], model="m"),
     ["agentId", "agentType", "agents", "model", "token", "type"]),
    (Status, dict(connected=True, agents=["hermes"], model="m"),
     ["agents", "connected", "model", "type"]),
    (Pong, dict(), ["type"]),
    (AgentText, dict(agentId="a", sessionKey="sk", text="hi", replyToId="r", threadId="th",
                     encrypted=True, messageId="m", notifyPreview="hi"),
     ["agentId", "encrypted", "messageId", "notifyPreview", "replyToId", "sessionKey",
      "text", "threadId", "type"]),
    (AgentMedia, dict(sessionKey="sk", mediaUrl="https://x/y.png", caption="c", replyToId="r",
                      threadId="th", encrypted=True, mediaEncrypted=True, messageId="m", notifyPreview="c"),
     ["caption", "encrypted", "mediaEncrypted", "mediaUrl", "messageId", "notifyPreview",
      "replyToId", "sessionKey", "threadId", "type"]),
    (AgentStreamStart, dict(sessionKey="sk", runId="r"), ["runId", "sessionKey", "type"]),
    (AgentStreamChunk, dict(sessionKey="sk", runId="r", text="t", encrypted=True, chunkId="c"),
     ["chunkId", "encrypted", "runId", "sessionKey", "text", "type"]),
    (AgentStreamEnd, dict(sessionKey="sk", runId="r"), ["runId", "sessionKey", "type"]),
    (AgentActivity, dict(sessionKey="sk", runId="r", kind="tool_start", text="t", toolName="tt",
                         durationMs=12, encrypted=True, activityId="a"),
     ["activityId", "durationMs", "encrypted", "kind", "runId", "sessionKey", "text", "toolName", "type"]),
    (AgentA2ui, dict(sessionKey="sk", jsonl="{}", replyToId="r", threadId="th", encrypted=True),
     ["encrypted", "jsonl", "replyToId", "sessionKey", "threadId", "type"]),
    (TaskScanResult, dict(tasks=[TaskInfo(cronJobId="cj", name="n", schedule="s", agentId="a",
                                          enabled=True, instructions="i", model="m",
                                          lastRun=TaskLastRun(status="ok", ts=1, summary="s"))]),
     ["tasks", "type"]),
    (TaskScheduleAck, dict(cronJobId="cj", taskId="t", ok=True, error=None),
     ["cronJobId", "ok", "taskId", "type"]),
    (JobUpdate, dict(cronJobId="cj", jobId="j", sessionKey="sk", status="ok", summary="s",
                     startedAt=1, finishedAt=2, durationMs=3, encrypted=True),
     ["cronJobId", "durationMs", "encrypted", "finishedAt", "jobId", "sessionKey",
      "startedAt", "status", "summary", "type"]),
    (JobOutput, dict(cronJobId="cj", jobId="j", text="out"), ["cronJobId", "jobId", "text", "type"]),
    (ModelsList, dict(models=[ModelInfo(id="m", name="M", provider="p")]), ["models", "type"]),
    (ModelChanged, dict(model="m", sessionKey="sk"), ["model", "sessionKey", "type"]),
    (DefaultModelUpdated, dict(model="m"), ["model", "type"]),
]


@pytest.mark.parametrize("cls,kwargs,expected", ALL_FIELDS, ids=[c.__name__ for c, _, _ in ALL_FIELDS])
def test_exact_wire_keys(cls, kwargs, expected):
    """Serialized keys are EXACTLY the types.ts field set (camelCase)."""
    msg = cls(**kwargs)
    assert sorted(msg.to_dict().keys()) == sorted(expected)


@pytest.mark.parametrize("cls,kwargs,_", ALL_FIELDS, ids=[c.__name__ for c, _, _ in ALL_FIELDS])
def test_dict_roundtrip(cls, kwargs, _):
    msg = cls(**kwargs)
    revived = CloudMessage.from_dict(msg.to_dict())
    assert revived == msg
    assert type(revived) is cls


@pytest.mark.parametrize("cls,kwargs,_", ALL_FIELDS, ids=[c.__name__ for c, _, _ in ALL_FIELDS])
def test_json_roundtrip(cls, kwargs, _):
    msg = cls(**kwargs)
    revived = CloudMessage.from_json(msg.to_json())
    assert revived == msg


def test_none_fields_omitted():
    """Optional fields set to None must not appear on the wire."""
    msg = AgentText(sessionKey="sk", text="hi", messageId="m1")
    assert msg.to_dict() == {"type": "agent.text", "sessionKey": "sk", "text": "hi", "messageId": "m1"}


def test_canonical_json():
    msg = AgentText(sessionKey="agent:x", text="Hi", messageId="m1")
    assert msg.to_json() == '{"type":"agent.text","sessionKey":"agent:x","text":"Hi","messageId":"m1"}'


def test_nested_coercion():
    """Nested shapes come back as dataclasses, not raw dicts."""
    msg = TaskScanResult(tasks=[TaskInfo(cronJobId="cj", name="n", schedule="every 1h", agentId="a",
                                         enabled=True, instructions="i")])
    revived = CloudMessage.from_dict(msg.to_dict())
    task = revived.tasks[0]
    assert isinstance(task, TaskInfo)
    assert task.cronJobId == "cj"
    assert task.schedule == "every 1h"


def test_nested_lastrun_coercion():
    msg = TaskScanResult(tasks=[TaskInfo(cronJobId="cj", name="n", schedule="s", agentId="a",
                                         enabled=True, instructions="i",
                                         lastRun=TaskLastRun(status="ok", ts=42, summary="done"))])
    revived = CloudMessage.from_dict(msg.to_dict())
    assert revived.tasks[0].lastRun == TaskLastRun(status="ok", ts=42, summary="done")


def test_user_message_realistic_frame():
    """A real ConnectionDO user.message frame (threaded, E2E) parses fully."""
    wire = {
        "type": "user.message",
        "sessionKey": "agent:main:botschat:u_1:ses:ses_9",
        "text": "aGVsbG8=",
        "userId": "u_1",
        "messageId": "11111111-2222-3333-4444-555555555555",
        "encrypted": 1,
        "parentMessageId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "parentText": "cGFyZW50",
        "parentSender": "user",
        "parentEncrypted": 1,
        "threadId": None,
        "mediaUrl": None,
        "targetAgentId": None,
    }
    msg = CloudMessage.from_dict(wire)
    assert isinstance(msg, UserMessage)
    assert msg.sessionKey == "agent:main:botschat:u_1:ses:ses_9"
    assert msg.encrypted == 1
    assert msg.parentEncrypted == 1
    assert msg.mediaUrl is None


def test_unknown_type_raises():
    with pytest.raises(KeyError):
        CloudMessage.from_dict({"type": "does.not.exist"})


def test_empty_scan_result_serializes():
    """task.scan.result with no jobs must emit tasks: [] (what the DO expects)."""
    assert TaskScanResult().to_dict() == {"type": "task.scan.result", "tasks": []}
