"""BotsChat wire protocol — faithful port of packages/plugin/src/types.ts.

The envelope is a FLAT JSON object; the `type` string field is the
discriminator. There is no {type, payload} wrapper. Correlation is done with
sessionKey (conversation id) + messageId (per-message UUID, also the E2E nonce
context); runId correlates streaming events.

Field names use the EXACT camelCase of types.ts: Python attributes are named
like the JSON keys so there is no mapping layer to drift. `to_dict()` drops
None fields (optionals are omitted on the wire, like the TS object literals).
`from_dict()` ignores unknown keys (forward-compatible).

Inbound = Cloud -> Plugin (the messages a Hermes adapter must handle).
Outbound = Plugin -> Cloud (the messages a Hermes adapter may send).
"""

import json
import typing
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any, ClassVar, Optional


# ---------------------------------------------------------------------------
# Nested shapes
# ---------------------------------------------------------------------------


@dataclass
class AgentInfo:
    """One entry of auth.ok.availableAgents."""

    id: str
    name: str
    type: str
    role: str
    capabilities: list = field(default_factory=list)
    status: str = ""


@dataclass
class ModelInfo:
    """One entry of models.list.models."""

    id: str
    name: str
    provider: str


@dataclass
class TaskLastRun:
    """One entry of TaskInfo.lastRun."""

    status: str
    ts: int
    summary: Optional[str] = None


@dataclass
class TaskInfo:
    """One entry of task.scan.result.tasks."""

    cronJobId: str
    name: str
    schedule: str
    agentId: str
    enabled: bool
    instructions: str
    model: Optional[str] = None
    lastRun: Optional[TaskLastRun] = None
    encrypted: Optional[bool] = None
    iv: Optional[str] = None


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


@dataclass
class CloudMessage:
    """Base for every wire message. Subclasses declare `type` with a default."""

    type: str

    _TYPES: ClassVar[dict] = {}

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "CloudMessage":
        """Dispatch on the `type` discriminator; unknown types raise KeyError."""
        wire_type = d.get("type")
        sub = cls._TYPES.get(wire_type)
        if sub is None:
            raise KeyError(f"Unknown BotsChat message type: {wire_type!r}")
        return _from_dict(sub, d)

    @classmethod
    def from_json(cls, raw: str) -> "CloudMessage":
        return cls.from_dict(json.loads(raw))


def _from_dict(cls, d: dict):
    """Build a dataclass from a dict, coercing nested shapes recursively."""
    kwargs = {}
    for f in fields(cls):
        if f.name in d:
            kwargs[f.name] = _coerce(f.type, d[f.name])
    return cls(**kwargs)


def _coerce(field_type, value):
    """Build nested dataclasses from dicts, honoring Optional[X] and list[X]."""
    origin = typing.get_origin(field_type)
    args = typing.get_args(field_type)
    if origin is typing.Union:  # Optional[X] and friends
        inner = next((a for a in args if a is not type(None)), None)
        if inner is not None and value is not None:
            return _coerce(inner, value)
        return value
    if origin in (list, typing.List):
        (inner,) = args
        if inner is not type(None) and is_dataclass(inner):
            return [_coerce(inner, v) for v in value]
        return value
    if is_dataclass(field_type) and isinstance(value, dict):
        return _from_dict(field_type, value)
    return value


def _register(*subclasses):
    for sub in subclasses:
        CloudMessage._TYPES[sub.type] = sub


# ---------------------------------------------------------------------------
# Inbound — Cloud -> Plugin
# ---------------------------------------------------------------------------


@dataclass
class AuthOk(CloudMessage):
    type: str = "auth.ok"
    userId: Optional[str] = None
    agentId: Optional[str] = None
    availableAgents: Optional[list[AgentInfo]] = None


@dataclass
class AuthFail(CloudMessage):
    type: str = "auth.fail"
    reason: str = ""


@dataclass
class UserMessage(CloudMessage):
    type: str = "user.message"
    sessionKey: str = ""
    text: str = ""
    userId: str = ""
    messageId: str = ""
    targetAgentId: Optional[str] = None
    mediaUrl: Optional[str] = None
    # Parent-message fields — attached by ConnectionDO for thread messages.
    parentMessageId: Optional[str] = None
    parentText: Optional[str] = None
    parentSender: Optional[str] = None
    parentEncrypted: Optional[int] = None  # 0 = plaintext, 1 = encrypted
    # Sent by the browser in practice (types.ts casts to `any`): 0/1.
    encrypted: Optional[int] = None


@dataclass
class UserMedia(CloudMessage):
    type: str = "user.media"
    sessionKey: str = ""
    mediaUrl: str = ""
    userId: str = ""


@dataclass
class UserAction(CloudMessage):
    type: str = "user.action"
    sessionKey: str = ""
    action: str = ""
    params: dict = field(default_factory=dict)


@dataclass
class UserCommand(CloudMessage):
    type: str = "user.command"
    sessionKey: str = ""
    command: str = ""
    args: Optional[str] = None


@dataclass
class ConfigRequest(CloudMessage):
    type: str = "config.request"
    method: str = ""
    params: Any = None


@dataclass
class Ping(CloudMessage):
    type: str = "ping"


@dataclass
class TaskSchedule(CloudMessage):
    type: str = "task.schedule"
    taskId: Optional[str] = None
    name: Optional[str] = None
    cronJobId: str = ""
    agentId: str = ""
    schedule: str = ""
    instructions: str = ""
    enabled: bool = True
    model: Optional[str] = None


