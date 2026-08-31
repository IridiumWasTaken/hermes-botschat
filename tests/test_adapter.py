"""Registration-surface tests: register() wires the platform + A2UI section.

Importing ``botschat.adapter`` pulls in the real gateway modules (conftest adds
the Hermes source tree to sys.path), so these also catch import-time breakage
against the installed Hermes version.
"""

import asyncio

import pytest

from botschat import adapter

from gateway.config import PlatformConfig


@pytest.fixture(scope="module", autouse=True)
def _platform_member():
    """Make ``Platform("botschat")`` resolvable in tests.

    At runtime the plugin registry pre-registers the name so the enum's
    ``_missing_`` creates a pseudo-member; under pytest that never happens.
    Inject the same pseudo-member directly (``_missing_`` consults
    ``_value2member_map_`` first, so this is exactly what it would produce).
    """
    from gateway.config import Platform

    if "botschat" not in Platform._value2member_map_:
        pseudo = object.__new__(Platform)
        pseudo._value_ = "botschat"
        pseudo._name_ = "BOTSCHAT"
        Platform._value2member_map_["botschat"] = pseudo
        Platform._member_map_["BOTSCHAT"] = pseudo
    return Platform("botschat")


class StubCtx:
    """Captures register_platform / register_system_prompt_section calls."""

    def __init__(self):
        self.platform_kwargs = None
        self.sections = []

    def register_platform(self, **kwargs):
        self.platform_kwargs = kwargs

    def register_system_prompt_section(self, section_id, content, **kwargs):
        self.sections.append((section_id, content, kwargs))

    def register_hook(self, name, handler):
        self.hooks = getattr(self, "hooks", [])
        self.hooks.append((name, handler))


@pytest.fixture()
def ctx():
    return StubCtx()


def test_register_wires_platform(ctx):
    adapter.register(ctx)
    assert ctx.platform_kwargs is not None
    assert ctx.platform_kwargs["name"] == "botschat"
    assert ctx.platform_kwargs["label"] == "BotsChat"
    assert callable(ctx.platform_kwargs["adapter_factory"])
    assert callable(ctx.platform_kwargs["check_fn"])
    assert callable(ctx.platform_kwargs["validate_config"])
    assert callable(ctx.platform_kwargs["env_enablement_fn"])
    assert ctx.platform_kwargs["emoji"] == "🤖"
    # The enablement sweep's credential gate must be wired so deps-only
    # check_fn can't auto-enable botschat in unconfigured profiles.
    assert ctx.platform_kwargs["is_connected"] is adapter.validate_config


def test_check_requirements_is_dep_probe(monkeypatch):
    """check_fn passes WITHOUT env vars — config-only setups must create."""
    for k in (
        "BOTSCHAT_CLOUD_URL", "BOTSCHAT_PAIRING_TOKEN",
        "BOTSCHAT_E2E_PASSWORD", "BOTSCHAT_AGENT_ID",
    ):
        monkeypatch.delenv(k, raising=False)
    assert adapter.check_requirements() is True


def test_validate_config_accepts_extra_only(monkeypatch):
    """Credentials in config extra (no env) pass the credential gate."""
    monkeypatch.delenv("BOTSCHAT_CLOUD_URL", raising=False)
    monkeypatch.delenv("BOTSCHAT_PAIRING_TOKEN", raising=False)
    good = PlatformConfig(
        enabled=True, extra={"cloudUrl": "https://x", "pairingToken": "bc_pat_x"}
    )
    assert adapter.validate_config(good) is True
    bad = PlatformConfig(enabled=True, extra={})
    assert adapter.validate_config(bad) is False


def test_register_wires_a2ui_section(ctx):
    adapter.register(ctx)
    assert any(sid == adapter.A2UI_SECTION_ID for sid, _, _ in ctx.sections), "A2UI section missing"
    sid, content, _ = next(s for s in ctx.sections if s[0] == adapter.A2UI_SECTION_ID)
    assert isinstance(content, str)
    assert "```action" in content
    assert '"kind":"buttons"' in content
    assert "primary" in content


def test_a2ui_section_id_is_valid(ctx):
    """Must match Hermes' section-id rule: lowercase letters/digits/./_/-."""
    adapter.register(ctx)
    sid = adapter.A2UI_SECTION_ID
    assert all(c.islower() or c.isdigit() or c in "._-" for c in sid)
    assert 1 <= len(sid) <= 128


def test_thread_id_from_session_key():
    assert adapter.thread_id_from_session_key("agent:main:botschat:u_1:adhoc") is None
    assert (
        adapter.thread_id_from_session_key("agent:main:botschat:u_1:ses:s1:thread:t_42")
        == "t_42"
    )