@dataclass
class TaskDelete(CloudMessage):
    type: str = "task.delete"
    cronJobId: str = ""


@dataclass
class TaskRun(CloudMessage):
    type: str = "task.run"
    cronJobId: str = ""
    agentId: str = ""
    instructions: str = ""
    model: Optional[str] = None


@dataclass
class TaskScanRequest(CloudMessage):
    type: str = "task.scan.request"


@dataclass
class ModelsRequest(CloudMessage):
    type: str = "models.request"


@dataclass
class SettingsDefaultModel(CloudMessage):
    type: str = "settings.defaultModel"
    defaultModel: str = ""


@dataclass
class SettingsNotifyPreview(CloudMessage):
    type: str = "settings.notifyPreview"
    enabled: bool = False


# ---------------------------------------------------------------------------
# Outbound — Plugin -> Cloud
# ---------------------------------------------------------------------------


@dataclass
class Auth(CloudMessage):
    type: str = "auth"
    token: str = ""
    agentId: Optional[str] = None
    agentType: Optional[str] = None
    agents: Optional[list] = None
    model: Optional[str] = None


@dataclass
class Status(CloudMessage):
    type: str = "status"
    connected: bool = True
    agents: list = field(default_factory=list)
    model: Optional[str] = None


@dataclass
class Pong(CloudMessage):
    type: str = "pong"


@dataclass
class AgentText(CloudMessage):
    type: str = "agent.text"
    agentId: Optional[str] = None
    sessionKey: str = ""
    text: str = ""
    replyToId: Optional[str] = None
    threadId: Optional[str] = None
    encrypted: Optional[bool] = None
    messageId: Optional[str] = None
    notifyPreview: Optional[str] = None


@dataclass
class AgentMedia(CloudMessage):
    type: str = "agent.media"
    sessionKey: str = ""
    mediaUrl: str = ""
    caption: Optional[str] = None
    replyToId: Optional[str] = None
    threadId: Optional[str] = None
    encrypted: Optional[bool] = None
    mediaEncrypted: Optional[bool] = None
    messageId: Optional[str] = None
    notifyPreview: Optional[str] = None


@dataclass
class AgentStreamStart(CloudMessage):
    type: str = "agent.stream.start"
    sessionKey: str = ""
    runId: str = ""


@dataclass
class AgentStreamChunk(CloudMessage):
    type: str = "agent.stream.chunk"
    sessionKey: str = ""
    runId: str = ""
    text: str = ""
    encrypted: Optional[bool] = None
    chunkId: Optional[str] = None


@dataclass
class AgentStreamEnd(CloudMessage):
    type: str = "agent.stream.end"
    sessionKey: str = ""
    runId: str = ""


@dataclass
class AgentActivity(CloudMessage):
    type: str = "agent.activity"
    sessionKey: str = ""
    runId: str = ""
    kind: str = ""  # "reasoning" | "tool_start" | "tool_end"
    text: Optional[str] = None
    toolName: Optional[str] = None
    durationMs: Optional[int] = None
    encrypted: Optional[bool] = None
    activityId: Optional[str] = None


@dataclass
class AgentA2ui(CloudMessage):
    type: str = "agent.a2ui"
    sessionKey: str = ""
    jsonl: str = ""
    replyToId: Optional[str] = None
    threadId: Optional[str] = None
    encrypted: Optional[bool] = None


@dataclass
class TaskScanResult(CloudMessage):
    type: str = "task.scan.result"
    tasks: list[TaskInfo] = field(default_factory=list)


@dataclass
class TaskScheduleAck(CloudMessage):
    type: str = "task.schedule.ack"
    cronJobId: str = ""
    taskId: Optional[str] = None
    ok: bool = True
    error: Optional[str] = None


@dataclass
class JobUpdate(CloudMessage):
    type: str = "job.update"
    cronJobId: str = ""
    jobId: str = ""
    sessionKey: str = ""
    status: str = ""  # "running" | "ok" | "error" | "skipped"
    summary: Optional[str] = None
    startedAt: int = 0
    finishedAt: Optional[int] = None
    durationMs: Optional[int] = None
    encrypted: Optional[bool] = None


@dataclass
class JobOutput(CloudMessage):
    type: str = "job.output"
    cronJobId: str = ""
    jobId: str = ""
    text: str = ""


@dataclass
class ModelsList(CloudMessage):
    type: str = "models.list"
    models: list[ModelInfo] = field(default_factory=list)


@dataclass
class ModelChanged(CloudMessage):
    type: str = "model.changed"
    model: str = ""
    sessionKey: str = ""


@dataclass
class DefaultModelUpdated(CloudMessage):
    type: str = "defaultModel.updated"
    model: str = ""


# Populate the dispatch registry.
_register(
    # inbound
    AuthOk,
    AuthFail,
    UserMessage,
    UserMedia,
    UserAction,
    UserCommand,
    ConfigRequest,
    Ping,
    TaskSchedule,
    TaskDelete,
    TaskRun,
    TaskScanRequest,
    ModelsRequest,
    SettingsDefaultModel,
    SettingsNotifyPreview,
    # outbound
    Auth,
    Status,
    Pong,
    AgentText,
    AgentMedia,
    AgentStreamStart,
    AgentStreamChunk,
    AgentStreamEnd,
    AgentActivity,
    AgentA2ui,
    TaskScanResult,
    TaskScheduleAck,
    JobUpdate,
    JobOutput,
    ModelsList,
    ModelChanged,
    DefaultModelUpdated,
)