def test_match_botschat_session():
    known = {
        "agent:main:botschat:dev-test-user:adhoc",
        "agent:main:botschat:u_1:ses:ses_9",
        "agent:main:botschat:u_2:ses:ses_1",
    }
    # Exact match
    assert adapter.match_botschat_session("agent:main:botschat:u_1:ses:ses_9", known) == (
        "agent:main:botschat:u_1:ses:ses_9"
    )
    # Suffix match (gateway session id embeds the BotsChat key)
    assert adapter.match_botschat_session(
        "agent:main:botschat:dm:agent:main:botschat:dev-test-user:adhoc", known
    ) == "agent:main:botschat:dev-test-user:adhoc"
    # Longest-match wins
    known2 = known | {"dev-test-user:adhoc"}
    assert adapter.match_botschat_session(
        "agent:main:botschat:dm:agent:main:botschat:dev-test-user:adhoc", known2
    ) == "agent:main:botschat:dev-test-user:adhoc"
    # No match
    assert adapter.match_botschat_session("agent:main:telegram:dm:123", known) is None
    assert adapter.match_botschat_session("", known) is None


@pytest.mark.parametrize(
    "session_id,expected",
    [
        ("cron_44ce9233b2dd_20260830_174836", "44ce9233b2dd"),
        ("cron_67348e728b97_20260830_091500", "67348e728b97"),
        ("cron_4ce7800cbee0_20260831_000001", "4ce7800cbee0"),
        ("not-a-cron-session", None),
        ("cron_", None),
        ("", None),
    ],
)
def test_cron_job_id_from_session(session_id, expected):
    assert adapter.cron_job_id_from_session(session_id) == expected


# ---------------------------------------------------------------------------
# M6: multi-profile token lock
# ---------------------------------------------------------------------------


def _make_adapter():
    return adapter.BotsChatAdapter(PlatformConfig(enabled=True, extra={}))


def _env(monkeypatch):
    monkeypatch.setenv("BOTSCHAT_CLOUD_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("BOTSCHAT_PAIRING_TOKEN", "bc_pat_super_secret_123")
    monkeypatch.setattr(adapter, "_hermes_config_get", lambda key: None)


def test_connect_acquires_token_lock(monkeypatch):
    acquired = {}

    def fake_acquire(scope, identity, metadata=None):
        acquired["scope"] = scope
        acquired["identity"] = identity
        return (True, None)

    monkeypatch.setattr("gateway.status.acquire_scoped_lock", fake_acquire)
    _env(monkeypatch)

    async def scenario():
        a = _make_adapter()
        try:
            assert await a.connect()
            assert acquired["scope"] == "botschat"
            assert a._lock_key == acquired["identity"]
            # Lock identity is a sha256 hash prefix — never the raw token.
            assert len(a._lock_key) == 16
            assert "bc_pat" not in a._lock_key
        finally:
            await a.disconnect()

    asyncio.run(scenario())


def test_connect_fails_on_lock_conflict(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda scope, identity, metadata=None: (False, {"pid": 9999, "profile": "other"}),
    )
    _env(monkeypatch)

    async def scenario():
        a = _make_adapter()
        assert not await a.connect()
        assert a._fatal_error_code == "lock_conflict"
        assert not a._fatal_error_retryable
        assert a._client is None, "must not start a client when the token is locked"
        assert a._lock_key is None
        assert not a._running

    asyncio.run(scenario())


def test_disconnect_releases_lock(monkeypatch):
    released = []
    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda scope, identity, metadata=None: (True, None),
    )
    monkeypatch.setattr(
        "gateway.status.release_scoped_lock",
        lambda scope, identity: released.append((scope, identity)),
    )
    _env(monkeypatch)

    async def scenario():
        a = _make_adapter()
        await a.connect()
        key = a._lock_key
        assert key is not None
        await a.disconnect()
        assert a._lock_key is None
        assert released == [("botschat", key)]

    asyncio.run(scenario())


def test_connect_threads_distinct_agent_id(monkeypatch):
    """BOTSCHAT_AGENT_ID becomes the client's agent identity + agents list."""
    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda scope, identity, metadata=None: (True, None),
    )
    _env(monkeypatch)
    monkeypatch.setenv("BOTSCHAT_AGENT_ID", "hermes-2")

    async def scenario():
        a = _make_adapter()
        try:
            assert await a.connect()
            assert a._client is not None
            assert a._client.agent_id == "hermes-2"
            assert a._client.agent_ids == ["hermes-2"]
        finally:
            await a.disconnect()

    asyncio.run(scenario())


def test_connect_defaults_to_hermes_agent(monkeypatch):
    """Without BOTSCHAT_AGENT_ID the client stays the default 'hermes'."""
    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda scope, identity, metadata=None: (True, None),
    )
    _env(monkeypatch)

    async def scenario():
        a = _make_adapter()
        try:
            assert await a.connect()
            assert a._client is not None
            assert a._client.agent_id is None
            assert a._client.agent_ids == ["hermes"]
        finally:
            await a.disconnect()

    asyncio.run(scenario())
